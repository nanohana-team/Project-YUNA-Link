import argparse
import os
import sys

# ルートから実行しやすいように import パスを調整
PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

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
from src.audio.TTS_cevioAI import CevioAITalker, CAST_NAME  # noqa: E402


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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Input -> Local LLM(Qwen3) -> CeVIO AI TTS"
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--persona-file", default=None)
    parser.add_argument("--user-id", default="default")
    parser.add_argument("--cast", default=CAST_NAME)
    parser.add_argument("--max-history-turns", type=int, default=DEFAULT_MAX_HISTORY_TURNS)
    parser.add_argument("--max-new-tokens", type=int, default=DEFAULT_MAX_NEW_TOKENS)
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    parser.add_argument("--top-p", type=float, default=DEFAULT_TOP_P)
    parser.add_argument("--repetition-penalty", type=float, default=DEFAULT_REPETITION_PENALTY)
    parser.add_argument("--no-tts", action="store_true", help="TTSを無効化してテキスト出力のみ行う")
    args = parser.parse_args()

    llm = None
    talker = None

    try:
        print("[INFO] LLM を初期化中...")
        llm = build_llm_service(args)
        print("[INFO] LLM ready")

        if not args.no_tts:
            print(f"[INFO] CeVIO AI を初期化中... Cast={args.cast}")
            talker = CevioAITalker(args.cast)
            print("[INFO] CeVIO AI ready")
        else:
            print("[INFO] TTS disabled (--no-tts)")

        print("入力待ちです。Enterで送信、/reset で履歴リセット、/exit で終了。")

        while True:
            try:
                user_text = input("\nYou> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n終了します。")
                break

            if not user_text:
                continue

            if user_text in {"/exit", "exit", "quit"}:
                print("終了します。")
                break

            if user_text == "/reset":
                llm.reset(args.user_id)
                print("AI> 会話履歴をリセットしたよ。")
                continue

            try:
                response = llm.generate(
                    user_id=args.user_id,
                    user_message=user_text,
                )
                reply = response.reply.strip()
            except Exception as e:
                print(f"[ERROR] LLM生成失敗: {e}")
                continue

            print(f"AI> {reply}")

            if talker is not None and reply:
                try:
                    talker.speak(reply)
                except Exception as e:
                    print(f"[ERROR] TTS読み上げ失敗: {e}")

        return 0

    except Exception as e:
        print(f"[ERROR] 初期化失敗: {e}")
        return 1

    finally:
        if talker is not None:
            talker.close()


if __name__ == "__main__":
    raise SystemExit(main())