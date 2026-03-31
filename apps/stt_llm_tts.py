import argparse
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

# ルートから実行しやすいように import パスを調整
PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.audio.voice import RealtimeVoiceEngine  # noqa: E402
from src.audio.TTS_cevioAI import CevioAITalker, CAST_NAME  # noqa: E402
from src.llm.local_llm_qwen3 import (  # noqa: E402
    EngineConfig,
    LocalLLMService,
    load_persona_text,
    DEFAULT_MODEL,
    DEFAULT_MAX_HISTORY_TURNS,
    DEFAULT_MAX_NEW_TOKENS,
    DEFAULT_TEMPERATURE,
    DEFAULT_TOP_P,
    DEFAULT_REPETITION_PENALTY,
)


# ============================================================
# Config
# ============================================================

DEFAULT_STT_MODEL = "kotoba-tech/kotoba-whisper-v2.1"


@dataclass
class BridgeConfig:
    user_id: str
    stt_model: str
    stt_device: str
    cast_name: str
    no_tts: bool
    debug_voice: bool
    min_reply_interval_sec: float
    suppress_during_tts: bool


# ============================================================
# TTS Worker
# ============================================================

class AsyncTTSWorker:
    def __init__(self, cast_name: str):
        self.cast_name = cast_name
        self._talker: Optional[CevioAITalker] = None
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._stop_event = threading.Event()
        self._speaking_event = threading.Event()
        self._init_error: Optional[Exception] = None

    @property
    def is_speaking(self) -> bool:
        return self._speaking_event.is_set()

    @property
    def init_error(self) -> Optional[Exception]:
        return self._init_error

    def start(self) -> None:
        self._thread.start()

    def speak_async(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        self._queue.put(text)

    def stop(self) -> None:
        self._stop_event.set()
        self._queue.put(None)
        if self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        try:
            self._talker = CevioAITalker(self.cast_name)
            print(f"[TTS] CeVIO ready. Cast={self.cast_name}")
        except Exception as e:
            self._init_error = e
            print(f"[ERROR] CeVIO初期化失敗: {e}")
            return

        try:
            while not self._stop_event.is_set():
                item = self._queue.get()
                if item is None:
                    break

                try:
                    self._speaking_event.set()
                    print(f"[TTS] >>> {item}")
                    self._talker.speak(item)
                except Exception as e:
                    print(f"[ERROR] TTS読み上げ失敗: {e}")
                finally:
                    self._speaking_event.clear()

        finally:
            try:
                if self._talker is not None:
                    self._talker.close()
            except Exception as e:
                print(f"[WARN] CeVIO終了処理失敗: {e}")


# ============================================================
# LLM
# ============================================================

def build_llm_service(args) -> LocalLLMService:
    persona_text = load_persona_text(args.persona_file)

    config = EngineConfig(
        model_id=args.model,
        max_history_turns=args.max_history_turns,
        max_new_tokens=args.max_new_tokens,
        temperature=args.temperature,
        top_p=args.top_p,
        repetition_penalty=args.repetition_penalty,
        persona_text=persona_text,
    )
    return LocalLLMService(config)


# ============================================================
# Bridge
# ============================================================

class STTLLMTTSBridge:
    def __init__(
        self,
        voice_engine: RealtimeVoiceEngine,
        llm: LocalLLMService,
        tts: Optional[AsyncTTSWorker],
        config: BridgeConfig,
    ):
        self.voice_engine = voice_engine
        self.llm = llm
        self.tts = tts
        self.config = config

        self.last_reply_ts = 0.0
        self._lock = threading.Lock()

        self.voice_engine.set_on_partial_text(self._on_partial_text)
        self.voice_engine.set_on_final_text(self._on_final_text)

    # --------------------------------------------------------
    # Callbacks
    # --------------------------------------------------------

    def _on_partial_text(self, text: str) -> None:
        # partial は表示だけ
        print(f"[STT][PART] {text}")

    def _on_final_text(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return

        if self.config.suppress_during_tts and self.tts is not None and self.tts.is_speaking:
            print(f"[BRIDGE][SKIP] TTS再生中のため無視: {text}")
            return

        now = time.time()
        if now - self.last_reply_ts < self.config.min_reply_interval_sec:
            print(f"[BRIDGE][SKIP] 応答間隔抑制: {text}")
            return

        threading.Thread(
            target=self._handle_final_text,
            args=(text,),
            daemon=True,
        ).start()

    # --------------------------------------------------------
    # Internal
    # --------------------------------------------------------

    def _handle_final_text(self, text: str) -> None:
        with self._lock:
            try:
                print(f"[STT][FINAL] {text}")

                response = self.llm.generate(
                    user_id=self.config.user_id,
                    user_message=text,
                )
                reply = (response.reply or "").strip()

                if not reply:
                    print("[LLM] 空応答のためスキップ")
                    return

                print(f"[LLM] {reply}")
                self.last_reply_ts = time.time()

                if self.tts is not None:
                    self.tts.speak_async(reply)

            except Exception as e:
                print(f"[ERROR] LLM生成失敗: {e}")

    # --------------------------------------------------------
    # Public
    # --------------------------------------------------------

    def run(self) -> None:
        print("========================================")
        print(" STT -> LLM -> TTS Bridge")
        print("========================================")
        print("Ctrl+C で終了")
        print()

        self.voice_engine.run_forever()


# ============================================================
# CLI
# ============================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Realtime STT -> Local LLM(Qwen3) -> CeVIO AI TTS"
    )

    # LLM
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--persona-file", default=None)
    parser.add_argument("--user-id", default="default")
    parser.add_argument("--max-history-turns", type=int, default=DEFAULT_MAX_HISTORY_TURNS)
    parser.add_argument("--max-new-tokens", type=int, default=DEFAULT_MAX_NEW_TOKENS)
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    parser.add_argument("--top-p", type=float, default=DEFAULT_TOP_P)
    parser.add_argument("--repetition-penalty", type=float, default=DEFAULT_REPETITION_PENALTY)

    # STT
    parser.add_argument("--stt-model", default=DEFAULT_STT_MODEL)
    parser.add_argument("--stt-device", default="cuda")
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--block-duration", type=float, default=0.08)
    parser.add_argument("--silence-duration", type=float, default=0.45)
    parser.add_argument("--partial-window-sec", type=float, default=1.8)
    parser.add_argument("--min-utterance-sec", type=float, default=0.8)
    parser.add_argument("--use-silero-vad", action="store_true")
    parser.add_argument("--debug-voice", action="store_true")

    # TTS
    parser.add_argument("--cast", default=CAST_NAME)
    parser.add_argument("--no-tts", action="store_true")

    # Bridge behavior
    parser.add_argument("--min-reply-interval-sec", type=float, default=1.0)
    parser.add_argument(
        "--allow-during-tts",
        action="store_true",
        help="TTS再生中でもSTT確定文をLLMに流す",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    config = BridgeConfig(
        user_id=args.user_id,
        stt_model=args.stt_model,
        stt_device=args.stt_device,
        cast_name=args.cast,
        no_tts=args.no_tts,
        debug_voice=args.debug_voice,
        min_reply_interval_sec=args.min_reply_interval_sec,
        suppress_during_tts=not args.allow_during_tts,
    )

    llm = None
    tts = None
    voice_engine = None

    try:
        print("[INFO] LLM を初期化中...")
        llm = build_llm_service(args)
        print("[INFO] LLM ready")

        if not config.no_tts:
            print(f"[INFO] CeVIO AI を初期化中... Cast={config.cast_name}")
            tts = AsyncTTSWorker(config.cast_name)
            tts.start()

            # 初期化失敗が即時に出るケース確認用に少し待つ
            time.sleep(0.2)
            if tts.init_error is not None:
                raise RuntimeError(f"CeVIO初期化失敗: {tts.init_error}")
        else:
            print("[INFO] TTS disabled (--no-tts)")

        print("[INFO] STT を初期化中...")
        voice_engine = RealtimeVoiceEngine(
            model_id=config.stt_model,
            device=config.stt_device,
            sample_rate=args.sample_rate,
            block_duration=args.block_duration,
            silence_duration=args.silence_duration,
            partial_window_sec=args.partial_window_sec,
            min_utterance_sec=args.min_utterance_sec,
            use_silero_vad=args.use_silero_vad,
            debug=config.debug_voice,
        )
        print("[INFO] STT ready")

        bridge = STTLLMTTSBridge(
            voice_engine=voice_engine,
            llm=llm,
            tts=tts,
            config=config,
        )
        bridge.run()
        return 0

    except KeyboardInterrupt:
        print("\n[INFO] 終了します。")
        return 0

    except Exception as e:
        print(f"[ERROR] 初期化失敗: {e}")
        return 1

    finally:
        try:
            if voice_engine is not None:
                voice_engine.stop()
        except Exception:
            pass

        try:
            if tts is not None:
                tts.stop()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())