# ============================================================
# Project YUNA Link - Voice Engine (Kotoba-Whisper v2.1)
# ============================================================
# 役割:
#   - Windows既定再生デバイスのループバック録音
#   - BGM/SE をある程度落とす簡易 voice gate
#   - kotoba-whisper-v2.1 によるリアルタイム STT
#   - 幻覚文 / ループ文 / 短すぎる文の抑制
#   - 疑似話者分離（簡易）
#
# 依存:
#   pip install soundcard numpy torch torchaudio accelerate transformers
#   optional:
#     pip install silero-vad
#
# 備考:
#   - kotoba-whisper-v2.1 は transformers pipeline + trust_remote_code=True を使用
#   - punctuators はこの版では使わない（必要なら後処理で別途追加）
# ============================================================

from __future__ import annotations

import os
import re
import time
import queue
import threading
import traceback
from dataclasses import dataclass, field
from typing import Callable, Optional, List, Dict, Any

import numpy as np
import soundcard as sc
import torch

try:
    from transformers import pipeline
except Exception as e:
    pipeline = None
    _HF_IMPORT_ERROR = e
else:
    _HF_IMPORT_ERROR = None

try:
    from silero_vad import load_silero_vad, get_speech_timestamps
except Exception:
    load_silero_vad = None
    get_speech_timestamps = None


# ============================================================
# Data Models
# ============================================================

@dataclass
class SpeechEvent:
    speaker_id: str
    text: str
    is_final: bool
    start_ts: float
    end_ts: float
    energy: float
    is_overlap: bool
    confidence: float = 0.0
    speaker_changed: bool = False
    debug: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VoiceChunkInfo:
    rms: float
    voice_band_ratio: float
    zcr: float
    peak: float
    is_voice: bool
    reason: str


# ============================================================
# Desktop Loopback Capture
# ============================================================

class DesktopLoopbackCapture:
    def __init__(
        self,
        sample_rate: int = 16000,
        block_duration: float = 0.08,
        max_queue_size: int = 64,
        debug: bool = False,
    ) -> None:
        self.sample_rate = sample_rate
        self.block_duration = block_duration
        self.block_frames = int(sample_rate * block_duration)
        self.debug = debug

        self.audio_queue: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=max_queue_size)
        self.running = False
        self.capture_error: Optional[BaseException] = None
        self.worker: Optional[threading.Thread] = None

        self.speaker = self._find_default_speaker()

    def _find_default_speaker(self):
        sp = sc.default_speaker()
        if sp is None:
            raise RuntimeError("既定の再生デバイスが取得できませんでした。")
        return sp

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.worker = threading.Thread(target=self._capture_worker, daemon=True)
        self.worker.start()

    def stop(self) -> None:
        self.running = False
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=1.0)

    def _capture_worker(self) -> None:
        try:
            print(f"[VOICE] Using default speaker: {self.speaker.name}")
            mic = sc.get_microphone(id=str(self.speaker.name), include_loopback=True)

            with mic.recorder(samplerate=self.sample_rate) as recorder:
                print("[VOICE] Listening desktop audio...")

                while self.running:
                    data = recorder.record(numframes=self.block_frames)

                    if data is None or data.size == 0:
                        continue

                    if data.ndim == 2:
                        mono = data.mean(axis=1).astype(np.float32)
                    else:
                        mono = data.astype(np.float32)

                    try:
                        self.audio_queue.put(mono, timeout=0.5)
                    except queue.Full:
                        try:
                            _ = self.audio_queue.get_nowait()
                        except queue.Empty:
                            pass
                        try:
                            self.audio_queue.put_nowait(mono)
                        except queue.Full:
                            pass

        except BaseException as e:
            self.capture_error = e
            print("[VOICE][ERROR] capture worker crashed")
            traceback.print_exc()

    def read(self, timeout: float = 1.0) -> np.ndarray:
        if self.capture_error is not None:
            raise RuntimeError(f"capture worker failed: {self.capture_error}")
        return self.audio_queue.get(timeout=timeout)


# ============================================================
# Voice Gate
# ============================================================

class VoiceGate:
    def __init__(
        self,
        sample_rate: int = 16000,
        voice_band_low_hz: float = 120.0,
        voice_band_high_hz: float = 4300.0,
        voice_band_ratio_threshold: float = 0.70,
        rms_threshold: float = 0.005,
        zcr_min: float = 0.02,
        zcr_max: float = 0.15,
        use_silero_vad: bool = False,
    ) -> None:
        self.sample_rate = sample_rate
        self.voice_band_low_hz = voice_band_low_hz
        self.voice_band_high_hz = voice_band_high_hz
        self.voice_band_ratio_threshold = voice_band_ratio_threshold
        self.rms_threshold = rms_threshold
        self.zcr_min = zcr_min
        self.zcr_max = zcr_max
        self.use_silero_vad = use_silero_vad and load_silero_vad is not None

        self.vad_model = load_silero_vad() if self.use_silero_vad else None

    def rms(self, audio: np.ndarray) -> float:
        if audio.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(np.square(audio), dtype=np.float64)))

    def peak(self, audio: np.ndarray) -> float:
        if audio.size == 0:
            return 0.0
        return float(np.max(np.abs(audio)))

    def zcr(self, audio: np.ndarray) -> float:
        if audio.size < 2:
            return 0.0
        signs = np.signbit(audio)
        return float(np.mean(signs[:-1] != signs[1:]))

    def voice_band_ratio(self, audio: np.ndarray) -> float:
        if audio.size == 0:
            return 0.0

        spectrum = np.fft.rfft(audio)
        freqs = np.fft.rfftfreq(audio.size, d=1.0 / self.sample_rate)
        power = np.abs(spectrum) ** 2

        total = float(np.sum(power)) + 1e-12
        mask = (freqs >= self.voice_band_low_hz) & (freqs <= self.voice_band_high_hz)
        voice = float(np.sum(power[mask]))
        return voice / total

    def bandpass_voice(self, audio: np.ndarray) -> np.ndarray:
        if audio.size == 0:
            return audio

        spectrum = np.fft.rfft(audio)
        freqs = np.fft.rfftfreq(audio.size, d=1.0 / self.sample_rate)
        mask = (freqs >= self.voice_band_low_hz) & (freqs <= self.voice_band_high_hz)
        spectrum[~mask] = 0
        return np.fft.irfft(spectrum, n=audio.size).astype(np.float32)

    def has_speech_silero(self, audio: np.ndarray) -> bool:
        if self.vad_model is None or get_speech_timestamps is None:
            return True

        timestamps = get_speech_timestamps(
            audio.astype(np.float32),
            self.vad_model,
            sampling_rate=self.sample_rate,
            threshold=0.5,
            min_speech_duration_ms=120,
            min_silence_duration_ms=100,
            speech_pad_ms=30,
            return_seconds=False,
        )
        return len(timestamps) > 0

    def inspect(self, audio: np.ndarray) -> VoiceChunkInfo:
        rms = self.rms(audio)
        peak = self.peak(audio)
        ratio = self.voice_band_ratio(audio)
        zcr = self.zcr(audio)

        if rms < self.rms_threshold:
            return VoiceChunkInfo(rms, ratio, zcr, peak, False, "low_rms")

        if ratio < self.voice_band_ratio_threshold:
            return VoiceChunkInfo(rms, ratio, zcr, peak, False, "low_voice_band_ratio")

        if not (self.zcr_min <= zcr <= self.zcr_max):
            return VoiceChunkInfo(rms, ratio, zcr, peak, False, "zcr_out_of_range")

        if self.use_silero_vad and not self.has_speech_silero(audio):
            return VoiceChunkInfo(rms, ratio, zcr, peak, False, "silero_no_speech")

        return VoiceChunkInfo(rms, ratio, zcr, peak, True, "ok")


# ============================================================
# Pseudo Speaker Tracker
# ============================================================

class PseudoSpeakerTracker:
    def __init__(
        self,
        max_speakers: int = 8,
        speaker_timeout_sec: float = 30.0,
        feature_distance_threshold: float = 0.18,
    ) -> None:
        self.max_speakers = max_speakers
        self.speaker_timeout_sec = speaker_timeout_sec
        self.feature_distance_threshold = feature_distance_threshold

        self.speakers: Dict[str, Dict[str, Any]] = {}
        self.next_speaker_idx = 0
        self.last_assigned_speaker: Optional[str] = None

    def _extract_features(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        if audio.size == 0:
            return np.zeros(4, dtype=np.float32)

        rms = float(np.sqrt(np.mean(np.square(audio), dtype=np.float64)))
        zcr = float(np.mean(np.signbit(audio[:-1]) != np.signbit(audio[1:]))) if audio.size > 1 else 0.0

        spectrum = np.fft.rfft(audio)
        mag = np.abs(spectrum) + 1e-12
        freqs = np.fft.rfftfreq(audio.size, d=1.0 / sample_rate)

        centroid = float(np.sum(freqs * mag) / np.sum(mag))
        centroid_norm = min(centroid / 4000.0, 1.0)

        energy = mag ** 2
        total = np.sum(energy) + 1e-12
        p = energy / total
        entropy = float(-np.sum(p * np.log(p + 1e-12)))
        entropy_norm = min(entropy / 12.0, 1.0)

        return np.array([rms, zcr, centroid_norm, entropy_norm], dtype=np.float32)

    def _distance(self, a: np.ndarray, b: np.ndarray) -> float:
        return float(np.linalg.norm(a - b))

    def _cleanup(self, now_ts: float) -> None:
        stale = [
            speaker_id
            for speaker_id, info in self.speakers.items()
            if now_ts - info["last_seen"] > self.speaker_timeout_sec
        ]
        for speaker_id in stale:
            self.speakers.pop(speaker_id, None)

    def assign(self, audio: np.ndarray, sample_rate: int, now_ts: float) -> tuple[str, bool, float]:
        self._cleanup(now_ts)

        feat = self._extract_features(audio, sample_rate)

        best_id = None
        best_dist = 999.0

        for speaker_id, info in self.speakers.items():
            dist = self._distance(feat, info["feature"])
            if dist < best_dist:
                best_dist = dist
                best_id = speaker_id

        if best_id is not None and best_dist <= self.feature_distance_threshold:
            info = self.speakers[best_id]
            info["feature"] = 0.7 * info["feature"] + 0.3 * feat
            info["last_seen"] = now_ts
            speaker_changed = best_id != self.last_assigned_speaker
            self.last_assigned_speaker = best_id
            confidence = max(0.0, 1.0 - best_dist / self.feature_distance_threshold)
            return best_id, speaker_changed, confidence

        if len(self.speakers) >= self.max_speakers:
            oldest_id = min(self.speakers.items(), key=lambda x: x[1]["last_seen"])[0]
            self.speakers.pop(oldest_id, None)

        speaker_id = f"spk_{self.next_speaker_idx}"
        self.next_speaker_idx += 1
        self.speakers[speaker_id] = {
            "feature": feat,
            "last_seen": now_ts,
        }
        speaker_changed = speaker_id != self.last_assigned_speaker
        self.last_assigned_speaker = speaker_id
        return speaker_id, speaker_changed, 0.45


# ============================================================
# Realtime Voice Engine
# ============================================================

class RealtimeVoiceEngine:
    def __init__(
        self,
        model_id: str = "kotoba-tech/kotoba-whisper-v2.1",
        device: str = "cuda",
        sample_rate: int = 16000,
        block_duration: float = 0.08,
        silence_duration: float = 0.45,
        partial_window_sec: float = 1.8,
        min_utterance_sec: float = 0.8,
        use_silero_vad: bool = False,
        debug: bool = True,
    ) -> None:
        if pipeline is None:
            raise RuntimeError(
                "transformers.pipeline の読み込みに失敗しました。"
                f" 元エラー: {_HF_IMPORT_ERROR}"
            )

        self.sample_rate = sample_rate
        self.silence_duration = silence_duration
        self.partial_window_sec = partial_window_sec
        self.min_utterance_sec = min_utterance_sec
        self.debug = debug

        self.device = "cuda:0" if device == "cuda" and torch.cuda.is_available() else "cpu"
        self.torch_dtype = torch.float16 if self.device.startswith("cuda") else torch.float32
        self.model_kwargs = {"attn_implementation": "sdpa"} if self.device.startswith("cuda") else {}

        print(f"[VOICE] Loading model: {model_id}")
        self.asr_pipe = pipeline(
            task="automatic-speech-recognition",
            model=model_id,
            dtype=self.torch_dtype,
            device=self.device,
            model_kwargs=self.model_kwargs,
            trust_remote_code=True,
            batch_size=1,
        )

        self.generate_kwargs = {
            "language": "ja",
            "task": "transcribe",
        }

        self.capture = DesktopLoopbackCapture(
            sample_rate=sample_rate,
            block_duration=block_duration,
            debug=debug,
        )
        self.voice_gate = VoiceGate(
            sample_rate=sample_rate,
            use_silero_vad=use_silero_vad,
        )
        self.speaker_tracker = PseudoSpeakerTracker()

        self.running = False
        self.buffer: List[np.ndarray] = []
        self.speaking = False
        self.last_voice_time = 0.0
        self.start_time: Optional[float] = None
        self.last_partial_text = ""
        self.last_debug_print = 0.0

        self.hallucination_patterns = [
            r"^ご視聴ありがとうございました[。！!]*$",
            r"^ありがとうございました[。！!]*$",
            r"^ご清聴ありがとうございました[。！!]*$",
            r"^字幕.*$",
            r"^チャンネル登録.*$",
            r"^チャンネル登録をお願いします[。！!]*$",
            r"^高評価.*$",
            r"^では、また見てね[。！!]*$",
            r"^次の動画でお会いしましょう[。！!]*$",
            r"^スタッフ.*$",
            r"^【ED】$",
            r"^- 終わり-$",
            r"^- スタッフ-$",
            r"^thanks for watching[.! ]*$",
            r"^thank you[.! ]*$",
            r"^subtitles by .*",
            r"^hello everyone welcome to my channel.*",
        ]

        self.on_speech_event: Optional[Callable[[SpeechEvent], None]] = None
        self.on_partial_text: Optional[Callable[[str], None]] = None
        self.on_final_text: Optional[Callable[[str], None]] = None

    # --------------------------------------------------------
    # Public API
    # --------------------------------------------------------

    def set_on_speech_event(self, callback: Callable[[SpeechEvent], None]) -> None:
        self.on_speech_event = callback

    def set_on_partial_text(self, callback: Callable[[str], None]) -> None:
        self.on_partial_text = callback

    def set_on_final_text(self, callback: Callable[[str], None]) -> None:
        self.on_final_text = callback

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.capture.start()
        print("[VOICE] Main loop started.")

    def stop(self) -> None:
        self.running = False
        self.capture.stop()

    def run_forever(self) -> None:
        self.start()
        try:
            while self.running:
                try:
                    audio = self.capture.read(timeout=1.0)
                except queue.Empty:
                    print("[VOICE][DEBUG] waiting audio...")
                    continue

                self._process_chunk(audio)

        except KeyboardInterrupt:
            print("\n[VOICE] Stopped by user.")
        finally:
            self.stop()

    # --------------------------------------------------------
    # Internal
    # --------------------------------------------------------

    def _normalize_text(self, text: str) -> str:
        text = text.strip()
        text = re.sub(r"\s+", " ", text)
        return text

    def _is_hallucination_text(self, text: str) -> bool:
        if not text:
            return True

        norm = self._normalize_text(text).lower()

        if re.fullmatch(r"[。、,.!！?？…ー\\-~ ]*", norm):
            return True

        for pat in self.hallucination_patterns:
            if re.fullmatch(pat, norm, flags=re.IGNORECASE):
                return True

        return False

    def _is_loop_text(self, text: str) -> bool:
        if not text:
            return False

        norm = self._normalize_text(text)

        if len(set(norm)) <= 2 and len(norm) > 10:
            return True

        tokens = re.findall(r"[一-龥ぁ-んァ-ヶーA-Za-z0-9]+", norm)
        if len(tokens) >= 6 and len(set(tokens)) <= 2:
            return True

        for size in range(2, 7):
            if len(norm) < size * 6:
                continue
            chunks = [norm[i:i + size] for i in range(0, len(norm) - size + 1, size)]
            if len(chunks) >= 6 and len(set(chunks)) <= 2:
                return True

        return False

    def _is_valid_final(self, text: str) -> bool:
        norm = self._normalize_text(text)
        if not norm:
            return False

        if len(norm) <= 2:
            return False

        if len(norm) > 80:
            return False

        if re.fullmatch(r"[ぁ-んァ-ヶー]{1,5}", norm):
            return False

        if self._is_loop_text(norm):
            return False

        return True

    def _is_valid_partial(self, text: str) -> bool:
        norm = self._normalize_text(text)
        if not norm:
            return False

        if len(norm) < 3:
            return False

        if self._is_loop_text(norm):
            return False

        return True

    def _transcribe(self, audio: np.ndarray) -> str:
        if audio.size == 0:
            return ""

        filtered = self.voice_gate.bandpass_voice(audio)

        try:
            result = self.asr_pipe(
                {"array": filtered, "sampling_rate": self.sample_rate},
                return_timestamps=False,
                generate_kwargs=self.generate_kwargs,
            )
        except Exception as e:
            print(f"[VOICE][WARN] transcription failed: {e}")
            return ""

        if isinstance(result, dict):
            text = result.get("text", "")
        else:
            text = str(result)

        return self._normalize_text(text)

    def _emit_event(self, event: SpeechEvent) -> None:
        kind = "FINAL" if event.is_final else "PART"
        print(f"[VOICE][{kind}][{event.speaker_id}] {event.text}")

        if self.on_speech_event:
            self.on_speech_event(event)

        if event.is_final:
            if self.on_final_text:
                self.on_final_text(event.text)
        else:
            if self.on_partial_text:
                self.on_partial_text(event.text)

    def _process_chunk(self, audio: np.ndarray) -> None:
        now = time.time()
        info = self.voice_gate.inspect(audio)

        if self.debug and now - self.last_debug_print >= 0.5:
            self.last_debug_print = now
            print(
                f"[VOICE][DEBUG] rms={info.rms:.5f} "
                f"voice_ratio={info.voice_band_ratio:.3f} "
                f"zcr={info.zcr:.3f} peak={info.peak:.3f} "
                f"is_voice={info.is_voice} reason={info.reason} "
                f"queue={self.capture.audio_queue.qsize()}"
            )

        if info.is_voice:
            if not self.speaking:
                self.speaking = True
                self.start_time = now
                self.buffer = []
                self.last_partial_text = ""
                print("[VOICE] Speech start")

            self.buffer.append(audio)
            self.last_voice_time = now

        else:
            if self.speaking:
                self.buffer.append(audio)

                if now - self.last_voice_time > self.silence_duration:
                    self._finalize_utterance(now)

        if self.speaking and self.buffer:
            self._update_partial(now)

    def _finalize_utterance(self, now: float) -> None:
        self.speaking = False

        full_audio = np.concatenate(self.buffer).astype(np.float32)
        utterance_sec = full_audio.size / float(self.sample_rate)

        if self.debug:
            print(f"[VOICE][DEBUG] finalize utterance_sec={utterance_sec:.2f}")

        if utterance_sec >= self.min_utterance_sec:
            text = self._transcribe(full_audio)

            if (
                text
                and not self._is_hallucination_text(text)
                and self._is_valid_final(text)
            ):
                speaker_id, speaker_changed, confidence = self.speaker_tracker.assign(
                    full_audio,
                    self.sample_rate,
                    now,
                )

                event = SpeechEvent(
                    speaker_id=speaker_id,
                    text=text,
                    is_final=True,
                    start_ts=self.start_time if self.start_time else now,
                    end_ts=now,
                    energy=self.voice_gate.rms(full_audio),
                    is_overlap=False,
                    confidence=confidence,
                    speaker_changed=speaker_changed,
                    debug={"duration_sec": utterance_sec},
                )
                self._emit_event(event)
            elif text:
                print(f"[VOICE][DROP][FINAL] {text}")

        self.buffer = []
        self.start_time = None
        self.last_partial_text = ""
        print("[VOICE] Speech end")

    def _update_partial(self, now: float) -> None:
        recent = np.concatenate(self.buffer).astype(np.float32)
        max_frames = int(self.sample_rate * self.partial_window_sec)

        if recent.size > max_frames:
            recent = recent[-max_frames:]

        if recent.size < int(self.sample_rate * 1.8):
            return

        info = self.voice_gate.inspect(recent)
        if not info.is_voice:
            return

        text = self._transcribe(recent)

        if (
            text
            and text != self.last_partial_text
            and not self._is_hallucination_text(text)
            and self._is_valid_partial(text)
        ):
            self.last_partial_text = text

            speaker_id, speaker_changed, confidence = self.speaker_tracker.assign(
                recent,
                self.sample_rate,
                now,
            )

            event = SpeechEvent(
                speaker_id=speaker_id,
                text=text,
                is_final=False,
                start_ts=self.start_time if self.start_time else now,
                end_ts=now,
                energy=self.voice_gate.rms(recent),
                is_overlap=True,
                confidence=confidence,
                speaker_changed=speaker_changed,
                debug={"partial_window_sec": self.partial_window_sec},
            )
            self._emit_event(event)
        elif text and (
            self._is_hallucination_text(text) or not self._is_valid_partial(text)
        ):
            print(f"[VOICE][DROP][PART] {text}")


# ============================================================
# Convenience API
# ============================================================

_engine_singleton: Optional[RealtimeVoiceEngine] = None


def create_voice_engine(**kwargs) -> RealtimeVoiceEngine:
    return RealtimeVoiceEngine(**kwargs)


def get_voice_engine(**kwargs) -> RealtimeVoiceEngine:
    global _engine_singleton
    if _engine_singleton is None:
        _engine_singleton = RealtimeVoiceEngine(**kwargs)
    return _engine_singleton


# ============================================================
# Entry
# ============================================================

def main() -> None:
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

    engine = RealtimeVoiceEngine(
        model_id="kotoba-tech/kotoba-whisper-v2.1",
        device="cuda",
        sample_rate=16000,
        block_duration=0.08,
        silence_duration=0.45,
        partial_window_sec=1.8,
        min_utterance_sec=0.8,
        use_silero_vad=False,
        debug=True,
    )

    engine.run_forever()


if __name__ == "__main__":
    main()