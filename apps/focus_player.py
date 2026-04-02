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
DEFAULT_SOUND_HOST = "127.0.0.1"
DEFAULT_SOUND_PORT = 28768
DEFAULT_CTRL_HOST = "127.0.0.1"
DEFAULT_CTRL_PORT = 28765

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

    def set_left_stick(self, x: float, y: float = 0.0) -> bool:
        return self.send_line(f"SET L_STICK {x:.4f} {y:.4f}")

    def set_right_stick(self, x: float, y: float = 0.0) -> bool:
        return self.send_line(f"SET R_STICK {x:.4f} {y:.4f}")

    def set_head_rx(self, deg: float) -> bool:
        return self.send_line(f"SET HEAD rX {deg:.4f}")

    def set_head_ry(self, deg: float) -> bool:
        return self.send_line(f"SET HEAD rY {deg:.4f}")

    def set_head_rz(self, deg: float) -> bool:
        return self.send_line(f"SET HEAD rZ {deg:.4f}")


def _player_to_head_angles(
    x_norm: float,
    y_norm: float,
    invert_x: bool,
    invert_y: bool,
    yaw_gain_deg: float,
    pitch_gain_deg: float,
    roll_gain_deg: float,
    yaw_max_deg: float,
    pitch_max_deg: float,
    roll_max_deg: float,
    deadzone_x_norm: float,
    deadzone_y_norm: float,
) -> tuple[float, float, float]:
    x = float(x_norm)
    y = float(y_norm)

    if invert_x:
        x = -x
    if invert_y:
        y = -y

    if abs(x) < deadzone_x_norm:
        x = 0.0
    if abs(y) < deadzone_y_norm:
        y = 0.0

    head_ry = _clamp(x * yaw_gain_deg, -yaw_max_deg, yaw_max_deg)
    head_rx = _clamp(y * pitch_gain_deg, -pitch_max_deg, pitch_max_deg)
    head_rz = _clamp(0.0 * roll_gain_deg, -roll_max_deg, roll_max_deg)
    return head_rx, head_ry, head_rz


def _smooth_angle(current: float, target: float, alpha: float) -> float:
    alpha = _clamp(alpha, 0.0, 1.0)
    return current + (target - current) * alpha


def _sound_yaw_to_turn_plan(
    yaw_deg: float,
    turn_rate_deg_per_sec_at_full_stick: float,
    stick_x_mag: float = 1.0,
    min_turn_sec: float = 0.05,
    max_turn_sec: float = 1.20,
    gain: float = 1.0,
) -> tuple[float, float]:
    if abs(yaw_deg) < 1e-6:
        return 0.0, 0.0

    scaled_yaw = abs(yaw_deg) * max(0.0, gain)
    effective_rate = max(1e-6, turn_rate_deg_per_sec_at_full_stick * abs(stick_x_mag))
    duration = scaled_yaw / effective_rate
    duration = _clamp(duration, min_turn_sec, max_turn_sec)

    stick_x = abs(stick_x_mag) if yaw_deg > 0.0 else -abs(stick_x_mag)
    return stick_x, duration


def _x_norm_to_body_stick(
    x_norm: float,
    deadzone_x: float,
    full_x_norm: float,
    min_stick_x: float,
    max_stick_x: float,
) -> float:
    x = float(x_norm)

    if abs(x) <= deadzone_x:
        return 0.0

    sign = 1.0 if x > 0.0 else -1.0
    mag01 = (abs(x) - deadzone_x) / max(1e-6, (full_x_norm - deadzone_x))
    mag01 = _clamp(mag01, 0.0, 1.0)

    stick = min_stick_x + (max_stick_x - min_stick_x) * mag01
    return sign * stick


def _box_area_to_forward_stick(
    box_area_norm: Optional[float],
    area_threshold: float,
    min_forward_y: float,
    max_forward_y: float,
    area_zero_at: float,
) -> float:
    if box_area_norm is None:
        return 0.0

    area = max(0.0, float(box_area_norm))
    if area >= area_threshold:
        return 0.0

    denom = max(1e-6, area_threshold - area_zero_at)
    t = (area_threshold - area) / denom
    t = _clamp(t, 0.0, 1.0)

    y = min_forward_y + (max_forward_y - min_forward_y) * t
    return _clamp(y, 0.0, 1.0)


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Focus player: sound turn + YOLO centering + forward by box size"
    )

    p.add_argument("--query-host", default=DEFAULT_QUERY_HOST)
    p.add_argument("--query-port", type=int, default=DEFAULT_QUERY_PORT)
    p.add_argument("--sound-host", default=DEFAULT_SOUND_HOST)
    p.add_argument("--sound-port", type=int, default=DEFAULT_SOUND_PORT)
    p.add_argument("--ctrl-host", default=DEFAULT_CTRL_HOST)
    p.add_argument("--ctrl-port", type=int, default=DEFAULT_CTRL_PORT)

    p.add_argument("--interval", type=float, default=0.012)
    p.add_argument("--print-every", type=float, default=0.5)

    # Head control
    p.add_argument("--vision-timeout-sec", type=float, default=0.90)
    p.add_argument("--vision-grace-sec", type=float, default=0.30)

    p.add_argument("--head-yaw-gain-deg", type=float, default=18.0)
    p.add_argument("--head-pitch-gain-deg", type=float, default=12.0)
    p.add_argument("--head-roll-gain-deg", type=float, default=0.0)
    p.add_argument("--head-yaw-max-deg", type=float, default=18.0)
    p.add_argument("--head-pitch-max-deg", type=float, default=12.0)
    p.add_argument("--head-roll-max-deg", type=float, default=8.0)
    p.add_argument("--head-deadzone-x-norm", type=float, default=0.02)
    p.add_argument("--head-deadzone-y-norm", type=float, default=0.02)
    p.add_argument("--head-smooth", type=float, default=0.18)
    p.add_argument("--head-return-smooth", type=float, default=0.12)
    p.add_argument("--invert-vision-x", action="store_true", default=True)
    p.add_argument("--no-invert-vision-x", dest="invert_vision_x", action="store_false")
    p.add_argument("--invert-vision-y", action="store_true", default=False)
    p.add_argument("--no-invert-vision-y", dest="invert_vision_y", action="store_false")

    # Sound control
    p.add_argument("--sound-timeout-sec", type=float, default=1.20)
    p.add_argument("--sound-min-yaw-deg", type=float, default=8.0)
    p.add_argument("--sound-retrigger-cooldown-sec", type=float, default=0.70)

    # Timed body turn by sound
    p.add_argument("--turn-rate-deg-per-sec-full-stick", type=float, default=185.0)
    p.add_argument("--body-turn-stick-x", type=float, default=1.0)
    p.add_argument("--body-turn-gain", type=float, default=0.50)
    p.add_argument("--body-turn-min-sec", type=float, default=0.05)
    p.add_argument("--body-turn-max-sec", type=float, default=1.20)
    p.add_argument("--body-turn-yaw-sign", type=float, default=1.0)
    p.add_argument("--yolo-confirm-sec", type=float, default=0.35)

    # YOLO body centering assist
    p.add_argument("--body-yolo-assist-enabled", action="store_true", default=True)
    p.add_argument("--no-body-yolo-assist", dest="body_yolo_assist_enabled", action="store_false")
    p.add_argument("--body-yolo-deadzone-x", type=float, default=0.04)
    p.add_argument("--body-yolo-full-x-norm", type=float, default=0.50)
    p.add_argument("--body-yolo-max-stick-x", type=float, default=0.14)
    p.add_argument("--body-yolo-min-stick-x", type=float, default=0.02)
    p.add_argument("--body-yolo-acquire-sec", type=float, default=0.03)

    # Forward move by box size
    p.add_argument("--forward-by-box-enabled", action="store_true", default=True)
    p.add_argument("--no-forward-by-box", dest="forward_by_box_enabled", action="store_false")
    p.add_argument("--forward-box-area-threshold", type=float, default=0.16)
    p.add_argument("--forward-box-area-zero-at", type=float, default=0.02)
    p.add_argument("--forward-min-stick-y", type=float, default=0.22)
    p.add_argument("--forward-max-stick-y", type=float, default=1.00)

    # Optional: only move forward when target is roughly centered
    p.add_argument("--forward-center-required", action="store_true", default=True)
    p.add_argument("--no-forward-center-required", dest="forward_center_required", action="store_false")
    p.add_argument("--forward-center-max-x", type=float, default=0.35)

    # Vision smoothing
    p.add_argument("--vision-x-smooth", type=float, default=0.22)
    p.add_argument("--vision-y-smooth", type=float, default=0.22)
    p.add_argument("--vision-box-smooth", type=float, default=0.18)
    p.add_argument("--vision-return-smooth", type=float, default=0.10)

    # Stick smoothing
    p.add_argument("--body-stick-smooth", type=float, default=0.22)
    p.add_argument("--body-stick-return-smooth", type=float, default=0.16)
    p.add_argument("--move-stick-smooth", type=float, default=0.18)
    p.add_argument("--move-stick-return-smooth", type=float, default=0.14)

    # Send thresholds
    p.add_argument("--send-threshold-stick", type=float, default=0.01)
    p.add_argument("--send-threshold-head-deg", type=float, default=0.12)

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
    ctrl = PersistentCtrlClient(args.ctrl_host, args.ctrl_port)

    print("========================================")
    print(" Focus Player")
    print(" body : sound timed turn + YOLO centering assist")
    print(" move : forward by YOLO box size")
    print(" head : YOLO face tracking")
    print("========================================")
    print(f"[INFO] vision query : {args.query_host}:{args.query_port}")
    print(f"[INFO] sound query  : {args.sound_host}:{args.sound_port}")
    print(f"[INFO] local ctrl   : {args.ctrl_host}:{args.ctrl_port}")
    print(f"[INFO] interval     : {args.interval:.3f}s")
    print(f"[INFO] turn rate    : {args.turn_rate_deg_per_sec_full_stick:.1f} deg/s @ stick=1")
    print(f"[INFO] turn gain    : {args.body_turn_gain:.2f}")
    print(f"[INFO] yaw sign     : {args.body_turn_yaw_sign:+.1f}")
    print(f"[INFO] yolo assist  : {args.body_yolo_assist_enabled}")
    print(f"[INFO] forward box  : {args.forward_by_box_enabled}")
    print(f"[INFO] box thresh   : {args.forward_box_area_threshold:.3f}")
    print(f"[INFO] box zero-at  : {args.forward_box_area_zero_at:.3f}")
    print("")

    last_print = 0.0
    last_send_time = 0.0
    keepalive_sec = 0.20

    last_sent_r_stick_x: Optional[float] = None
    last_sent_l_stick_y: Optional[float] = None
    last_sent_head_rx: Optional[float] = None
    last_sent_head_ry: Optional[float] = None
    last_sent_head_rz: Optional[float] = None

    current_head_rx = 0.0
    current_head_ry = 0.0
    current_head_rz = 0.0
    last_vision_seen_time = 0.0

    turning_until = 0.0
    turn_stick_x = 0.0
    yolo_confirm_until = 0.0
    last_sound_trigger_time = 0.0
    last_turn_yaw_deg = 0.0
    last_source = "idle"
    vision_acquired_since = 0.0

    smoothed_x_norm = 0.0
    smoothed_y_norm = 0.0
    smoothed_box_area = 0.0
    smoothed_r_stick_x = 0.0
    smoothed_l_stick_y = 0.0

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
                last_vision_seen_time = now
                if vision_acquired_since == 0.0:
                    vision_acquired_since = now
            else:
                vision_acquired_since = 0.0

            # Smooth vision values
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

            # Head control
            if has_vision or (now - last_vision_seen_time) <= args.vision_grace_sec:
                head_rx_t, head_ry_t, head_rz_t = _player_to_head_angles(
                    x_norm=smoothed_x_norm,
                    y_norm=smoothed_y_norm,
                    invert_x=args.invert_vision_x,
                    invert_y=args.invert_vision_y,
                    yaw_gain_deg=args.head_yaw_gain_deg,
                    pitch_gain_deg=args.head_pitch_gain_deg,
                    roll_gain_deg=args.head_roll_gain_deg,
                    yaw_max_deg=args.head_yaw_max_deg,
                    pitch_max_deg=args.head_pitch_max_deg,
                    roll_max_deg=args.head_roll_max_deg,
                    deadzone_x_norm=args.head_deadzone_x_norm,
                    deadzone_y_norm=args.head_deadzone_y_norm,
                )
            else:
                head_rx_t, head_ry_t, head_rz_t = 0.0, 0.0, 0.0

            head_alpha = args.head_smooth if has_vision else args.head_return_smooth

            current_head_rx = _smooth_angle(current_head_rx, head_rx_t, head_alpha)
            current_head_ry = _smooth_angle(current_head_ry, head_ry_t, head_alpha)
            current_head_rz = _smooth_angle(current_head_rz, head_rz_t, head_alpha)

            if abs(current_head_rx) < 0.15:
                current_head_rx = 0.0
            if abs(current_head_ry) < 0.15:
                current_head_ry = 0.0
            if abs(current_head_rz) < 0.15:
                current_head_rz = 0.0

            send_head_rx = round(current_head_rx, 2)
            send_head_ry = round(current_head_ry, 2)
            send_head_rz = round(current_head_rz, 2)

            # Body yaw control
            sound_fresh = sound.ok and ((now - sound.timestamp) <= args.sound_timeout_sec)
            sound_usable = sound_fresh and sound.hold_active and (abs(sound.target_yaw_deg) >= args.sound_min_yaw_deg)

            vision_ready_for_body_assist = (
                has_vision
                and vision_acquired_since > 0.0
                and (now - vision_acquired_since) >= args.body_yolo_acquire_sec
            )

            raw_r_stick_x = 0.0

            if now < turning_until:
                raw_r_stick_x = turn_stick_x
                last_source = "sound_turn"

            elif now < yolo_confirm_until:
                raw_r_stick_x = 0.0
                last_source = "yolo_confirm"

            elif vision_ready_for_body_assist and args.body_yolo_assist_enabled:
                raw_r_stick_x = _x_norm_to_body_stick(
                    x_norm=smoothed_x_norm,
                    deadzone_x=args.body_yolo_deadzone_x,
                    full_x_norm=args.body_yolo_full_x_norm,
                    min_stick_x=args.body_yolo_min_stick_x,
                    max_stick_x=args.body_yolo_max_stick_x,
                )
                last_source = "vision_body_assist"

            elif has_vision:
                raw_r_stick_x = 0.0
                last_source = "vision_head_only"

            elif sound_usable and (now - last_sound_trigger_time) >= args.sound_retrigger_cooldown_sec:
                planned_yaw_deg = sound.target_yaw_deg * args.body_turn_yaw_sign
                stick_x, duration = _sound_yaw_to_turn_plan(
                    yaw_deg=planned_yaw_deg,
                    turn_rate_deg_per_sec_at_full_stick=args.turn_rate_deg_per_sec_full_stick,
                    stick_x_mag=abs(args.body_turn_stick_x),
                    min_turn_sec=args.body_turn_min_sec,
                    max_turn_sec=args.body_turn_max_sec,
                    gain=args.body_turn_gain,
                )

                if duration > 0.0:
                    turn_stick_x = stick_x
                    turning_until = now + duration
                    yolo_confirm_until = turning_until + args.yolo_confirm_sec
                    last_sound_trigger_time = now
                    last_turn_yaw_deg = planned_yaw_deg
                    raw_r_stick_x = turn_stick_x
                    last_source = f"sound_turn_start({duration:.2f}s)"
                else:
                    raw_r_stick_x = 0.0
                    last_source = "sound_small"

            else:
                raw_r_stick_x = 0.0
                last_source = "idle"

            body_stick_alpha = args.body_stick_smooth if abs(raw_r_stick_x) > abs(smoothed_r_stick_x) else args.body_stick_return_smooth
            smoothed_r_stick_x = _smooth_value(smoothed_r_stick_x, raw_r_stick_x, body_stick_alpha)
            if abs(smoothed_r_stick_x) < 0.003:
                smoothed_r_stick_x = 0.0
            send_r_stick_x = round(smoothed_r_stick_x, 3)

            # Forward move by box size
            raw_l_stick_y = 0.0
            if has_vision and args.forward_by_box_enabled:
                can_move = True
                if args.forward_center_required:
                    can_move = abs(smoothed_x_norm) <= args.forward_center_max_x

                if can_move:
                    raw_l_stick_y = _box_area_to_forward_stick(
                        box_area_norm=smoothed_box_area,
                        area_threshold=args.forward_box_area_threshold,
                        min_forward_y=args.forward_min_stick_y,
                        max_forward_y=args.forward_max_stick_y,
                        area_zero_at=args.forward_box_area_zero_at,
                    )

            move_stick_alpha = args.move_stick_smooth if raw_l_stick_y > smoothed_l_stick_y else args.move_stick_return_smooth
            smoothed_l_stick_y = _smooth_value(smoothed_l_stick_y, raw_l_stick_y, move_stick_alpha)
            if abs(smoothed_l_stick_y) < 0.003:
                smoothed_l_stick_y = 0.0
            send_l_stick_y = round(smoothed_l_stick_y, 3)

            # Send controls
            should_send_r_stick = (
                last_sent_r_stick_x is None
                or abs(send_r_stick_x - last_sent_r_stick_x) >= args.send_threshold_stick
                or (now - last_send_time) >= keepalive_sec
            )

            should_send_l_stick = (
                last_sent_l_stick_y is None
                or abs(send_l_stick_y - last_sent_l_stick_y) >= args.send_threshold_stick
                or (now - last_send_time) >= keepalive_sec
            )

            should_send_head = (
                last_sent_head_rx is None
                or last_sent_head_ry is None
                or last_sent_head_rz is None
                or abs(send_head_rx - last_sent_head_rx) >= args.send_threshold_head_deg
                or abs(send_head_ry - last_sent_head_ry) >= args.send_threshold_head_deg
                or abs(send_head_rz - last_sent_head_rz) >= args.send_threshold_head_deg
                or (now - last_send_time) >= keepalive_sec
            )

            if should_send_r_stick:
                if ctrl.set_right_stick(send_r_stick_x, 0.0):
                    last_sent_r_stick_x = send_r_stick_x
                    last_send_time = now

            if should_send_l_stick:
                if ctrl.set_left_stick(0.0, send_l_stick_y):
                    last_sent_l_stick_y = send_l_stick_y
                    last_send_time = now

            if should_send_head:
                ok1 = ctrl.set_head_rx(send_head_rx)
                ok2 = ctrl.set_head_ry(send_head_ry)
                ok3 = ctrl.set_head_rz(send_head_rz)
                if ok1 and ok2 and ok3:
                    last_sent_head_rx = send_head_rx
                    last_sent_head_ry = send_head_ry
                    last_sent_head_rz = send_head_rz
                    last_send_time = now

            if now - last_print >= args.print_every:
                last_print = now
                vx = f"{smoothed_x_norm:+.3f}"
                vy = f"{smoothed_y_norm:+.3f}"
                sound_yaw = f"{sound.target_yaw_deg:+7.2f}" if sound.ok else "  None "
                box_area = f"{smoothed_box_area:.4f}" if has_vision or smoothed_box_area > 0.0 else "None"

                print(
                    f"[FOCUS] src={last_source:<24} "
                    f"vision_ok={has_vision} mode={player.track_mode:<18} "
                    f"x_norm={vx:>7} y_norm={vy:>7} "
                    f"box_area={box_area:>7} "
                    f"sound_yaw={sound_yaw} "
                    f"last_turn_yaw={last_turn_yaw_deg:+7.2f} "
                    f"R=({send_r_stick_x:+6.3f},+0.000) "
                    f"L=(+0.000,{send_l_stick_y:+6.3f}) "
                    f"head=({send_head_rx:+6.2f},{send_head_ry:+6.2f},{send_head_rz:+6.2f})",
                    flush=True,
                )

            sleep_sec = args.interval - (time.perf_counter() - loop_start)
            if sleep_sec > 0:
                time.sleep(sleep_sec)

    finally:
        try:
            ctrl.set_right_stick(0.0, 0.0)
            ctrl.set_left_stick(0.0, 0.0)
            ctrl.set_head_rx(0.0)
            ctrl.set_head_ry(0.0)
            ctrl.set_head_rz(0.0)
        except Exception:
            pass
        ctrl.close()
        print("[INFO] Focus Player stopped", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())