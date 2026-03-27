"""
apps/pose_sender.py
Project YUNA Link - SteamVR driver pose sender

Named Pipe write uses Win32 API (ctypes) instead of Python file I/O.
Python's open() + write() is unreliable for Named Pipes on Windows.

Usage:
    python apps/pose_sender.py           # idle loop
    python apps/pose_sender.py --mode test  # connection test only
"""

import argparse
import struct
import time
import math
import sys
import ctypes
import ctypes.wintypes as wt

# =============================================================================
# Win32 API via ctypes
# =============================================================================

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

INVALID_HANDLE_VALUE = wt.HANDLE(-1).value
GENERIC_WRITE        = 0x40000000
OPEN_EXISTING        = 3
FILE_ATTRIBUTE_NORMAL= 0x80
ERROR_PIPE_BUSY      = 231

_k32.CreateFileW.restype  = wt.HANDLE
_k32.CreateFileW.argtypes = [
    wt.LPCWSTR, wt.DWORD, wt.DWORD,
    ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE
]
_k32.WriteFile.restype  = wt.BOOL
_k32.WriteFile.argtypes = [
    wt.HANDLE, ctypes.c_void_p, wt.DWORD,
    ctypes.POINTER(wt.DWORD), ctypes.c_void_p
]
_k32.CloseHandle.restype  = wt.BOOL
_k32.CloseHandle.argtypes = [wt.HANDLE]
_k32.WaitNamedPipeW.restype  = wt.BOOL
_k32.WaitNamedPipeW.argtypes = [wt.LPCWSTR, wt.DWORD]
_k32.GetLastError.restype  = wt.DWORD
_k32.GetLastError.argtypes = []

# =============================================================================
# Protocol constants  (must match src/pose_server.h exactly)
# =============================================================================

PIPE_NAME = r"\\.\pipe\YunaLinkPose"

PACKET_TYPE_POSE  = 0x01
PACKET_TYPE_INPUT = 0x02

DEVICE_HMD        = 0
DEVICE_CTRL_LEFT  = 1
DEVICE_CTRL_RIGHT = 2

_HDR_FMT   = "<BH"       # type(u8) + length(u16)  = 3 bytes
_POSE_FMT  = "<B7d"      # device(u8) + 7xdouble   = 57 bytes
_INPUT_FMT = "<B????fff" # device(u8)+4xbool+3xf32 = 16 bytes

_HDR_SIZE   = struct.calcsize(_HDR_FMT)
_POSE_SIZE  = struct.calcsize(_POSE_FMT)
_INPUT_SIZE = struct.calcsize(_INPUT_FMT)


# =============================================================================
# Utility
# =============================================================================

def euler_to_quat(yaw_deg: float, pitch_deg: float,
                  roll_deg: float) -> tuple:
    y = math.radians(yaw_deg)   / 2
    p = math.radians(pitch_deg) / 2
    r = math.radians(roll_deg)  / 2
    cy, sy = math.cos(y), math.sin(y)
    cp, sp = math.cos(p), math.sin(p)
    cr, sr = math.cos(r), math.sin(r)
    return (
        cr*cp*cy + sr*sp*sy,
        sr*cp*cy - cr*sp*sy,
        cr*sp*cy + sr*cp*sy,
        cr*cp*sy - sr*sp*cy,
    )


# =============================================================================
# YunaPoseSender
# =============================================================================

class YunaPoseSender:

    def __init__(self):
        self._handle = INVALID_HANDLE_VALUE

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self, timeout_sec: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            # Wait up to 1 s for the pipe to become available
            _k32.WaitNamedPipeW(PIPE_NAME, 1000)

            h = _k32.CreateFileW(
                PIPE_NAME,
                GENERIC_WRITE,
                0,          # no sharing
                None,       # default security
                OPEN_EXISTING,
                FILE_ATTRIBUTE_NORMAL,
                None
            )

            if h != INVALID_HANDLE_VALUE:
                self._handle = h
                print(f"[YUNA] Connected to {PIPE_NAME}")
                return True

            err = _k32.GetLastError()
            if err == ERROR_PIPE_BUSY:
                print("[YUNA] Pipe busy, retrying...", end="\r", flush=True)
                time.sleep(0.1)
            else:
                print(f"[YUNA] Waiting for driver (err={err})...",
                      end="\r", flush=True)
                time.sleep(0.5)

        print("\n[YUNA] Connection timeout.")
        return False

    def disconnect(self):
        if self._handle != INVALID_HANDLE_VALUE:
            _k32.CloseHandle(self._handle)
            self._handle = INVALID_HANDLE_VALUE

    def __enter__(self):  return self
    def __exit__(self, *_): self.disconnect()

    # ------------------------------------------------------------------
    # Low-level send
    # ------------------------------------------------------------------

    def _send(self, data: bytes) -> bool:
        if self._handle == INVALID_HANDLE_VALUE:
            raise RuntimeError("Not connected")
        buf      = (ctypes.c_char * len(data)).from_buffer_copy(data)
        written  = wt.DWORD(0)
        ok = _k32.WriteFile(
            self._handle, buf, len(data),
            ctypes.byref(written), None
        )
        if not ok:
            err = _k32.GetLastError()
            raise OSError(f"WriteFile failed (err={err})")
        return True

    def _pose_packet(self, device, x, y, z, qw, qx, qy, qz) -> bytes:
        payload = struct.pack(_POSE_FMT, device, x, y, z, qw, qx, qy, qz)
        header  = struct.pack(_HDR_FMT,  PACKET_TYPE_POSE, _POSE_SIZE)
        return header + payload

    def _input_packet(self, device,
                      trigger_click, grip_click, a_click, b_click,
                      trigger_value, joy_x, joy_y) -> bytes:
        payload = struct.pack(_INPUT_FMT, device,
                              trigger_click, grip_click, a_click, b_click,
                              trigger_value, joy_x, joy_y)
        header  = struct.pack(_HDR_FMT, PACKET_TYPE_INPUT, _INPUT_SIZE)
        return header + payload

    # ------------------------------------------------------------------
    # High-level API
    # ------------------------------------------------------------------

    def send_hmd(self, x, y, z, yaw=0.0, pitch=0.0, roll=0.0):
        qw, qx, qy, qz = euler_to_quat(yaw, pitch, roll)
        self._send(self._pose_packet(DEVICE_HMD, x, y, z, qw, qx, qy, qz))

    def send_left_hand(self, x, y, z, yaw=0.0, pitch=0.0, roll=0.0):
        qw, qx, qy, qz = euler_to_quat(yaw, pitch, roll)
        self._send(self._pose_packet(DEVICE_CTRL_LEFT, x, y, z, qw, qx, qy, qz))

    def send_right_hand(self, x, y, z, yaw=0.0, pitch=0.0, roll=0.0):
        qw, qx, qy, qz = euler_to_quat(yaw, pitch, roll)
        self._send(self._pose_packet(DEVICE_CTRL_RIGHT, x, y, z, qw, qx, qy, qz))

    def send_left_input(self, trigger_click=False, grip_click=False,
                        a_click=False, b_click=False,
                        trigger_value=0.0, joy_x=0.0, joy_y=0.0):
        self._send(self._input_packet(
            DEVICE_CTRL_LEFT,
            trigger_click, grip_click, a_click, b_click,
            trigger_value, joy_x, joy_y))

    def send_right_input(self, trigger_click=False, grip_click=False,
                         a_click=False, b_click=False,
                         trigger_value=0.0, joy_x=0.0, joy_y=0.0):
        self._send(self._input_packet(
            DEVICE_CTRL_RIGHT,
            trigger_click, grip_click, a_click, b_click,
            trigger_value, joy_x, joy_y))


# =============================================================================
# Idle demo loop
# =============================================================================

def demo_idle(sender: YunaPoseSender):
    print("[YUNA] Idle loop start  (Ctrl+C to stop)")
    t   = 0.0
    dt  = 1.0 / 60.0
    try:
        while True:
            breath = math.sin(t * 0.4) * 0.004

            sender.send_hmd       ( 0.00, 1.6  + breath,       0.0)
            sender.send_left_hand (-0.25, 1.1  + breath * 0.5, -0.1, pitch=-10)
            sender.send_right_hand( 0.25, 1.1  + breath * 0.5, -0.1, pitch=-10)
            sender.send_left_input()
            sender.send_right_input()

            t  += dt
            time.sleep(dt)

    except KeyboardInterrupt:
        print("\n[YUNA] Stopped.")


# =============================================================================
# Entry point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="YUNA Link - Pose Sender")
    parser.add_argument("--mode", choices=["idle", "test"],
                        default="idle",
                        help="idle: continuous loop  test: connection check only")
    args = parser.parse_args()

    with YunaPoseSender() as sender:
        if not sender.connect(timeout_sec=20.0):
            sys.exit(1)

        if args.mode == "test":
            print("[YUNA] Connection OK.")
        else:
            demo_idle(sender)


if __name__ == "__main__":
    main()
