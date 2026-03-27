# models/

このディレクトリにはモデルファイルを配置する。
`.gitignore` により `*.pt` / `*.onnx` / `*.bin` は git 管理外。

## 初期版で使用するモデル

| モデル | ファイル名 | 用途 |
|---|---|---|
| YOLOv8n | `yolov8n.pt` | 人物検出（初期版） |

## 取得方法

```bash
pip install ultralytics
python -c "from ultralytics import YOLO; YOLO('yolov8n.pt')"
# ダウンロードされたファイルをこのディレクトリに移動する
```
