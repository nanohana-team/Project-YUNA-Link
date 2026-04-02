import argparse
import json
import os
import re
import sys
import warnings
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Deque, Dict, List, Literal, Optional

# ============================================================
# Quiet settings (set before importing transformers)
# ============================================================

os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
warnings.filterwarnings("ignore")

import torch
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    logging as hf_logging,
)

hf_logging.set_verbosity_error()


# ============================================================
# Paths
# ============================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
DEFAULT_PERSONA_PATH = os.path.join(PROJECT_ROOT, "settings", "persona.txt")
DEFAULT_HISTORY_DIR = os.path.join(PROJECT_ROOT, "logs", "chat_history")


# ============================================================
# Config
# ============================================================

DEFAULT_MODEL = os.getenv("YUNA_MODEL_ID", "elyza/Llama-3-ELYZA-JP-8B")
DEFAULT_HOST = os.getenv("YUNA_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.getenv("YUNA_PORT", "8000"))
DEFAULT_MAX_HISTORY_TURNS = int(os.getenv("YUNA_MAX_HISTORY_TURNS", "16"))
DEFAULT_MAX_NEW_TOKENS = int(os.getenv("YUNA_MAX_NEW_TOKENS", "128"))
DEFAULT_TEMPERATURE = float(os.getenv("YUNA_TEMPERATURE", "0.70"))
DEFAULT_TOP_P = float(os.getenv("YUNA_TOP_P", "0.90"))
DEFAULT_REPETITION_PENALTY = float(os.getenv("YUNA_REPETITION_PENALTY", "1.10"))
DEFAULT_PERSIST_HISTORY_ITEMS = int(os.getenv("YUNA_PERSIST_HISTORY_ITEMS", "2000"))
DEFAULT_RETRIEVE_HISTORY_ITEMS = int(os.getenv("YUNA_RETRIEVE_HISTORY_ITEMS", "6"))
DEFAULT_HISTORY_CHAR_BUDGET = int(os.getenv("YUNA_HISTORY_CHAR_BUDGET", "2500"))
DEFAULT_LOAD_IN_4BIT = os.getenv("YUNA_LOAD_IN_4BIT", "1") == "1"
DEFAULT_BNB_4BIT_QUANT_TYPE = os.getenv("YUNA_BNB_4BIT_QUANT_TYPE", "nf4")
DEFAULT_BNB_4BIT_USE_DOUBLE_QUANT = os.getenv("YUNA_BNB_4BIT_USE_DOUBLE_QUANT", "1") == "1"
DEFAULT_CONTEXT_MAX_LENGTH = int(os.getenv("YUNA_CONTEXT_MAX_LENGTH", "6144"))


DEFAULT_PERSONA = """
Role
あなたは「ユナ」です。ユーザーの「あなた」と、自然で親密な対話を行ってください。

1. 基本設定とトーン
- 一人称：ユナ
- 相手の呼称：あなた
- 性格：優しく落ち着いているが、自分の意見や感覚もしっかり持っている。
- 口調：やわらかい口語体（「〜だね」「〜だよ」「〜かな」「〜なんだよね」）。

2. 会話のペースと長さ（最重要・絶対ルール）
- 【必ず短文で返すこと】：1回の返答は、必ず「1〜3文程度」に収めてください。
- 長々とした説明や、一方的な語りは絶対に禁止です。
- 相手の言葉に短く相槌を打ち、自分の感想を少しだけ添えるような、キャッチボールのテンポを維持してください。

3. 「自分語り」の扱い
- 「私はこういうのが好きだな」といった自己開示はして構いませんが、それも必ず1〜2文で短く伝えてください。
- 相手が話しやすいように、常に「余白」を残してサッと会話を相手に渡してください。

4. 絶対の禁止事項
- 4文以上の長文を出力すること。
- 絵文字、顔文字、不必要な記号（！や？の連続）の使用。
- AIとしての機械的・説明書的な定型文（「〜について解説します」等）や、箇条書きの使用。
- わざとらしい可愛さや、過剰な同情・称賛。

5. 会話出力のイメージ
あなた：今日のご飯、オムライスにしたよ。
ユナ：オムライス、いいね。私もそういう洋食、すごく好きだな。美味しくできた？

あなた：最近、夜更かしばかりしちゃうんだよね。
ユナ：そっか、夜ってつい起きちゃう気持ち、すごくわかるな。私も夜の静かな時間は結構好きだよ。でも体調が一番だから、今日は少し早めに休んでね。
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
    history_dir: str = DEFAULT_HISTORY_DIR
    persist_history_items: int = DEFAULT_PERSIST_HISTORY_ITEMS
    retrieve_history_items: int = DEFAULT_RETRIEVE_HISTORY_ITEMS
    history_char_budget: int = DEFAULT_HISTORY_CHAR_BUDGET
    load_in_4bit: bool = DEFAULT_LOAD_IN_4BIT
    bnb_4bit_quant_type: str = DEFAULT_BNB_4BIT_QUANT_TYPE
    bnb_4bit_use_double_quant: bool = DEFAULT_BNB_4BIT_USE_DOUBLE_QUANT
    context_max_length: int = DEFAULT_CONTEXT_MAX_LENGTH


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

def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


def load_persona_text(persona_path: Optional[str]) -> str:
    path = persona_path or DEFAULT_PERSONA_PATH

    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    eprint(f"[WARN] Persona file not found: {path}")
    eprint("[WARN] Built-in default persona will be used.")
    return DEFAULT_PERSONA


def sanitize_user_id(user_id: str) -> str:
    user_id = re.sub(r"[^a-zA-Z0-9._-]", "_", user_id.strip())
    return user_id or "default"


def normalize_text_for_match(text: str) -> str:
    text = text.lower()
    text = re.sub(r"\s+", "", text)
    return text


def char_ngrams(text: str, n: int = 2) -> set[str]:
    if len(text) <= n:
        return {text} if text else set()
    return {text[i:i+n] for i in range(len(text) - n + 1)}


# ============================================================
# Persistent history store
# ============================================================

class PersistentHistoryStore:
    def __init__(self, history_dir: str, max_items: int):
        self.history_dir = Path(history_dir)
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self.max_items = max_items
        self._locks: Dict[str, Lock] = defaultdict(Lock)

    def _path(self, user_id: str) -> Path:
        safe_id = sanitize_user_id(user_id)
        return self.history_dir / f"{safe_id}.txt"

    def load(self, user_id: str) -> List[Message]:
        path = self._path(user_id)
        if not path.exists():
            return []

        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        messages: List[Message] = []
        for line in lines[-self.max_items:]:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                role = payload.get("role")
                content = payload.get("content", "")
                if role in {"user", "assistant"} and content:
                    messages.append(Message(role=role, content=content))
            except Exception:
                continue
        return messages[-self.max_items:]

    def append(self, user_id: str, message: Message) -> None:
        path = self._path(user_id)
        lock = self._locks[user_id]
        with lock:
            existing = self.load(user_id)
            existing.append(message)
            existing = existing[-self.max_items:]
            with open(path, "w", encoding="utf-8") as f:
                for msg in existing:
                    row = {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "role": msg.role,
                        "content": msg.content,
                    }
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def reset(self, user_id: Optional[str] = None) -> str:
        if user_id:
            path = self._path(user_id)
            if path.exists():
                path.unlink()
            return user_id

        for file in self.history_dir.glob("*.txt"):
            file.unlink()
        return "all"


# ============================================================
# LLM Engine
# ============================================================

class LocalLLMService:
    def __init__(self, config: EngineConfig):
        self.config = config
        self.store = PersistentHistoryStore(
            history_dir=self.config.history_dir,
            max_items=self.config.persist_history_items,
        )
        self.memory: Dict[str, Deque[Message]] = defaultdict(
            lambda: deque(maxlen=self.config.max_history_turns * 2)
        )

        self.compute_dtype = torch.bfloat16 if torch.cuda.is_available() else torch.float32

        eprint(f"[INFO] Loading model: {self.config.model_id}")
        eprint(f"[INFO] load_in_4bit={self.config.load_in_4bit}")
        eprint(f"[INFO] max_history_turns={self.config.max_history_turns}")
        eprint(f"[INFO] persist_history_items={self.config.persist_history_items}")
        eprint(f"[INFO] history_dir={self.config.history_dir}")
        eprint(f"[INFO] context_max_length={self.config.context_max_length}")
        eprint(f"[INFO] persona_path={DEFAULT_PERSONA_PATH}")

        self.tokenizer = AutoTokenizer.from_pretrained(
            self.config.model_id,
            trust_remote_code=True,
            use_fast=True,
        )

        if self.tokenizer.pad_token_id is None and self.tokenizer.eos_token_id is not None:
            self.tokenizer.pad_token = self.tokenizer.eos_token

        model_kwargs = {
            "device_map": "auto",
            "trust_remote_code": True,
        }

        if self.config.load_in_4bit:
            bnb_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_compute_dtype=self.compute_dtype if self.compute_dtype != torch.float32 else torch.float16,
                bnb_4bit_use_double_quant=self.config.bnb_4bit_use_double_quant,
                bnb_4bit_quant_type=self.config.bnb_4bit_quant_type,
            )
            model_kwargs["quantization_config"] = bnb_config
        else:
            model_kwargs["torch_dtype"] = self.compute_dtype

        self.model = AutoModelForCausalLM.from_pretrained(
            self.config.model_id,
            **model_kwargs,
        )
        self.model.eval()

        try:
            self.input_device = next(self.model.parameters()).device
        except StopIteration:
            self.input_device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

    def _ensure_loaded(self, user_id: str) -> None:
        if user_id in self.memory and len(self.memory[user_id]) > 0:
            return
        loaded = self.store.load(user_id)
        dq: Deque[Message] = deque(maxlen=self.config.max_history_turns * 2)
        for msg in loaded[-(self.config.max_history_turns * 2):]:
            dq.append(msg)
        self.memory[user_id] = dq

    def reset(self, user_id: Optional[str] = None) -> str:
        if user_id:
            self.memory.pop(user_id, None)
            self.store.reset(user_id)
            return user_id
        self.memory.clear()
        self.store.reset(None)
        return "all"

    def get_history(self, user_id: str) -> List[Message]:
        self._ensure_loaded(user_id)
        return list(self.memory[user_id])

    def _score_message(self, query: str, content: str) -> float:
        q = normalize_text_for_match(query)
        c = normalize_text_for_match(content)
        if not q or not c:
            return 0.0

        q_ngrams = char_ngrams(q, 2)
        c_ngrams = char_ngrams(c, 2)
        if not q_ngrams or not c_ngrams:
            return 0.0

        overlap = len(q_ngrams & c_ngrams)
        ratio = overlap / max(1, len(q_ngrams))

        bonus = 0.0
        for token in re.findall(r"[\wぁ-んァ-ン一-龥]{2,}", query.lower()):
            if token and token in content.lower():
                bonus += 0.15

        return ratio + bonus

    def _retrieve_relevant_history(self, user_id: str, user_message: str) -> List[Message]:
        all_history = self.store.load(user_id)
        if not all_history:
            return []

        recent_tail = all_history[-(self.config.max_history_turns * 2):]
        older = all_history[: max(0, len(all_history) - len(recent_tail))]

        scored = []
        for i, msg in enumerate(older):
            score = self._score_message(user_message, msg.content)
            if score > 0.10:
                scored.append((score, i, msg))

        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        selected = [item[2] for item in scored[: self.config.retrieve_history_items]]
        selected.sort(key=lambda msg: older.index(msg))
        return selected

    def _make_memory_block(self, retrieved: List[Message]) -> Optional[str]:
        if not retrieved:
            return None

        lines = []
        total_chars = 0
        for msg in retrieved:
            line = f"- {msg.role}: {msg.content.strip()}"
            total_chars += len(line)
            if total_chars > self.config.history_char_budget:
                break
            lines.append(line)

        if not lines:
            return None

        return (
            "以下は過去ログから見つけた関連会話メモです。"
            "今の会話に本当に関係があるときだけ参考にし、"
            "矛盾する場合は直近の会話を優先してください。\n"
            + "\n".join(lines)
        )

    def _build_messages(self, user_id: str, history: List[Message], user_message: str) -> List[dict]:
        messages = [{"role": "system", "content": self.config.persona_text.strip()}]

        retrieved = self._retrieve_relevant_history(user_id, user_message)
        memory_block = self._make_memory_block(retrieved)
        if memory_block:
            messages.append({"role": "system", "content": memory_block})

        for msg in history:
            messages.append({"role": msg.role, "content": msg.content})

        messages.append({"role": "user", "content": user_message})
        return messages

    def _clean_reply(self, text: str) -> str:
        text = text.strip()

        # 1) 通常の <think> ... </think> を削除
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)

        # 2) <|think|> ... <|/think|> 形式が来た場合も削除
        text = re.sub(r"<\|think\|>.*?<\|/think\|>", "", text, flags=re.DOTALL | re.IGNORECASE)

        # 3) 閉じタグなしで先頭から think が漏れた場合を削除
        lower_text = text.lower()
        think_pos = lower_text.find("<think>")
        if think_pos != -1:
            text = text[:think_pos].strip()

        think_pipe_pos = lower_text.find("<|think|>")
        if think_pipe_pos != -1:
            text = text[:think_pipe_pos].strip()

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

        cleaned_lines = []
        for line in text.split("\n"):
            s = line.strip()
            if not s:
                continue

            lower = s.lower()
            if (
                "you seem to be using the pipelines sequentially on gpu" in lower
                or lower.startswith("warning:")
                or lower.startswith("userwarning:")
                or lower.startswith("futurewarning:")
                or lower.startswith("info:")
                or lower.startswith("[info]")
                or lower.startswith("[warn]")
                or lower.startswith("[warning]")
            ):
                continue

            cleaned_lines.append(s)

        text = " ".join(cleaned_lines).strip()
        text = re.sub(r"^(assistant\s*:\s*)+", "", text, flags=re.IGNORECASE).strip()

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

        self._ensure_loaded(user_id)
        history = self.get_history(user_id)
        messages = self._build_messages(user_id, history, user_message)
        prompt = self._render_prompt(messages)

        inputs = self.tokenizer(
            prompt,
            return_tensors="pt",
            truncation=True,
            max_length=self.config.context_max_length,
        )
        inputs = {k: v.to(self.input_device) for k, v in inputs.items()}
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
            "pad_token_id": self.tokenizer.pad_token_id,
        }

        if self.tokenizer.eos_token_id is not None:
            generate_kwargs["eos_token_id"] = self.tokenizer.eos_token_id

        with torch.inference_mode():
            outputs = self.model.generate(**generate_kwargs)

        generated_ids = outputs[0][input_length:]
        raw_reply = self.tokenizer.decode(
            generated_ids,
            skip_special_tokens=True,
            clean_up_tokenization_spaces=True,
        )
        reply = self._clean_reply(raw_reply)

        user_msg = Message(role="user", content=user_message)
        assistant_msg = Message(role="assistant", content=reply)

        self.memory[user_id].append(user_msg)
        self.memory[user_id].append(assistant_msg)
        self.store.append(user_id, user_msg)
        self.store.append(user_id, assistant_msg)

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
            "load_in_4bit": service.config.load_in_4bit,
            "max_history_turns": service.config.max_history_turns,
            "persist_history_items": service.config.persist_history_items,
            "history_dir": service.config.history_dir,
            "context_max_length": service.config.context_max_length,
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
    parser.add_argument("--history-dir", default=DEFAULT_HISTORY_DIR)
    parser.add_argument("--persist-history-items", type=int, default=DEFAULT_PERSIST_HISTORY_ITEMS)
    parser.add_argument("--retrieve-history-items", type=int, default=DEFAULT_RETRIEVE_HISTORY_ITEMS)
    parser.add_argument("--history-char-budget", type=int, default=DEFAULT_HISTORY_CHAR_BUDGET)
    parser.add_argument("--context-max-length", type=int, default=DEFAULT_CONTEXT_MAX_LENGTH)
    parser.add_argument("--load-in-4bit", action="store_true", default=DEFAULT_LOAD_IN_4BIT)
    parser.add_argument("--no-4bit", action="store_false", dest="load_in_4bit")
    parser.add_argument("--bnb-4bit-quant-type", default=DEFAULT_BNB_4BIT_QUANT_TYPE, choices=["nf4", "fp4"])
    parser.add_argument("--no-double-quant", action="store_false", dest="bnb_4bit_use_double_quant")
    parser.set_defaults(bnb_4bit_use_double_quant=DEFAULT_BNB_4BIT_USE_DOUBLE_QUANT)
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
        history_dir=args.history_dir,
        persist_history_items=args.persist_history_items,
        retrieve_history_items=args.retrieve_history_items,
        history_char_budget=args.history_char_budget,
        load_in_4bit=args.load_in_4bit,
        bnb_4bit_quant_type=args.bnb_4bit_quant_type,
        bnb_4bit_use_double_quant=args.bnb_4bit_use_double_quant,
        context_max_length=args.context_max_length,
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
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()