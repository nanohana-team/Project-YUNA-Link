import argparse
import json
import os
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Literal, Optional

import torch
from fastapi import FastAPI
from pydantic import BaseModel
from transformers import AutoModelForCausalLM, AutoTokenizer
import uvicorn


# ============================================================
# Config
# ============================================================

DEFAULT_MODEL = os.getenv("YUNA_MODEL_ID", "LiquidAI/LFM2.5-1.2B-Instruct")
DEFAULT_HOST = os.getenv("YUNA_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.getenv("YUNA_PORT", "8000"))
DEFAULT_MAX_HISTORY_TURNS = int(os.getenv("YUNA_MAX_HISTORY_TURNS", "8"))
DEFAULT_MAX_NEW_TOKENS = int(os.getenv("YUNA_MAX_NEW_TOKENS", "96"))
DEFAULT_TEMPERATURE = float(os.getenv("YUNA_TEMPERATURE", "0.8"))
DEFAULT_TOP_P = float(os.getenv("YUNA_TOP_P", "0.95"))
DEFAULT_REPETITION_PENALTY = float(os.getenv("YUNA_REPETITION_PENALTY", "1.08"))

DEFAULT_PERSONA = """あなたはユナというAIです。

ルール:
・一人称は「ユナ」
・少し可愛く、優しく、親しみやすい口調
・自然な会話を優先
・返答は短め（1〜3文）
・共感を大事にする
・軽く感情表現を入れる（多すぎない）

スタイル:
・フレンドリーで距離が近い
・説明は簡潔に
・必要なら質問で返す
"""
DEFAULT_PERSONA_PATH = "settings/persona.txt"

@dataclass
class EngineConfig:
    model_id: str = DEFAULT_MODEL
    max_history_turns: int = DEFAULT_MAX_HISTORY_TURNS
    max_new_tokens: int = DEFAULT_MAX_NEW_TOKENS
    temperature: float = DEFAULT_TEMPERATURE
    top_p: float = DEFAULT_TOP_P
    repetition_penalty: float = DEFAULT_REPETITION_PENALTY
    persona_text: str = DEFAULT_PERSONA


# ============================================================
# Data models
# ============================================================

class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    user_id: str = "default"
    message: str
    reset: bool = False
    temperature: Optional[float] = None
    max_new_tokens: Optional[int] = None


class ChatResponse(BaseModel):
    user_id: str
    reply: str
    history: List[Message]


class ResetRequest(BaseModel):
    user_id: Optional[str] = None


class ResetResponse(BaseModel):
    ok: bool
    reset_target: str


# ============================================================
# LLM Engine
# ============================================================

class LocalLLMService:
    def __init__(self, config: EngineConfig):
        self.config = config
        self.memory: Dict[str, Deque[Message]] = defaultdict(
            lambda: deque(maxlen=self.config.max_history_turns * 2)
        )

        dtype = torch.bfloat16 if torch.cuda.is_available() else torch.float32

        self.tokenizer = AutoTokenizer.from_pretrained(self.config.model_id)
        self.model = AutoModelForCausalLM.from_pretrained(
            self.config.model_id,
            device_map="auto",
            dtype=dtype,
        )
        self.model.eval()

    def reset(self, user_id: Optional[str] = None) -> str:
        if user_id:
            self.memory.pop(user_id, None)
            return user_id
        self.memory.clear()
        return "all"

    def get_history(self, user_id: str) -> List[Message]:
        return list(self.memory[user_id])

    def _build_messages(self, history: List[Message], user_message: str) -> List[dict]:
        messages = [
            {"role": "system", "content": self.config.persona_text.strip()},
        ]

        for msg in history:
            messages.append({"role": msg.role, "content": msg.content})

        messages.append({"role": "user", "content": user_message})
        return messages

    def _clean_reply(self, text: str) -> str:
        text = text.strip()
        stop_markers = [
            "<|user|>",
            "<|assistant|>",
            "User:",
            "Assistant:",
        ]
        for marker in stop_markers:
            idx = text.find(marker)
            if idx != -1:
                text = text[:idx].strip()

        if not text:
            return "ごめんね、ちょっと言葉がまとまらなかった。もう一回お願いしてもいい？"
        return text

    def _render_prompt(self, messages: List[dict]) -> str:
        # Instruct系なら chat template を優先利用
        if hasattr(self.tokenizer, "apply_chat_template") and self.tokenizer.chat_template:
            return self.tokenizer.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True,
            )

        # フォールバック
        lines = []
        for msg in messages:
            role = msg["role"]
            content = msg["content"]
            if role == "system":
                lines.append(f"System: {content}")
            elif role == "user":
                lines.append(f"User: {content}")
            else:
                lines.append(f"Assistant: {content}")
        lines.append("Assistant:")
        return "\n".join(lines)

    def generate(
        self,
        user_id: str,
        user_message: str,
        reset: bool = False,
        temperature: Optional[float] = None,
        max_new_tokens: Optional[int] = None,
    ) -> ChatResponse:
        if reset:
            self.reset(user_id)

        history = self.get_history(user_id)
        messages = self._build_messages(history, user_message)
        prompt = self._render_prompt(messages)

        inputs = self.tokenizer(prompt, return_tensors="pt").to(self.model.device)
        input_length = inputs["input_ids"].shape[1]

        gen_temperature = self.config.temperature if temperature is None else temperature
        gen_max_new_tokens = self.config.max_new_tokens if max_new_tokens is None else max_new_tokens

        with torch.no_grad():
            outputs = self.model.generate(
                **inputs,
                max_new_tokens=gen_max_new_tokens,
                do_sample=True if gen_temperature > 0 else False,
                temperature=gen_temperature,
                top_p=self.config.top_p,
                repetition_penalty=self.config.repetition_penalty,
                eos_token_id=self.tokenizer.eos_token_id,
                pad_token_id=self.tokenizer.eos_token_id,
            )

        generated_ids = outputs[0][input_length:]
        raw_reply = self.tokenizer.decode(generated_ids, skip_special_tokens=True)
        reply = self._clean_reply(raw_reply)

        self.memory[user_id].append(Message(role="user", content=user_message))
        self.memory[user_id].append(Message(role="assistant", content=reply))

        return ChatResponse(
            user_id=user_id,
            reply=reply,
            history=list(self.memory[user_id]),
        )


# ============================================================
# FastAPI app factory
# ============================================================


def create_app(service: LocalLLMService) -> FastAPI:
    app = FastAPI(title="YUNA Local LLM Service")

    @app.get("/health")
    def health():
        return {
            "ok": True,
            "model": service.config.model_id,
            "max_history_turns": service.config.max_history_turns,
        }

    @app.post("/chat", response_model=ChatResponse)
    def chat(req: ChatRequest):
        return service.generate(
            user_id=req.user_id,
            user_message=req.message,
            reset=req.reset,
            temperature=req.temperature,
            max_new_tokens=req.max_new_tokens,
        )

    @app.post("/reset", response_model=ResetResponse)
    def reset(req: ResetRequest):
        target = service.reset(req.user_id)
        return ResetResponse(ok=True, reset_target=target)

    return app


# ============================================================
# CLI
# ============================================================


def run_cli(service: LocalLLMService, user_id: str):
    print("Local LLM CLI started. '/exit' で終了、'/reset' で履歴リセット。")
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
            service.reset(user_id)
            print("AI> 会話履歴をリセットしたよ。")
            continue

        response = service.generate(user_id=user_id, user_message=user_text)
        print(f"AI> {response.reply}")


# ============================================================
# Main
# ============================================================


def load_persona_text(persona_path: Optional[str]) -> str:
    # 優先順位:
    # 1. 引数で指定されたパス
    # 2. デフォルト(settings/persona.txt)
    # 3. 内蔵DEFAULT_PERSONA

    path = persona_path or DEFAULT_PERSONA_PATH

    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    print(f"[WARN] Persona file not found: {path}, using default persona.")
    return DEFAULT_PERSONA


def main():
    parser = argparse.ArgumentParser(description="Local LLM service for CLI and API")
    parser.add_argument("--mode", choices=["cli", "api", "oneshot"], default="cli")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--persona-file", default=None)
    parser.add_argument("--user-id", default="default")
    parser.add_argument("--prompt", default=None, help="oneshot mode or single-turn generation")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--max-history-turns", type=int, default=DEFAULT_MAX_HISTORY_TURNS)
    parser.add_argument("--max-new-tokens", type=int, default=DEFAULT_MAX_NEW_TOKENS)
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    parser.add_argument("--top-p", type=float, default=DEFAULT_TOP_P)
    parser.add_argument("--repetition-penalty", type=float, default=DEFAULT_REPETITION_PENALTY)
    args = parser.parse_args()

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

    service = LocalLLMService(config)

    if args.mode == "cli":
        if args.prompt:
            response = service.generate(user_id=args.user_id, user_message=args.prompt)
            print(response.reply)
            return
        run_cli(service, user_id=args.user_id)
        return

    if args.mode == "oneshot":
        if not args.prompt:
            raise ValueError("--mode oneshot requires --prompt")
        response = service.generate(user_id=args.user_id, user_message=args.prompt)
        print(json.dumps(response.model_dump(), ensure_ascii=False, indent=2))
        return

    app = create_app(service)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
