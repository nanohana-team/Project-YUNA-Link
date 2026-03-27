# Project YUNA Link

VRChat内で「人間のプレイヤーのように振る舞うAIアバター」を実現するプロジェクト。

本プロジェクトは、視覚・会話・身体制御を統合し、周囲のプレイヤーを認識しながら自然に会話・動作できるAIプレイヤーの構築を目的とする。

---

## ✨ 概要

このプロジェクトでは、以下を段階的に実現する：

* VRChat内に存在するAIアバター
* プレイヤーの検出と距離把握
* 音声による会話
* 視線・手・姿勢などの簡易的な身体表現

まずは最小構成の初期版を開発し、その後に自然な振る舞いを持つAIへと拡張する。

---

## 📁 ディレクトリ構成

```
Project-YUNA-Link/
├── apps/
│   └── pose_sender.py          # Python姿勢送信クライアント
├── config/
│   └── driver_settings.yaml    # ドライバ設定
├── docs/
│   ├── setup.md                # セットアップ手順
│   ├── protocol.md             # 通信プロトコル仕様
│   └── architecture.md        # アーキテクチャ概要
├── logs/                       # ログ出力先（git管理外）
├── models/                     # モデルファイル置き場（git管理外）
├── scripts/
│   └── install_driver.bat      # SteamVRへのインストール
├── src/
│   ├── driver_main.cpp/h       # ドライバDLLエントリ・プロバイダ
│   ├── hmd_device.cpp/h        # 仮想HMD（頭）
│   ├── controller_device.cpp/h # 仮想コントローラ（左手・右手）
│   ├── pose_server.cpp/h       # Named Pipeサーバー
│   ├── driver_yuna.vcxproj     # Visual Studio 2022 プロジェクト
│   └── driver_yuna/
│       ├── driver.vrdrivermanifest
│       └── resources/input/
│           └── yuna_controller_profile.json
├── ProjectYUNALink.sln         # Visual Studio 2022 ソリューション
├── .gitignore
├── .gitattributes
└── License.txt
```

---

## 🚀 クイックスタート

### 1. 前提ソフトウェア

| ソフトウェア | バージョン |
|---|---|
| Windows | 10 / 11 |
| Visual Studio | 2022（C++デスクトップ開発ワークロード） |
| OpenVR SDK | 最新 |
| Python | 3.10 以上 |
| SteamVR | 最新（Steam経由） |

### 2. OpenVR SDK の取得

```bat
git clone https://github.com/ValveSoftware/openvr.git C:\openvr
```

### 3. 環境変数の設定

```bat
setx OPENVR_SDK_PATH "C:\openvr"
```

設定後、**新しいコマンドプロンプトを開き直す**こと。

### 4. ビルド

`ProjectYUNALink.sln` を Visual Studio 2022 で開き、
構成を `Release | x64` に設定してビルドする。

ビルド成功後、以下に DLL が生成される：

```
src/driver_yuna/bin/win64/driver_yuna.dll
src/driver_yuna/bin/win64/openvr_api.dll  ← 自動コピー
```

### 5. SteamVR へのインストール

```bat
scripts\install_driver.bat
```

### 6. 動作確認

```bat
REM SteamVR を起動してから実行
python apps\pose_sender.py --mode test
```

`[YUNA] Connection OK.` が表示されれば成功。

アイドルループ（アバター直立）の確認：

```bat
python apps\pose_sender.py
```

---

## 🧠 アーキテクチャ

```
apps/pose_sender.py
  │  Named Pipe  \\.\pipe\YunaLinkPose
  ▼
driver_yuna.dll  (SteamVR ドライバ)
  ├─ YunaHMD           ← 頭
  ├─ YunaController    ← 左手
  └─ YunaController    ← 右手
  │  OpenVR Driver API
  ▼
SteamVR / VRChat
```

詳細は [`docs/architecture.md`](docs/architecture.md) を参照。

---

## 🛠️ 開発ステップ（仕様書より）

### 初期版

1. ✅ SteamVR仮想デバイス（本リポジトリ）
2. 仮想デバイス制御ソフト
3. 簡易状態管理
4. 音声認識
5. 小型LLM
6. TTS
7. SteamVR映像取得
8. YOLO人物検出
9. 距離概算

### 最終目標への拡張

1. 視線制御
2. 手のジェスチャ
3. YOLOモデル追加学習
4. 対象追跡
5. 距離推定高度化
6. 会話対象切り替え高度化
7. 複数人対応

---

## ⚠️ 技術的課題

* VRChatアバター（人外・非標準体型）への検出精度
* 距離推定の安定性
* 音声認識とTTSの干渉
* LLMの応答品質
* 複数人環境での制御複雑性

---

## 📄 ドキュメント

* [`docs/setup.md`](docs/setup.md) — 詳細セットアップ手順
* [`docs/protocol.md`](docs/protocol.md) — Named Pipe 通信プロトコル仕様
* [`docs/architecture.md`](docs/architecture.md) — システム構成

---

## 📄 仕様書

詳細仕様は [`docs/architecture.md`](docs/architecture.md) を参照。
