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


@dataclass
class PlayerInfo:
    ok: bool
    timestamp: float
    track_mode: str
    x_px: Optional[float]
    y_px: Optional[float]
    x_norm: Optional[float]
    y_norm: Optional[float]
    frame_width: int
    frame_height: int
    distance_m: Optional[float]
    distance_raw_m: Optional[float]
    disparity_px: Optional[float]
    category: str


class LocalCtrlClient:
    def __init__(self, host: str, port: int, timeout: float = 1.0):
        self.host = host
        self.port = port
        self.timeout = timeout

    def send(self, command: str) -> None:
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
            sock.sendall((command.rstrip() + "\n").encode("utf-8"))

    def set_r_stick(self, x: float, y: float = 0.0) -> None:
        self.send(f"SET R_STICK {x:.4f} {y:.4f}")

    def set_l_stick(self, x: float, y: float = 0.0) -> None:
        self.send(f"SET L_STICK {x:.4f} {y:.4f}")


class PlayerQueryClient:
    def __init__(self, host: str, port: int, timeout: float = 1.0):
        self.host = host
        self.port = port
        self.timeout = timeout

    def request(self, command: str) -> dict:
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
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
        screen = payload.get("screen") or {}
        return PlayerInfo(
            ok=bool(payload.get("ok")),
            timestamp=float(payload.get("timestamp") or 0.0),
            track_mode=str(payload.get("track_mode") or "unknown"),
            x_px=_to_opt_float(screen.get("x_px")),
            y_px=_to_opt_float(screen.get("y_px")),
            x_norm=_to_opt_float(screen.get("x_norm")),
            y_norm=_to_opt_float(screen.get("y_norm")),
            frame_width=int(screen.get("frame_width") or 0),
            frame_height=int(screen.get("frame_height") or 0),
            distance_m=_to_opt_float(payload.get("distance_m")),
            distance_raw_m=_to_opt_float(payload.get("distance_raw_m")),
            disparity_px=_to_opt_float(payload.get("disparity_px")),
            category=str(payload.get("category") or "unknown"),
        )


def _to_opt_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class FocusController:
    deadzone: float = 0.08
    gain: float = 1.35
    max_turn: float = 0.65
    smooth: float = 0.25
    target_timeout_sec: float = 0.7
    invert_x: bool = False
    current_turn: float = 0.0
    last_seen_ts: float = 0.0

    def compute_target_turn(self, info: PlayerInfo) -> float:
        if not info.ok or info.x_norm is None:
            return 0.0

        error_x = float(info.x_norm)
        if self.invert_x:
            error_x = -error_x

        if abs(error_x) < self.deadzone:
            return 0.0

        turn = error_x * self.gain
        if turn > self.max_turn:
            turn = self.max_turn
        elif turn < -self.max_turn:
            turn = -self.max_turn
        return turn

    def update(self, info: PlayerInfo) -> tuple[float, bool]:
        now = time.time()
        target_turn = 0.0
        has_target = bool(info.ok and info.x_norm is not None)

        if has_target:
            self.last_seen_ts = now
            target_turn = self.compute_target_turn(info)
        elif (now - self.last_seen_ts) > self.target_timeout_sec:
            target_turn = 0.0
        else:
            target_turn = self.current_turn

        alpha = min(max(self.smooth, 0.0), 1.0)
        self.current_turn = self.current_turn + (target_turn - self.current_turn) * alpha
        if abs(self.current_turn) < 0.001:
            self.current_turn = 0.0
        return self.current_turn, has_target


def build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rotate toward detected player using R_STICK")
    parser.add_argument("--query-host", default=DEFAULT_QUERY_HOST)
    parser.add_argument("--query-port", type=int, default=DEFAULT_QUERY_PORT)
    parser.add_argument("--ctrl-host", default=DEFAULT_CTRL_HOST)
    parser.add_argument("--ctrl-port", type=int, default=DEFAULT_CTRL_PORT)
    parser.add_argument("--interval", type=float, default=0.05, help="control loop interval seconds")
    parser.add_argument("--deadzone", type=float, default=0.08)
    parser.add_argument("--gain", type=float, default=1.35)
    parser.add_argument("--max-turn", type=float, default=0.65)
    parser.add_argument("--smooth", type=float, default=0.25)
    parser.add_argument("--target-timeout-sec", type=float, default=0.7)
    parser.add_argument("--invert-x", action="store_true")
    parser.add_argument("--print-every", type=float, default=0.5)
    return parser


def install_signal_handlers() -> None:
    def _handler(_signum, _frame):
        global RUNNING
        RUNNING = False

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def main() -> int:
    global RUNNING
    args = build_argparser().parse_args()
    install_signal_handlers()

    query = PlayerQueryClient(args.query_host, args.query_port)
    ctrl = LocalCtrlClient(args.ctrl_host, args.ctrl_port)
    controller = FocusController(
        deadzone=args.deadzone,
        gain=args.gain,
        max_turn=args.max_turn,
        smooth=args.smooth,
        target_timeout_sec=args.target_timeout_sec,
        invert_x=args.invert_x,
    )

    print("========================================")
    print(" Focus Player")
    print("========================================")
    print(f"[INFO] query server : {args.query_host}:{args.query_port}")
    print(f"[INFO] local ctrl   : {args.ctrl_host}:{args.ctrl_port}")
    print(f"[INFO] interval     : {args.interval:.3f}s")
    print(f"[INFO] deadzone     : {args.deadzone:.3f}")
    print(f"[INFO] gain         : {args.gain:.3f}")
    print(f"[INFO] max_turn     : {args.max_turn:.3f}")
    print(f"[INFO] smooth       : {args.smooth:.3f}")
    print("[INFO] Ctrl+C to stop")
    print("========================================")

    last_print = 0.0

    try:
        while RUNNING:
            loop_t0 = time.perf_counter()
            try:
                info = query.get_player_info()
                turn_x, has_target = controller.update(info)
                ctrl.set_r_stick(turn_x, 0.0)

                now = time.time()
                if now - last_print >= args.print_every:
                    last_print = now
                    x_norm = "None" if info.x_norm is None else f"{info.x_norm:+.3f}"
                    dist = "None" if info.distance_m is None else f"{info.distance_m:.2f}m"
                    print(
                        f"[FOCUS] target={has_target} mode={info.track_mode:<18} "
                        f"x_norm={x_norm:>7} dist={dist:>7} turn={turn_x:+.3f}",
                        flush=True,
                    )
            except (ConnectionError, OSError, RuntimeError, json.JSONDecodeError) as exc:
                controller.update(PlayerInfo(False, 0.0, "lost", None, None, None, None, 0, 0, None, None, None, "unknown"))
                try:
                    ctrl.set_r_stick(0.0, 0.0)
                except Exception:
                    pass
                now = time.time()
                if now - last_print >= args.print_every:
                    last_print = now
                    print(f"[WARN] waiting for detect/local ctrl: {exc}", flush=True)
                time.sleep(min(max(args.interval, 0.02), 0.25))
                continue

            sleep_sec = args.interval - (time.perf_counter() - loop_t0)
            if sleep_sec > 0:
                time.sleep(sleep_sec)
    finally:
        try:
            ctrl.set_r_stick(0.0, 0.0)
        except Exception:
            pass
        print("[INFO] Focus Player stopped", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
