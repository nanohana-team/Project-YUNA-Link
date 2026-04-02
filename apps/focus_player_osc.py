from __future__ import annotations

import argparse
import json
import signal
import socket
import struct
import sys
import time
from dataclasses import dataclass
from typing import Optional

DEFAULT_QUERY_HOST = "127.0.0.1"
DEFAULT_QUERY_PORT = 28766
DEFAULT_SOUND_HOST = "127.0.0.1"
DEFAULT_SOUND_PORT = 28768
DEFAULT_OSC_HOST = "127.0.0.1"
DEFAULT_OSC_PORT = 9000

RUNNING = True


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _to_opt_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _smooth_value(current: float, target: float, alpha: float) -> float:
    alpha = _clamp(alpha, 0.0, 1.0)
    return current + (target - current) * alpha


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
    box_w_norm: Optional[float]
    box_h_norm: Optional[float]
    box_area_norm: Optional[float]


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
        screen = payload.get("screen") or {}

        box_w_norm = _to_opt_float(screen.get("box_w_norm"))
        box_h_norm = _to_opt_float(screen.get("box_h_norm"))
        box_area_norm = _to_opt_float(screen.get("box_area_norm"))

        if box_area_norm is None and box_w_norm is not None and box_h_norm is not None:
            box_area_norm = box_w_norm * box_h_norm

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
            box_w_norm=box_w_norm,
            box_h_norm=box_h_norm,
            box_area_norm=box_area_norm,
        )


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


class OSCClient:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    @staticmethod
    def _pad4(data: bytes) -> bytes:
        padding = (-len(data)) % 4
        if padding:
            data += b"\x00" * padding
        return data

    def _build_message(self, address: str, *args) -> bytes:
        if not address.startswith("/"):
            raise ValueError(f"OSC address must start with '/': {address}")

        msg = self._pad4(address.encode("utf-8") + b"\x00")

        typetags = [","]
        payload = b""
        for arg in args:
            if isinstance(arg, bool):
                typetags.append("T" if arg else "F")
            elif isinstance(arg, int) and not isinstance(arg, bool):
                typetags.append("i")
                payload += struct.pack(">i", int(arg))
            elif isinstance(arg, float):
                typetags.append("f")
                payload += struct.pack(">f", float(arg))
            elif isinstance(arg, str):
                typetags.append("s")
                payload += self._pad4(arg.encode("utf-8") + b"\x00")
            else:
                raise TypeError(f"unsupported OSC arg type: {type(arg)!r}")

        msg += self._pad4("".join(typetags).encode("utf-8") + b"\x00")
        msg += payload
        return msg

    def send(self, address: str, *args) -> bool:
        try:
            packet = self._build_message(address, *args)
            self.sock.sendto(packet, (self.host, self.port))
            return True
        except OSError as exc:
            print(f"[FOCUS] OSC send error ({address}): {exc}", flush=True)
            return False

    def send_axis(self, address: str, value: float) -> bool:
        return self.send(address, float(_clamp(value, -1.0, 1.0)))

    def send_button(self, address: str, pressed: bool) -> bool:
        return self.send(address, 1 if pressed else 0)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def _sound_yaw_to_turn_plan(
    yaw_deg: float,
    turn_rate_deg_per_sec_at_full_axis: float,
    axis_mag: float = 1.0,
    min_turn_sec: float = 0.05,
    max_turn_sec: float = 1.20,
    gain: float = 1.0,
) -> tuple[float, float]:
    if abs(yaw_deg) < 1e-6:
        return 0.0, 0.0

    scaled_yaw = abs(yaw_deg) * max(0.0, gain)
    effective_rate = max(1e-6, turn_rate_deg_per_sec_at_full_axis * abs(axis_mag))
    duration = scaled_yaw / effective_rate
    duration = _clamp(duration, min_turn_sec, max_turn_sec)

    axis_x = abs(axis_mag) if yaw_deg > 0.0 else -abs(axis_mag)
    return axis_x, duration


def _x_norm_to_look_axis(
    x_norm: float,
    deadzone_x: float,
    full_x_norm: float,
    min_axis_x: float,
    max_axis_x: float,
    invert_x: bool,
) -> float:
    x = -float(x_norm) if invert_x else float(x_norm)

    if abs(x) <= deadzone_x:
        return 0.0

    sign = 1.0 if x > 0.0 else -1.0
    mag01 = (abs(x) - deadzone_x) / max(1e-6, (full_x_norm - deadzone_x))
    mag01 = _clamp(mag01, 0.0, 1.0)

    axis = min_axis_x + (max_axis_x - min_axis_x) * mag01
    return sign * axis




def _centering_look_axis(
    x_norm: float,
    gain: float,
    deadzone: float,
    max_axis: float,
) -> float:
    x = float(x_norm)
    if abs(x) <= deadzone:
        return 0.0
    axis = x * gain
    return _clamp(axis, -abs(max_axis), abs(max_axis))

def _estimate_distance_from_box_area(
    box_area_norm: Optional[float],
    near_area: float,
    near_distance_m: float,
    far_area: float,
    far_distance_m: float,
) -> Optional[float]:
    if box_area_norm is None:
        return None

    area = max(1e-6, float(box_area_norm))
    near_area = max(1e-6, float(near_area))
    far_area = max(1e-6, float(far_area))
    near_distance_m = max(1e-3, float(near_distance_m))
    far_distance_m = max(1e-3, float(far_distance_m))

    if near_area == far_area:
        return near_distance_m

    # 見かけの面積はおおむね距離^2に反比例する前提
    k_near = near_distance_m * (near_area ** 0.5)
    k_far = far_distance_m * (far_area ** 0.5)
    k = (k_near + k_far) * 0.5
    est = k / (area ** 0.5)

    min_d = min(near_distance_m, far_distance_m)
    max_d = max(near_distance_m, far_distance_m)
    return _clamp(est, min_d, max_d)


def _distance_to_forward_axis(
    distance_m: Optional[float],
    stop_distance_m: float,
    slow_distance_m: float,
    max_distance_m: float,
    min_forward: float,
    max_forward: float,
) -> float:
    if distance_m is None:
        return 0.0

    d = max(0.0, float(distance_m))
    if d <= stop_distance_m:
        return 0.0

    if d >= max_distance_m:
        return _clamp(max_forward, 0.0, 1.0)

    if slow_distance_m <= stop_distance_m:
        t = (d - stop_distance_m) / max(1e-6, (max_distance_m - stop_distance_m))
        return _clamp(min_forward + (max_forward - min_forward) * _clamp(t, 0.0, 1.0), 0.0, 1.0)

    if d <= slow_distance_m:
        t = (d - stop_distance_m) / max(1e-6, (slow_distance_m - stop_distance_m))
        axis = min_forward * _clamp(t, 0.0, 1.0)
        return _clamp(axis, 0.0, 1.0)

    t = (d - slow_distance_m) / max(1e-6, (max_distance_m - slow_distance_m))
    axis = min_forward + (max_forward - min_forward) * _clamp(t, 0.0, 1.0)
    return _clamp(axis, 0.0, 1.0)


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Focus player for VRChat Desktop OSC: sound turn + YOLO centering + forward by estimated distance from box size"
    )

    p.add_argument("--query-host", default=DEFAULT_QUERY_HOST)
    p.add_argument("--query-port", type=int, default=DEFAULT_QUERY_PORT)
    p.add_argument("--sound-host", default=DEFAULT_SOUND_HOST)
    p.add_argument("--sound-port", type=int, default=DEFAULT_SOUND_PORT)
    p.add_argument("--osc-host", default=DEFAULT_OSC_HOST)
    p.add_argument("--osc-port", type=int, default=DEFAULT_OSC_PORT)

    p.add_argument("--interval", type=float, default=0.012)
    p.add_argument("--print-every", type=float, default=0.5)

    p.add_argument("--vision-timeout-sec", type=float, default=0.90)
    p.add_argument("--sound-timeout-sec", type=float, default=1.20)
    p.add_argument("--sound-min-yaw-deg", type=float, default=8.0)
    p.add_argument("--sound-retrigger-cooldown-sec", type=float, default=0.70)

    p.add_argument("--vision-x-smooth", type=float, default=0.22)
    p.add_argument("--vision-y-smooth", type=float, default=0.22)
    p.add_argument("--vision-box-smooth", type=float, default=0.18)
    p.add_argument("--vision-return-smooth", type=float, default=0.10)
    p.add_argument("--distance-smooth", type=float, default=0.18)

    p.add_argument("--turn-rate-deg-per-sec-full-axis", type=float, default=185.0)
    p.add_argument("--body-turn-axis-x", type=float, default=1.0)
    p.add_argument("--body-turn-gain", type=float, default=0.50)
    p.add_argument("--body-turn-min-sec", type=float, default=0.05)
    p.add_argument("--body-turn-max-sec", type=float, default=1.20)
    p.add_argument("--body-turn-yaw-sign", type=float, default=1.0)
    p.add_argument("--yolo-confirm-sec", type=float, default=0.35)

    p.add_argument("--body-yolo-assist-enabled", action="store_true", default=True)
    p.add_argument("--no-body-yolo-assist", dest="body_yolo_assist_enabled", action="store_false")
    p.add_argument("--body-yolo-deadzone-x", type=float, default=0.04)
    p.add_argument("--body-yolo-full-x-norm", type=float, default=0.50)
    p.add_argument("--body-yolo-max-axis-x", type=float, default=0.18)
    p.add_argument("--body-yolo-min-axis-x", type=float, default=0.03)
    p.add_argument("--body-yolo-acquire-sec", type=float, default=0.03)
    p.add_argument("--invert-vision-x", action="store_true", default=True)
    p.add_argument("--no-invert-vision-x", dest="invert_vision_x", action="store_false")

    p.add_argument("--forward-enabled", action="store_true", default=True)
    p.add_argument("--no-forward", dest="forward_enabled", action="store_false")
    p.add_argument("--forward-center-required", action="store_true", default=True)
    p.add_argument("--no-forward-center-required", dest="forward_center_required", action="store_false")
    p.add_argument("--forward-center-max-x", type=float, default=0.35)
    p.add_argument("--forward-min-axis", type=float, default=0.22)
    p.add_argument("--forward-max-axis", type=float, default=1.00)
    p.add_argument("--forward-stop-distance-m", type=float, default=2.00)
    p.add_argument("--forward-slow-distance-m", type=float, default=3.50)
    p.add_argument("--forward-max-distance-m", type=float, default=4.50)

    # 箱サイズ -> 距離概算のキャリブレーション点
    p.add_argument("--box-near-area", type=float, default=0.16)
    p.add_argument("--box-near-distance-m", type=float, default=1.2)
    p.add_argument("--box-far-area", type=float, default=0.02)
    p.add_argument("--box-far-distance-m", type=float, default=4.5)

    p.add_argument("--enable-strafe", action="store_true", default=False)
    p.add_argument("--strafe-deadzone-x", type=float, default=0.10)
    p.add_argument("--strafe-full-x-norm", type=float, default=0.60)
    p.add_argument("--strafe-min-axis", type=float, default=0.10)
    p.add_argument("--strafe-max-axis", type=float, default=0.60)

    p.add_argument("--look-axis-smooth", type=float, default=0.22)
    p.add_argument("--look-axis-return-smooth", type=float, default=0.16)
    p.add_argument("--move-axis-smooth", type=float, default=0.18)
    p.add_argument("--move-axis-return-smooth", type=float, default=0.14)
    p.add_argument("--strafe-axis-smooth", type=float, default=0.18)
    p.add_argument("--strafe-axis-return-smooth", type=float, default=0.14)

    p.add_argument("--send-threshold-axis", type=float, default=0.01)
    p.add_argument("--keepalive-sec", type=float, default=0.20)
    p.add_argument("--run-threshold", type=float, default=0.55)
    p.add_argument("--run-while-turning", action="store_true", default=False)

    p.add_argument("--centering-gain", type=float, default=2.8)
    p.add_argument("--centering-deadzone", type=float, default=0.02)
    p.add_argument("--centering-max-axis", type=float, default=0.9)

    return p


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

    player_query = PlayerQueryClient(args.query_host, args.query_port)
    sound_query = SoundQueryClient(args.sound_host, args.sound_port)
    osc = OSCClient(args.osc_host, args.osc_port)

    print("========================================")
    print(" Focus Player (VRChat Desktop OSC)")
    print(" look : /input/LookHorizontal")
    print(" move : /input/Vertical (+ optional /input/Horizontal)")
    print(" run  : /input/Run")
    print("========================================")
    print(f"[INFO] vision query : {args.query_host}:{args.query_port}")
    print(f"[INFO] sound query  : {args.sound_host}:{args.sound_port}")
    print(f"[INFO] OSC target   : {args.osc_host}:{args.osc_port}")
    print(f"[INFO] interval     : {args.interval:.3f}s")
    print(f"[INFO] turn rate    : {args.turn_rate_deg_per_sec_full_axis:.1f} deg/s @ axis=1")
    print(f"[INFO] turn gain    : {args.body_turn_gain:.2f}")
    print(f"[INFO] yaw sign     : {args.body_turn_yaw_sign:+.1f}")
    print(f"[INFO] yolo assist  : {args.body_yolo_assist_enabled}")
    print(f"[INFO] forward      : {args.forward_enabled}")
    print(f"[INFO] box near     : area={args.box_near_area:.4f} -> {args.box_near_distance_m:.2f}m")
    print(f"[INFO] box far      : area={args.box_far_area:.4f} -> {args.box_far_distance_m:.2f}m")
    print("")

    last_print = 0.0
    last_send_time = 0.0

    last_sent_look: Optional[float] = None
    last_sent_vertical: Optional[float] = None
    last_sent_horizontal: Optional[float] = None
    last_sent_run: Optional[int] = None

    turning_until = 0.0
    turn_axis_x = 0.0
    yolo_confirm_until = 0.0
    last_sound_trigger_time = 0.0
    last_turn_yaw_deg = 0.0
    last_source = "idle"
    vision_acquired_since = 0.0

    smoothed_x_norm = 0.0
    smoothed_y_norm = 0.0
    smoothed_box_area = 0.0
    smoothed_est_distance_m = 0.0
    smoothed_look_axis = 0.0
    smoothed_vertical = 0.0
    smoothed_horizontal = 0.0

    try:
        while RUNNING:
            loop_start = time.perf_counter()
            now = time.time()

            try:
                player = player_query.get_player_info()
            except Exception as exc:
                print(f"[FOCUS] player query error: {exc}", flush=True)
                player = PlayerInfo(
                    False, 0.0, "lost",
                    None, None, None, None,
                    0, 0,
                    None, None, None,
                    "unknown",
                    None, None, None,
                )

            try:
                sound = sound_query.get_sound_info()
            except Exception as exc:
                print(f"[FOCUS] sound query error: {exc}", flush=True)
                sound = SoundInfo(False, 0.0, False, False, 0.0, 0.0, 0.0, 0.0)

            has_vision = (
                player.ok
                and player.x_norm is not None
                and player.y_norm is not None
                and (now - player.timestamp) <= args.vision_timeout_sec
            )

            if has_vision:
                if vision_acquired_since == 0.0:
                    vision_acquired_since = now
            else:
                vision_acquired_since = 0.0

            if has_vision:
                smoothed_x_norm = _smooth_value(smoothed_x_norm, float(player.x_norm), args.vision_x_smooth)
                smoothed_y_norm = _smooth_value(smoothed_y_norm, float(player.y_norm), args.vision_y_smooth)
                if player.box_area_norm is not None:
                    smoothed_box_area = _smooth_value(
                        smoothed_box_area,
                        float(player.box_area_norm),
                        args.vision_box_smooth,
                    )
            else:
                smoothed_x_norm = _smooth_value(smoothed_x_norm, 0.0, args.vision_return_smooth)
                smoothed_y_norm = _smooth_value(smoothed_y_norm, 0.0, args.vision_return_smooth)
                smoothed_box_area = _smooth_value(smoothed_box_area, 0.0, args.vision_return_smooth)

            if abs(smoothed_x_norm) < 0.002:
                smoothed_x_norm = 0.0
            if abs(smoothed_y_norm) < 0.002:
                smoothed_y_norm = 0.0
            if abs(smoothed_box_area) < 0.0001:
                smoothed_box_area = 0.0

            est_distance = _estimate_distance_from_box_area(
                box_area_norm=smoothed_box_area if has_vision or smoothed_box_area > 0.0 else None,
                near_area=args.box_near_area,
                near_distance_m=args.box_near_distance_m,
                far_area=args.box_far_area,
                far_distance_m=args.box_far_distance_m,
            )

            target_est_d = est_distance if est_distance is not None else 0.0
            dist_alpha = args.distance_smooth if est_distance is not None else args.vision_return_smooth
            smoothed_est_distance_m = _smooth_value(smoothed_est_distance_m, target_est_d, dist_alpha)
            if smoothed_est_distance_m < 0.01:
                smoothed_est_distance_m = 0.0

            sound_fresh = sound.ok and ((now - sound.timestamp) <= args.sound_timeout_sec)
            sound_usable = sound_fresh and sound.hold_active and (abs(sound.target_yaw_deg) >= args.sound_min_yaw_deg)

            vision_ready_for_body_assist = (
                has_vision
                and vision_acquired_since > 0.0
                and (now - vision_acquired_since) >= args.body_yolo_acquire_sec
            )

            raw_look_axis = 0.0

            if now < turning_until:
                raw_look_axis = turn_axis_x
                last_source = "sound_turn"

            elif now < yolo_confirm_until:
                raw_look_axis = 0.0
                last_source = "yolo_confirm"

            elif vision_ready_for_body_assist and args.body_yolo_assist_enabled:
                raw_look_axis = _centering_look_axis(
                    x_norm=smoothed_x_norm,
                    gain=args.centering_gain,
                    deadzone=args.centering_deadzone,
                    max_axis=args.centering_max_axis,
                )
                last_source = "vision_centering"

            elif sound_usable and (now - last_sound_trigger_time) >= args.sound_retrigger_cooldown_sec:
                planned_yaw_deg = sound.target_yaw_deg * args.body_turn_yaw_sign
                axis_x, duration = _sound_yaw_to_turn_plan(
                    yaw_deg=planned_yaw_deg,
                    turn_rate_deg_per_sec_at_full_axis=args.turn_rate_deg_per_sec_full_axis,
                    axis_mag=abs(args.body_turn_axis_x),
                    min_turn_sec=args.body_turn_min_sec,
                    max_turn_sec=args.body_turn_max_sec,
                    gain=args.body_turn_gain,
                )

                if duration > 0.0:
                    turn_axis_x = axis_x
                    turning_until = now + duration
                    yolo_confirm_until = turning_until + args.yolo_confirm_sec
                    last_sound_trigger_time = now
                    last_turn_yaw_deg = planned_yaw_deg
                    raw_look_axis = turn_axis_x
                    last_source = f"sound_turn_start({duration:.2f}s)"
                else:
                    raw_look_axis = 0.0
                    last_source = "sound_small"
            else:
                raw_look_axis = 0.0
                last_source = "idle"

            look_alpha = args.look_axis_smooth if abs(raw_look_axis) > abs(smoothed_look_axis) else args.look_axis_return_smooth
            smoothed_look_axis = _smooth_value(smoothed_look_axis, raw_look_axis, look_alpha)
            if abs(smoothed_look_axis) < 0.003:
                smoothed_look_axis = 0.0
            send_look_axis = round(smoothed_look_axis, 3)

            raw_vertical = 0.0
            if has_vision and args.forward_enabled:
                can_move = True
                if args.forward_center_required:
                    can_move = abs(smoothed_x_norm) <= args.forward_center_max_x

                if can_move:
                    raw_vertical = _distance_to_forward_axis(
                        distance_m=smoothed_est_distance_m if smoothed_est_distance_m > 0.0 else None,
                        stop_distance_m=args.forward_stop_distance_m,
                        slow_distance_m=args.forward_slow_distance_m,
                        max_distance_m=args.forward_max_distance_m,
                        min_forward=args.forward_min_axis,
                        max_forward=args.forward_max_axis,
                    )

            move_alpha = args.move_axis_smooth if raw_vertical > smoothed_vertical else args.move_axis_return_smooth
            smoothed_vertical = _smooth_value(smoothed_vertical, raw_vertical, move_alpha)
            if abs(smoothed_vertical) < 0.003:
                smoothed_vertical = 0.0
            send_vertical = round(smoothed_vertical, 3)

            raw_horizontal = 0.0
            if args.enable_strafe and has_vision:
                raw_horizontal = _x_norm_to_look_axis(
                    x_norm=smoothed_x_norm,
                    deadzone_x=args.strafe_deadzone_x,
                    full_x_norm=args.strafe_full_x_norm,
                    min_axis_x=args.strafe_min_axis,
                    max_axis_x=args.strafe_max_axis,
                    invert_x=False,
                )

            strafe_alpha = args.strafe_axis_smooth if abs(raw_horizontal) > abs(smoothed_horizontal) else args.strafe_axis_return_smooth
            smoothed_horizontal = _smooth_value(smoothed_horizontal, raw_horizontal, strafe_alpha)
            if abs(smoothed_horizontal) < 0.003:
                smoothed_horizontal = 0.0
            send_horizontal = round(smoothed_horizontal, 3)

            send_run = 1 if (send_vertical >= args.run_threshold or (args.run_while_turning and abs(send_look_axis) >= 0.8)) else 0

            should_send_look = (
                last_sent_look is None
                or abs(send_look_axis - last_sent_look) >= args.send_threshold_axis
                or (now - last_send_time) >= args.keepalive_sec
            )
            should_send_vertical = (
                last_sent_vertical is None
                or abs(send_vertical - last_sent_vertical) >= args.send_threshold_axis
                or (now - last_send_time) >= args.keepalive_sec
            )
            should_send_horizontal = (
                last_sent_horizontal is None
                or abs(send_horizontal - last_sent_horizontal) >= args.send_threshold_axis
                or (now - last_send_time) >= args.keepalive_sec
            )
            should_send_run = (
                last_sent_run is None
                or send_run != last_sent_run
                or (now - last_send_time) >= args.keepalive_sec
            )

            if should_send_look and osc.send_axis("/input/LookHorizontal", send_look_axis):
                last_sent_look = send_look_axis
                last_send_time = now

            if should_send_vertical and osc.send_axis("/input/Vertical", send_vertical):
                last_sent_vertical = send_vertical
                last_send_time = now

            if should_send_horizontal and osc.send_axis("/input/Horizontal", send_horizontal):
                last_sent_horizontal = send_horizontal
                last_send_time = now

            if should_send_run and osc.send_button("/input/Run", bool(send_run)):
                last_sent_run = send_run
                last_send_time = now

            if now - last_print >= args.print_every:
                last_print = now
                vx = f"{smoothed_x_norm:+.3f}"
                vy = f"{smoothed_y_norm:+.3f}"
                sound_yaw = f"{sound.target_yaw_deg:+7.2f}" if sound.ok else "  None "
                box_area = f"{smoothed_box_area:.4f}" if has_vision or smoothed_box_area > 0.0 else "None"
                est_dist_str = f"{smoothed_est_distance_m:.2f}m" if smoothed_est_distance_m > 0.0 else "None"

                print(
                    f"[FOCUS] src={last_source:<24} "
                    f"vision_ok={has_vision} mode={player.track_mode:<18} "
                    f"x_norm={vx:>7} y_norm={vy:>7} "
                    f"box_area={box_area:>7} est_dist={est_dist_str:>7} "
                    f"sound_yaw={sound_yaw} "
                    f"last_turn_yaw={last_turn_yaw_deg:+7.2f} "
                    f"look={send_look_axis:+6.3f} "
                    f"vert={send_vertical:+6.3f} "
                    f"hori={send_horizontal:+6.3f} "
                    f"run={send_run}",
                    flush=True,
                )

            sleep_sec = args.interval - (time.perf_counter() - loop_start)
            if sleep_sec > 0:
                time.sleep(sleep_sec)

    finally:
        try:
            osc.send_axis("/input/LookHorizontal", 0.0)
            osc.send_axis("/input/Vertical", 0.0)
            osc.send_axis("/input/Horizontal", 0.0)
            osc.send_button("/input/Run", False)
        except Exception:
            pass
        osc.close()
        print("[INFO] Focus Player stopped", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
