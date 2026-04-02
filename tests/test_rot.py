# test_rstick_sequence.py
import socket
import time

HOST = "127.0.0.1"
PORT = 28765


def send_cmd(cmd: str):
    with socket.create_connection((HOST, PORT), timeout=2.0) as s:
        s.sendall((cmd.strip() + "\n").encode("utf-8"))
        try:
            s.recv(1024)
        except:
            pass


def main():
    print("[TEST] STEP1: send 1 0")
    send_cmd("SET R_STICK 1.0000 0.0000")

    print("[TEST] wait 1.0 sec")
    time.sleep(1.0)

    print("[TEST] STEP2: send 0 0 x10")
    for i in range(10):
        send_cmd("SET R_STICK 0.0000 0.0000")
        time.sleep(0.02)  # 50Hzくらい

    print("[TEST] done")


if __name__ == "__main__":
    main()