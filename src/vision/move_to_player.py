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

    right_dx_px: Optional[float]
    right_dy_px: Optional[float]
    right_dx_norm: Optional[float]
    right_dy_norm: Optional[float]

    distance_m: Optional[float]
    distance_raw_m: Optional[float]
    disparity_px: Optional[float]
    category: str


def _to_opt_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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

    def stop_all(self) -> None:
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
        right_offset = payload.get("right_offset") or {}

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

            right_dx_px=_to_opt_float(right_offset.get("dx_px")),
            right_dy_px=_to_opt_float(right_offset.get("dy_px")),
            right_dx_norm=_to_opt_float(right_offset.get("dx_norm")),
            right_dy_norm=_to_opt_float(right_offset.get("dy_norm")),

            distance_m=_to_opt_float(payload.get("distance_m")),
            distance_raw_m=_to_opt_float(payload.get("distance_raw_m")),
            disparity_px=_to_opt_float(payload.get("disparity_px")),
            category=str(payload.get("category") or "unknown"),
        )


@dataclass
class NormalizedTarget:
    x_norm: Optional[float]
    y_norm: Optional[float]
    dx_px: Optional[float]
    dy_px: Optional[float]


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _apply_stick_min(v: float, min_abs: float) -> float:
    if v == 0.0:
        return 0.0
    if abs(v) < min_abs:
        return min_abs if v > 0.0 else -min_abs
    return v


def _target_from_right_offset(info: PlayerInfo) -> NormalizedTarget:
    return NormalizedTarget(
        x_norm=info.right_dx_norm,
        y_norm=info.right_dy_norm,
        dx_px=info.right_dx_px,
        dy_px=info.right_dy_px,
    )


@dataclass
class MoveController:
    move_align_deadzone: float = 0.18
    move_gain: float = 0.90
    move_max_forward: float = 0.85
    move_smooth: float = 0.20
    stick_min: float = 0.20

    target_offset_x: float = 0.00
    stop_distance_m: float = 2.50
    target_timeout_sec: float = 0.70
    invert_x: bool = False

    current_forward: float = 0.0
    last_seen_ts: float = 0.0

    def _compute_forward(self, x_norm: float, info: PlayerInfo) -> float:
        align_err = x_norm - self.target_offset_x
        if self.invert_x:
            align_err = -align_err

        if abs(align_err) > self.move_align_deadzone:
            return 0.0

        if info.distance_m is None:
            return 0.0

        dist = float(info.distance_m)
        if dist <= self.stop_distance_m:
            return 0.0

        gap = dist - self.stop_distance_m
        out = _clamp(gap * self.move_gain, 0.0, self.move_max_forward)
        return _apply_stick_min(out, self.stick_min) if out > 0.0 else 0.0

    def update(self, info: PlayerInfo, tgt: NormalizedTarget) -> tuple[float, bool]:
        now = time.time()
        has_target = bool(info.ok and tgt.x_norm is not None)

        target_forward = 0.0
        if has_target:
            self.last_seen_ts = now
            target_forward = self._compute_forward(float(tgt.x_norm), info)
        elif (now - self.last_seen_ts) <= self.target_timeout_sec:
            target_forward = 0.0

        self.current_forward += (target_forward - self.current_forward) * _clamp(self.move_smooth, 0.0, 1.0)

        if abs(self.current_forward) < 0.001:
            self.current_forward = 0.0
        else:
            self.current_forward = _apply_stick_min(self.current_forward, self.stick_min)

        return self.current_forward, has_target


def build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Move to player with L_STICK only (no head control)")
    parser.add_argument("--query-host", default=DEFAULT_QUERY_HOST)
    parser.add_argument("--query-port", type=int, default=DEFAULT_QUERY_PORT)
    parser.add_argument("--ctrl-host", default=DEFAULT_CTRL_HOST)
    parser.add_argument("--ctrl-port", type=int, default=DEFAULT_CTRL_PORT)

    parser.add_argument("--interval", type=float, default=0.05, help="control loop interval seconds")
    parser.add_argument("--print-every", type=float, default=0.5)

    parser.add_argument("--move-align-deadzone", type=float, default=0.18)
    parser.add_argument("--move-gain", type=float, default=0.90)
    parser.add_argument("--move-max-forward", type=float, default=0.85)
    parser.add_argument("--move-smooth", type=float, default=0.20)
    parser.add_argument("--stick-min", type=float, default=0.20)

    parser.add_argument("--target-offset-x", type=float, default=0.00)
    parser.add_argument("--stop-distance-m", type=float, default=2.50)
    parser.add_argument("--target-timeout-sec", type=float, default=0.70)

    parser.add_argument("--invert-x", action="store_true")
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

    controller = MoveController(
        move_align_deadzone=args.move_align_deadzone,
        move_gain=args.move_gain,
        move_max_forward=args.move_max_forward,
        move_smooth=args.move_smooth,
        stick_min=args.stick_min,
        target_offset_x=args.target_offset_x,
        stop_distance_m=args.stop_distance_m,
        target_timeout_sec=args.target_timeout_sec,
        invert_x=args.invert_x,
    )

    print("========================================")
    print(" Move To Player (L_STICK only)")
    print("========================================")
    print(f"[INFO] query server      : {args.query_host}:{args.query_port}")
    print(f"[INFO] local ctrl        : {args.ctrl_host}:{args.ctrl_port}")
    print(f"[INFO] interval          : {args.interval:.3f}s")
    print(f"[INFO] target offset x   : {args.target_offset_x:+.3f}")
    print(f"[INFO] stop distance     : {args.stop_distance_m:.2f}m")
    print(f"[INFO] stick min         : {args.stick_min:.3f}")
    print("[INFO] head control      : disabled (handled by focus_player)")
    print("[INFO] Ctrl+C to stop")
    print("========================================")

    last_print = 0.0

    try:
        while RUNNING:
            loop_t0 = time.perf_counter()

            try:
                info = query.get_player_info()
                tgt = _target_from_right_offset(info)

                forward_y, has_target = controller.update(info, tgt)
                ctrl.set_l_stick(0.0, forward_y)

                now = time.time()
                if now - last_print >= args.print_every:
                    last_print = now
                    dx_px = "None" if tgt.dx_px is None else f"{tgt.dx_px:+.1f}"
                    dx_norm = "None" if tgt.x_norm is None else f"{tgt.x_norm:+.3f}"
                    dist = "None" if info.distance_m is None else f"{info.distance_m:.2f}m"

                    print(
                        f"[MOVE] target={has_target} mode={info.track_mode:<18} "
                        f"dx_px={dx_px:>7} dx={dx_norm:>7} "
                        f"aim_x={args.target_offset_x:+.2f} "
                        f"dist={dist:>7} fwd={forward_y:+.3f}",
                        flush=True,
                    )

            except (ConnectionError, OSError, RuntimeError, json.JSONDecodeError) as exc:
                try:
                    ctrl.stop_all()
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
        ctrl.stop_all()
        print("[INFO] Move To Player stopped", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())