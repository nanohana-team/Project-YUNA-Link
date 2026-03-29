# local_llm_cli.py
import argparse
import sys
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

DEFAULT_MODEL = "LiquidAI/LFM2.5-1.2B-Base"


def build_prompt(user_text: str) -> str:
    # Base modelなので chat template は使わず、生テキスト補完として与える
    # 後で人格や応答スタイルを載せたいなら、このプレフィックスを調整していく
    return (
        "You are a concise and natural conversational AI for VR interaction.\n"
        "Respond in Japanese.\n"
        "Keep replies short, clear, and spoken-language friendly.\n\n"
        f"User: {user_text}\n"
        "Assistant:"
    )


def load_model(model_id: str):
    dtype = torch.bfloat16 if torch.cuda.is_available() else torch.float32

    tokenizer = AutoTokenizer.from_pretrained(model_id)
    model = AutoModelForCausalLM.from_pretrained(
        model_id,
        device_map="auto",
        dtype=dtype,
    )
    model.eval()
    return tokenizer, model


def generate_text(
    tokenizer,
    model,
    prompt: str,
    max_new_tokens: int = 128,
    temperature: float = 0.7,
    top_p: float = 0.95,
    repetition_penalty: float = 1.1,
):
    inputs = tokenizer(prompt, return_tensors="pt").to(model.device)

    with torch.no_grad():
        outputs = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            do_sample=True if temperature > 0 else False,
            temperature=temperature,
            top_p=top_p,
            repetition_penalty=repetition_penalty,
            eos_token_id=tokenizer.eos_token_id,
            pad_token_id=tokenizer.eos_token_id,
        )

    full_text = tokenizer.decode(outputs[0], skip_special_tokens=True)

    # prompt以降だけ返す
    if full_text.startswith(prompt):
        return full_text[len(prompt):].strip()

    return full_text.strip()


def interactive_loop(tokenizer, model, args):
    print("Local LLM CLI started. '/exit' で終了。")
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

        prompt = build_prompt(user_text)
        reply = generate_text(
            tokenizer,
            model,
            prompt,
            max_new_tokens=args.max_new_tokens,
            temperature=args.temperature,
            top_p=args.top_p,
            repetition_penalty=args.repetition_penalty,
        )
        print(f"AI> {reply}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--prompt", type=str, default=None)
    parser.add_argument("--max-new-tokens", type=int, default=128)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--top-p", type=float, default=0.95)
    parser.add_argument("--repetition-penalty", type=float, default=1.1)
    args = parser.parse_args()

    tokenizer, model = load_model(args.model)

    if args.prompt is not None:
        prompt = build_prompt(args.prompt)
        reply = generate_text(
            tokenizer,
            model,
            prompt,
            max_new_tokens=args.max_new_tokens,
            temperature=args.temperature,
            top_p=args.top_p,
            repetition_penalty=args.repetition_penalty,
        )
        print(reply)
        sys.exit(0)

    interactive_loop(tokenizer, model, args)


if __name__ == "__main__":
    main()