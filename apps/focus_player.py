from __future__ import annotations

import argparse
import json
import signal
import socket
import sys
import time
from dataclasses import dataclass
from typing import Optional

DEFAULT_SOUND_HOST = "127.0.0.1"
DEFAULT_SOUND_PORT = 28768
DEFAULT_CTRL_HOST = "127.0.0.1"
DEFAULT_CTRL_PORT = 28765

RUNNING = True


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


@dataclass
class SoundInfo:
    ok: bool
    timestamp: float
    voice_active: bool
    hold_active: bool
    rms: float
    raw_direction: float
    filtered_direction: float
    target_yaw_deg: float


class SoundQueryClient:
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
            raise RuntimeError("empty response from focus_sound query server")
        return json.loads(text.splitlines()[0])

    def get_sound_info(self) -> SoundInfo:
        payload = self.request("GET SOUND_INFO")
        return SoundInfo(
            ok=bool(payload.get("ok")),
            timestamp=float(payload.get("timestamp") or 0.0),
            voice_active=bool(payload.get("voice_active")),
            hold_active=bool(payload.get("hold_active")),
            rms=float(payload.get("rms") or 0.0),
            raw_direction=float(payload.get("raw_direction") or 0.0),
            filtered_direction=float(payload.get("filtered_direction") or 0.0),
            target_yaw_deg=float(payload.get("target_yaw_deg") or 0.0),
        )


class PersistentCtrlClient:
    def __init__(self, host: str, port: int, timeout: float = 1.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None

    def connect(self) -> bool:
        if self.sock is not None:
            return True
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self.sock.settimeout(self.timeout)
            print(f"[FOCUS] connected local ctrl: {self.host}:{self.port}", flush=True)
            return True
        except OSError as exc:
            self.sock = None
            print(f"[FOCUS] ctrl connect error: {exc}", flush=True)
            return False

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def send_line(self, command: str) -> bool:
        if not self.connect():
            return False
        try:
            assert self.sock is not None
            self.sock.sendall((command.rstrip() + "\n").encode("utf-8"))
            return True
        except OSError as exc:
            print(f"[FOCUS] ctrl send error: {exc}", flush=True)
            self.close()
            return False

    def set_head_ry(self, deg: float) -> bool:
        return self.send_line(f"SET HEAD rY {deg:.4f}")


@dataclass
class FocusController:
    yaw_smooth: float = 0.22
    max_yaw_deg: float = 180.0
    return_to_center: bool = True
    stale_timeout_sec: float = 1.0

    current_yaw_deg: float = 0.0

    def update(self, info: SoundInfo, now: float) -> tuple[float, bool]:
        fresh = (now - info.timestamp) <= self.stale_timeout_sec
        use_sound = info.ok and fresh and info.hold_active

        if use_sound:
            target = _clamp(-info.target_yaw_deg, -self.max_yaw_deg, self.max_yaw_deg)
        else:
            target = self.current_yaw_deg

        a = _clamp(self.yaw_smooth, 0.0, 1.0)
        self.current_yaw_deg += (target - self.current_yaw_deg) * a


        return self.current_yaw_deg, use_sound


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Apply sound focus yaw to HEAD rY")
    p.add_argument("--sound-host", default=DEFAULT_SOUND_HOST)
    p.add_argument("--sound-port", type=int, default=DEFAULT_SOUND_PORT)
    p.add_argument("--ctrl-host", default=DEFAULT_CTRL_HOST)
    p.add_argument("--ctrl-port", type=int, default=DEFAULT_CTRL_PORT)

    p.add_argument("--interval", type=float, default=0.033, help="control loop interval sec")
    p.add_argument("--print-every", type=float, default=0.5)

    p.add_argument("--yaw-smooth", type=float, default=0.22)
    p.add_argument("--max-yaw-deg", type=float, default=180.0)
    p.add_argument("--stale-timeout-sec", type=float, default=1.0)

    p.add_argument("--send-threshold-deg", type=float, default=0.35)
    p.add_argument("--return-to-center", action="store_true", default=True)
    p.add_argument("--no-return-to-center", dest="return_to_center", action="store_false")
    return p


def install_signal_handlers() -> None:
    def _handler(_signum, _frame):
        global RUNNING
        RUNNING = False

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)
    

def main() -> int:

    last_send_time = 0.0
    keepalive_sec = 0.20

    global RUNNING

    args = build_argparser().parse_args()
    install_signal_handlers()

    sound = SoundQueryClient(args.sound_host, args.sound_port)
    ctrl = PersistentCtrlClient(args.ctrl_host, args.ctrl_port)

    controller = FocusController(
        yaw_smooth=args.yaw_smooth,
        max_yaw_deg=args.max_yaw_deg,
        return_to_center=args.return_to_center,
        stale_timeout_sec=args.stale_timeout_sec,
    )

    print("========================================")
    print(" Focus Player (sound -> HEAD rY)")
    print("========================================")
    print(f"[INFO] sound query : {args.sound_host}:{args.sound_port}")
    print(f"[INFO] local ctrl  : {args.ctrl_host}:{args.ctrl_port}")
    print(f"[INFO] interval    : {args.interval:.3f}s")
    print(f"[INFO] yaw smooth  : {args.yaw_smooth:.3f}")
    print(f"[INFO] max yaw deg : {args.max_yaw_deg:.2f}")
    print(f"[INFO] Ctrl+C to stop")
    print("========================================")

    last_print = 0.0
    last_sent_yaw: Optional[float] = None

    try:
        while RUNNING:
            t0 = time.perf_counter()
            now = time.time()

            try:
                info = sound.get_sound_info()
                yaw_deg, use_sound = controller.update(info, now)

                send_yaw = round(yaw_deg, 2)
                should_send = (
                    last_sent_yaw is None
                    or abs(send_yaw - last_sent_yaw) >= args.send_threshold_deg
                    or (now - last_send_time) >= keepalive_sec
                )

                if should_send:
                    if ctrl.set_head_ry(send_yaw):
                        last_sent_yaw = send_yaw
                        last_send_time = now

                if now - last_print >= args.print_every:
                    last_print = now
                    age = now - info.timestamp if info.timestamp > 0 else -1.0
                    print(
                        f"[FOCUS] use_sound={use_sound} "
                        f"voice={info.voice_active} hold={info.hold_active} "
                        f"rms={info.rms:.5f} raw={info.raw_direction:+.3f} "
                        f"filtered={info.filtered_direction:+.3f} "
                        f"target={info.target_yaw_deg:+6.2f} yaw={yaw_deg:+6.2f} "
                        f"age={age:.3f}s",
                        flush=True,
                    )

            except (ConnectionError, OSError, RuntimeError, json.JSONDecodeError) as exc:
                if now - last_print >= args.print_every:
                    last_print = now
                    print(f"[WARN] waiting for focus_sound query server: {exc}", flush=True)

            sleep_sec = args.interval - (time.perf_counter() - t0)
            if sleep_sec > 0:
                time.sleep(sleep_sec)

    finally:
        ctrl.close()
        print("[INFO] Focus Player stopped", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())