# -*- coding: utf-8 -*-
"""
src/audio/focus_sound.py

Windows default output device (WASAPI loopback) -> sound direction query server

依存:
    pip install pyaudiowpatch numpy

概要:
- Windowsの既定出力デバイスをWASAPI loopbackで直接キャプチャ
- ステレオ左右差から簡易方向推定
- 音が来た方向を shared state に保持
- query server で GET SOUND_INFO / PING を返す
- もう HEAD は直接動かさない
"""

from __future__ import annotations

import argparse
import json
import socketserver
import threading
import time
from dataclasses import dataclass, field

import numpy as np
import pyaudiowpatch as pyaudio


# ============================================================
# Config
# ============================================================

@dataclass
class SoundFocusConfig:
    rate: int = 48000
    channels: int = 2
    frames_per_buffer: int = 1024

    voice_rms_threshold: float = 0.008
    hold_seconds: float = 0.55

    min_total_energy: float = 1e-8
    direction_deadzone: float = 0.14
    direction_gamma: float = 1.15
    max_yaw_deg: float = 180.0

    ema_alpha: float = 0.18


# ============================================================
# Shared state / query server
# ============================================================

@dataclass
class SharedSoundState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    timestamp: float = 0.0
    ok: bool = False
    voice_active: bool = False
    hold_active: bool = False
    rms: float = 0.0
    raw_direction: float = 0.0
    filtered_direction: float = 0.0
    target_yaw_deg: float = 0.0

    def update(
        self,
        *,
        ok: bool,
        voice_active: bool,
        hold_active: bool,
        rms: float,
        raw_direction: float,
        filtered_direction: float,
        target_yaw_deg: float,
    ) -> None:
        with self.lock:
            self.timestamp = time.time()
            self.ok = bool(ok)
            self.voice_active = bool(voice_active)
            self.hold_active = bool(hold_active)
            self.rms = float(rms)
            self.raw_direction = float(raw_direction)
            self.filtered_direction = float(filtered_direction)
            self.target_yaw_deg = float(target_yaw_deg)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "timestamp": self.timestamp,
                "ok": self.ok,
                "voice_active": self.voice_active,
                "hold_active": self.hold_active,
                "rms": self.rms,
                "raw_direction": self.raw_direction,
                "filtered_direction": self.filtered_direction,
                "target_yaw_deg": self.target_yaw_deg,
            }


class SoundQueryTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, handler_cls, shared_state: SharedSoundState):
        super().__init__(server_address, handler_cls)
        self.shared_state = shared_state


class SoundQueryHandler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            while True:
                line = self.rfile.readline()
                if not line:
                    return

                cmd = line.decode("utf-8", errors="ignore").strip().upper()
                snap = self.server.shared_state.snapshot()

                if cmd == "PING":
                    payload = {"ok": True, "reply": "PONG"}

                elif cmd == "GET SOUND_INFO":
                    payload = {
                        "ok": bool(snap["ok"]),
                        "timestamp": snap["timestamp"],
                        "voice_active": snap["voice_active"],
                        "hold_active": snap["hold_active"],
                        "rms": snap["rms"],
                        "raw_direction": snap["raw_direction"],
                        "filtered_direction": snap["filtered_direction"],
                        "target_yaw_deg": snap["target_yaw_deg"],
                    }

                else:
                    payload = {
                        "ok": False,
                        "error": "unknown_command",
                        "supported": ["PING", "GET SOUND_INFO"],
                    }

                self.wfile.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))

        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            return


def start_query_server(host: str, port: int, shared_state: SharedSoundState):
    server = SoundQueryTCPServer((host, port), SoundQueryHandler, shared_state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


# ============================================================
# Audio helpers
# ============================================================

def print_devices() -> None:
    with pyaudio.PyAudio() as pa:
        print("================================================")
        print(" PyAudioWPatch Device List")
        print("================================================")
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            print(
                f"[{i:03d}] "
                f"name={info.get('name')} | "
                f"hostApi={info.get('hostApi')} | "
                f"in={info.get('maxInputChannels')} | "
                f"out={info.get('maxOutputChannels')} | "
                f"rate={info.get('defaultSampleRate')}"
            )

        print()
        try:
            loop = pa.get_default_wasapi_loopback()
            print("[DEFAULT WASAPI LOOPBACK]")
            print(loop)
        except Exception as e:
            print(f"[WARN] could not get default WASAPI loopback: {e}")


def get_default_loopback_info(pa: pyaudio.PyAudio) -> dict:
    info = pa.get_default_wasapi_loopback()
    if not info:
        raise RuntimeError("default WASAPI loopback device not found")
    return info


# ============================================================
# Focus controller
# ============================================================

class SoundFocusController:
    def __init__(self, cfg: SoundFocusConfig) -> None:
        self.cfg = cfg
        self.filtered_direction = 0.0
        self.target_yaw_deg = 0.0
        self.last_voice_time = 0.0

        self.last_rms = 0.0
        self.last_raw_direction = 0.0
        self.last_voice_active = False
        self.last_hold_active = False

    @staticmethod
    def _rms(x: np.ndarray) -> float:
        return float(np.sqrt(np.mean(np.square(x), dtype=np.float64) + 1e-12))

    def process_block(self, interleaved_i16: bytes, now: float) -> None:
        audio = np.frombuffer(interleaved_i16, dtype=np.int16)
        if audio.size < 2:
            self.last_rms = 0.0
            self.last_raw_direction = 0.0
            self.last_voice_active = False
            self.last_hold_active = False
            self.target_yaw_deg = 0.0
            return

        frame_count = audio.size // 2
        audio = audio[: frame_count * 2].reshape(-1, 2).astype(np.float32) / 32768.0

        left = audio[:, 0].astype(np.float64, copy=False)
        right = audio[:, 1].astype(np.float64, copy=False)
        mono = 0.5 * (left + right)

        rms = self._rms(mono)
        voice_active = rms >= self.cfg.voice_rms_threshold
        if voice_active:
            self.last_voice_time = now

        hold_active = (now - self.last_voice_time) <= self.cfg.hold_seconds

        l_energy = float(np.mean(left * left) + 1e-12)
        r_energy = float(np.mean(right * right) + 1e-12)
        total = l_energy + r_energy

        raw_direction = 0.0
        if total > self.cfg.min_total_energy:
            raw_direction = (r_energy - l_energy) / total
            raw_direction = max(-1.0, min(1.0, raw_direction))

        if abs(raw_direction) < self.cfg.direction_deadzone:
            raw_direction = 0.0
        else:
            sign = 1.0 if raw_direction >= 0.0 else -1.0
            raw_direction = sign * (abs(raw_direction) ** self.cfg.direction_gamma)

        if hold_active:
            a = self.cfg.ema_alpha
            self.filtered_direction = (1.0 - a) * self.filtered_direction + a * raw_direction
            self.target_yaw_deg = self.filtered_direction * self.cfg.max_yaw_deg
        else:
            # 最後の方向を保持
            self.target_yaw_deg = self.filtered_direction * self.cfg.max_yaw_deg

        self.last_rms = rms
        self.last_raw_direction = raw_direction
        self.last_voice_active = voice_active
        self.last_hold_active = hold_active


# ============================================================
# Main
# ============================================================

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Default output loopback -> sound direction query server")
    p.add_argument("--query-host", default="127.0.0.1")
    p.add_argument("--query-port", type=int, default=28768)

    p.add_argument("--rate", type=int, default=48000)
    p.add_argument("--frames-per-buffer", type=int, default=1024)

    p.add_argument("--voice-threshold", type=float, default=0.006)
    p.add_argument("--hold-seconds", type=float, default=0.35)
    p.add_argument("--deadzone", type=float, default=0.06)
    p.add_argument("--max-yaw", type=float, default=180.0)
    p.add_argument("--ema-alpha", type=float, default=0.28)

    p.add_argument("--list-devices", action="store_true")
    p.add_argument("--debug", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if args.list_devices:
        print_devices()
        return 0

    cfg = SoundFocusConfig(
        rate=args.rate,
        frames_per_buffer=args.frames_per_buffer,
        voice_rms_threshold=args.voice_threshold,
        hold_seconds=args.hold_seconds,
        direction_deadzone=args.deadzone,
        max_yaw_deg=args.max_yaw,
        ema_alpha=args.ema_alpha,
    )

    controller = SoundFocusController(cfg)
    shared_state = SharedSoundState()
    query_server = None

    print("================================================")
    print(" YUNA Link - Sound Direction Query Server")
    print("================================================")

    with pyaudio.PyAudio() as pa:
        loopback_info = get_default_loopback_info(pa)

        device_index = int(loopback_info["index"])
        device_name = str(loopback_info.get("name"))
        max_input_channels = int(loopback_info.get("maxInputChannels", 2) or 2)
        default_rate = int(float(loopback_info.get("defaultSampleRate", cfg.rate)))

        channels = min(2, max_input_channels)
        if channels < 2:
            print(f"[WARN] loopback device reports mono input ({channels}ch). stereo direction may not work well.")

        actual_rate = default_rate if default_rate > 0 else cfg.rate

        print(f"[INFO] Loopback device : {device_name}")
        print(f"[INFO] Device index    : {device_index}")
        print(f"[INFO] Channels        : {channels}")
        print(f"[INFO] Sample rate     : {actual_rate}")
        print(f"[INFO] Buffer frames   : {cfg.frames_per_buffer}")
        print(f"[INFO] Voice threshold : {cfg.voice_rms_threshold}")
        print(f"[INFO] Max yaw         : {cfg.max_yaw_deg:.1f} deg")
        print(f"[INFO] Query server    : {args.query_host}:{args.query_port}")
        print()

        try:
            query_server, _ = start_query_server(args.query_host, args.query_port, shared_state)
            print(f"[INFO] sound query server listening on {args.query_host}:{args.query_port}")
        except Exception as exc:
            print(f"[WARN] sound query server start failed: {exc}")
            return 1

        stream = pa.open(
            format=pyaudio.paInt16,
            channels=channels,
            rate=actual_rate,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=cfg.frames_per_buffer,
        )

        last_debug_print = 0.0

        try:
            while True:
                data = stream.read(cfg.frames_per_buffer, exception_on_overflow=False)
                now = time.perf_counter()

                controller.process_block(data, now)

                shared_state.update(
                    ok=True,
                    voice_active=controller.last_voice_active,
                    hold_active=controller.last_hold_active,
                    rms=controller.last_rms,
                    raw_direction=controller.last_raw_direction,
                    filtered_direction=controller.filtered_direction,
                    target_yaw_deg=controller.target_yaw_deg,
                )

                if args.debug and (time.time() - last_debug_print) >= 0.2:
                    last_debug_print = time.time()
                    print(
                        f"[DEBUG] rms={controller.last_rms:.5f} "
                        f"raw_dir={controller.last_raw_direction:+.3f} "
                        f"filtered={controller.filtered_direction:+.3f} "
                        f"hold={controller.last_hold_active} "
                        f"target={controller.target_yaw_deg:+6.2f}"
                    )

        except KeyboardInterrupt:
            print("\n[SOUND FOCUS] stopped by user")
        finally:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass

    try:
        if query_server is not None:
            query_server.shutdown()
            query_server.server_close()
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())