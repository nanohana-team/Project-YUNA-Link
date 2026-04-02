# src/vision/come_here_on_voice.py
from __future__ import annotations

import argparse
import json
import signal
import socket
import sys
import time
from dataclasses import dataclass
from typing import Optional

DEFAULT_QUERY_HOST = "127.0.0.1"
DEFAULT_QUERY_PORT = 28766
DEFAULT_CTRL_HOST = "127.0.0.1"
DEFAULT_CTRL_PORT = 28765

RUNNING = True


def normalize_text(text: str) -> str:
    if not text:
        return ""
    t = text.strip()
    t = t.replace(" ", "").replace("　", "")
    return t


def is_come_here_command(text: str) -> bool:
    t = normalize_text(text)
    return t in {
        "こっち来て",
        "こっちきて",
        "こっちおいで",
    }


@dataclass
class PlayerInfo:
    ok: bool
    timestamp: float
    track_mode: str
    distance_m: Optional[float]


class LocalCtrlClient:
    def __init__(self, host: str, port: int, timeout: float = 1.0):
        self.host = host
        self.port = port
        self.timeout = timeout

    def send(self, command: str) -> str:
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
            sock.settimeout(self.timeout)
            sock.sendall((command.rstrip() + "\n").encode("utf-8"))

            data = b""
            while b"\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
            return data.decode("utf-8", errors="ignore").strip()

    def set_l_stick(self, x: float, y: float = 0.0) -> str:
        return self.send(f"SET L_STICK {x:.4f} {y:.4f}")

    def stop(self) -> None:
        try:
            self.set_l_stick(0.0, 0.0)
        except Exception:
            pass


class PlayerQueryClient:
    def __init__(self, host: str, port: int, timeout: float = 1.0):
        self.host = host
        self.port = port
        self.timeout = timeout

    def request(self, command: str) -> dict:
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
            sock.settimeout(self.timeout)
            sock.sendall((command.rstrip() + "\n").encode("utf-8"))
            sock.shutdown(socket.SHUT_WR)

            data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk

        text = data.decode("utf-8", errors="ignore").strip()
        if not text:
            raise RuntimeError("empty response from detect_player_dist query server")
        return json.loads(text.splitlines()[0])

    def get_player_info(self) -> PlayerInfo:
        payload = self.request("GET PLAYER_INFO")
        return PlayerInfo(
            ok=bool(payload.get("ok")),
            timestamp=float(payload.get("timestamp") or 0.0),
            track_mode=str(payload.get("track_mode") or ""),
            distance_m=float(payload["distance_m"]) if payload.get("distance_m") is not None else None,
        )


def install_signal_handlers() -> None:
    def _handler(_signum, _frame):
        global RUNNING
        RUNNING = False

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def run_come_here(
    text: str,
    query: PlayerQueryClient,
    ctrl: LocalCtrlClient,
    forward_value: float,
    interval: float,
    lost_grace_sec: float,
    print_every: float,
) -> int:
    if not is_come_here_command(text):
        print(f"[COME] ignored: {text}", flush=True)
        return 1

    print("[COME] command accepted", flush=True)
    print("[COME] move forward until YOLO target is lost", flush=True)

    lost_since: Optional[float] = None
    last_print = 0.0

    try:
        while RUNNING:
            loop_t0 = time.perf_counter()
            now = time.time()

            try:
                info = query.get_player_info()

                if info.ok:
                    lost_since = None
                    ctrl.set_l_stick(0.0, forward_value)
                else:
                    if lost_since is None:
                        lost_since = now

                    ctrl.set_l_stick(0.0, 0.0)

                    if (now - lost_since) >= lost_grace_sec:
                        print("[COME] target lost -> stop", flush=True)
                        break

                if now - last_print >= print_every:
                    last_print = now
                    dist = "None" if info.distance_m is None else f"{info.distance_m:.2f}m"
                    print(
                        f"[COME] target={info.ok} mode={info.track_mode:<18} "
                        f"dist={dist:>7} forward={forward_value:.3f}",
                        flush=True,
                    )

            except (ConnectionError, OSError, RuntimeError, json.JSONDecodeError) as exc:
                ctrl.set_l_stick(0.0, 0.0)
                if now - last_print >= print_every:
                    last_print = now
                    print(f"[COME][WARN] query/ctrl error: {exc}", flush=True)

            sleep_sec = interval - (time.perf_counter() - loop_t0)
            if sleep_sec > 0:
                time.sleep(sleep_sec)

    finally:
        ctrl.stop()

    return 0


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description='Voice command "こっち来て" -> move until YOLO target is lost')
    p.add_argument("--text", required=True, help="recognized voice text")
    p.add_argument("--query-host", default=DEFAULT_QUERY_HOST)
    p.add_argument("--query-port", type=int, default=DEFAULT_QUERY_PORT)
    p.add_argument("--ctrl-host", default=DEFAULT_CTRL_HOST)
    p.add_argument("--ctrl-port", type=int, default=DEFAULT_CTRL_PORT)
    p.add_argument("--forward-value", type=float, default=0.30)
    p.add_argument("--interval", type=float, default=0.05)
    p.add_argument("--lost-grace-sec", type=float, default=0.35,
                   help="stop only after target is continuously lost for this duration")
    p.add_argument("--print-every", type=float, default=0.5)
    return p


def main() -> int:
    args = build_argparser().parse_args()
    install_signal_handlers()

    query = PlayerQueryClient(args.query_host, args.query_port)
    ctrl = LocalCtrlClient(args.ctrl_host, args.ctrl_port)

    return run_come_here(
        text=args.text,
        query=query,
        ctrl=ctrl,
        forward_value=args.forward_value,
        interval=args.interval,
        lost_grace_sec=args.lost_grace_sec,
        print_every=args.print_every,
    )


if __name__ == "__main__":
    sys.exit(main())