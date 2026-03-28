"""
apps/yuna_link.py
Project YUNA Link - Unified Python Client

Provides:
  YunaLink         - FramePacket + HMD pose sender (Named Pipe: YunaLinkPose)
  YunaInputClient  - Text command sender            (Named Pipe: YunaLinkInput)

Usage examples:
  # Continuous idle loop (HMD + controllers)
  python apps/yuna_link.py --mode idle

  # Send one input command
  python apps/yuna_link.py --cmd "TAP A"
  python apps/yuna_link.py --cmd "SET L_STICK 0.0 1.0"

  # Interactive REPL for input commands
  python apps/yuna_link.py --mode input

Protocol (YunaLinkPose):
  Header : type(u8) + length(u16)  = 3 bytes
  0x01   : HmdPosePacket            = 57 bytes   (device u8 + 7 double)
  0x10   : FramePacket              = 93 bytes   (see struct below)

Protocol (YunaLinkInput):
  Newline-delimited ASCII commands:
    SET START 0|1
    SET A 0|1
    SET L_STICK <x> <y>
    SET R_STICK <x> <y>
    RESET_INPUT
    TAP A
    TAP START
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
# Win32 Named Pipe helpers
# =============================================================================

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
INVALID_HANDLE_VALUE = wt.HANDLE(-1).value
GENERIC_WRITE        = 0x40000000
OPEN_EXISTING        = 3
FILE_ATTRIBUTE_NORMAL= 0x80
ERROR_PIPE_BUSY      = 231

_k32.CreateFileW.restype  = wt.HANDLE
_k32.CreateFileW.argtypes = [
    wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
    wt.DWORD, wt.DWORD, wt.HANDLE]
_k32.WriteFile.restype  = wt.BOOL
_k32.WriteFile.argtypes = [
    wt.HANDLE, ctypes.c_void_p, wt.DWORD,
    ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
_k32.CloseHandle.restype  = wt.BOOL
_k32.CloseHandle.argtypes = [wt.HANDLE]
_k32.WaitNamedPipeW.restype  = wt.BOOL
_k32.WaitNamedPipeW.argtypes = [wt.LPCWSTR, wt.DWORD]
_k32.GetLastError.restype  = wt.DWORD


def _pipe_connect(name: str, timeout_sec: float = 20.0):
    """Open a named pipe for writing. Returns handle or raises."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        _k32.WaitNamedPipeW(name, 1000)
        h = _k32.CreateFileW(name, GENERIC_WRITE, 0, None,
                             OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
        if h != INVALID_HANDLE_VALUE:
            return h
        err = _k32.GetLastError()
        if err != ERROR_PIPE_BUSY:
            time.sleep(0.3)
    raise TimeoutError(f"Could not connect to {name}")


def _pipe_write(handle, data: bytes):
    buf     = (ctypes.c_char * len(data)).from_buffer_copy(data)
    written = wt.DWORD(0)
    ok = _k32.WriteFile(handle, buf, len(data), ctypes.byref(written), None)
    if not ok:
        raise OSError(f"WriteFile failed err={_k32.GetLastError()}")


# =============================================================================
# Protocol constants  (must match src/protocol.h)
# =============================================================================

PKT_HMD_POSE = 0x01
PKT_FRAME    = 0x10

POSE_PIPE  = r"\\.\pipe\YunaLinkPose"
INPUT_PIPE = r"\\.\pipe\YunaLinkInput"

# HmdPosePacket: device(u8) + px,py,pz(d) + qw,qx,qy,qz(d) = 57 bytes
_HMD_FMT = "<B7d"
assert struct.calcsize(_HMD_FMT) == 57

# FramePacket: 93 bytes (see protocol.h)
# frameId(Q) timestamp(d)
# leftPose:  px,py,pz(3f) qx,qy,qz,qw(4f) trackingValid(B) connected(B)
# rightPose: same as leftPose
# leftInput: aButton(B) stickX(f) stickY(f)
# rightInput: same
# startButton(B)
_FRAME_FMT = "<Qd" + "7fBB" + "7fBB" + "Bff" + "Bff" + "B"
assert struct.calcsize(_FRAME_FMT) == 95


def _pack_header(pkt_type: int, body_len: int) -> bytes:
    return struct.pack("<BH", pkt_type, body_len)


# =============================================================================
# Quaternion helpers
# =============================================================================

def euler_to_quat(yaw_deg=0., pitch_deg=0., roll_deg=0.):
    y = math.radians(yaw_deg)   / 2
    p = math.radians(pitch_deg) / 2
    r = math.radians(roll_deg)  / 2
    cy, sy = math.cos(y), math.sin(y)
    cp, sp = math.cos(p), math.sin(p)
    cr, sr = math.cos(r), math.sin(r)
    return (
        cr*cp*cy + sr*sp*sy,  # qw
        sr*cp*cy - cr*sp*sy,  # qx
        cr*sp*cy + sr*cp*sy,  # qy
        cr*cp*sy - sr*sp*cy,  # qz
    )


def clamp(v, lo=-1., hi=1.):
    return max(lo, min(hi, v))


# =============================================================================
# YunaLink  (YunaLinkPose writer)
# =============================================================================

class YunaLink:
    """
    Send HMD pose and FramePackets to the driver.

    Typical usage:
        with YunaLink() as yl:
            while True:
                yl.send_hmd(0, 1.6, 0)
                yl.send_frame(
                    left_pos=(-0.25, 1.1, -0.1), right_pos=(0.25, 1.1, -0.1))
                time.sleep(1/60)
    """

    def __init__(self):
        self._handle    = INVALID_HANDLE_VALUE
        self._frame_id  = 0
        self._lock      = threading.Lock()

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

    # ------------------------------------------------------------------
    # HMD pose  (PKT_HMD_POSE = 0x01)
    # ------------------------------------------------------------------
    def send_hmd(self, x=0., y=1.6, z=0.,
                 yaw=0., pitch=0., roll=0.):
        qw, qx, qy, qz = euler_to_quat(yaw, pitch, roll)
        body   = struct.pack(_HMD_FMT, 0, x, y, z, qw, qx, qy, qz)
        packet = _pack_header(PKT_HMD_POSE, len(body)) + body
        with self._lock:
            _pipe_write(self._handle, packet)

    # ------------------------------------------------------------------
    # Frame packet  (PKT_FRAME = 0x10)
    # Carries both controller poses AND input state.
    # ------------------------------------------------------------------
    def send_frame(self,
                   left_pos=(-.25, 1.1, -.1), left_rot=(0., 0., 0.),
                   right_pos=(.25, 1.1, -.1), right_rot=(0., 0., 0.),
                   left_tracking=True,  left_connected=True,
                   right_tracking=True, right_connected=True,
                   a_button=False,
                   left_stick=(0., 0.), right_stick=(0., 0.),
                   start_button=False):

        self._frame_id += 1
        ts = time.monotonic()

        lqw, lqx, lqy, lqz = euler_to_quat(*left_rot)
        rqw, rqx, rqy, rqz = euler_to_quat(*right_rot)

        lsx = clamp(left_stick[0]);  lsy = clamp(left_stick[1])
        rsx = clamp(right_stick[0]); rsy = clamp(right_stick[1])

        body = struct.pack(_FRAME_FMT,
            self._frame_id, ts,
            # left pose
            left_pos[0], left_pos[1], left_pos[2],
            lqx, lqy, lqz, lqw,
            1 if left_tracking  else 0,
            1 if left_connected else 0,
            # right pose
            right_pos[0], right_pos[1], right_pos[2],
            rqx, rqy, rqz, rqw,
            1 if right_tracking  else 0,
            1 if right_connected else 0,
            # left input
            0, lsx, lsy,
            # right input
            1 if a_button else 0, rsx, rsy,
            # global
            1 if start_button else 0,
        )
        packet = _pack_header(PKT_FRAME, len(body)) + body
        with self._lock:
            _pipe_write(self._handle, packet)


# =============================================================================
# YunaInputClient  (YunaLinkInput text command writer)
# =============================================================================

class YunaInputClient:
    """
    Send text commands to the InputServer.

    Can be used standalone or alongside YunaLink.
    Example:
        with YunaInputClient() as inp:
            inp.tap_a()
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

    def set_start(self, pressed: bool):   self.send(f"SET START {1 if pressed else 0}")
    def set_a(self, pressed: bool):       self.send(f"SET A {1 if pressed else 0}")
    def set_left_stick(self, x, y):       self.send(f"SET L_STICK {x:.4f} {y:.4f}")
    def set_right_stick(self, x, y):      self.send(f"SET R_STICK {x:.4f} {y:.4f}")
    def reset(self):                      self.send("RESET_INPUT")
    def tap_a(self):                      self.send("TAP A")
    def tap_start(self):                  self.send("TAP START")


# =============================================================================
# Built-in modes
# =============================================================================

def mode_idle():
    """60Hz idle loop: breathing HMD + stable controller poses."""
    print("[YUNA] Idle mode - Ctrl+C to stop")
    with YunaLink() as yl:
        t = 0.
        dt = 1. / 60.
        try:
            while True:
                breath = math.sin(t * 0.4) * 0.004
                yl.send_hmd(0, 1.6 + breath, 0)
                yl.send_frame(
                    left_pos=(-0.25, 1.1 + breath*0.5, -0.1),
                    right_pos=(0.25, 1.1 + breath*0.5, -0.1))
                t  += dt
                time.sleep(dt)
        except KeyboardInterrupt:
            print("\n[YUNA] Stopped.")


def mode_input_repl():
    """Interactive command REPL for the InputServer."""
    HELP = """
Commands:
  SET START 0|1         set start button
  SET A 0|1             set A button (right hand)
  SET L_STICK <x> <y>   left stick  -1.0 to 1.0
  SET R_STICK <x> <y>   right stick -1.0 to 1.0
  RESET_INPUT           clear all inputs
  TAP A                 momentary A press
  TAP START             momentary Start press
  help / quit
"""
    print("[YUNA Input] Interactive mode.")
    with YunaInputClient() as inp:
        while True:
            try:
                line = input("yuna> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line: continue
            if line.lower() in ("quit", "exit", "q"): break
            if line.lower() == "help": print(HELP); continue
            try:
                inp.send(line)
                print(f"  -> {line}")
            except OSError as e:
                print(f"  [ERROR] {e}"); break
        inp.reset()
    print("[YUNA Input] Disconnected.")


def mode_single_cmd(cmd: str):
    """Send one command to InputServer and exit."""
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
    grp.add_argument("--mode",
                     choices=["idle", "input"],
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
