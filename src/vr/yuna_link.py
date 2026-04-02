"""
src/vr/yuna_link.py
Project YUNA Link - Unified Python Client

YunaLink        -- FramePacket + HMD sender  (\\.\pipe\YunaLinkPose)
YunaInputClient -- Text command sender       (\\.\pipe\YunaLinkInput)

Idle mode:
  - keeps controller pose alive
  - does NOT send HMD pose
  - maintains input state internally
  - local control server updates that input state

Usage:
  python src/vr/yuna_link.py --mode idle
  python src/vr/yuna_link.py --mode input
  python src/vr/yuna_link.py --cmd "TAP A"
  python src/vr/yuna_link.py --remote-cmd "SET R_STICK 0.6 0"
"""

import argparse
import ctypes
import ctypes.wintypes as wt
import math
import socket
import struct
import threading
import time


# =============================================================================
# Win32 Named Pipe
# =============================================================================

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
INVALID_HANDLE_VALUE = wt.HANDLE(-1).value
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
ERROR_PIPE_BUSY = 231

_k32.CreateFileW.restype = wt.HANDLE
_k32.CreateFileW.argtypes = [
    wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE
]
_k32.WriteFile.restype = wt.BOOL
_k32.WriteFile.argtypes = [
    wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p
]
_k32.CloseHandle.restype = wt.BOOL
_k32.CloseHandle.argtypes = [wt.HANDLE]
_k32.WaitNamedPipeW.restype = wt.BOOL
_k32.WaitNamedPipeW.argtypes = [wt.LPCWSTR, wt.DWORD]
_k32.GetLastError.restype = wt.DWORD


def _pipe_connect(name: str, timeout_sec: float = 20.0):
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        _k32.WaitNamedPipeW(name, 1000)
        h = _k32.CreateFileW(
            name,
            GENERIC_WRITE,
            0,
            None,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if h != INVALID_HANDLE_VALUE:
            return h
        if _k32.GetLastError() != ERROR_PIPE_BUSY:
            time.sleep(0.3)
    raise TimeoutError(f"Could not connect to {name}")


def _pipe_write(handle, data: bytes):
    buf = (ctypes.c_char * len(data)).from_buffer_copy(data)
    written = wt.DWORD(0)
    if not _k32.WriteFile(handle, buf, len(data), ctypes.byref(written), None):
        raise OSError(f"WriteFile failed err={_k32.GetLastError()}")


# =============================================================================
# Protocol constants
# =============================================================================

PKT_HMD_POSE = 0x01
PKT_FRAME = 0x10

POSE_PIPE = r"\\.\pipe\YunaLinkPose"
INPUT_PIPE = r"\\.\pipe\YunaLinkInput"

_HMD_FMT = "<B7d"
assert struct.calcsize(_HMD_FMT) == 57

_FRAME_FMT = "<Qd" + "7fBB" + "7fBB" + "BBBBffff" + "BBBBffff" + "BB"
assert struct.calcsize(_FRAME_FMT) == 118, f"FramePacket size={struct.calcsize(_FRAME_FMT)}"

DEFAULT_CONTROL_HOST = "127.0.0.1"
DEFAULT_CONTROL_PORT = 28765


def _hdr(pkt_type: int, body_len: int) -> bytes:
    return struct.pack("<BH", pkt_type, body_len)


def euler_to_quat(yaw=0.0, pitch=0.0, roll=0.0):
    y = math.radians(yaw) / 2
    p = math.radians(pitch) / 2
    r = math.radians(roll) / 2
    cy, sy = math.cos(y), math.sin(y)
    cp, sp = math.cos(p), math.sin(p)
    cr, sr = math.cos(r), math.sin(r)
    return (
        cr * cp * cy + sr * sp * sy,
        sr * cp * cy - cr * sp * sy,
        cr * sp * cy + sr * cp * sy,
        cr * cp * sy - sr * sp * cy,
    )


def clamp(v, lo=-1.0, hi=1.0):
    return max(lo, min(hi, v))


def clamp01(v):
    return max(0.0, min(1.0, v))


def parse_bool_token(token: str) -> bool:
    t = token.strip().lower()
    return t in ("1", "true", "on", "yes")


# =============================================================================
# YunaLink  (YunaLinkPose)
# =============================================================================

class YunaLink:
    """
    Send HMD pose and FramePackets (controller pose + input) to the driver.
    """

    def __init__(self):
        self._handle = INVALID_HANDLE_VALUE
        self._frame_id = 0
        self._lock = threading.Lock()

    def connect(self, timeout_sec=20.0):
        print(f"[YUNA] Connecting to {POSE_PIPE} ...", end=" ", flush=True)
        self._handle = _pipe_connect(POSE_PIPE, timeout_sec)
        print("OK")
        return self

    def disconnect(self):
        if self._handle != INVALID_HANDLE_VALUE:
            _k32.CloseHandle(self._handle)
            self._handle = INVALID_HANDLE_VALUE

    def __enter__(self):
        return self.connect()

    def __exit__(self, *_):
        self.disconnect()

    def send_hmd(self, x=0.0, y=1.6, z=0.0, yaw=0.0, pitch=0.0, roll=0.0):
        qw, qx, qy, qz = euler_to_quat(yaw, pitch, roll)
        body = struct.pack(_HMD_FMT, 0, x, y, z, qw, qx, qy, qz)
        packet = _hdr(PKT_HMD_POSE, len(body)) + body
        with self._lock:
            _pipe_write(self._handle, packet)

    def send_frame(
        self,
        left_pos=(-0.25, 1.1, -0.1),
        left_rot=(0.0, 0.0, 0.0),
        right_pos=(0.25, 1.1, -0.1),
        right_rot=(0.0, 0.0, 0.0),
        left_tracking=True,
        left_connected=True,
        right_tracking=True,
        right_connected=True,
        left_input: dict = None,
        right_input: dict = None,
        start_button=False,
        menu_button=False,
    ):
        li = left_input or {}
        ri = right_input or {}

        self._frame_id += 1
        ts = time.monotonic()

        lqw, lqx, lqy, lqz = euler_to_quat(*left_rot)
        rqw, rqx, rqy, rqz = euler_to_quat(*right_rot)

        body = struct.pack(
            _FRAME_FMT,
            self._frame_id, ts,

            # left pose
            left_pos[0], left_pos[1], left_pos[2],
            lqx, lqy, lqz, lqw,
            1 if left_tracking else 0,
            1 if left_connected else 0,

            # right pose
            right_pos[0], right_pos[1], right_pos[2],
            rqx, rqy, rqz, rqw,
            1 if right_tracking else 0,
            1 if right_connected else 0,

            # left input
            1 if li.get("a_button", False) else 0,
            1 if li.get("b_button", False) else 0,
            1 if li.get("x_button", False) else 0,
            1 if li.get("y_button", False) else 0,
            clamp01(li.get("trigger", 0.0)),
            clamp01(li.get("grip", 0.0)),
            clamp(li.get("stick_x", 0.0)),
            clamp(li.get("stick_y", 0.0)),

            # right input
            1 if ri.get("a_button", False) else 0,
            1 if ri.get("b_button", False) else 0,
            1 if ri.get("x_button", False) else 0,
            1 if ri.get("y_button", False) else 0,
            clamp01(ri.get("trigger", 0.0)),
            clamp01(ri.get("grip", 0.0)),
            clamp(ri.get("stick_x", 0.0)),
            clamp(ri.get("stick_y", 0.0)),

            # global
            1 if start_button else 0,
            1 if menu_button else 0,
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
    Mainly used for one-shot commands / REPL / compatibility.
    """

    def __init__(self):
        self._handle = INVALID_HANDLE_VALUE
        self._lock = threading.Lock()

    def connect(self, timeout_sec=20.0):
        print(f"[YUNA Input] Connecting to {INPUT_PIPE} ...", end=" ", flush=True)
        self._handle = _pipe_connect(INPUT_PIPE, timeout_sec)
        print("OK")
        return self

    def disconnect(self):
        if self._handle != INVALID_HANDLE_VALUE:
            _k32.CloseHandle(self._handle)
            self._handle = INVALID_HANDLE_VALUE

    def __enter__(self):
        return self.connect()

    def __exit__(self, *_):
        self.disconnect()

    def send(self, cmd: str):
        data = (cmd.strip() + "\n").encode("ascii")
        with self._lock:
            _pipe_write(self._handle, data)

    def set_start(self, v: bool):
        self.send(f"SET START {1 if v else 0}")

    def set_menu(self, v: bool):
        self.send(f"SET MENU {1 if v else 0}")

    def set_a(self, v: bool):
        self.send(f"SET A {1 if v else 0}")

    def set_b(self, v: bool):
        self.send(f"SET B {1 if v else 0}")

    def set_x(self, v: bool):
        self.send(f"SET X {1 if v else 0}")

    def set_y(self, v: bool):
        self.send(f"SET Y {1 if v else 0}")

    def set_rtrigger(self, v: float):
        self.send(f"SET RTRIGGER {v:.4f}")

    def set_rgrip(self, v: float):
        self.send(f"SET RGRIP {v:.4f}")

    def set_ltrigger(self, v: float):
        self.send(f"SET LTRIGGER {v:.4f}")

    def set_lgrip(self, v: float):
        self.send(f"SET LGRIP {v:.4f}")

    def set_left_stick(self, x: float, y: float):
        self.send(f"SET L_STICK {x:.4f} {y:.4f}")

    def set_right_stick(self, x: float, y: float):
        self.send(f"SET R_STICK {x:.4f} {y:.4f}")

    def reset(self):
        self.send("RESET_INPUT")

    def tap_a(self):
        self.send("TAP A")

    def tap_b(self):
        self.send("TAP B")

    def tap_x(self):
        self.send("TAP X")

    def tap_y(self):
        self.send("TAP Y")

    def tap_start(self):
        self.send("TAP START")

    def tap_menu(self):
        self.send("TAP MENU")

    def move(self, target: str, axis: str, delta: float):
        self.send(f"MOVE {target} {axis} {delta:.6f}")

    def rotate(self, target: str, axis: str, delta_deg: float):
        self.send(f"ROTATE {target} {axis} {delta_deg:.4f}")

    def set_pos(self, target: str, axis: str, value: float):
        self.send(f"SET {target} {axis} {value:.6f}")

    def set_rot(self, target: str, axis: str, deg: float):
        self.send(f"SET {target} {axis} {deg:.4f}")

    def reset_pose(self, target: str):
        self.send(f"RESET_POSE {target}")


# =============================================================================
# Idle input state
# =============================================================================

def make_idle_state() -> dict:
    return {
        "tap_start_frames": 0,
        "tap_menu_frames": 0,

        "start_button": False,
        "menu_button": False,

        "left_input": {
            "a_button": False,
            "b_button": False,
            "x_button": False,
            "y_button": False,
            "trigger": 0.0,
            "grip": 0.0,
            "stick_x": 0.0,
            "stick_y": 0.0,
        },
        "right_input": {
            "a_button": False,
            "b_button": False,
            "x_button": False,
            "y_button": False,
            "trigger": 0.0,
            "grip": 0.0,
            "stick_x": 0.0,
            "stick_y": 0.0,
        },
    }


def reset_idle_inputs(state: dict):
    state["tap_start_frames"] = 0
    state["tap_menu_frames"] = 0
    state["start_button"] = False
    state["menu_button"] = False

    for side in ("left_input", "right_input"):
        state[side]["a_button"] = False
        state[side]["b_button"] = False
        state[side]["x_button"] = False
        state[side]["y_button"] = False
        state[side]["trigger"] = 0.0
        state[side]["grip"] = 0.0
        state[side]["stick_x"] = 0.0
        state[side]["stick_y"] = 0.0


# =============================================================================
# Local control server
# =============================================================================

class LocalControlServer:
    """
    Receive commands from another local process and update idle input state.

    Protocol:
      TCP text lines on 127.0.0.1:<port>

    Supported:
      ping
      quit / exit / shutdown
      TAP A|B|X|Y|START|MENU
      SET START|MENU|A|B|X|Y 0|1
      SET RTRIGGER|RGRIP|LTRIGGER|LGRIP <f>
      SET L_STICK <x> <y>
      SET R_STICK <x> <y>

    Other commands are forwarded to YunaLinkInput for compatibility
    (pose commands, etc.).
    """

    def __init__(
        self,
        host: str,
        port: int,
        stop_event: threading.Event,
        command_state: dict,
        state_lock: threading.Lock,
    ):
        self.host = host
        self.port = port
        self.stop_event = stop_event
        self.command_state = command_state
        self.state_lock = state_lock
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def close(self):
        self.stop_event.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass

    def _run(self):
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((self.host, self.port))
            srv.listen(5)
            srv.settimeout(1.0)
            self._sock = srv
            print(f"[LOCAL CTRL] listening on {self.host}:{self.port}")
        except Exception as e:
            print(f"[LOCAL CTRL] failed to start: {e}")
            self.stop_event.set()
            return

        while not self.stop_event.is_set():
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            except Exception as e:
                print(f"[LOCAL CTRL] accept error: {e}")
                continue

            threading.Thread(
                target=self._handle_client,
                args=(conn, addr),
                daemon=True,
            ).start()

    def _reply_ok(self, conn: socket.socket):
        try:
            conn.sendall(b"OK\n")
        except OSError:
            pass

    def _reply_err(self, conn: socket.socket, msg: str):
        try:
            conn.sendall((f"ERR {msg}\n").encode("utf-8", errors="replace"))
        except OSError:
            pass

    def _handle_tap(self, button: str):
        b = button.upper()
        with self.state_lock:
            if b == "START":
                self.command_state["tap_start_frames"] = max(self.command_state["tap_start_frames"], 8)
            elif b == "MENU":
                self.command_state["tap_menu_frames"] = max(self.command_state["tap_menu_frames"], 8)
            elif b == "A":
                self.command_state["right_input"]["a_button"] = True
                self.command_state["right_input"]["_tap_a_until"] = time.monotonic() + (8 / 60.0)
            elif b == "B":
                self.command_state["right_input"]["b_button"] = True
                self.command_state["right_input"]["_tap_b_until"] = time.monotonic() + (8 / 60.0)
            elif b == "X":
                self.command_state["left_input"]["x_button"] = True
                self.command_state["left_input"]["_tap_x_until"] = time.monotonic() + (8 / 60.0)
            elif b == "Y":
                self.command_state["left_input"]["y_button"] = True
                self.command_state["left_input"]["_tap_y_until"] = time.monotonic() + (8 / 60.0)
            else:
                raise ValueError(f"unknown tap button: {button}")

    def _handle_set_command(self, parts: list[str]) -> bool:
        if len(parts) < 3:
            return False

        if parts[0].upper() != "SET":
            return False

        target = parts[1].upper()

        with self.state_lock:
            if target == "START":
                self.command_state["start_button"] = parse_bool_token(parts[2])
                return True

            if target == "MENU":
                self.command_state["menu_button"] = parse_bool_token(parts[2])
                return True

            if target == "L_STICK" and len(parts) >= 4:
                self.command_state["left_input"]["stick_x"] = clamp(float(parts[2]))
                self.command_state["left_input"]["stick_y"] = clamp(float(parts[3]))
                return True

            if target == "R_STICK" and len(parts) >= 4:
                self.command_state["right_input"]["stick_x"] = clamp(float(parts[2]))
                self.command_state["right_input"]["stick_y"] = clamp(float(parts[3]))
                return True

            if target == "LTRIGGER":
                self.command_state["left_input"]["trigger"] = clamp01(float(parts[2]))
                return True

            if target == "LGRIP":
                self.command_state["left_input"]["grip"] = clamp01(float(parts[2]))
                return True

            if target == "RTRIGGER":
                self.command_state["right_input"]["trigger"] = clamp01(float(parts[2]))
                return True

            if target == "RGRIP":
                self.command_state["right_input"]["grip"] = clamp01(float(parts[2]))
                return True

            if target == "A":
                self.command_state["right_input"]["a_button"] = parse_bool_token(parts[2])
                return True

            if target == "B":
                self.command_state["right_input"]["b_button"] = parse_bool_token(parts[2])
                return True

            if target == "X":
                self.command_state["left_input"]["x_button"] = parse_bool_token(parts[2])
                return True

            if target == "Y":
                self.command_state["left_input"]["y_button"] = parse_bool_token(parts[2])
                return True

        return False

    def _handle_client(self, conn: socket.socket, addr):
        peer = f"{addr[0]}:{addr[1]}"
        try:
            with conn:
                conn.settimeout(10.0)
                fileobj = conn.makefile("r", encoding="utf-8", newline="\n")

                for raw in fileobj:
                    line = raw.strip()
                    if not line:
                        continue

                    print(f"[LOCAL CTRL] <= {line}  ({peer})")
                    lower = line.lower()

                    if lower in ("quit", "exit", "shutdown"):
                        self.stop_event.set()
                        self._reply_ok(conn)
                        print("[LOCAL CTRL] shutdown requested")
                        return

                    if lower == "ping":
                        try:
                            conn.sendall(b"PONG\n")
                        except OSError:
                            pass
                        continue

                    if lower == "reset_input":
                        with self.state_lock:
                            reset_idle_inputs(self.command_state)
                        self._reply_ok(conn)
                        print("[LOCAL CTRL] idle input state reset")
                        continue

                    parts = line.split()

                    if len(parts) == 2 and parts[0].upper() == "TAP":
                        try:
                            self._handle_tap(parts[1])
                            self._reply_ok(conn)
                            print(f"[LOCAL CTRL] queued TAP {parts[1].upper()}")
                        except Exception as e:
                            self._reply_err(conn, str(e))
                        continue

                    try:
                        if self._handle_set_command(parts):
                            self._reply_ok(conn)
                            print(f"[LOCAL CTRL] state updated: {line}")
                            continue
                    except Exception as e:
                        self._reply_err(conn, str(e))
                        continue

                    try:
                        with YunaInputClient() as inp:
                            inp.send(line)
                        self._reply_ok(conn)
                        print(f"[LOCAL CTRL] forwarded: {line}")
                    except Exception as e:
                        self._reply_err(conn, str(e))
                        print(f"[LOCAL CTRL] forward failed: {e}")
                        return

        except (ConnectionResetError, BrokenPipeError, OSError) as e:
            print(f"[LOCAL CTRL] client disconnected ({peer}): {e}")


def send_remote_command(
    command: str,
    host: str = DEFAULT_CONTROL_HOST,
    port: int = DEFAULT_CONTROL_PORT,
    timeout_sec: float = 10.0,
) -> int:
    try:
        with socket.create_connection((host, port), timeout=timeout_sec) as s:
            s.settimeout(timeout_sec)
            s.sendall((command.strip() + "\n").encode("utf-8"))
            reply = s.recv(4096).decode("utf-8", errors="replace").strip()
            if reply:
                print(f"[REMOTE] reply: {reply}")
            return 0 if reply.startswith(("OK", "PONG")) else 1
    except Exception as e:
        print(f"[REMOTE] send failed: {e}")
        return 1


# =============================================================================
# Built-in modes
# =============================================================================

def mode_idle(control_host: str, control_port: int):
    print("[YUNA] Idle mode - Ctrl+C to stop")
    print("[YUNA] HMD pose is NOT sent in idle mode. Head pose is left to input commands / external controllers.")
    stop_event = threading.Event()

    command_state = make_idle_state()
    state_lock = threading.Lock()

    ctrl = LocalControlServer(
        control_host,
        control_port,
        stop_event,
        command_state,
        state_lock,
    )
    ctrl.start()

    with YunaLink() as yl:
        t = 0.0
        dt = 1.0 / 60.0
        last_debug_print = 0.0

        try:
            while not stop_event.is_set():
                breath = math.sin(t * 0.4) * 0.004
                now_mono = time.monotonic()

                with state_lock:
                    start_pressed = command_state["start_button"] or (command_state["tap_start_frames"] > 0)
                    menu_pressed = command_state["menu_button"] or (command_state["tap_menu_frames"] > 0)

                    if command_state["tap_start_frames"] > 0:
                        command_state["tap_start_frames"] -= 1
                    if command_state["tap_menu_frames"] > 0:
                        command_state["tap_menu_frames"] -= 1

                    # one-shot taps for ABXY
                    li = command_state["left_input"]
                    ri = command_state["right_input"]

                    if ri.get("_tap_a_until", 0.0) <= now_mono:
                        ri.pop("_tap_a_until", None)
                        if "a_button" in ri and not ri.get("_hold_a", False):
                            ri["a_button"] = ri["a_button"] and False

                    if ri.get("_tap_b_until", 0.0) <= now_mono:
                        ri.pop("_tap_b_until", None)
                        if "b_button" in ri and not ri.get("_hold_b", False):
                            ri["b_button"] = ri["b_button"] and False

                    if li.get("_tap_x_until", 0.0) <= now_mono:
                        li.pop("_tap_x_until", None)
                        if "x_button" in li and not li.get("_hold_x", False):
                            li["x_button"] = li["x_button"] and False

                    if li.get("_tap_y_until", 0.0) <= now_mono:
                        li.pop("_tap_y_until", None)
                        if "y_button" in li and not li.get("_hold_y", False):
                            li["y_button"] = li["y_button"] and False

                    left_input = {
                        "a_button": bool(li.get("a_button", False)),
                        "b_button": bool(li.get("b_button", False)),
                        "x_button": bool(li.get("x_button", False)),
                        "y_button": bool(li.get("y_button", False)),
                        "trigger": clamp01(li.get("trigger", 0.0)),
                        "grip": clamp01(li.get("grip", 0.0)),
                        "stick_x": clamp(li.get("stick_x", 0.0)),
                        "stick_y": clamp(li.get("stick_y", 0.0)),
                    }
                    right_input = {
                        "a_button": bool(ri.get("a_button", False)),
                        "b_button": bool(ri.get("b_button", False)),
                        "x_button": bool(ri.get("x_button", False)),
                        "y_button": bool(ri.get("y_button", False)),
                        "trigger": clamp01(ri.get("trigger", 0.0)),
                        "grip": clamp01(ri.get("grip", 0.0)),
                        "stick_x": clamp(ri.get("stick_x", 0.0)),
                        "stick_y": clamp(ri.get("stick_y", 0.0)),
                    }

                yl.send_frame(
                    left_pos=(-0.25, 1.1 + breath * 0.5, -0.1),
                    right_pos=(0.25, 1.1 + breath * 0.5, -0.1),
                    left_input=left_input,
                    right_input=right_input,
                    start_button=start_pressed,
                    menu_button=menu_pressed,
                )

                now = time.time()
                if now - last_debug_print >= 1.0:
                    last_debug_print = now
                    print(
                        "[YUNA][IDLE] "
                        f"L=({left_input['stick_x']:+.3f},{left_input['stick_y']:+.3f}) "
                        f"R=({right_input['stick_x']:+.3f},{right_input['stick_y']:+.3f}) "
                        f"LT={left_input['trigger']:.2f} RT={right_input['trigger']:.2f} "
                        f"START={1 if start_pressed else 0} MENU={1 if menu_pressed else 0}",
                        flush=True,
                    )

                t += dt
                time.sleep(dt)

        except KeyboardInterrupt:
            print("\n[YUNA] Stopped by keyboard.")
        finally:
            ctrl.close()
            stop_event.set()


_HELP = """
--- Pose commands ---
  MOVE   HEAD|L_CONTROLLER|R_CONTROLLER  x|y|z  <delta>
  ROTATE HEAD|L_CONTROLLER|R_CONTROLLER  x|y|z  <delta_deg>
  SET    HEAD|L_CONTROLLER|R_CONTROLLER  x|y|z  <value>
  SET    HEAD|L_CONTROLLER|R_CONTROLLER  rX|rY|rZ <deg>
  RESET_POSE HEAD|L_CONTROLLER|R_CONTROLLER

--- Input commands ---
  SET START|MENU|A|B|X|Y 0|1
  SET RTRIGGER|RGRIP|LTRIGGER|LGRIP <f>
  SET L_STICK <x> <y>    SET R_STICK <x> <y>
  TAP A|B|X|Y|START|MENU
  RESET_INPUT

--- Local control ---
  ping
  quit / exit / shutdown

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

            if not line:
                continue
            if line.lower() in ("quit", "exit", "q"):
                break
            if line.lower() == "help":
                print(_HELP)
                continue

            try:
                inp.send(line)
                print(f"  -> {line}")
            except OSError as e:
                print(f"  [ERROR] {e}")
                break

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
        epilog=__doc__,
    )

    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument(
        "--mode",
        choices=["idle", "input"],
        help="idle: controller/input keepalive loop | input: interactive REPL",
    )
    grp.add_argument(
        "--cmd",
        metavar="CMD",
        help="Send one input command to YunaLinkInput and exit",
    )
    grp.add_argument(
        "--remote-cmd",
        metavar="CMD",
        help="Send one command to an already-running idle process and exit",
    )

    parser.add_argument("--control-host", default=DEFAULT_CONTROL_HOST)
    parser.add_argument("--control-port", type=int, default=DEFAULT_CONTROL_PORT)

    args = parser.parse_args()

    if args.remote_cmd:
        raise SystemExit(
            send_remote_command(
                args.remote_cmd,
                host=args.control_host,
                port=args.control_port,
            )
        )
    elif args.cmd:
        mode_single_cmd(args.cmd)
    elif args.mode == "idle":
        mode_idle(args.control_host, args.control_port)
    elif args.mode == "input":
        mode_input_repl()


if __name__ == "__main__":
    main()