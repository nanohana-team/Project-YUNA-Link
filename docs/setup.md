# セットアップ手順

## 必要なもの

| ソフトウェア | バージョン | 入手先 |
|---|---|---|
| Windows | 10 / 11 (x64) | — |
| Visual Studio | 2022（C++デスクトップ開発） | https://visualstudio.microsoft.com |
| OpenVR SDK | 最新 | https://github.com/ValveSoftware/openvr/releases |
| Python | 3.10 以上 | https://python.org |
| SteamVR | 最新 | Steam 経由 |

---

## 手順

### Step 1: OpenVR SDK を取得する

```bat
git clone https://github.com/ValveSoftware/openvr.git C:\openvr
```

または [Releases](https://github.com/ValveSoftware/openvr/releases) から zip をダウンロードして展開。

### Step 2: 環境変数を設定する

```bat
setx OPENVR_SDK_PATH "C:\openvr"
```

設定後、**新しいターミナルを開く**こと（変数が反映されないため）。

確認：

```bat
echo %OPENVR_SDK_PATH%
REM → C:\openvr
```

### Step 3: Visual Studio 2022 でビルドする

1. `ProjectYUNALink.sln` をダブルクリックして VS2022 で開く
2. 構成を `Release` / プラットフォームを `x64` に設定
3. `ビルド → ソリューションのビルド`（または `Ctrl+Shift+B`）

ビルド成功後の出力：

```
src/driver_yuna/bin/win64/
  driver_yuna.dll     ← ドライバ本体
  openvr_api.dll      ← OpenVR API（自動コピー）
```

### Step 4: SteamVR へインストールする

```bat
scripts\install_driver.bat
```

インストール先：

```
<Steam>\steamapps\common\SteamVR\drivers\yuna\
```

### Step 5: 動作確認

1. SteamVR を起動する
2. 別のコマンドプロンプトで以下を実行：

```bat
python apps\pose_sender.py --mode test
```

`[YUNA] Connection OK.` が出れば成功。

### Step 6: アイドル動作の確認

```bat
python apps\pose_sender.py
```

VRChat を起動すると YUNA アバターが直立した状態で見えるはず。

---

## トラブルシューティング

| 症状 | 原因 / 対処 |
|---|---|
| ビルドエラー `openvr_driver.h not found` | `OPENVR_SDK_PATH` が未設定か間違い。新しいターミナルで `echo %OPENVR_SDK_PATH%` を確認 |
| ビルドエラー `openvr_api.lib not found` | OpenVR SDK の `lib/win64/` に `.lib` があるか確認 |
| `Connection timeout` | SteamVR が起動していない、またはドライバが認識されていない |
| SteamVR でデバイスが表示されない | `%APPDATA%\..\Local\openvr\vrserver.txt` のログを確認 |
| VRChat でアバターが動かない | SteamVR トラッキングが有効になっているか確認（OSC 設定ではない） |
