import sys
import time
import pythoncom
import win32com.client


SERVICE_CLSID = "CeVIO.Talk.RemoteService2.ServiceControl2"
TALKER_CLSID = "CeVIO.Talk.RemoteService2.Talker2V40"

CAST_NAME = "小春六花"   # ここを手持ちのキャスト名に変更
START_HOST_NO_WAIT = False    # False で通常起動


class CevioAITalker:
    def __init__(self, cast_name: str):
        pythoncom.CoInitialize()
        self.service = win32com.client.Dispatch(SERVICE_CLSID)
        self.talker = win32com.client.Dispatch(TALKER_CLSID)

        # CeVIO AI ホスト起動
        self.service.StartHost(START_HOST_NO_WAIT)

        # 念のため少し待つ
        time.sleep(1.0)

        # キャスト設定
        self.talker.Cast = cast_name

        # 基本パラメータ（0～100）
        self.talker.Volume = 70
        self.talker.Speed = 50
        self.talker.Tone = 50
        self.talker.ToneScale = 50
        self.talker.Alpha = 50

    def speak(self, text: str) -> None:
        text = text.strip()
        if not text:
            return

        # Speak は再生状態オブジェクトを返す
        state = self.talker.Speak(text)

        # 再生完了待ち
        state.Wait()

    def close(self) -> None:
        try:
            # 0 は通常終了
            self.service.CloseHost(0)
        except Exception:
            pass
        finally:
            pythoncom.CoUninitialize()


def main() -> int:
        talker = None
        try:
            talker = CevioAITalker(CAST_NAME)
            print(f"CeVIO AI ready. Cast = {CAST_NAME}")
            print("文字を入力して Enter で読み上げます。exit / quit で終了。")

            while True:
                try:
                    text = input(">>> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\n終了します。")
                    break

                if not text:
                    continue

                if text.lower() in ("exit", "quit"):
                    print("終了します。")
                    break

                try:
                    talker.speak(text)
                except Exception as e:
                    print(f"[ERROR] 読み上げ失敗: {e}")

            return 0

        except Exception as e:
            print(f"[ERROR] 初期化失敗: {e}")
            return 1

        finally:
            if talker is not None:
                talker.close()


if __name__ == "__main__":
    sys.exit(main())