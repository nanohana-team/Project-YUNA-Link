# Project YUNA Link — 統合アーキテクチャ

本ドキュメントは以下を統合した最終アーキテクチャ仕様である。

* システム全体構成
* OpenVR Driver 実装仕様
* コントローラー入力制御
* Python連携API（Pose/Input）

---

## システム全体図

```text
┌─────────────────────────────────────────────────────┐
│  YUNA 制御プロセス                                  │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌──────────┐  │
│  │ 視覚系   │ │ 会話系  │ │ 制御系   │ │ 振る舞い │  │
│  │  YOLO   │ │ASR+LLM  │ │状態管理  │ │  系      │  │
│  └────┬────┘ └────┬────┘ └────┬────┘ └────┬─────┘  │
│       └───────────┴───────────┴────────────┘        │
│                         │                           │
│         apps/pose_sender.py / input_sender.py       │
└─────────────────────────┼───────────────────────────┘
                          │ Named Pipe
                          │ \\.\pipe\YunaLinkPose
┌─────────────────────────▼───────────────────────────┐
│  driver_yuna.dll  (OpenVR Driver)                   │
│  ┌──────────────────────────────────────────────┐   │
│  │ PoseServer / InputServer 受信スレッド         │   │
│  └──────┬───────────────────────────────────────┘   │
│         │ 内部共有バッファ                           │
│  ┌──────▼──────┐  ┌────────────┐  ┌────────────┐   │
│  │  YunaHMD    │  │ YunaCtrl   │  │ YunaCtrl   │   │
│  │   (頭)      │  │  (左手)     │  │  (右手)    │   │
│  └──────┬──────┘  └─────┬──────┘  └─────┬──────┘   │
└─────────┼───────────────┼───────────────┼───────────┘
          │ OpenVR Driver API
┌─────────▼──────────────────────────────────────────┐
│ SteamVR                                            │
└─────────┬──────────────────────────────────────────┘
          │ トラッキング / 入力
┌─────────▼──────────────────────────────────────────┐
│ VRChat                                             │
└────────────────────────────────────────────────────┘
```

---

## データフロー

```text
Python制御
  → FramePacket生成
    → Named Pipe送信
      → Pose/Input受信
        → 内部状態更新
          → RunFrame
            → SteamVR
```

---

## OpenVR Driver 設計

### 採用インターフェース

* IServerTrackedDeviceProvider
* ITrackedDeviceServerDriver
* IVRDisplayComponent
* IVRDriverInput

### ライフサイクル

```text
Init
 → TrackedDeviceAdded
   → Activate
     → RunFrameループ
```

### 実装ルール

* RunFrameは軽量に保つ
* 外部受信スレッドから直接SteamVR APIを叩かない
* 内部バッファ経由で状態を反映

---

## 仮想デバイス構成

### デバイス一覧

* HMD
* 左コントローラー
* 右コントローラー

### コントローラー仕様

* DeviceClass: Controller
* Role:

  * 左: LeftHand
  * 右: RightHand

### 入力コンポーネント

* /input/a/click
* /input/start/click
* /input/thumbstick/x
* /input/thumbstick/y

---

## Python連携API（統合仕様）

### 通信

* Named Pipe
* 1フレーム = 1パケット

### FramePacket

```cpp
struct Vec3 { float x, y, z; };
struct Quat { float x, y, z, w; };

struct ControllerPose {
    Vec3 position;
    Quat rotation;
    bool trackingValid;
    bool connected;
};

struct ControllerInput {
    bool aButton;
    float stickX;
    float stickY;
};

struct FramePacket {
    uint64_t frameId;
    double timestamp;

    ControllerPose leftPose;
    ControllerPose rightPose;

    ControllerInput leftInput;
    ControllerInput rightInput;

    bool startButton;
};
```

---

## 座標系

* 単位: メートル
* +X: 右
* +Y: 上
* -Z: 前
* 回転: クォータニオン

---

## 入力制御

### 内部状態

```cpp
struct HandInputState {
    bool aButton;
    float stickX;
    float stickY;
};

struct GlobalInputState {
    bool startButton;
    HandInputState left;
    HandInputState right;
};
```

---

## フェイルセーフ

* 250ms無通信 → tracking無効
* 切断時 → 入力リセット

---

## 更新周期

* 60Hz 推奨
* 最大90Hz

---

## 拡張性

将来的に以下へ拡張可能

* trigger / grip
* skeletal input
* 視線制御
* ジェスチャ制御
* 複数人対応

---

## 設計まとめ

* Pythonからフレーム単位で制御
* Driver側はバッファ同期
* PoseとInput統合
* OpenVRに安全に反映

この構成により、AIによる身体制御・会話・視覚処理を統合可能な基盤を構築する。
