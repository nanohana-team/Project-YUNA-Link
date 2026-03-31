import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


WINDOWS = os.name == "nt"


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def python_executable() -> str:
    return sys.executable


def stream_output(name: str, pipe):
    try:
        for line in iter(pipe.readline, ""):
            if not line:
                break
            print(f"[{name}] {line.rstrip()}")
    except Exception as e:
        print(f"[{name}] [LOG ERROR] {e}")
    finally:
        try:
            pipe.close()
        except Exception:
            pass


class ManagedProcess:
    def __init__(self, name: str, cmd: list[str], cwd: Path, interactive: bool = False):
        self.name = name
        self.cmd = cmd
        self.cwd = cwd
        self.interactive = interactive
        self.proc: subprocess.Popen | None = None

    def start(self):
        print(f"[LAUNCH] {self.name}")
        print("         " + " ".join(f'"{x}"' if " " in x else x for x in self.cmd))

        creationflags = 0
        stdout = subprocess.PIPE
        stderr = subprocess.PIPE
        stdin = subprocess.DEVNULL

        if WINDOWS and self.interactive:
            creationflags = subprocess.CREATE_NEW_CONSOLE
            stdout = None
            stderr = None
            stdin = None

        self.proc = subprocess.Popen(
            self.cmd,
            cwd=str(self.cwd),
            stdout=stdout,
            stderr=stderr,
            stdin=stdin,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
            bufsize=1,
        )

        if self.proc.stdout is not None:
            threading.Thread(
                target=stream_output,
                args=(self.name, self.proc.stdout),
                daemon=True,
            ).start()

        if self.proc.stderr is not None:
            threading.Thread(
                target=stream_output,
                args=(self.name + ":ERR", self.proc.stderr),
                daemon=True,
            ).start()

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.proc is None or self.proc.poll() is not None:
            return

        print(f"[STOP] {self.name}")

        try:
            if WINDOWS:
                self.proc.terminate()
            else:
                self.proc.send_signal(signal.SIGINT)
        except Exception:
            pass

        try:
            self.proc.wait(timeout=5)
            return
        except Exception:
            pass

        try:
            self.proc.kill()
        except Exception:
            pass


def first_existing(root: Path, candidates: list[str]) -> Path | None:
    for rel in candidates:
        p = root / rel
        if p.exists():
            return p
    return None


def send_remote_command(root: Path, command: str, control_port: int, timeout: float = 10.0):
    py = python_executable()
    script = first_existing(root, [
        "src/vr/yuna_link.py",
        "apps/yuna_link.py",
    ])
    if script is None:
        print("[REMOTE] yuna_link.py が見つからない")
        return 1, "", "script not found"

    cmd = [
        py, str(script),
        "--remote-cmd", command,
        "--control-host", "127.0.0.1",
        "--control-port", str(control_port),
    ]

    try:
        result = subprocess.run(
            cmd,
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except Exception as e:
        return 1, "", str(e)


def wait_for_pose_control(root: Path, control_port: int, timeout_sec: float = 20.0) -> bool:
    deadline = time.time() + timeout_sec

    while time.time() < deadline:
        rc, out, err = send_remote_command(root, "ping", control_port, timeout=5.0)

        if out:
            print("[REMOTE][PING][STDOUT]")
            print(out.rstrip())
        if err:
            print("[REMOTE][PING][STDERR]")
            print(err.rstrip())

        if rc == 0:
            print("[REMOTE] pose control is ready")
            return True

        time.sleep(0.5)

    print("[REMOTE] pose control did not become ready in time")
    return False


def wait_for_query_server(host: str, port: int, timeout_sec: float = 20.0, ping_command: str = "PING") -> bool:
    import socket

    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2.0) as s:
                s.sendall((ping_command.rstrip() + "\n").encode("utf-8"))
                data = s.recv(1024).decode("utf-8", errors="replace").strip()
                if data:
                    print(f"[QUERY {host}:{port}] ready: {data}")
                    return True
        except Exception:
            pass

        time.sleep(0.5)

    print(f"[QUERY] query server did not become ready in time: {host}:{port}")
    return False


def add_common_vision_args(cmd: list[str], args):
    cmd.extend([
        "--window-title", args.window_title,
        "--model", args.yolo_model,
        "--conf", str(args.conf),
        "--imgsz", str(args.imgsz),
        "--device", str(args.device),
    ])

    if args.query_port > 0:
        cmd.extend(["--query-port", str(args.query_port)])


def build_processes(args):
    root = project_root()
    py = python_executable()
    procs: list[ManagedProcess] = []

    pose_script = first_existing(root, [
        "src/vr/yuna_link.py",
        "apps/yuna_link.py",
    ])

    vision_script = first_existing(root, [
        "src/vision/detect_player_dist.py",
        "src/vision/yolo_person_detect.py",
    ])

    chat_script = first_existing(root, [
        "apps/stt_llm_tts.py",
        "stt-llm-tts.py",
        "apps/chat_llm_tts.py",
    ])

    move_script = first_existing(root, [
        "src/vision/move_to_player.py",
    ])

    sound_script = first_existing(root, [
        "src/audio/focus_sound.py",
    ])

    focus_script = first_existing(root, [
        "apps/focus_player.py",
    ])

    print(f"[DEBUG] pose_script   = {pose_script}")
    print(f"[DEBUG] vision_script = {vision_script}")
    print(f"[DEBUG] chat_script   = {chat_script}")
    print(f"[DEBUG] move_script   = {move_script}")
    print(f"[DEBUG] sound_script  = {sound_script}")
    print(f"[DEBUG] focus_script  = {focus_script}")

    if not args.no_pose:
        if pose_script is not None:
            cmd = [
                py, str(pose_script),
                "--mode", "idle",
                "--control-host", "127.0.0.1",
                "--control-port", str(args.control_port),
            ]
            procs.append(ManagedProcess("POSE", cmd, root, interactive=True))
        else:
            print("[WARN] yuna_link.py が見つからないので POSE をスキップ")

    if not args.no_vision:
        if vision_script is not None:
            cmd = [py, str(vision_script)]
            add_common_vision_args(cmd, args)
            procs.append(ManagedProcess("VISION", cmd, root))
        else:
            print("[WARN] detect_player_dist.py が見つからないので VISION をスキップ")

    if args.behavior == "move":
        if sound_script is not None:
            cmd = [
                py, str(sound_script),
                "--query-host", "127.0.0.1",
                "--query-port", str(args.sound_query_port),
            ]
            procs.append(ManagedProcess("SOUND", cmd, root, interactive=True))
        else:
            print("[WARN] src/audio/focus_sound.py が見つからないので SOUND をスキップ")

        if focus_script is not None:
            cmd = [
                py, str(focus_script),
                "--sound-host", "127.0.0.1",
                "--sound-port", str(args.sound_query_port),
                "--ctrl-host", "127.0.0.1",
                "--ctrl-port", str(args.control_port),
            ]
            procs.append(ManagedProcess("FOCUS", cmd, root, interactive=True))
        else:
            print("[WARN] apps/focus_player.py が見つからないので FOCUS をスキップ")

        if move_script is not None:
            cmd = [
                py, str(move_script),
                "--query-host", "127.0.0.1",
                "--query-port", str(args.query_port),
                "--ctrl-host", "127.0.0.1",
                "--ctrl-port", str(args.control_port),
            ]
            procs.append(ManagedProcess("MOVE", cmd, root, interactive=True))
        else:
            print("[WARN] src/vision/move_to_player.py が見つからないので MOVE をスキップ")

    if not args.no_chat:
        if chat_script is not None:
            cmd = [py, str(chat_script)]
            procs.append(ManagedProcess("CHAT", cmd, root, interactive=True))
            print(f"[DEBUG] CHAT appended: {cmd}")
        else:
            print("[WARN] CHAT script が見つからないので CHAT をスキップ")
    else:
        print("[INFO] --no-chat 指定なので CHAT をスキップ")

    return procs


def parse_args():
    parser = argparse.ArgumentParser(
        description="Launch major Project YUNA Link features together."
    )

    parser.add_argument("--no-pose", action="store_true")
    parser.add_argument("--no-vision", action="store_true")
    parser.add_argument("--no-chat", action="store_true")

    parser.add_argument(
        "--behavior",
        choices=["none", "move"],
        default="none",
        help="none: no player-follow behavior / move: sound focus + player movement",
    )

    parser.add_argument("--window-title", default="YUNA Link - VR View")
    parser.add_argument("--yolo-model", default="x")
    parser.add_argument("--conf", type=float, default=0.30)
    parser.add_argument("--imgsz", type=int, default=960)
    parser.add_argument("--device", default="0")

    parser.add_argument("--tap-start-delay", type=float, default=3.0)
    parser.add_argument("--control-port", type=int, default=28765)
    parser.add_argument("--query-port", type=int, default=28766)
    parser.add_argument("--sound-query-port", type=int, default=28768)

    parser.add_argument("--pose-ready-timeout", type=float, default=20.0)
    parser.add_argument("--vision-ready-timeout", type=float, default=20.0)
    parser.add_argument("--sound-ready-timeout", type=float, default=20.0)

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = project_root()

    print("================================================")
    print(" Project YUNA Link - Start All Major Features")
    print("================================================")
    print(f"[INFO] Project root      : {root}")
    print(f"[INFO] Python            : {python_executable()}")
    print(f"[INFO] Behavior          : {args.behavior}")
    print(f"[INFO] Control port      : {args.control_port}")
    print(f"[INFO] Vision query port : {args.query_port}")
    print(f"[INFO] Sound query port  : {args.sound_query_port}")
    print()

    processes = build_processes(args)
    if not processes:
        print("[ERROR] 起動対象が1つも見つからなかったよ")
        return 1

    try:
        pose_proc = next((p for p in processes if p.name == "POSE"), None)
        vision_proc = next((p for p in processes if p.name == "VISION"), None)
        sound_proc = next((p for p in processes if p.name == "SOUND"), None)
        focus_proc = next((p for p in processes if p.name == "FOCUS"), None)
        move_proc = next((p for p in processes if p.name == "MOVE"), None)
        chat_proc = next((p for p in processes if p.name == "CHAT"), None)

        started: list[ManagedProcess] = []

        # 1. POSE
        if pose_proc is not None:
            pose_proc.start()
            started.append(pose_proc)
            time.sleep(1.0)

            if wait_for_pose_control(root, args.control_port, timeout_sec=args.pose_ready_timeout):
                time.sleep(args.tap_start_delay)
                rc, out, err = send_remote_command(root, "TAP START", args.control_port, timeout=10.0)

                if out:
                    print("[REMOTE][TAP START][STDOUT]")
                    print(out.rstrip())
                if err:
                    print("[REMOTE][TAP START][STDERR]")
                    print(err.rstrip())

                print(f"[REMOTE] TAP START returncode={rc}")
            else:
                print("[WARN] POSE ready 待ちに失敗したので TAP START は送らない")

        # 2. VISION
        if vision_proc is not None:
            vision_proc.start()
            started.append(vision_proc)
            time.sleep(1.0)

            if args.behavior == "move":
                if not wait_for_query_server(
                    "127.0.0.1",
                    args.query_port,
                    timeout_sec=args.vision_ready_timeout,
                    ping_command="PING",
                ):
                    print("[WARN] VISION query server ready 待ちに失敗。MOVE は接続待ちになるかも")

        # 3. SOUND
        if sound_proc is not None:
            sound_proc.start()
            started.append(sound_proc)
            time.sleep(1.0)

            if not wait_for_query_server(
                "127.0.0.1",
                args.sound_query_port,
                timeout_sec=args.sound_ready_time_out if hasattr(args, "sound_ready_time_out") else args.sound_ready_timeout,
                ping_command="PING",
            ):
                print("[WARN] SOUND query server ready 待ちに失敗。FOCUS は接続待ちになるかも")

        # 4. FOCUS
        if focus_proc is not None:
            focus_proc.start()
            started.append(focus_proc)
            time.sleep(0.8)

        # 5. MOVE
        if move_proc is not None:
            move_proc.start()
            started.append(move_proc)
            time.sleep(0.8)

        # 6. CHAT
        if chat_proc is not None:
            chat_proc.start()
            started.append(chat_proc)
            time.sleep(0.8)

        print()
        print("[INFO] 主要機能を起動したよ。Ctrl+C でまとめて終了。")

        while True:
            alive = any(p.is_running() for p in started)
            if not alive:
                print("[INFO] 全プロセスが終了したよ。")
                break
            time.sleep(1.0)

        return 0

    except KeyboardInterrupt:
        print("\n[INFO] 停止するね。")
        return 0

    finally:
        for p in reversed(processes):
            p.stop()


if __name__ == "__main__":
    raise SystemExit(main())