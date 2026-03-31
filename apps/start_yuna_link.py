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
            threading.Thread(target=stream_output, args=(self.name, self.proc.stdout), daemon=True).start()

        if self.proc.stderr is not None:
            threading.Thread(target=stream_output, args=(self.name + ":ERR", self.proc.stderr), daemon=True).start()

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


def delayed_remote_command(root: Path, delay_sec: float, command: str, control_port: int):
    time.sleep(delay_sec)

    py = python_executable()
    script = first_existing(root, [
        "src/vr/yuna_link.py",
        "apps/yuna_link.py",
    ])
    if script is None:
        print("[REMOTE] yuna_link.py が見つからない")
        return

    cmd = [
        py, str(script),
        "--remote-cmd", command,
        "--control-host", "127.0.0.1",
        "--control-port", str(control_port),
    ]

    cmd_str = " ".join([f'"{x}"' if " " in x else x for x in cmd])
    print(f"[REMOTE] sending: {cmd_str}")

    try:
        result = subprocess.run(
            cmd,
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )

        if result.stdout:
            print("[REMOTE][STDOUT]")
            print(result.stdout.rstrip())
        if result.stderr:
            print("[REMOTE][STDERR]")
            print(result.stderr.rstrip())

        print(f"[REMOTE] returncode={result.returncode}")
    except Exception as e:
        print(f"[REMOTE] failed: {e}")


def build_processes(args):
    root = project_root()
    py = python_executable()
    procs = []

    if not args.no_pose:
        pose_script = first_existing(root, [
            "src/vr/yuna_link.py",
            "apps/yuna_link.py",
        ])
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
        vision_script = first_existing(root, [
            "src/vision/detect_player_dist.py",
            "src/vision/yolo_person_detect.py",
        ])
        if vision_script is not None:
            cmd = [
                py, str(vision_script),
                "--window-title", args.window_title,
                "--model", args.yolo_model,
                "--conf", str(args.conf),
                "--imgsz", str(args.imgsz),
                "--device", str(args.device),
            ]
            procs.append(ManagedProcess("VISION", cmd, root))
        else:
            print("[WARN] VISION をスキップ")

    if not args.no_chat:
        chat_script = first_existing(root, [
            "apps/stt_llm_tts.py",
            "stt-llm-tts.py",
            "apps/chat_llm_tts.py",
        ])
        if chat_script is not None:
            cmd = [py, str(chat_script)]
            procs.append(ManagedProcess(
                "CHAT",
                cmd,
                root,
                interactive=(chat_script.name == "chat_llm_tts.py"),
            ))
        else:
            print("[WARN] CHAT をスキップ")

    return procs


def parse_args():
    parser = argparse.ArgumentParser(description="Launch major Project YUNA Link features together.")
    parser.add_argument("--no-pose", action="store_true")
    parser.add_argument("--no-vision", action="store_true")
    parser.add_argument("--no-chat", action="store_true")

    parser.add_argument("--window-title", default="YUNA Link - VR View")
    parser.add_argument("--yolo-model", default="x")
    parser.add_argument("--conf", type=float, default=0.30)
    parser.add_argument("--imgsz", type=int, default=960)
    parser.add_argument("--device", default="0")

    parser.add_argument("--tap-start-delay", type=float, default=3.0)
    parser.add_argument("--control-port", type=int, default=28765)

    return parser.parse_args()

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

def main() -> int:
    args = parse_args()
    root = project_root()

    print("================================================")
    print(" Project YUNA Link - Start All Major Features")
    print("================================================")
    print(f"[INFO] Project root: {root}")
    print(f"[INFO] Python      : {python_executable()}")
    print()

    processes = build_processes(args)
    if not processes:
        print("[ERROR] 起動対象が1つも見つからなかったよ")
        return 1

    try:
        pose_proc = next((p for p in processes if p.name == "POSE"), None)
        other_procs = [p for p in processes if p.name != "POSE"]

        # 1. 先に POSE だけ起動
        if pose_proc is not None:
            pose_proc.start()
            time.sleep(1.0)

            # 2. POSE の制御口が ready になるまで待つ
            if wait_for_pose_control(root, args.control_port, timeout_sec=20.0):
                # 3. ready 後に TAP START
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

        # 4. そのあとで他を起動
        for p in other_procs:
            p.start()
            time.sleep(0.8)

        print()
        print("[INFO] 主要機能を起動したよ。Ctrl+C でまとめて終了。")

        while True:
            alive = False
            for p in processes:
                if p.is_running():
                    alive = True

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