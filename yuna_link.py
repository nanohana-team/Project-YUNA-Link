"""
apps/yuna_link.py
Project YUNA Link - Unified Python Client

YunaLink        -- FramePacket + HMD sender  (\\.\pipe\YunaLinkPose)
YunaInputClient -- Text command sender       (\\.\pipe\YunaLinkInput)

--- YunaLinkPose protocol ---
Header : type(u8) + length(u16)  = 3 bytes
0x01   : HmdPosePacket  = 57 bytes
0x10   : FramePacket    = 118 bytes

--- FramePacket layout (118 bytes, little-endian) ---
  frameId        Q  8
  timestamp      d  8
  leftPose:         30  (3f pos + 4f quat + 2B flags)
  rightPose:        30
  leftInput:        20  (4B buttons + 4f analog)
  rightInput:       20
  startButton    B  1
  menuButton     B  1

--- YunaLinkInput commands ---
  SET START 0|1       SET MENU 0|1
  SET A 0|1           SET B 0|1
  SET X 0|1           SET Y 0|1
  SET RTRIGGER <f>    SET RGRIP <f>
  SET LTRIGGER <f>    SET LGRIP <f>
  SET L_STICK <x> <y> SET R_STICK <x> <y>
  TAP A / TAP B / TAP X / TAP Y / TAP START / TAP MENU
  RESET_INPUT

Usage:
  python apps/yuna_link.py --mode idle
  python apps/yuna_link.py --mode input
  python apps/yuna_link.py --cmd "TAP A"
  python apps/yuna_link.py --cmd "SET RTRIGGER 0.8"
"""

import argparse
import ctypes
import ctypes.wintypes as wt
import math
import struct
import sys
import time
import threading

# =============================================================================
# Win32 Named Pipe
# =============================================================================

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
INVALID_HANDLE_VALUE = wt.HANDLE(-1).value
GENERIC_WRITE        = 0x40000000
OPEN_EXISTING        = 3
FILE_ATTRIBUTE_NORMAL= 0x80
ERROR_PIPE_BUSY      = 231

_k32.CreateFileW.restype  = wt.HANDLE
_k32.CreateFileW.argtypes = [wt.LPCWSTR,wt.DWORD,wt.DWORD,ctypes.c_void_p,wt.DWORD,wt.DWORD,wt.HANDLE]
_k32.WriteFile.restype    = wt.BOOL
_k32.WriteFile.argtypes   = [wt.HANDLE,ctypes.c_void_p,wt.DWORD,ctypes.POINTER(wt.DWORD),ctypes.c_void_p]
_k32.CloseHandle.restype  = wt.BOOL
_k32.CloseHandle.argtypes = [wt.HANDLE]
_k32.WaitNamedPipeW.restype  = wt.BOOL
_k32.WaitNamedPipeW.argtypes = [wt.LPCWSTR,wt.DWORD]
_k32.GetLastError.restype = wt.DWORD


def _pipe_connect(name: str, timeout_sec: float = 20.0):
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        _k32.WaitNamedPipeW(name, 1000)
        h = _k32.CreateFileW(name, GENERIC_WRITE, 0, None,
                             OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
        if h != INVALID_HANDLE_VALUE:
            return h
        if _k32.GetLastError() != ERROR_PIPE_BUSY:
            time.sleep(0.3)
    raise TimeoutError(f"Could not connect to {name}")


def _pipe_write(handle, data: bytes):
    buf     = (ctypes.c_char * len(data)).from_buffer_copy(data)
    written = wt.DWORD(0)
    if not _k32.WriteFile(handle, buf, len(data), ctypes.byref(written), None):
        raise OSError(f"WriteFile failed err={_k32.GetLastError()}")


# =============================================================================
# Protocol constants  (must match src/protocol.h)
# =============================================================================

PKT_HMD_POSE = 0x01
PKT_FRAME    = 0x10

POSE_PIPE  = r"\\.\pipe\YunaLinkPose"
INPUT_PIPE = r"\\.\pipe\YunaLinkInput"

# HmdPosePacket: device(B) + 7*double = 1+56 = 57 bytes
_HMD_FMT = "<B7d"
assert struct.calcsize(_HMD_FMT) == 57

# FramePacket: 118 bytes
# Q d  (frameId + timestamp)           = 16
# 7fBB (pose x2)                        = 30 x 2 = 60
# BBBBffff (input x2)                   = 20 x 2 = 40
# BB (startButton + menuButton)          = 2
# total = 16 + 60 + 40 + 2 = 118
_FRAME_FMT = "<Qd" + "7fBB" + "7fBB" + "BBBBffff" + "BBBBffff" + "BB"
assert struct.calcsize(_FRAME_FMT) == 118, f"FramePacket size={struct.calcsize(_FRAME_FMT)}"


def _hdr(pkt_type: int, body_len: int) -> bytes:
    return struct.pack("<BH", pkt_type, body_len)


def euler_to_quat(yaw=0., pitch=0., roll=0.):
    y=math.radians(yaw)/2; p=math.radians(pitch)/2; r=math.radians(roll)/2
    cy,sy=math.cos(y),math.sin(y); cp,sp=math.cos(p),math.sin(p); cr,sr=math.cos(r),math.sin(r)
    return (cr*cp*cy+sr*sp*sy, sr*cp*cy-cr*sp*sy, cr*sp*cy+sr*cp*sy, cr*cp*sy-sr*sp*cy)


def clamp(v, lo=-1., hi=1.):
    return max(lo, min(hi, v))


def clamp01(v):
    return max(0., min(1., v))


# =============================================================================
# YunaLink  (YunaLinkPose)
# =============================================================================

class YunaLink:
    """
    Send HMD pose and FramePackets (controller pose + input) to the driver.

    Example:
        with YunaLink() as yl:
            while True:
                yl.send_hmd(0, 1.6, 0)
                yl.send_frame(
                    right_input=dict(a_button=True, trigger=0.8))
                time.sleep(1/60)
    """

    def __init__(self):
        self._handle   = INVALID_HANDLE_VALUE
        self._frame_id = 0
        self._lock     = threading.Lock()

    def connect(self, timeout_sec=20.0):
        print(f"[YUNA] Connecting to {POSE_PIPE} ...", end=" ", flush=True)
        self._handle = _pipe_connect(POSE_PIPE, timeout_sec)
        print("OK")
        return self

    def disconnect(self):
        if self._handle != INVALID_HANDLE_VALUE:
            _k32.CloseHandle(self._handle)
            self._handle = INVALID_HANDLE_VALUE

    def __enter__(self):  return self.connect()
    def __exit__(self, *_): self.disconnect()

    def send_hmd(self, x=0., y=1.6, z=0., yaw=0., pitch=0., roll=0.):
        qw,qx,qy,qz = euler_to_quat(yaw, pitch, roll)
        body   = struct.pack(_HMD_FMT, 0, x, y, z, qw, qx, qy, qz)
        packet = _hdr(PKT_HMD_POSE, len(body)) + body
        with self._lock:
            _pipe_write(self._handle, packet)

    def send_frame(self,
                   # Controller poses
                   left_pos=(-.25,1.1,-.1),  left_rot=(0.,0.,0.),
                   right_pos=(.25,1.1,-.1),  right_rot=(0.,0.,0.),
                   left_tracking=True,  left_connected=True,
                   right_tracking=True, right_connected=True,
                   # Left input
                   left_input: dict = None,
                   # Right input
                   right_input: dict = None,
                   # Global
                   start_button=False,
                   menu_button=False):
        """
        left_input / right_input keys:
          a_button, b_button, x_button, y_button  (bool)
          trigger, grip                             (float 0~1)
          stick_x, stick_y                          (float -1~1)
        """
        li = left_input  or {}
        ri = right_input or {}

        self._frame_id += 1
        ts = time.monotonic()

        lqw,lqx,lqy,lqz = euler_to_quat(*left_rot)
        rqw,rqx,rqy,rqz = euler_to_quat(*right_rot)

        body = struct.pack(_FRAME_FMT,
            self._frame_id, ts,
            # left pose: px py pz qx qy qz qw track conn
            left_pos[0],  left_pos[1],  left_pos[2],
            lqx, lqy, lqz, lqw,
            1 if left_tracking  else 0,
            1 if left_connected else 0,
            # right pose
            right_pos[0], right_pos[1], right_pos[2],
            rqx, rqy, rqz, rqw,
            1 if right_tracking  else 0,
            1 if right_connected else 0,
            # left input: a b x y trigger grip stickX stickY
            1 if li.get("a_button", False) else 0,
            1 if li.get("b_button", False) else 0,
            1 if li.get("x_button", False) else 0,
            1 if li.get("y_button", False) else 0,
            clamp01(li.get("trigger", 0.)),
            clamp01(li.get("grip",    0.)),
            clamp(li.get("stick_x",  0.)),
            clamp(li.get("stick_y",  0.)),
            # right input
            1 if ri.get("a_button", False) else 0,
            1 if ri.get("b_button", False) else 0,
            1 if ri.get("x_button", False) else 0,
            1 if ri.get("y_button", False) else 0,
            clamp01(ri.get("trigger", 0.)),
            clamp01(ri.get("grip",    0.)),
            clamp(ri.get("stick_x",  0.)),
            clamp(ri.get("stick_y",  0.)),
            # global
            1 if start_button else 0,
            1 if menu_button  else 0,
        )
        packet = _hdr(PKT_FRAME, len(body)) + body
        with self._lock:
            _pipe_write(self._handle, packet)


# =============================================================================
# YunaInputClient  (YunaLinkInput)
# =============================================================================

class YunaInputClient:
    """
    Send text commands to the InputServer.

    Example:
        with YunaInputClient() as inp:
            inp.tap_a()
            inp.set_rtrigger(0.8)
            inp.set_left_stick(0, 1)
    """

    def __init__(self):
        self._handle = INVALID_HANDLE_VALUE
        self._lock   = threading.Lock()

    def connect(self, timeout_sec=20.0):
        print(f"[YUNA Input] Connecting to {INPUT_PIPE} ...", end=" ", flush=True)
        self._handle = _pipe_connect(INPUT_PIPE, timeout_sec)
        print("OK")
        return self

    def disconnect(self):
        if self._handle != INVALID_HANDLE_VALUE:
            _k32.CloseHandle(self._handle)
            self._handle = INVALID_HANDLE_VALUE

    def __enter__(self):  return self.connect()
    def __exit__(self, *_): self.disconnect()

    def send(self, cmd: str):
        data = (cmd.strip() + "\n").encode("ascii")
        with self._lock:
            _pipe_write(self._handle, data)

    # Boolean buttons
    def set_start(self, v: bool):   self.send(f"SET START {1 if v else 0}")
    def set_menu(self, v: bool):    self.send(f"SET MENU {1 if v else 0}")
    def set_a(self, v: bool):       self.send(f"SET A {1 if v else 0}")
    def set_b(self, v: bool):       self.send(f"SET B {1 if v else 0}")
    def set_x(self, v: bool):       self.send(f"SET X {1 if v else 0}")
    def set_y(self, v: bool):       self.send(f"SET Y {1 if v else 0}")

    # Analog (0~1)
    def set_rtrigger(self, v: float): self.send(f"SET RTRIGGER {v:.4f}")
    def set_rgrip(self, v: float):    self.send(f"SET RGRIP {v:.4f}")
    def set_ltrigger(self, v: float): self.send(f"SET LTRIGGER {v:.4f}")
    def set_lgrip(self, v: float):    self.send(f"SET LGRIP {v:.4f}")

    # Sticks (-1~1)
    def set_left_stick(self, x: float, y: float):  self.send(f"SET L_STICK {x:.4f} {y:.4f}")
    def set_right_stick(self, x: float, y: float): self.send(f"SET R_STICK {x:.4f} {y:.4f}")

    # Reset
    def reset(self): self.send("RESET_INPUT")

    # Taps
    def tap_a(self):     self.send("TAP A")
    def tap_b(self):     self.send("TAP B")
    def tap_x(self):     self.send("TAP X")
    def tap_y(self):     self.send("TAP Y")
    def tap_start(self): self.send("TAP START")
    def tap_menu(self):  self.send("TAP MENU")

    # ------------------------------------------------------------------
    # Pose commands
    # target: "HEAD" | "L_CONTROLLER" | "R_CONTROLLER"
    # ------------------------------------------------------------------

    def move(self, target: str, axis: str, delta: float):
        """Relative position move.  axis: 'x'|'y'|'z'"""
        self.send(f"MOVE {target} {axis} {delta:.6f}")

    def rotate(self, target: str, axis: str, delta_deg: float):
        """Relative rotation.  axis: 'x'|'y'|'z'  (or 'rX'|'rY'|'rZ')"""
        self.send(f"ROTATE {target} {axis} {delta_deg:.4f}")

    def set_pos(self, target: str, axis: str, value: float):
        """Absolute position set.  axis: 'x'|'y'|'z'"""
        self.send(f"SET {target} {axis} {value:.6f}")

    def set_rot(self, target: str, axis: str, deg: float):
        """Absolute rotation set (degrees).  axis: 'rX'|'rY'|'rZ'"""
        self.send(f"SET {target} {axis} {deg:.4f}")

    def reset_pose(self, target: str):
        """Deactivate pose override for target."""
        self.send(f"RESET_POSE {target}")


# =============================================================================
# Built-in modes
# =============================================================================

def mode_idle():
    print("[YUNA] Idle mode - Ctrl+C to stop")
    with YunaLink() as yl:
        t = 0.; dt = 1./60.
        try:
            while True:
                breath = math.sin(t*0.4)*0.004
                yl.send_hmd(0, 1.6+breath, 0)
                yl.send_frame(
                    left_pos=(-0.25, 1.1+breath*0.5, -0.1),
                    right_pos=(0.25, 1.1+breath*0.5, -0.1))
                t += dt; time.sleep(dt)
        except KeyboardInterrupt:
            print("\n[YUNA] Stopped.")


_HELP = """
--- Pose commands ---
  MOVE   HEAD|L_CONTROLLER|R_CONTROLLER  x|y|z  <delta>
    e.g. MOVE HEAD y 0.1        (move head up 10cm)
         MOVE L_CONTROLLER x -0.05

  ROTATE HEAD|L_CONTROLLER|R_CONTROLLER  x|y|z  <delta_deg>
    e.g. ROTATE HEAD Y 90       (turn head 90 deg around Y)
         ROTATE R_CONTROLLER Z 45

  SET    HEAD|L_CONTROLLER|R_CONTROLLER  x|y|z  <value>     (absolute position)
  SET    HEAD|L_CONTROLLER|R_CONTROLLER  rX|rY|rZ <deg>     (absolute rotation)
    e.g. SET HEAD y 1.6
         SET HEAD rY 0

  RESET_POSE HEAD|L_CONTROLLER|R_CONTROLLER

--- Input commands ---
  SET START|MENU|A|B|X|Y 0|1
  SET RTRIGGER|RGRIP|LTRIGGER|LGRIP <f>
  SET L_STICK <x> <y>    SET R_STICK <x> <y>
  TAP A|B|X|Y|START|MENU
  RESET_INPUT

  help / quit
"""


def mode_input_repl():
    print("[YUNA Input] Interactive mode. Type 'help' for commands.")
    with YunaInputClient() as inp:
        while True:
            try:
                line = input("yuna> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line: continue
            if line.lower() in ("quit","exit","q"): break
            if line.lower() == "help": print(_HELP); continue
            try:
                inp.send(line); print(f"  -> {line}")
            except OSError as e:
                print(f"  [ERROR] {e}"); break
        inp.reset()
    print("[YUNA Input] Disconnected.")


def mode_single_cmd(cmd: str):
    with YunaInputClient() as inp:
        inp.send(cmd)
        print(f"[YUNA Input] Sent: {cmd}")


# =============================================================================
# Entry point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="YUNA Link - Unified Python Client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--mode", choices=["idle","input"],
                     help="idle: HMD+controller loop | input: interactive REPL")
    grp.add_argument("--cmd", metavar="CMD",
                     help="Send one input command and exit")
    args = parser.parse_args()

    if args.cmd:
        mode_single_cmd(args.cmd)
    elif args.mode == "idle":
        mode_idle()
    elif args.mode == "input":
        mode_input_repl()


if __name__ == "__main__":
    main()
