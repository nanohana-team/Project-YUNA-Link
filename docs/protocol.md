# Named Pipe 通信プロトコル仕様

## パイプ名

```
\\.\pipe\YunaLinkPose
```

`apps/pose_sender.py`（送信側）と `src/pose_server.h`（受信側）の定数は完全一致させること。

---

## パケット構造

すべてのパケットは **ヘッダ（3バイト）＋ペイロード** の構成。
バイトオーダーはすべて **little-endian**。

### ヘッダ（3バイト）

| オフセット | 型 | フィールド | 説明 |
|---|---|---|---|
| 0 | uint8 | type | `0x01` = 姿勢パケット / `0x02` = 入力パケット |
| 1 | uint16 | length | ペイロードのバイト数（ヘッダを除く） |

---

### 姿勢パケット（type = 0x01、length = 57）

| オフセット | 型 | フィールド | 説明 |
|---|---|---|---|
| 0 | uint8 | device | `0` = HMD / `1` = 左手 / `2` = 右手 |
| 1 | double | px | X 座標（メートル） |
| 9 | double | py | Y 座標（メートル、上方向が正） |
| 17 | double | pz | Z 座標（メートル、前方向が負） |
| 25 | double | qw | クォータニオン w |
| 33 | double | qx | クォータニオン x |
| 41 | double | qy | クォータニオン y |
| 49 | double | qz | クォータニオン z |

合計：1 + 7×8 = **57 バイト**

---

### 入力パケット（type = 0x02、length = 16）

| オフセット | 型 | フィールド | 説明 |
|---|---|---|---|
| 0 | uint8 | device | `1` = 左手 / `2` = 右手 |
| 1 | bool | trigger_click | トリガーボタン押下 |
| 2 | bool | grip_click | グリップボタン押下 |
| 3 | bool | a_click | A ボタン押下 |
| 4 | bool | b_click | B ボタン押下 |
| 5 | float | trigger_value | トリガー軸値（0.0 〜 1.0） |
| 9 | float | joy_x | スティック X 軸（−1.0 〜 1.0） |
| 13 | float | joy_y | スティック Y 軸（−1.0 〜 1.0） |

合計：1 + 4×1 + 3×4 = **17 バイト**
※ `#pragma pack(push, 1)` によりパディングなし

---

## Python 側のフォーマット文字列

```python
_HDR_FMT   = "<BH"      # 3 bytes
_POSE_FMT  = "<B7d"     # 57 bytes
_INPUT_FMT = "<B????fff" # 16 bytes
```

---

## 送信例（Python）

```python
from apps.pose_sender import YunaPoseSender

with YunaPoseSender() as s:
    s.connect()

    # 頭を正面・高さ 1.6m に置く
    s.send_hmd(x=0.0, y=1.6, z=0.0)

    # 左手を左前・腰の高さに
    s.send_left_hand(x=-0.25, y=1.1, z=-0.1)

    # 右手を右前・腰の高さに
    s.send_right_hand(x=0.25, y=1.1, z=-0.1)

    # ボタン入力なし
    s.send_left_input()
    s.send_right_input()
```

---

## 接続フロー

```
SteamVR 起動
  → driver_yuna.dll がロードされる
    → PoseServer::Start() で Named Pipe を開く（ConnectNamedPipe で待機）

Python 起動
  → YunaPoseSender.connect() でパイプを open()
    → ハンドシェイクなし（バイナリをそのまま流す）
      → パイプ切断まで受信ループ継続
```
