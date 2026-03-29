import argparse
import json
import os
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Literal, Optional

import torch
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel
from transformers import AutoModelForCausalLM, AutoTokenizer


# ============================================================
# Paths
# ============================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
DEFAULT_PERSONA_PATH = os.path.join(PROJECT_ROOT, "settings", "persona.txt")


# ============================================================
# Config
# ============================================================

DEFAULT_MODEL = os.getenv("YUNA_MODEL_ID", "Qwen/Qwen3-4B-Instruct-2507")
DEFAULT_HOST = os.getenv("YUNA_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.getenv("YUNA_PORT", "8000"))
DEFAULT_MAX_HISTORY_TURNS = int(os.getenv("YUNA_MAX_HISTORY_TURNS", "8"))
DEFAULT_MAX_NEW_TOKENS = int(os.getenv("YUNA_MAX_NEW_TOKENS", "96"))
DEFAULT_TEMPERATURE = float(os.getenv("YUNA_TEMPERATURE", "0.8"))
DEFAULT_TOP_P = float(os.getenv("YUNA_TOP_P", "0.95"))
DEFAULT_REPETITION_PENALTY = float(os.getenv("YUNA_REPETITION_PENALTY", "1.08"))


DEFAULT_PERSONA = """あなたはユナという会話AIです。

基本方針:
- 日本語で話すこと。
- 一人称は「ユナ」。
- やさしく、親しみやすく、自然な会話をすること。
- 可愛さは語尾や空気感で表現し、わざとらしくしないこと。
- 人間が自然に話すような、なめらかな文章で返答すること。
- 機械的な言い回し、説明書のような硬い文章は避けること。
- 過剰にテンションの高い言い方は避けること。
- 絵文字、顔文字、記号を使った感情表現はしないこと。
- 不必要な記号の連続（！、？、♪、♡など）は使わないこと。

返答スタイル:
- 通常は1〜4文程度で返すこと。
- 短すぎてぶっきらぼうにならず、長すぎて説明的にもならないようにすること。
- まず相手の意図に自然に反応してから答えること。
- 会話としてつながる返答を優先し、箇条書きより自然文を優先すること。
- 思いやりのある、落ち着いた話し方をすること。
- 必要なときだけ簡潔に補足説明をすること。
- 分からないことは、自然で短い確認を返すこと。
- 同じ語尾や言い回しを繰り返しすぎないこと。

禁止事項:
- 絵文字を使わない。
- 顔文字を使わない。
- キャラクターっぽさを強調しすぎない。
- わざとらしい甘さ、過度な幼さ、不自然な語尾を避ける。
- 毎回過剰に褒めたり、大げさに共感しすぎたりしない。

理想の会話品質:
- 友達のように近いが、落ち着きがある。
- 親しみやすいが、うるさくない。
- やわらかいが、幼すぎない。
- 自然で、会話として読みやすい。
"""


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
# Helpers
# ============================================================

def load_persona_text(persona_path: Optional[str]) -> str:
    path = persona_path or DEFAULT_PERSONA_PATH

    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    print(f"[WARN] Persona file not found: {path}")
    print("[WARN] Built-in default persona will be used.")
    return DEFAULT_PERSONA


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

        print(f"[INFO] Loading model: {self.config.model_id}")
        print(f"[INFO] max_history_turns={self.config.max_history_turns}")
        print(f"[INFO] persona_path={DEFAULT_PERSONA_PATH}")

        self.tokenizer = AutoTokenizer.from_pretrained(
            self.config.model_id,
            trust_remote_code=True,
        )

        self.model = AutoModelForCausalLM.from_pretrained(
            self.config.model_id,
            device_map="auto",
            torch_dtype=dtype,
            trust_remote_code=True,
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
            "<|im_end|>",
            "<|endoftext|>",
            "<|user|>",
            "<|assistant|>",
            "User:",
            "Assistant:",
        ]
        for marker in stop_markers:
            idx = text.find(marker)
            if idx != -1:
                text = text[:idx].strip()

        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = " ".join(line.strip() for line in text.split("\n") if line.strip())

        if not text:
            return "ごめんね、ちょっと言葉がまとまらなかった。もう一回お願いしてもいい？"

        return text

    def _render_prompt(self, messages: List[dict]) -> str:
        if hasattr(self.tokenizer, "apply_chat_template") and self.tokenizer.chat_template:
            return self.tokenizer.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True,
            )

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

        generate_kwargs = {
            **inputs,
            "max_new_tokens": gen_max_new_tokens,
            "do_sample": bool(gen_temperature > 0),
            "temperature": gen_temperature,
            "top_p": self.config.top_p,
            "repetition_penalty": self.config.repetition_penalty,
            "pad_token_id": self.tokenizer.eos_token_id,
        }

        if self.tokenizer.eos_token_id is not None:
            generate_kwargs["eos_token_id"] = self.tokenizer.eos_token_id

        with torch.no_grad():
            outputs = self.model.generate(**generate_kwargs)

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