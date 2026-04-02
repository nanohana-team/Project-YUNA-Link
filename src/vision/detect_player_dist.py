from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes
import json
import socketserver
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import cv2
import mss
import numpy as np
import win32gui
from ultralytics import YOLO


THIS_FILE = Path(__file__).resolve()
REPO_ROOT = THIS_FILE.parents[2]
MODEL_DIR = REPO_ROOT / "models" / "yolo"
ENABLE_LOG = False
ENABLE_TRACE = False

DETECT_MODEL_MAP = {
    "n": MODEL_DIR / "yolo26n.pt",
    "s": MODEL_DIR / "yolo26s.pt",
    "m": MODEL_DIR / "yolo26m.pt",
    "l": MODEL_DIR / "yolo26l.pt",
    "x": MODEL_DIR / "yolo26x.pt",
}

POSE_MODEL_PATH = MODEL_DIR / "yolo26x-pose.pt"

DEFAULT_WINDOW_TITLE = "YUNA Link - VR View"

# ============================================================
# Win32 / GDI
# ============================================================

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

PW_CLIENTONLY = 0x00000001
PW_RENDERFULLCONTENT = 0x00000002
SRCCOPY = 0x00CC0020
BI_RGB = 0
DIB_RGB_COLORS = 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


# ============================================================
# Logging
# ============================================================

_LOG_FILE_HANDLE = None


def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    if "[TRACE]" in msg and not ENABLE_TRACE:
        return
    if not ENABLE_LOG:
        return

    line = f"[{now_str()}] {msg}"
    print(line, flush=True)
    if _LOG_FILE_HANDLE is not None:
        _LOG_FILE_HANDLE.write(line + "\n")
        _LOG_FILE_HANDLE.flush()


class StepTimer:
    def __init__(self, name: str, slow_ms: float = 250.0):
        self.name = name
        self.slow_ms = slow_ms
        self.t0 = None

    def __enter__(self):
        self.t0 = time.perf_counter()
        log(f"[TRACE] ENTER {self.name}")
        return self

    def __exit__(self, exc_type, exc, tb):
        dt_ms = (time.perf_counter() - self.t0) * 1000.0
        if exc is None:
            if dt_ms >= self.slow_ms:
                log(f"[SLOW ] EXIT  {self.name} {dt_ms:.1f} ms")
            else:
                log(f"[TRACE] EXIT  {self.name} {dt_ms:.1f} ms")
        else:
            log(f"[ERROR] EXCEPT {self.name} after {dt_ms:.1f} ms: {exc!r}")
            log(traceback.format_exc())
        return False


# ============================================================
# Args
# ============================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Detect player from single-eye VR window using person detection + pose-based face point"
    )
    parser.add_argument("--model", choices=["n", "s", "m", "l", "x"], default="x")
    parser.add_argument("--conf", type=float, default=0.35)
    parser.add_argument("--imgsz", type=int, default=960)
    parser.add_argument("--device", default="0")

    parser.add_argument("--pose-model", default=str(POSE_MODEL_PATH))
    parser.add_argument("--pose-conf", type=float, default=0.25)
    parser.add_argument("--face-kpt-conf", type=float, default=0.35)

    parser.add_argument("--window-title", default=DEFAULT_WINDOW_TITLE)
    parser.add_argument("--hwnd", type=int, default=0, help="Use specific hwnd directly")

    parser.add_argument("--fallback-top", type=int, default=0)
    parser.add_argument("--fallback-left", type=int, default=0)
    parser.add_argument("--fallback-width", type=int, default=1920)
    parser.add_argument("--fallback-height", type=int, default=1080)

    parser.add_argument("--title", default="YOLO Player Detect (Right Eye)")
    parser.add_argument("--max-persons", type=int, default=6)
    parser.add_argument("--center-priority", action="store_true")

    parser.add_argument("--debug-windows", action="store_true")
    parser.add_argument("--debug-match", action="store_true")
    parser.add_argument("--debug-capture-info", action="store_true")

    parser.add_argument("--trace", action="store_true", help="Enable detailed trace logs")
    parser.add_argument("--heartbeat-sec", type=float, default=2.0)
    parser.add_argument("--slow-ms", type=float, default=250.0)
    parser.add_argument("--log-file", default="", help="Optional log file path")

    parser.add_argument(
        "--save-zero-detect-every",
        type=int,
        default=30,
        help="Save debug frames every N frames when no detections are found (0=disable)",
    )
    parser.add_argument(
        "--debug-dir",
        default="logs/detect_debug_frames",
        help="Directory for saved debug frames",
    )
    parser.add_argument(
        "--max-track-miss",
        type=int,
        default=8,
        help="How many frames to keep the last tracked box after detection is lost",
    )

    parser.add_argument(
        "--disable-printwindow",
        action="store_true",
        help="Disable PrintWindow capture and always use screen-region capture",
    )
    parser.add_argument(
        "--printwindow-test-threshold",
        type=float,
        default=5.0,
        help="Mean brightness threshold to judge whether PrintWindow is usable",
    )

    parser.add_argument("--query-host", default="127.0.0.1")
    parser.add_argument("--query-port", type=int, default=28766)
    parser.add_argument(
        "--disable-query-server",
        action="store_true",
        help="Disable local query server for player position requests",
    )

    return parser.parse_args()


# ============================================================
# Window helpers
# ============================================================

def normalize_title(s: str) -> str:
    s = (s or "").strip().lower()
    for ch in ["\u3000", "\t", "\r", "\n"]:
        s = s.replace(ch, " ")
    for ch in ["-", "_", ":", "：", "|", "/", "\\", "(", ")", "[", "]"]:
        s = s.replace(ch, " ")
    while "  " in s:
        s = s.replace("  ", " ")
    return s.strip()


def is_excluded_window_title(title: str) -> bool:
    t = normalize_title(title)
    exclude_keywords = [
        "cmd.exe",
        "powershell",
        "windows terminal",
        "python",
        "yolo26 person detection",
        "yolo player distance",
        "yolo player detect",
    ]
    return any(k in t for k in exclude_keywords)


def get_capture_region_from_hwnd(hwnd: int) -> Optional[dict]:
    try:
        if not win32gui.IsWindow(hwnd):
            return None

        title = win32gui.GetWindowText(hwnd)

        client_left, client_top, client_right, client_bottom = win32gui.GetClientRect(hwnd)
        client_width = client_right - client_left
        client_height = client_bottom - client_top

        if client_width <= 0 or client_height <= 0:
            return None

        screen_left, screen_top = win32gui.ClientToScreen(hwnd, (0, 0))

        return {
            "hwnd": hwnd,
            "title": title,
            "top": int(screen_top),
            "left": int(screen_left),
            "width": int(client_width),
            "height": int(client_height),
        }
    except Exception:
        log("[WARN ] get_capture_region_from_hwnd failed")
        log(traceback.format_exc())
        return None


def enum_candidate_windows():
    results = []

    def enum_handler(hwnd, _):
        if not win32gui.IsWindow(hwnd):
            return
        if not win32gui.IsWindowVisible(hwnd):
            return

        title = win32gui.GetWindowText(hwnd)
        if not title:
            return
        if is_excluded_window_title(title):
            return

        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            width = right - left
            height = bottom - top
            if width <= 0 or height <= 0:
                return

            client = get_capture_region_from_hwnd(hwnd)
            client_width = client["width"] if client is not None else 0
            client_height = client["height"] if client is not None else 0

            results.append(
                {
                    "hwnd": hwnd,
                    "title": title,
                    "left": left,
                    "top": top,
                    "width": width,
                    "height": height,
                    "client_width": client_width,
                    "client_height": client_height,
                }
            )
        except Exception:
            pass

    win32gui.EnumWindows(enum_handler, None)
    return results


def debug_print_all_windows() -> None:
    items = enum_candidate_windows()
    log("=== Visible Candidate Windows ===")
    for item in items:
        log(
            f"hwnd={item['hwnd']} title={item['title']!r} "
            f"rect=({item['left']},{item['top']},{item['width']},{item['height']}) "
            f"client=({item['client_width']}x{item['client_height']})"
        )
    log("=================================")


def _prefix_match_bonus(needle: str, hay: str) -> int:
    if not needle or not hay:
        return 0

    n = min(len(needle), len(hay))
    count = 0
    for i in range(n):
        if needle[i] != hay[i]:
            break
        count += 1
    return count * 50


def find_target_window(title_substring: str, debug: bool = False):
    needle_raw = title_substring or ""
    needle = normalize_title(needle_raw)
    needle_tokens = [t for t in needle.split(" ") if t]

    candidates = enum_candidate_windows()
    scored = []

    for item in candidates:
        hay_raw = item["title"]
        hay = normalize_title(hay_raw)
        score = None

        if hay == needle and needle:
            score = 100000
        elif needle and needle in hay:
            score = 80000 + len(needle) * 10
        elif needle_tokens:
            matched_tokens = sum(1 for t in needle_tokens if t in hay)
            if matched_tokens > 0:
                score = 50000 + matched_tokens * 3000
                score += _prefix_match_bonus(needle, hay)
                if hay.startswith(needle[: max(1, min(len(needle), 8))]):
                    score += 500

        if score is None:
            continue

        if item["client_width"] > 0 and item["client_height"] > 0:
            score += 10000
            score += (item["client_width"] * item["client_height"]) // 1000

        score += (item["width"] * item["height"]) // 10000
        scored.append((score, item))

    scored.sort(key=lambda x: x[0], reverse=True)

    if debug:
        log(f"[DEBUG] search needle raw  = {needle_raw!r}")
        log(f"[DEBUG] search needle norm = {needle!r}")
        if scored:
            log("[DEBUG] matched windows (sorted):")
            for score, item in scored[:10]:
                log(
                    f"  score={score} hwnd={item['hwnd']} "
                    f"title={item['title']!r} "
                    f"rect=({item['left']},{item['top']},{item['width']},{item['height']}) "
                    f"client=({item['client_width']}x{item['client_height']})"
                )
        else:
            log("[DEBUG] no matched windows")
            log("[DEBUG] visible candidates:")
            for item in candidates[:30]:
                log(
                    f"  hwnd={item['hwnd']} "
                    f"title={item['title']!r} "
                    f"rect=({item['left']},{item['top']},{item['width']},{item['height']}) "
                    f"client=({item['client_width']}x{item['client_height']})"
                )

    if not scored:
        return None

    best = scored[0][1]
    return (
        best["hwnd"],
        best["title"],
        best["left"],
        best["top"],
        best["width"],
        best["height"],
    )


# ============================================================
# PrintWindow / Capture
# ============================================================

def grab_window_printwindow(hwnd: int) -> Optional[np.ndarray]:
    if not hwnd or not win32gui.IsWindow(hwnd):
        return None

    w_dc = None
    mem_dc = None
    bmp = None
    old_obj = None

    try:
        rect = ctypes.wintypes.RECT()
        ok_rect = user32.GetClientRect(hwnd, ctypes.byref(rect))
        if not ok_rect:
            return None

        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width <= 0 or height <= 0:
            return None

        w_dc = user32.GetDC(hwnd)
        if not w_dc:
            return None

        mem_dc = gdi32.CreateCompatibleDC(w_dc)
        if not mem_dc:
            return None

        bmp = gdi32.CreateCompatibleBitmap(w_dc, width, height)
        if not bmp:
            return None

        old_obj = gdi32.SelectObject(mem_dc, bmp)

        flags = PW_CLIENTONLY | PW_RENDERFULLCONTENT
        ok = user32.PrintWindow(hwnd, mem_dc, flags)

        if not ok:
            gdi32.BitBlt(mem_dc, 0, 0, width, height, w_dc, 0, 0, SRCCOPY)

        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = width
        bmi.biHeight = -height
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = BI_RGB
        bmi.biSizeImage = width * height * 4

        buf = ctypes.create_string_buffer(width * height * 4)

        scanlines = gdi32.GetDIBits(
            mem_dc,
            bmp,
            0,
            height,
            buf,
            ctypes.byref(bmi),
            DIB_RGB_COLORS,
        )

        if scanlines != height:
            return None

        img = np.frombuffer(buf, dtype=np.uint8).reshape((height, width, 4))
        img = img[:, :, :3].copy()
        img = np.ascontiguousarray(img)
        return img

    except Exception:
        log("[WARN ] grab_window_printwindow failed")
        log(traceback.format_exc())
        return None

    finally:
        try:
            if old_obj and mem_dc:
                gdi32.SelectObject(mem_dc, old_obj)
        except Exception:
            pass
        try:
            if bmp:
                gdi32.DeleteObject(bmp)
        except Exception:
            pass
        try:
            if mem_dc:
                gdi32.DeleteDC(mem_dc)
        except Exception:
            pass
        try:
            if w_dc:
                user32.ReleaseDC(hwnd, w_dc)
        except Exception:
            pass


def is_printwindow_frame_usable(frame: Optional[np.ndarray], threshold: float) -> bool:
    if frame is None or frame.size == 0:
        return False
    mean_val = float(frame.mean())
    return mean_val > threshold


# ============================================================
# YOLO
# ============================================================

def load_model(model_path: Path) -> YOLO:
    if not model_path.exists():
        raise FileNotFoundError(f"Model not found: {model_path}")
    return YOLO(str(model_path))


def get_person_class_id(model: YOLO) -> int:
    names = model.names
    for class_id, class_name in names.items():
        if class_name == "person":
            return int(class_id)
    raise RuntimeError(f"'person' class not found in model labels: {names}")


def init_yolo(model_path: Path, conf: float, device: str, imgsz: int, slow_ms: float):
    with StepTimer("init_yolo.load_model", slow_ms):
        model = load_model(model_path)

    with StepTimer("init_yolo.get_person_class_id", slow_ms):
        person_class_id = get_person_class_id(model)

    warmup_img = np.zeros((imgsz, imgsz, 3), dtype=np.uint8)
    actual_device = device
    if str(device).lower() == "auto":
        actual_device = "0"

    try:
        with StepTimer(f"init_yolo.warmup[{actual_device}]-1", slow_ms):
            model.predict(
                warmup_img,
                conf=conf,
                device=actual_device,
                verbose=False,
                imgsz=imgsz,
            )
        for i in range(2):
            with StepTimer(f"init_yolo.warmup[{actual_device}]-extra{i+1}", slow_ms):
                model.predict(
                    warmup_img,
                    conf=conf,
                    device=actual_device,
                    verbose=False,
                    imgsz=imgsz,
                )
    except Exception as exc:
        if str(actual_device).lower() != "cpu":
            log(f"[WARN ] GPU init failed ({exc}) -> fallback to CPU")
            actual_device = "cpu"
            with StepTimer("init_yolo.warmup[cpu]", slow_ms):
                model.predict(
                    warmup_img,
                    conf=conf,
                    device="cpu",
                    verbose=False,
                    imgsz=imgsz,
                )
        else:
            raise

    return model, person_class_id, actual_device


def init_pose_yolo(model_path: Path, conf: float, device: str, imgsz: int, slow_ms: float):
    with StepTimer("init_pose_yolo.load_model", slow_ms):
        model = load_model(model_path)

    warmup_img = np.zeros((imgsz, imgsz, 3), dtype=np.uint8)
    actual_device = device
    if str(device).lower() == "auto":
        actual_device = "0"

    try:
        with StepTimer(f"init_pose_yolo.warmup[{actual_device}]-1", slow_ms):
            model.predict(
                warmup_img,
                conf=conf,
                device=actual_device,
                verbose=False,
                imgsz=imgsz,
            )
        for i in range(2):
            with StepTimer(f"init_pose_yolo.warmup[{actual_device}]-extra{i+1}", slow_ms):
                model.predict(
                    warmup_img,
                    conf=conf,
                    device=actual_device,
                    verbose=False,
                    imgsz=imgsz,
                )
    except Exception as exc:
        if str(actual_device).lower() != "cpu":
            log(f"[WARN ] pose GPU init failed ({exc}) -> fallback to CPU")
            actual_device = "cpu"
            with StepTimer("init_pose_yolo.warmup[cpu]", slow_ms):
                model.predict(
                    warmup_img,
                    conf=conf,
                    device="cpu",
                    verbose=False,
                    imgsz=imgsz,
                )
        else:
            raise

    return model, actual_device


# ============================================================
# Detection helpers
# ============================================================

def box_center(box_xyxy):
    x1, y1, x2, y2 = box_xyxy
    return ((float(x1) + float(x2)) * 0.5, (float(y1) + float(y2)) * 0.5)


def box_size_norm(box_xyxy, image_w: int, image_h: int):
    x1, y1, x2, y2 = box_xyxy
    bw = max(0.0, float(x2) - float(x1))
    bh = max(0.0, float(y2) - float(y1))
    if image_w <= 0 or image_h <= 0:
        return None, None, None
    bw_norm = bw / float(image_w)
    bh_norm = bh / float(image_h)
    area_norm = bw_norm * bh_norm
    return float(bw_norm), float(bh_norm), float(area_norm)


def extract_person_boxes_single_pass(
    model: YOLO,
    person_class_id: int,
    frame: np.ndarray,
    conf: float,
    imgsz: int,
    device: str,
):
    results = model.predict(
        source=frame,
        conf=conf,
        imgsz=imgsz,
        device=device,
        verbose=False,
    )

    boxes = []
    for result in results:
        if result.boxes is None:
            continue

        boxes_xyxy = result.boxes.xyxy.cpu().numpy()
        boxes_cls = result.boxes.cls.cpu().numpy().astype(int)
        boxes_conf = result.boxes.conf.cpu().numpy()

        for box, cls_id, score in zip(boxes_xyxy, boxes_cls, boxes_conf):
            if cls_id != person_class_id:
                continue
            x1, y1, x2, y2 = map(float, box)
            boxes.append({"xyxy": (x1, y1, x2, y2), "conf": float(score)})
    return boxes


def distance_to_image_center_score(box_xyxy, image_w: int, image_h: int) -> float:
    cx, cy = box_center(box_xyxy)
    dx = cx - (image_w * 0.5)
    dy = cy - (image_h * 0.5)
    return dx * dx + dy * dy


def sort_and_limit_boxes(boxes, image_w: int, image_h: int, max_count: int, center_priority: bool):
    if center_priority:
        boxes = sorted(
            boxes,
            key=lambda b: (
                distance_to_image_center_score(b["xyxy"], image_w, image_h),
                -b["conf"],
            ),
        )
    else:
        boxes = sorted(boxes, key=lambda b: -b["conf"])
    return boxes[:max_count]


# ============================================================
# Pose / Face helpers
# ============================================================

KP_NOSE = 0
KP_LEFT_EYE = 1
KP_RIGHT_EYE = 2
KP_LEFT_EAR = 3
KP_RIGHT_EAR = 4
KP_LEFT_SHOULDER = 5
KP_RIGHT_SHOULDER = 6


def _pick_valid_kpt(points_xy: np.ndarray, points_conf: np.ndarray, idx: int, min_conf: float):
    if points_xy is None or points_conf is None:
        return None
    if idx < 0 or idx >= len(points_xy):
        return None
    x, y = points_xy[idx]
    c = float(points_conf[idx])
    if not np.isfinite(x) or not np.isfinite(y):
        return None
    if c < min_conf:
        return None
    return float(x), float(y), c


def _mean_xy(items):
    if not items:
        return None
    xs = [p[0] for p in items]
    ys = [p[1] for p in items]
    cs = [p[2] for p in items]
    return float(np.mean(xs)), float(np.mean(ys)), float(np.mean(cs))


def extract_pose_people(
    model: YOLO,
    frame: np.ndarray,
    conf: float,
    imgsz: int,
    device: str,
    face_kpt_conf: float,
):
    results = model.predict(
        source=frame,
        conf=conf,
        imgsz=imgsz,
        device=device,
        verbose=False,
    )

    people = []

    for result in results:
        if result.boxes is None:
            continue
        if result.keypoints is None:
            continue

        boxes_xyxy = result.boxes.xyxy.cpu().numpy()
        boxes_conf = result.boxes.conf.cpu().numpy()

        kpt_xy = result.keypoints.xy.cpu().numpy() if result.keypoints.xy is not None else None
        if hasattr(result.keypoints, "conf") and result.keypoints.conf is not None:
            kpt_conf = result.keypoints.conf.cpu().numpy()
        else:
            kpt_conf = None

        if kpt_xy is None or kpt_conf is None:
            continue

        count = min(len(boxes_xyxy), len(kpt_xy), len(kpt_conf))
        for i in range(count):
            box = tuple(map(float, boxes_xyxy[i]))
            det_conf = float(boxes_conf[i])

            pts_xy = kpt_xy[i]
            pts_cf = kpt_conf[i]

            nose = _pick_valid_kpt(pts_xy, pts_cf, KP_NOSE, face_kpt_conf)
            left_eye = _pick_valid_kpt(pts_xy, pts_cf, KP_LEFT_EYE, face_kpt_conf)
            right_eye = _pick_valid_kpt(pts_xy, pts_cf, KP_RIGHT_EYE, face_kpt_conf)
            left_ear = _pick_valid_kpt(pts_xy, pts_cf, KP_LEFT_EAR, face_kpt_conf)
            right_ear = _pick_valid_kpt(pts_xy, pts_cf, KP_RIGHT_EAR, face_kpt_conf)
            left_shoulder = _pick_valid_kpt(pts_xy, pts_cf, KP_LEFT_SHOULDER, face_kpt_conf)
            right_shoulder = _pick_valid_kpt(pts_xy, pts_cf, KP_RIGHT_SHOULDER, face_kpt_conf)

            face_point = None
            face_source = "none"

            if nose is not None:
                face_point = (nose[0], nose[1], nose[2])
                face_source = "nose"
            else:
                eyes_mean = _mean_xy([p for p in [left_eye, right_eye] if p is not None])
                if eyes_mean is not None:
                    face_point = eyes_mean
                    face_source = "eyes_mean"
                else:
                    head_mean = _mean_xy([p for p in [left_eye, right_eye, left_ear, right_ear] if p is not None])
                    if head_mean is not None:
                        face_point = head_mean
                        face_source = "head_mean"
                    else:
                        shoulders_mean = _mean_xy([p for p in [left_shoulder, right_shoulder] if p is not None])
                        if shoulders_mean is not None:
                            x1, y1, x2, y2 = box
                            box_h = max(1.0, y2 - y1)
                            fx = shoulders_mean[0]
                            fy = shoulders_mean[1] - box_h * 0.18
                            fc = shoulders_mean[2] * 0.5
                            face_point = (float(fx), float(fy), float(fc))
                            face_source = "shoulder_est"

            people.append(
                {
                    "xyxy": box,
                    "conf": det_conf,
                    "face_point": face_point,
                    "face_source": face_source,
                    "nose": nose,
                    "left_eye": left_eye,
                    "right_eye": right_eye,
                    "left_ear": left_ear,
                    "right_ear": right_ear,
                    "left_shoulder": left_shoulder,
                    "right_shoulder": right_shoulder,
                }
            )

    return people


# ============================================================
# Single-eye tracking
# ============================================================

@dataclass
class TrackState:
    box: Optional[tuple] = None
    conf: float = 0.0
    miss_count: int = 0


@dataclass
class SharedPlayerState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    timestamp: float = 0.0
    available: bool = False
    track_mode: str = "lost"
    distance_m: Optional[float] = None
    distance_raw_m: Optional[float] = None
    disparity_px: Optional[float] = None
    category: str = "unknown"

    screen_x_px: Optional[float] = None
    screen_y_px: Optional[float] = None
    screen_x_norm: Optional[float] = None
    screen_y_norm: Optional[float] = None

    frame_width: int = 0
    frame_height: int = 0

    right_cx_px: Optional[float] = None
    right_cy_px: Optional[float] = None
    right_dx_px: Optional[float] = None
    right_dy_px: Optional[float] = None
    right_dx_norm: Optional[float] = None
    right_dy_norm: Optional[float] = None

    aim_source: str = "body"

    body_available: bool = False
    body_x_px: Optional[float] = None
    body_y_px: Optional[float] = None
    body_x_norm: Optional[float] = None
    body_y_norm: Optional[float] = None

    face_available: bool = False
    face_x_px: Optional[float] = None
    face_y_px: Optional[float] = None
    face_x_norm: Optional[float] = None
    face_y_norm: Optional[float] = None
    face_conf: Optional[float] = None
    face_source: str = "none"

    box_w_norm: Optional[float] = None
    box_h_norm: Optional[float] = None
    box_area_norm: Optional[float] = None

    def update(
        self,
        *,
        available: bool,
        track_mode: str,
        frame_width: int,
        frame_height: int,
        distance_m: Optional[float],
        distance_raw_m: Optional[float],
        disparity_px: Optional[float],
        category: str,
        screen_x_px: Optional[float],
        screen_y_px: Optional[float],
        screen_x_norm: Optional[float],
        screen_y_norm: Optional[float],
        right_cx_px: Optional[float],
        right_cy_px: Optional[float],
        right_dx_px: Optional[float],
        right_dy_px: Optional[float],
        right_dx_norm: Optional[float],
        right_dy_norm: Optional[float],
        aim_source: str,
        body_available: bool,
        body_x_px: Optional[float],
        body_y_px: Optional[float],
        body_x_norm: Optional[float],
        body_y_norm: Optional[float],
        face_available: bool,
        face_x_px: Optional[float],
        face_y_px: Optional[float],
        face_x_norm: Optional[float],
        face_y_norm: Optional[float],
        face_conf: Optional[float],
        face_source: str,
        box_w_norm: Optional[float],
        box_h_norm: Optional[float],
        box_area_norm: Optional[float],
    ) -> None:
        with self.lock:
            self.timestamp = time.time()
            self.available = available
            self.track_mode = track_mode
            self.frame_width = int(frame_width)
            self.frame_height = int(frame_height)
            self.distance_m = distance_m
            self.distance_raw_m = distance_raw_m
            self.disparity_px = disparity_px
            self.category = category

            self.screen_x_px = screen_x_px
            self.screen_y_px = screen_y_px
            self.screen_x_norm = screen_x_norm
            self.screen_y_norm = screen_y_norm

            self.right_cx_px = right_cx_px
            self.right_cy_px = right_cy_px
            self.right_dx_px = right_dx_px
            self.right_dy_px = right_dy_px
            self.right_dx_norm = right_dx_norm
            self.right_dy_norm = right_dy_norm

            self.aim_source = aim_source

            self.body_available = body_available
            self.body_x_px = body_x_px
            self.body_y_px = body_y_px
            self.body_x_norm = body_x_norm
            self.body_y_norm = body_y_norm

            self.face_available = face_available
            self.face_x_px = face_x_px
            self.face_y_px = face_y_px
            self.face_x_norm = face_x_norm
            self.face_y_norm = face_y_norm
            self.face_conf = face_conf
            self.face_source = face_source

            self.box_w_norm = box_w_norm
            self.box_h_norm = box_h_norm
            self.box_area_norm = box_area_norm

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "timestamp": self.timestamp,
                "available": self.available,
                "track_mode": self.track_mode,
                "distance_m": self.distance_m,
                "distance_raw_m": self.distance_raw_m,
                "disparity_px": self.disparity_px,
                "category": self.category,

                "screen_x_px": self.screen_x_px,
                "screen_y_px": self.screen_y_px,
                "screen_x_norm": self.screen_x_norm,
                "screen_y_norm": self.screen_y_norm,
                "frame_width": self.frame_width,
                "frame_height": self.frame_height,

                "right_cx_px": self.right_cx_px,
                "right_cy_px": self.right_cy_px,
                "right_dx_px": self.right_dx_px,
                "right_dy_px": self.right_dy_px,
                "right_dx_norm": self.right_dx_norm,
                "right_dy_norm": self.right_dy_norm,

                "aim_source": self.aim_source,

                "body_available": self.body_available,
                "body_x_px": self.body_x_px,
                "body_y_px": self.body_y_px,
                "body_x_norm": self.body_x_norm,
                "body_y_norm": self.body_y_norm,

                "face_available": self.face_available,
                "face_x_px": self.face_x_px,
                "face_y_px": self.face_y_px,
                "face_x_norm": self.face_x_norm,
                "face_y_norm": self.face_y_norm,
                "face_conf": self.face_conf,
                "face_source": self.face_source,

                "box_w_norm": self.box_w_norm,
                "box_h_norm": self.box_h_norm,
                "box_area_norm": self.box_area_norm,
            }


class PlayerQueryTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, handler_cls, shared_state: SharedPlayerState):
        super().__init__(server_address, handler_cls)
        self.shared_state = shared_state


class PlayerQueryHandler(socketserver.StreamRequestHandler):
    def handle(self):
        while True:
            line = self.rfile.readline()
            if not line:
                return

            cmd = line.decode("utf-8", errors="ignore").strip().upper()
            snap = self.server.shared_state.snapshot()

            if cmd == "PING":
                payload = {"ok": True, "reply": "PONG"}

            elif cmd == "GET PLAYER_POS":
                payload = {
                    "ok": bool(snap["available"]),
                    "timestamp": snap["timestamp"],
                    "track_mode": snap["track_mode"],
                    "aim_source": snap["aim_source"],
                    "screen": {
                        "x_px": snap["screen_x_px"],
                        "y_px": snap["screen_y_px"],
                        "x_norm": snap["screen_x_norm"],
                        "y_norm": snap["screen_y_norm"],
                        "frame_width": snap["frame_width"],
                        "frame_height": snap["frame_height"],
                        "box_w_norm": snap["box_w_norm"],
                        "box_h_norm": snap["box_h_norm"],
                        "box_area_norm": snap["box_area_norm"],
                    },
                    "body": {
                        "available": snap["body_available"],
                        "x_px": snap["body_x_px"],
                        "y_px": snap["body_y_px"],
                        "x_norm": snap["body_x_norm"],
                        "y_norm": snap["body_y_norm"],
                    },
                    "face": {
                        "available": snap["face_available"],
                        "x_px": snap["face_x_px"],
                        "y_px": snap["face_y_px"],
                        "x_norm": snap["face_x_norm"],
                        "y_norm": snap["face_y_norm"],
                        "conf": snap["face_conf"],
                        "source": snap["face_source"],
                    },
                    "right_offset": {
                        "dx_px": snap["right_dx_px"],
                        "dy_px": snap["right_dy_px"],
                        "dx_norm": snap["right_dx_norm"],
                        "dy_norm": snap["right_dy_norm"],
                    },
                }

            elif cmd == "GET PLAYER_DIST":
                payload = {
                    "ok": False,
                    "timestamp": snap["timestamp"],
                    "track_mode": snap["track_mode"],
                    "distance_m": None,
                    "distance_raw_m": None,
                    "disparity_px": None,
                    "category": "unknown",
                    "reason": "single_eye_mode_no_stereo_distance",
                }

            elif cmd == "GET PLAYER_INFO":
                payload = {
                    "ok": bool(snap["available"]),
                    "timestamp": snap["timestamp"],
                    "track_mode": snap["track_mode"],
                    "aim_source": snap["aim_source"],
                    "screen": {
                        "x_px": snap["screen_x_px"],
                        "y_px": snap["screen_y_px"],
                        "x_norm": snap["screen_x_norm"],
                        "y_norm": snap["screen_y_norm"],
                        "frame_width": snap["frame_width"],
                        "frame_height": snap["frame_height"],
                        "box_w_norm": snap["box_w_norm"],
                        "box_h_norm": snap["box_h_norm"],
                        "box_area_norm": snap["box_area_norm"],
                    },
                    "body": {
                        "available": snap["body_available"],
                        "x_px": snap["body_x_px"],
                        "y_px": snap["body_y_px"],
                        "x_norm": snap["body_x_norm"],
                        "y_norm": snap["body_y_norm"],
                    },
                    "face": {
                        "available": snap["face_available"],
                        "x_px": snap["face_x_px"],
                        "y_px": snap["face_y_px"],
                        "x_norm": snap["face_x_norm"],
                        "y_norm": snap["face_y_norm"],
                        "conf": snap["face_conf"],
                        "source": snap["face_source"],
                    },
                    "right_offset": {
                        "dx_px": snap["right_dx_px"],
                        "dy_px": snap["right_dy_px"],
                        "dx_norm": snap["right_dx_norm"],
                        "dy_norm": snap["right_dy_norm"],
                    },
                    "distance_m": None,
                    "distance_raw_m": None,
                    "disparity_px": None,
                    "category": "unknown",
                }

            else:
                payload = {
                    "ok": False,
                    "error": "unknown_command",
                    "supported": ["PING", "GET PLAYER_POS", "GET PLAYER_DIST", "GET PLAYER_INFO"],
                }

            self.wfile.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))


def start_query_server(host: str, port: int, shared_state: SharedPlayerState):
    server = PlayerQueryTCPServer((host, port), PlayerQueryHandler, shared_state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def compute_point_screen_position(point_xy, frame_width: int, frame_height: int):
    if point_xy is None:
        return None

    px, py = point_xy
    screen_x_px = float(px)
    screen_y_px = float(py)

    center_x = frame_width * 0.5
    center_y = frame_height * 0.5

    x_norm = 0.0
    y_norm = 0.0
    if center_x > 1e-6:
        x_norm = (screen_x_px - center_x) / center_x
    if center_y > 1e-6:
        y_norm = (screen_y_px - center_y) / center_y

    x_norm = float(np.clip(x_norm, -1.0, 1.0))
    y_norm = float(np.clip(y_norm, -1.0, 1.0))

    right_dx_px = float(screen_x_px - center_x)
    right_dy_px = float(screen_y_px - center_y)

    right_dx_norm = 0.0
    right_dy_norm = 0.0
    if center_x > 1e-6:
        right_dx_norm = right_dx_px / center_x
    if center_y > 1e-6:
        right_dy_norm = right_dy_px / center_y

    right_dx_norm = float(np.clip(right_dx_norm, -1.0, 1.0))
    right_dy_norm = float(np.clip(right_dy_norm, -1.0, 1.0))

    return {
        "screen_x_px": screen_x_px,
        "screen_y_px": screen_y_px,
        "screen_x_norm": x_norm,
        "screen_y_norm": y_norm,
        "right_cx_px": screen_x_px,
        "right_cy_px": screen_y_px,
        "right_dx_px": right_dx_px,
        "right_dy_px": right_dy_px,
        "right_dx_norm": right_dx_norm,
        "right_dy_norm": right_dy_norm,
    }


def compute_player_screen_position(box, frame_width: int, frame_height: int):
    if box is None:
        return None
    cx, cy = box_center(box)
    return compute_point_screen_position((cx, cy), frame_width, frame_height)


def iou_xyxy(box_a, box_b) -> float:
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b

    inter_x1 = max(ax1, bx1)
    inter_y1 = max(ay1, by1)
    inter_x2 = min(ax2, bx2)
    inter_y2 = min(ay2, by2)

    inter_w = max(0.0, inter_x2 - inter_x1)
    inter_h = max(0.0, inter_y2 - inter_y1)
    inter = inter_w * inter_h

    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter

    if union <= 1e-6:
        return 0.0
    return inter / union


def center_distance_sq(box_a, box_b) -> float:
    acx, acy = box_center(box_a)
    bcx, bcy = box_center(box_b)
    dx = acx - bcx
    dy = acy - bcy
    return dx * dx + dy * dy


def find_best_match_for_track(prev_box, current_boxes):
    if prev_box is None or not current_boxes:
        return None

    best_idx = None
    best_score = None

    for i, item in enumerate(current_boxes):
        box = item["xyxy"]
        iou = iou_xyxy(prev_box, box)
        dist2 = center_distance_sq(prev_box, box)
        score = dist2 - iou * 50000.0

        if best_score is None or score < best_score:
            best_score = score
            best_idx = i

    return best_idx


def update_track_with_detections(track: TrackState, boxes, max_miss: int = 8):
    found = False

    if boxes:
        idx = find_best_match_for_track(track.box, boxes)
        if idx is None:
            idx = 0
        chosen = boxes.pop(idx)
        track.box = chosen["xyxy"]
        track.conf = float(chosen["conf"])
        found = True

    if found:
        track.miss_count = 0
        return True, "detected"

    track.miss_count += 1
    if track.miss_count <= max_miss and track.box is not None:
        track.conf *= 0.90
        return True, "reused_last_frame"

    track.box = None
    track.conf = 0.0
    return False, "lost"


def find_best_pose_person_for_track(track_box, pose_people):
    if track_box is None or not pose_people:
        return None

    best = None
    best_score = None

    for person in pose_people:
        pbox = person["xyxy"]
        iou = iou_xyxy(track_box, pbox)
        dist2 = center_distance_sq(track_box, pbox)
        score = -(iou * 100000.0) + dist2
        if best_score is None or score < best_score:
            best_score = score
            best = person

    return best


def draw_track_box(img, track: TrackState, mode: str):
    color = (0, 200, 255) if mode == "detected" else (0, 120, 255)

    if track.box is not None:
        x1, y1, x2, y2 = map(int, track.box)
        cx, cy = box_center(track.box)
        cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
        cv2.circle(img, (int(cx), int(cy)), 4, color, -1)
        cv2.putText(
            img,
            f"TRACK {track.conf:.2f}",
            (x1, max(24, y1 - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            color,
            2,
            cv2.LINE_AA,
        )


def draw_boxes(img: np.ndarray, boxes, color, label_prefix: str):
    for i, b in enumerate(boxes):
        x1, y1, x2, y2 = map(int, b["xyxy"])
        cx, cy = box_center(b["xyxy"])

        cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
        cv2.circle(img, (int(cx), int(cy)), 4, color, -1)
        cv2.putText(
            img,
            f"{label_prefix}{i} {b['conf']:.2f}",
            (x1, max(24, y1 - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            color,
            2,
            cv2.LINE_AA,
        )


def draw_pose_debug(img, pose_person):
    if pose_person is None:
        return

    face_point = pose_person.get("face_point")
    face_source = pose_person.get("face_source", "none")

    if face_point is not None:
        fx, fy, fc = face_point
        cv2.circle(img, (int(fx), int(fy)), 6, (0, 80, 255), -1)
        cv2.putText(
            img,
            f"FACE {face_source} {fc:.2f}",
            (int(fx) + 8, int(fy) - 8),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (0, 80, 255),
            2,
            cv2.LINE_AA,
        )

    for key in ["nose", "left_eye", "right_eye", "left_ear", "right_ear"]:
        p = pose_person.get(key)
        if p is not None:
            x, y, _ = p
            cv2.circle(img, (int(x), int(y)), 3, (255, 0, 255), -1)


# ============================================================
# Main
# ============================================================

def main() -> int:
    global _LOG_FILE_HANDLE, ENABLE_LOG, ENABLE_TRACE

    args = parse_args()
    ENABLE_TRACE = bool(args.trace)
    ENABLE_LOG = bool(args.trace or args.debug_windows or args.debug_match or args.debug_capture_info or args.log_file)

    if args.log_file:
        log_path = Path(args.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _LOG_FILE_HANDLE = open(log_path, "w", encoding="utf-8")
        log(f"[INFO ] logging to {log_path}")

    debug_dir = REPO_ROOT / args.debug_dir
    debug_dir.mkdir(parents=True, exist_ok=True)

    log("[INFO ] detect_player_dist.py start (single-eye right-eye mode)")
    log(f"[INFO ] args={vars(args)}")

    if args.debug_windows:
        with StepTimer("debug_print_all_windows", args.slow_ms):
            debug_print_all_windows()

    model_path = DETECT_MODEL_MAP[args.model]
    pose_model_path = Path(args.pose_model)

    try:
        detect_model, person_class_id, actual_device = init_yolo(
            model_path=model_path,
            conf=args.conf,
            device=args.device,
            imgsz=args.imgsz,
            slow_ms=args.slow_ms,
        )
    except Exception as exc:
        log(f"[ERROR] Failed to init detect model: {exc!r}")
        log(traceback.format_exc())
        return 1

    try:
        pose_model, pose_device = init_pose_yolo(
            model_path=pose_model_path,
            conf=args.pose_conf,
            device=args.device,
            imgsz=args.imgsz,
            slow_ms=args.slow_ms,
        )
    except Exception as exc:
        log(f"[ERROR] Failed to init pose model: {exc!r}")
        log(traceback.format_exc())
        return 1

    log(f"[INFO ] detect_model.names : {detect_model.names}")
    log(f"[INFO ] person_class_id    : {person_class_id}")

    fallback_monitor = {
        "top": args.fallback_top,
        "left": args.fallback_left,
        "width": args.fallback_width,
        "height": args.fallback_height,
    }

    log("========================================")
    log(" YOLO Player Detect (Right Eye)")
    log("========================================")
    log(f"[INFO ] Detect model : {model_path}")
    log(f"[INFO ] Pose model   : {pose_model_path}")
    log(f"[INFO ] Device(req)  : {args.device}")
    log(f"[INFO ] Device(det)  : {actual_device}")
    log(f"[INFO ] Device(pose) : {pose_device}")
    log(f"[INFO ] Window title : {repr(args.window_title)}")
    log(f"[INFO ] PrintWindow  : {'OFF' if args.disable_printwindow else 'AUTO'}")
    log("[INFO ] Distance     : disabled (single-eye mode)")
    log("========================================")

    sct = mss.mss()
    prev_time = time.time()
    cv2.namedWindow(args.title, cv2.WINDOW_NORMAL)

    cached_hwnd: Optional[int] = args.hwnd if args.hwnd > 0 else None
    last_window_name = "N/A"
    debug_last_print = 0.0
    debug_capture_last_print = 0.0
    last_heartbeat = 0.0

    frame_index = 0
    prev_frame_small = None

    track = TrackState()
    track_mode = "lost"
    max_track_miss = args.max_track_miss

    shared_state = SharedPlayerState()
    query_server = None
    if not args.disable_query_server:
        try:
            query_server, _query_thread = start_query_server(
                args.query_host,
                args.query_port,
                shared_state,
            )
            log(f"[INFO ] query server listening on {args.query_host}:{args.query_port}")
        except Exception as exc:
            log(f"[WARN ] query server start failed: {exc!r}")

    printwindow_state = None
    printwindow_tested_hwnd = None

    while True:
        frame_index += 1
        loop_t0 = time.perf_counter()

        if time.time() - last_heartbeat >= args.heartbeat_sec:
            last_heartbeat = time.time()
            log(
                f"[HEART] frame={frame_index} cached_hwnd={cached_hwnd} "
                f"last_window={last_window_name!r} pw_state={printwindow_state}"
            )

        found = False
        monitor = fallback_monitor
        capture_mode = "screen_region"
        frame = None

        try:
            if cached_hwnd is not None:
                with StepTimer("get_capture_region_from_cached_hwnd", args.slow_ms):
                    info = get_capture_region_from_hwnd(cached_hwnd)
                if info is not None:
                    monitor = {
                        "top": info["top"],
                        "left": info["left"],
                        "width": info["width"],
                        "height": info["height"],
                    }
                    last_window_name = info["title"]
                    found = True
                else:
                    if args.hwnd > 0:
                        log(f"[WARN ] specified hwnd is invalid: {cached_hwnd}")
                    cached_hwnd = None
                    printwindow_state = None
                    printwindow_tested_hwnd = None

            if not found and args.hwnd <= 0:
                do_debug = args.debug_match and (time.time() - debug_last_print > 1.0)
                with StepTimer("find_target_window", args.slow_ms):
                    target_window = find_target_window(args.window_title, debug=do_debug)
                if do_debug:
                    debug_last_print = time.time()

                if target_window is not None:
                    hwnd, title, _left, _top, _width, _height = target_window
                    cached_hwnd = hwnd
                    printwindow_state = None
                    printwindow_tested_hwnd = None
                    log(f"[INFO ] target_window hwnd={hwnd} title={title!r}")

                    with StepTimer("get_capture_region_from_found_hwnd", args.slow_ms):
                        info = get_capture_region_from_hwnd(hwnd)

                    if info is not None:
                        monitor = {
                            "top": info["top"],
                            "left": info["left"],
                            "width": info["width"],
                            "height": info["height"],
                        }
                        last_window_name = info["title"]
                        found = True
                    else:
                        last_window_name = "fallback(client_rect_error)"
                else:
                    last_window_name = "fallback(not_found)"

            if (
                not args.disable_printwindow
                and cached_hwnd is not None
                and win32gui.IsWindow(cached_hwnd)
            ):
                if printwindow_tested_hwnd != cached_hwnd or printwindow_state is None:
                    with StepTimer("printwindow_test", args.slow_ms):
                        test_frame = grab_window_printwindow(cached_hwnd)
                    printwindow_tested_hwnd = cached_hwnd
                    printwindow_state = is_printwindow_frame_usable(
                        test_frame,
                        threshold=args.printwindow_test_threshold,
                    )
                    if printwindow_state:
                        log(
                            f"[INFO ] PrintWindow usable "
                            f"(mean={float(test_frame.mean()):.2f}, shape={test_frame.shape})"
                        )
                        frame = test_frame
                        capture_mode = "printwindow"
                    else:
                        if test_frame is None:
                            log("[WARN ] PrintWindow test failed: frame is None -> fallback to mss")
                        else:
                            log(
                                f"[WARN ] PrintWindow test returned dark frame "
                                f"(mean={float(test_frame.mean()):.2f}) -> fallback to mss"
                            )

                elif printwindow_state:
                    with StepTimer("grab_window_printwindow", args.slow_ms):
                        pw_frame = grab_window_printwindow(cached_hwnd)
                    if is_printwindow_frame_usable(pw_frame, threshold=2.0):
                        frame = pw_frame
                        capture_mode = "printwindow"
                    else:
                        log("[WARN ] PrintWindow returned unusable frame this loop -> fallback to mss")

            if frame is None:
                with StepTimer("mss.grab", args.slow_ms):
                    screenshot = sct.grab(monitor)

                with StepTimer("mss.to_numpy", args.slow_ms):
                    frame = np.array(screenshot, dtype=np.uint8)[:, :, :3]
                    frame = np.ascontiguousarray(frame)
                    capture_mode = "screen_region"

            with StepTimer("frame_diff_calc", args.slow_ms):
                small = cv2.resize(frame, (160, 90), interpolation=cv2.INTER_AREA)
                gray_small = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

                if prev_frame_small is None:
                    frame_diff_mean = -1.0
                else:
                    diff = cv2.absdiff(gray_small, prev_frame_small)
                    frame_diff_mean = float(diff.mean())

                prev_frame_small = gray_small

            frame_mean = float(frame.mean())
            frame_std = float(frame.std())
            log(
                f"[TRACE] capture_mode={capture_mode} frame.shape={frame.shape} "
                f"mean={frame_mean:.2f} std={frame_std:.2f} diff={frame_diff_mean:.3f}"
            )

            if args.debug_capture_info and (time.time() - debug_capture_last_print > 1.0):
                debug_capture_last_print = time.time()
                log(f"[DEBUG] capture_mode={capture_mode}")
                if cached_hwnd is not None:
                    log(f"[DEBUG] hwnd={cached_hwnd}")
                log(f"[DEBUG] frame.shape={frame.shape}")
                log(f"[DEBUG] frame_diff_mean={frame_diff_mean:.3f}")
                log(f"[DEBUG] printwindow_state={printwindow_state}")

            frame_h, frame_w = frame.shape[:2]

            with StepTimer("extract_person_boxes_single_pass", args.slow_ms):
                boxes = extract_person_boxes_single_pass(
                    model=detect_model,
                    person_class_id=person_class_id,
                    frame=frame,
                    conf=args.conf,
                    imgsz=args.imgsz,
                    device=actual_device,
                )
            log(f"[TRACE] boxes={len(boxes)}")

            if len(boxes) == 0 and args.save_zero_detect_every > 0:
                if frame_index % args.save_zero_detect_every == 0:
                    debug_path = debug_dir / f"frame_{frame_index:06d}.png"
                    cv2.imwrite(str(debug_path), frame)
                    log(f"[DEBUG] saved zero-detect frame : {debug_path}")

                    h, w = frame.shape[:2]
                    cx1 = int(w * 0.25)
                    cy1 = int(h * 0.20)
                    cx2 = int(w * 0.75)
                    cy2 = int(h * 0.80)
                    center_crop = frame[cy1:cy2, cx1:cx2].copy()

                    with StepTimer("center_crop_recheck", args.slow_ms):
                        crop_boxes = extract_person_boxes_single_pass(
                            model=detect_model,
                            person_class_id=person_class_id,
                            frame=center_crop,
                            conf=max(0.15, args.conf - 0.10),
                            imgsz=max(args.imgsz, 960),
                            device=actual_device,
                        )
                    log(
                        f"[DEBUG] center_crop_boxes={len(crop_boxes)} "
                        f"crop_shape={center_crop.shape}"
                    )

            with StepTimer("sort_and_limit_boxes", args.slow_ms):
                boxes = sort_and_limit_boxes(
                    boxes, frame_w, frame_h, args.max_persons, args.center_priority
                )

            log(f"[TRACE] boxes_limited={len(boxes)}")

            with StepTimer("copy_visual_frame", args.slow_ms):
                vis = frame.copy()

            with StepTimer("draw_boxes", args.slow_ms):
                draw_boxes(vis, boxes, (0, 255, 0), "P")

            with StepTimer("track_update", args.slow_ms):
                boxes_for_track = [dict(b) for b in boxes]
                track_ok, track_mode = update_track_with_detections(
                    track,
                    boxes_for_track,
                    max_miss=max_track_miss,
                )

            if track_ok and track.box is not None:
                with StepTimer("draw_track_box", args.slow_ms):
                    draw_track_box(vis, track, track_mode)

                log(
                    f"[TRACE] track_mode={track_mode} miss_count={track.miss_count} "
                    f"box={track.box} conf={track.conf:.3f}"
                )
            else:
                log(f"[TRACE] track_mode={track_mode} miss_count={track.miss_count}")

            pose_people = []
            matched_pose_person = None
            with StepTimer("extract_pose_people", args.slow_ms):
                pose_people = extract_pose_people(
                    model=pose_model,
                    frame=frame,
                    conf=args.pose_conf,
                    imgsz=args.imgsz,
                    device=pose_device,
                    face_kpt_conf=args.face_kpt_conf,
                )
            log(f"[TRACE] pose_people={len(pose_people)}")

            if track_ok and track.box is not None and pose_people:
                matched_pose_person = find_best_pose_person_for_track(track.box, pose_people)

            body_pos = None
            box_w_norm = None
            box_h_norm = None
            box_area_norm = None
            if track_ok and track.box is not None:
                body_pos = compute_player_screen_position(track.box, frame_w, frame_h)
                box_w_norm, box_h_norm, box_area_norm = box_size_norm(track.box, frame_w, frame_h)

            face_pos = None
            face_available = False
            face_conf = None
            face_source = "none"

            if matched_pose_person is not None:
                draw_pose_debug(vis, matched_pose_person)
                fp = matched_pose_person.get("face_point")
                if fp is not None:
                    face_pos = compute_point_screen_position((fp[0], fp[1]), frame_w, frame_h)
                    face_available = True
                    face_conf = float(fp[2])
                    face_source = matched_pose_person.get("face_source", "none")

            main_pos = face_pos if face_pos is not None else body_pos
            aim_source = "face" if face_pos is not None else "body"

            shared_state.update(
                available=bool(track_ok and main_pos is not None),
                track_mode=track_mode,
                frame_width=frame_w,
                frame_height=frame_h,
                distance_m=None,
                distance_raw_m=None,
                disparity_px=None,
                category="unknown",
                screen_x_px=main_pos["screen_x_px"] if main_pos is not None else None,
                screen_y_px=main_pos["screen_y_px"] if main_pos is not None else None,
                screen_x_norm=main_pos["screen_x_norm"] if main_pos is not None else None,
                screen_y_norm=main_pos["screen_y_norm"] if main_pos is not None else None,
                right_cx_px=main_pos["right_cx_px"] if main_pos is not None else None,
                right_cy_px=main_pos["right_cy_px"] if main_pos is not None else None,
                right_dx_px=main_pos["right_dx_px"] if main_pos is not None else None,
                right_dy_px=main_pos["right_dy_px"] if main_pos is not None else None,
                right_dx_norm=main_pos["right_dx_norm"] if main_pos is not None else None,
                right_dy_norm=main_pos["right_dy_norm"] if main_pos is not None else None,
                aim_source=aim_source,
                body_available=bool(body_pos is not None),
                body_x_px=body_pos["screen_x_px"] if body_pos is not None else None,
                body_y_px=body_pos["screen_y_px"] if body_pos is not None else None,
                body_x_norm=body_pos["screen_x_norm"] if body_pos is not None else None,
                body_y_norm=body_pos["screen_y_norm"] if body_pos is not None else None,
                face_available=face_available,
                face_x_px=face_pos["screen_x_px"] if face_pos is not None else None,
                face_y_px=face_pos["screen_y_px"] if face_pos is not None else None,
                face_x_norm=face_pos["screen_x_norm"] if face_pos is not None else None,
                face_y_norm=face_pos["screen_y_norm"] if face_pos is not None else None,
                face_conf=face_conf,
                face_source=face_source,
                box_w_norm=box_w_norm,
                box_h_norm=box_h_norm,
                box_area_norm=box_area_norm,
            )

            now = time.time()
            fps = 1.0 / max(now - prev_time, 1e-6)
            prev_time = now

            with StepTimer("draw_overlay", args.slow_ms):
                cv2.putText(vis, f"FPS: {fps:.1f}", (20, 40),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 255), 2, cv2.LINE_AA)
                cv2.putText(vis, f"Persons: {len(boxes)}", (20, 75),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2, cv2.LINE_AA)
                cv2.putText(vis, f"PosePeople: {len(pose_people)}", (20, 110),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2, cv2.LINE_AA)
                cv2.putText(vis, f"CaptureMode: {capture_mode}", (20, 145),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2, cv2.LINE_AA)
                cv2.putText(vis, f"Window: {last_window_name[:70]}", (20, 175),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2, cv2.LINE_AA)
                cv2.putText(vis, f"Capture: {monitor['left']},{monitor['top']} {monitor['width']}x{monitor['height']}", (20, 205),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2, cv2.LINE_AA)

                if cached_hwnd is not None:
                    cv2.putText(vis, f"HWND: {cached_hwnd}", (20, 235),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2, cv2.LINE_AA)

                cv2.putText(vis, f"FrameDiff: {frame_diff_mean:.3f}", (20, 265),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2, cv2.LINE_AA)

                pw_text = f"PrintWindow: {printwindow_state}"
                cv2.putText(vis, pw_text, (20, 295),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2, cv2.LINE_AA)

                y_base = 330

                cv2.putText(
                    vis,
                    "Distance: N/A (single-eye mode)",
                    (20, y_base),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.75,
                    (0, 180, 255),
                    2,
                    cv2.LINE_AA,
                )

                cv2.putText(
                    vis,
                    f"TrackMode: {track_mode} miss={track.miss_count}",
                    (20, y_base + 30),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.7,
                    (0, 255, 255) if track_mode == "detected" else (0, 180, 255),
                    2,
                    cv2.LINE_AA,
                )

                cv2.putText(
                    vis,
                    f"AimSource: {aim_source}",
                    (20, y_base + 60),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.7,
                    (0, 220, 255) if aim_source == "face" else (255, 220, 0),
                    2,
                    cv2.LINE_AA,
                )

                if body_pos is not None:
                    cv2.putText(
                        vis,
                        f"BodyXY: ({body_pos['screen_x_px']:.1f}, {body_pos['screen_y_px']:.1f}) px",
                        (20, y_base + 90),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.7,
                        (100, 255, 255),
                        2,
                        cv2.LINE_AA,
                    )
                    cv2.putText(
                        vis,
                        f"BodyNorm: ({body_pos['screen_x_norm']:+.3f}, {body_pos['screen_y_norm']:+.3f})",
                        (20, y_base + 120),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.7,
                        (100, 255, 255),
                        2,
                        cv2.LINE_AA,
                    )
                    cv2.putText(
                        vis,
                        f"BoxNorm: w={box_w_norm:.3f} h={box_h_norm:.3f} area={box_area_norm:.4f}",
                        (20, y_base + 150),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.7,
                        (100, 255, 180),
                        2,
                        cv2.LINE_AA,
                    )
                else:
                    cv2.putText(
                        vis,
                        "BodyXY: N/A",
                        (20, y_base + 90),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.7,
                        (100, 255, 255),
                        2,
                        cv2.LINE_AA,
                    )

                if face_pos is not None:
                    cv2.putText(
                        vis,
                        f"FaceXY: ({face_pos['screen_x_px']:.1f}, {face_pos['screen_y_px']:.1f}) px",
                        (20, y_base + 180),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.7,
                        (0, 80, 255),
                        2,
                        cv2.LINE_AA,
                    )
                    cv2.putText(
                        vis,
                        f"FaceNorm: ({face_pos['screen_x_norm']:+.3f}, {face_pos['screen_y_norm']:+.3f})",
                        (20, y_base + 210),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.7,
                        (0, 80, 255),
                        2,
                        cv2.LINE_AA,
                    )
                    cv2.putText(
                        vis,
                        f"FaceMeta: conf={face_conf:.2f} src={face_source}",
                        (20, y_base + 240),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.7,
                        (0, 80, 255),
                        2,
                        cv2.LINE_AA,
                    )
                else:
                    cv2.putText(
                        vis,
                        "FaceXY: N/A",
                        (20, y_base + 180),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.7,
                        (0, 80, 255),
                        2,
                        cv2.LINE_AA,
                    )

                if main_pos is not None:
                    cv2.putText(
                        vis,
                        f"MainOffsetNorm: ({main_pos['right_dx_norm']:+.3f}, {main_pos['right_dy_norm']:+.3f})",
                        (20, y_base + 270),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.7,
                        (255, 180, 0),
                        2,
                        cv2.LINE_AA,
                    )

            with StepTimer("imshow", args.slow_ms):
                cv2.imshow(args.title, vis)

            with StepTimer("waitKey", args.slow_ms):
                key = cv2.waitKey(1) & 0xFF

            loop_ms = (time.perf_counter() - loop_t0) * 1000.0
            if loop_ms >= args.slow_ms:
                log(f"[SLOW ] loop frame={frame_index} total={loop_ms:.1f} ms")
            else:
                log(f"[TRACE] loop frame={frame_index} total={loop_ms:.1f} ms")

            if key in (27, ord("q"), ord("Q")):
                log("[INFO ] quit key detected")
                break

        except KeyboardInterrupt:
            log("[WARN ] KeyboardInterrupt")
            break
        except Exception as exc:
            log(f"[ERROR] main loop exception: {exc!r}")
            log(traceback.format_exc())
            time.sleep(0.2)

    cv2.destroyAllWindows()
    log("[INFO ] cv2.destroyAllWindows done")

    try:
        if query_server is not None:
            query_server.shutdown()
            query_server.server_close()
            log("[INFO ] query server shutdown done")
    except Exception:
        log("[WARN ] query server shutdown failed")
        log(traceback.format_exc())

    if _LOG_FILE_HANDLE is not None:
        _LOG_FILE_HANDLE.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())