import argparse
import math
import os
import sys
import time
from dataclasses import dataclass
from enum import Enum, auto


# ------------------------------------------------------------
# import path
# ------------------------------------------------------------
PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from apps.pose_sender import YunaPoseSender  # noqa: E402


# ------------------------------------------------------------
# math helpers
# ------------------------------------------------------------
def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def lerp(current: float, target: float, alpha: float) -> float:
    return current + (target - current) * alpha


def exp_alpha(speed: float, dt: float) -> float:
    """
    dt に依存して破綻しにくい補間係数。
    speed が大きいほど早く追従する。
    """
    if speed <= 0.0:
        return 1.0
    return 1.0 - math.exp(-speed * dt)


# ------------------------------------------------------------
# pose containers
# ------------------------------------------------------------
@dataclass
class HeadPose:
    x: float
    y: float
    z: float
    xrot_deg: float


@dataclass
class HandPose:
    x: float
    y: float
    z: float
    yaw_deg: float
    pitch_deg: float
    roll_deg: float


class MotionState(Enum):
    IDLE = auto()
    GREET = auto()
    LISTEN = auto()
    SPEAK = auto()


# ------------------------------------------------------------
# motion controller
# ------------------------------------------------------------
class MotionDemoController:
    def __init__(self, fps: float = 60.0):
        self.fps = fps
        self.dt = 1.0 / fps

        # --- current smoothed poses ---
        self.head = HeadPose(0.0, 1.60, 0.00, 0.0)
        self.left_hand = HandPose(-0.23, 1.10, -0.10, 0.0, -10.0, 0.0)
        self.right_hand = HandPose(0.23, 1.10, -0.10, 0.0, -10.0, 0.0)

        # --- base / neutral poses ---
        self.head_base = HeadPose(0.0, 1.60, 0.00, 0.0)
        self.left_idle = HandPose(-0.23, 1.10, -0.10, 0.0, -10.0, 8.0)
        self.right_idle = HandPose(0.23, 1.10, -0.10, 0.0, -10.0, -8.0)

        self.state = MotionState.IDLE
        self.state_elapsed = 0.0

        # state durations
        self.state_durations = {
            MotionState.IDLE: 4.0,
            MotionState.GREET: 2.2,
            MotionState.LISTEN: 4.0,
            MotionState.SPEAK: 5.0,
        }

        # smoothing speeds
        self.head_pos_speed = 8.0
        self.head_rot_speed = 10.0
        self.hand_pos_speed = 10.0
        self.hand_rot_speed = 12.0

    def update(self, sender: YunaPoseSender) -> None:
        target_head, target_left, target_right = self._build_targets(
            self.state, self.state_elapsed
        )

        self._smooth_to_targets(target_head, target_left, target_right)
        self._send_current_pose(sender)

        self.state_elapsed += self.dt
        duration = self.state_durations[self.state]
        if self.state_elapsed >= duration:
            self._advance_state()

    def _advance_state(self) -> None:
        if self.state == MotionState.IDLE:
            self.state = MotionState.GREET
        elif self.state == MotionState.GREET:
            self.state = MotionState.LISTEN
        elif self.state == MotionState.LISTEN:
            self.state = MotionState.SPEAK
        else:
            self.state = MotionState.IDLE

        self.state_elapsed = 0.0
        print(f"[STATE] -> {self.state.name}")

    def _build_targets(
        self, state: MotionState, t: float
    ) -> tuple[HeadPose, HandPose, HandPose]:
        """
        頭は xyz + xrot のみ。
        左手は基本待機。
        右手は greet 時に wave。
        """

        # ----------------------------
        # common micro motion
        # ----------------------------
        breath = math.sin(t * 1.4) * 0.003
        tiny_nod = math.sin(t * 1.9) * 0.4

        # neutral defaults
        head = HeadPose(
            x=self.head_base.x,
            y=self.head_base.y + breath,
            z=self.head_base.z,
            xrot_deg=tiny_nod,
        )
        left = HandPose(**vars(self.left_idle))
        right = HandPose(**vars(self.right_idle))

        # ----------------------------
        # IDLE
        # ----------------------------
        if state == MotionState.IDLE:
            # わずかに呼吸してる感じだけ
            head.y += math.sin(t * 0.9) * 0.002
            head.z += math.sin(t * 0.5) * 0.002
            head.xrot_deg += math.sin(t * 0.8) * 0.6

        # ----------------------------
        # GREET
        # ----------------------------
        elif state == MotionState.GREET:
            # 頭を少し起こして反応
            head.y += 0.006 + math.sin(t * 3.0) * 0.0015
            head.z += -0.010
            head.xrot_deg += -2.0 + math.sin(t * 5.0) * 0.8

            # 右手を上げる
            raise_blend = clamp(t / 0.35, 0.0, 1.0)
            base_wave_x = lerp(self.right_idle.x, 0.30, raise_blend)
            base_wave_y = lerp(self.right_idle.y, 1.28, raise_blend)
            base_wave_z = lerp(self.right_idle.z, -0.03, raise_blend)
            base_wave_yaw = lerp(self.right_idle.yaw_deg, 10.0, raise_blend)
            base_wave_pitch = lerp(self.right_idle.pitch_deg, -30.0, raise_blend)
            base_wave_roll = lerp(self.right_idle.roll_deg, -25.0, raise_blend)

            # 振り動作
            if t >= 0.35:
                wt = t - 0.35
                swing = math.sin(wt * 10.5)  # 2~3 回くらい振れる
                right.x = base_wave_x + swing * 0.020
                right.y = base_wave_y + math.sin(wt * 21.0) * 0.006
                right.z = base_wave_z
                right.yaw_deg = base_wave_yaw + swing * 10.0
                right.pitch_deg = base_wave_pitch + math.sin(wt * 10.5) * 4.0
                right.roll_deg = base_wave_roll + swing * 20.0
            else:
                right.x = base_wave_x
                right.y = base_wave_y
                right.z = base_wave_z
                right.yaw_deg = base_wave_yaw
                right.pitch_deg = base_wave_pitch
                right.roll_deg = base_wave_roll

        # ----------------------------
        # LISTEN
        # ----------------------------
        elif state == MotionState.LISTEN:
            # 少し前のめり + 小さくうなずく感じ
            head.y += -0.004 + math.sin(t * 0.8) * 0.001
            head.z += -0.020
            head.xrot_deg += 5.5 + math.sin(t * 2.6) * 1.4

            # 手は静かめ
            left.y += -0.005
            right.y += -0.005
            left.pitch_deg = -16.0
            right.pitch_deg = -16.0

        # ----------------------------
        # SPEAK
        # ----------------------------
        elif state == MotionState.SPEAK:
            # 少し前に出つつ、軽くリズム
            beat = math.sin(t * 3.4)
            head.y += beat * 0.004
            head.z += -0.012 + math.sin(t * 1.7) * 0.002
            head.xrot_deg += 2.0 + beat * 1.8

            # 左手はほぼ待機
            left.y += math.sin(t * 2.0) * 0.003
            left.pitch_deg = -12.0

            # 右手は軽い会話ジェスチャ
            right.x = self.right_idle.x + 0.03 + math.sin(t * 2.2) * 0.018
            right.y = self.right_idle.y + 0.05 + math.sin(t * 3.4) * 0.010
            right.z = self.right_idle.z + 0.03 + math.sin(t * 2.7) * 0.010
            right.yaw_deg = 8.0 + math.sin(t * 2.2) * 6.0
            right.pitch_deg = -22.0 + math.sin(t * 3.4) * 7.0
            right.roll_deg = -15.0 + math.sin(t * 2.7) * 10.0

        return head, left, right

    def _smooth_to_targets(
        self, target_head: HeadPose, target_left: HandPose, target_right: HandPose
    ) -> None:
        hp_alpha = exp_alpha(self.head_pos_speed, self.dt)
        hr_alpha = exp_alpha(self.head_rot_speed, self.dt)
        pp_alpha = exp_alpha(self.hand_pos_speed, self.dt)
        pr_alpha = exp_alpha(self.hand_rot_speed, self.dt)

        # head
        self.head.x = lerp(self.head.x, target_head.x, hp_alpha)
        self.head.y = lerp(self.head.y, target_head.y, hp_alpha)
        self.head.z = lerp(self.head.z, target_head.z, hp_alpha)
        self.head.xrot_deg = lerp(self.head.xrot_deg, target_head.xrot_deg, hr_alpha)

        # left hand
        self.left_hand.x = lerp(self.left_hand.x, target_left.x, pp_alpha)
        self.left_hand.y = lerp(self.left_hand.y, target_left.y, pp_alpha)
        self.left_hand.z = lerp(self.left_hand.z, target_left.z, pp_alpha)
        self.left_hand.yaw_deg = lerp(self.left_hand.yaw_deg, target_left.yaw_deg, pr_alpha)
        self.left_hand.pitch_deg = lerp(self.left_hand.pitch_deg, target_left.pitch_deg, pr_alpha)
        self.left_hand.roll_deg = lerp(self.left_hand.roll_deg, target_left.roll_deg, pr_alpha)

        # right hand
        self.right_hand.x = lerp(self.right_hand.x, target_right.x, pp_alpha)
        self.right_hand.y = lerp(self.right_hand.y, target_right.y, pp_alpha)
        self.right_hand.z = lerp(self.right_hand.z, target_right.z, pp_alpha)
        self.right_hand.yaw_deg = lerp(self.right_hand.yaw_deg, target_right.yaw_deg, pr_alpha)
        self.right_hand.pitch_deg = lerp(self.right_hand.pitch_deg, target_right.pitch_deg, pr_alpha)
        self.right_hand.roll_deg = lerp(self.right_hand.roll_deg, target_right.roll_deg, pr_alpha)

    def _send_current_pose(self, sender: YunaPoseSender) -> None:
        # pose_sender の euler 引数は yaw, pitch, roll 順。
        # 今回の「頭 xrot」は pitch に載せる。
        sender.send_hmd(
            self.head.x,
            self.head.y,
            self.head.z,
            yaw=0.0,
            pitch=self.head.xrot_deg,
            roll=0.0,
        )

        sender.send_left_hand(
            self.left_hand.x,
            self.left_hand.y,
            self.left_hand.z,
            yaw=self.left_hand.yaw_deg,
            pitch=self.left_hand.pitch_deg,
            roll=self.left_hand.roll_deg,
        )

        sender.send_right_hand(
            self.right_hand.x,
            self.right_hand.y,
            self.right_hand.z,
            yaw=self.right_hand.yaw_deg,
            pitch=self.right_hand.pitch_deg,
            roll=self.right_hand.roll_deg,
        )

        # 入力はニュートラルで送り続ける
        sender.send_left_input()
        sender.send_right_input()


# ------------------------------------------------------------
# main loop
# ------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="YUNA Link motion demo: head xyz+xrot + right hand wave"
    )
    parser.add_argument("--fps", type=float, default=60.0)
    parser.add_argument("--mode", choices=["demo", "idle"], default="demo")
    parser.add_argument("--connect-timeout", type=float, default=20.0)
    args = parser.parse_args()

    controller = MotionDemoController(fps=args.fps)

    if args.mode == "idle":
        controller.state = MotionState.IDLE
        controller.state_durations[MotionState.IDLE] = 999999.0

    with YunaPoseSender() as sender:
        if not sender.connect(timeout_sec=args.connect_timeout):
            return 1

        print("[INFO] motion_head_wave start (Ctrl+C to stop)")
        print(f"[INFO] mode={args.mode} fps={args.fps:.1f}")
        print("[STATE] ->", controller.state.name)

        try:
            while True:
                frame_start = time.perf_counter()

                controller.update(sender)

                elapsed = time.perf_counter() - frame_start
                sleep_sec = controller.dt - elapsed
                if sleep_sec > 0:
                    time.sleep(sleep_sec)

        except KeyboardInterrupt:
            print("\n[INFO] stopped")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())