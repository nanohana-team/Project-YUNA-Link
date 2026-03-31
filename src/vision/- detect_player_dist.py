# src/vision/detect_player_dist.py
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes
import sys
import time
import traceback
from dataclasses import dataclass
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
        description="Detect player distance from stereo VR window using PrintWindow or screen-region fallback"
    )
    parser.add_argument("--model", choices=["n", "s", "m", "l", "x"], default="n")
    parser.add_argument("--conf", type=float, default=0.35)
    parser.add_argument("--imgsz", type=int, default=320)
    parser.add_argument("--device", default="0")

    parser.add_argument("--window-title", default=DEFAULT_WINDOW_TITLE)
    parser.add_argument("--hwnd", type=int, default=0, help="Use specific hwnd directly")

    parser.add_argument("--fallback-top", type=int, default=0)
    parser.add_argument("--fallback-left", type=int, default=0)
    parser.add_argument("--fallback-width", type=int, default=1920)
    parser.add_argument("--fallback-height", type=int, default=1080)

    parser.add_argument("--title", default="YOLO Player Distance")
    parser.add_argument("--baseline-m", type=float, default=0.064)
    parser.add_argument("--h-fov-deg", type=float, default=100.0)
    parser.add_argument("--min-disparity-px", type=float, default=1.0)
    parser.add_argument("--max-per-eye", type=int, default=6)
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
        help="How many frames to keep the last tracked stereo pair after detection is lost",
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


# ============================================================
# Stereo helpers
# ============================================================

def split_left_right(frame: np.ndarray):
    h, w = frame.shape[:2]
    mid = w // 2
    return frame[:, :mid], frame[:, mid:]


def box_center(box_xyxy):
    x1, y1, x2, y2 = box_xyxy
    return ((float(x1) + float(x2)) * 0.5, (float(y1) + float(y2)) * 0.5)


def box_size(box_xyxy):
    x1, y1, x2, y2 = box_xyxy
    return (max(0.0, float(x2) - float(x1)), max(0.0, float(y2) - float(y1)))


def focal_length_px_from_fov(width_px: int, fov_deg: float) -> float:
    fov_rad = np.deg2rad(fov_deg)
    return (width_px / 2.0) / np.tan(fov_rad / 2.0)


def calc_distance_from_points(
    left_x: float,
    right_x: float,
    image_width_px: int,
    h_fov_deg: float,
    baseline_m: float,
    min_disparity_px: float = 1.0,
):
    focal_px = focal_length_px_from_fov(image_width_px, h_fov_deg)
    disparity_px = float(left_x) - float(right_x)
    d = abs(disparity_px)

    if d < float(min_disparity_px):
        distance_m = None
    else:
        distance_m = (float(focal_px) * float(baseline_m)) / d

    if distance_m is None:
        category = "unknown"
    elif distance_m < 1.0:
        category = "near"
    elif distance_m < 2.5:
        category = "mid"
    else:
        category = "far"

    return {
        "disparity_px": disparity_px,
        "distance_m": distance_m,
        "category": category,
        "focal_px": focal_px,
    }


# ============================================================
# Detection
# ============================================================

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


def split_global_boxes_to_eyes(boxes, frame_width: int):
    mid = frame_width // 2
    left_boxes = []
    right_boxes = []

    for b in boxes:
        x1, y1, x2, y2 = b["xyxy"]
        cx, _cy = box_center(b["xyxy"])

        if cx < mid:
            left_boxes.append({"xyxy": (x1, y1, x2, y2), "conf": b["conf"]})
        else:
            right_boxes.append({"xyxy": (x1 - mid, y1, x2 - mid, y2), "conf": b["conf"]})

    return left_boxes, right_boxes


# ============================================================
# Short-term tracking / interpolation
# ============================================================

@dataclass
class TrackState:
    left_box: Optional[tuple] = None
    right_box: Optional[tuple] = None
    left_conf: float = 0.0
    right_conf: float = 0.0
    miss_count: int = 0
    last_distance_m: Optional[float] = None
    last_disparity_px: Optional[float] = None


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


def update_track_with_detections(track: TrackState, left_boxes, right_boxes, max_miss: int = 8):
    left_found = False
    right_found = False

    if left_boxes:
        idx = find_best_match_for_track(track.left_box, left_boxes)
        if idx is None:
            idx = 0
        chosen = left_boxes.pop(idx)
        track.left_box = chosen["xyxy"]
        track.left_conf = float(chosen["conf"])
        left_found = True

    if right_boxes:
        idx = find_best_match_for_track(track.right_box, right_boxes)
        if idx is None:
            idx = 0
        chosen = right_boxes.pop(idx)
        track.right_box = chosen["xyxy"]
        track.right_conf = float(chosen["conf"])
        right_found = True

    if left_found and right_found:
        track.miss_count = 0
        return True, "detected"

    if left_found or right_found:
        track.miss_count += 1
        if track.miss_count <= max_miss and track.left_box is not None and track.right_box is not None:
            track.left_conf *= 0.92
            track.right_conf *= 0.92
            return True, "partial_interpolate"
        return False, "partial_lost"

    track.miss_count += 1
    if track.miss_count <= max_miss and track.left_box is not None and track.right_box is not None:
        track.left_conf *= 0.88
        track.right_conf *= 0.88
        return True, "reused_last_frame"

    track.left_box = None
    track.right_box = None
    track.left_conf = 0.0
    track.right_conf = 0.0
    track.last_distance_m = None
    track.last_disparity_px = None
    return False, "lost"


def draw_track_boxes(left_img, right_img, track: TrackState, mode: str):
    color = (0, 200, 255) if mode == "detected" else (0, 120, 255)

    if track.left_box is not None:
        x1, y1, x2, y2 = map(int, track.left_box)
        cx, cy = box_center(track.left_box)
        cv2.rectangle(left_img, (x1, y1), (x2, y2), color, 2)
        cv2.circle(left_img, (int(cx), int(cy)), 4, color, -1)
        cv2.putText(
            left_img,
            f"TRACK L {track.left_conf:.2f}",
            (x1, max(24, y1 - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            color,
            2,
            cv2.LINE_AA,
        )

    if track.right_box is not None:
        x1, y1, x2, y2 = map(int, track.right_box)
        cx, cy = box_center(track.right_box)
        cv2.rectangle(right_img, (x1, y1), (x2, y2), color, 2)
        cv2.circle(right_img, (int(cx), int(cy)), 4, color, -1)
        cv2.putText(
            right_img,
            f"TRACK R {track.right_conf:.2f}",
            (x1, max(24, y1 - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            color,
            2,
            cv2.LINE_AA,
        )


# ============================================================
# Draw
# ============================================================

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

    log("[INFO ] detect_player_dist.py start")
    log(f"[INFO ] args={vars(args)}")

    if args.debug_windows:
        with StepTimer("debug_print_all_windows", args.slow_ms):
            debug_print_all_windows()

    model_path = DETECT_MODEL_MAP[args.model]

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

    log(f"[INFO ] model.names    : {detect_model.names}")
    log(f"[INFO ] person_class_id: {person_class_id}")

    fallback_monitor = {
        "top": args.fallback_top,
        "left": args.fallback_left,
        "width": args.fallback_width,
        "height": args.fallback_height,
    }

    log("========================================")
    log(" YOLO Player Distance")
    log("========================================")
    log(f"[INFO ] Detect model : {model_path}")
    log(f"[INFO ] Device(req)  : {args.device}")
    log(f"[INFO ] Device(real) : {actual_device}")
    log(f"[INFO ] Window title : {repr(args.window_title)}")
    log(f"[INFO ] Baseline[m]  : {args.baseline_m}")
    log(f"[INFO ] H-FOV[deg]   : {args.h_fov_deg}")
    log(f"[INFO ] PrintWindow  : {'OFF' if args.disable_printwindow else 'AUTO'}")
    log("========================================")

    sct = mss.mss()
    prev_time = time.time()
    cv2.namedWindow(args.title, cv2.WINDOW_NORMAL)

    cached_hwnd: Optional[int] = args.hwnd if args.hwnd > 0 else None
    last_window_name = "N/A"
    debug_last_print = 0.0
    debug_capture_last_print = 0.0
    last_heartbeat = 0.0

    ema_distance = None
    ema_alpha = 0.25
    frame_index = 0
    prev_frame_small = None

    track = TrackState()
    track_mode = "lost"
    max_track_miss = args.max_track_miss

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

            with StepTimer("split_left_right", args.slow_ms):
                left_frame, right_frame = split_left_right(frame)
                left_h, left_w = left_frame.shape[:2]
                right_h, right_w = right_frame.shape[:2]

            with StepTimer("extract_person_boxes_single_pass", args.slow_ms):
                global_boxes = extract_person_boxes_single_pass(
                    model=detect_model,
                    person_class_id=person_class_id,
                    frame=frame,
                    conf=args.conf,
                    imgsz=args.imgsz,
                    device=actual_device,
                )
            log(f"[TRACE] global_boxes={len(global_boxes)}")

            if len(global_boxes) == 0 and args.save_zero_detect_every > 0:
                if frame_index % args.save_zero_detect_every == 0:
                    debug_path = debug_dir / f"frame_{frame_index:06d}.png"
                    left_path = debug_dir / f"frame_{frame_index:06d}_L.png"
                    right_path = debug_dir / f"frame_{frame_index:06d}_R.png"
                    cv2.imwrite(str(debug_path), frame)
                    cv2.imwrite(str(left_path), left_frame)
                    cv2.imwrite(str(right_path), right_frame)
                    log(f"[DEBUG] saved zero-detect frame : {debug_path}")
                    log(f"[DEBUG] saved zero-detect left  : {left_path}")
                    log(f"[DEBUG] saved zero-detect right : {right_path}")

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
                            imgsz=max(args.imgsz, 640),
                            device=actual_device,
                        )
                    log(
                        f"[DEBUG] center_crop_boxes={len(crop_boxes)} "
                        f"crop_shape={center_crop.shape}"
                    )

            with StepTimer("split_global_boxes_to_eyes", args.slow_ms):
                left_boxes, right_boxes = split_global_boxes_to_eyes(global_boxes, frame.shape[1])

            with StepTimer("sort_and_limit_boxes[left]", args.slow_ms):
                left_boxes = sort_and_limit_boxes(
                    left_boxes, left_w, left_h, args.max_per_eye, args.center_priority
                )
            with StepTimer("sort_and_limit_boxes[right]", args.slow_ms):
                right_boxes = sort_and_limit_boxes(
                    right_boxes, right_w, right_h, args.max_per_eye, args.center_priority
                )

            log(f"[TRACE] left_boxes={len(left_boxes)} right_boxes={len(right_boxes)}")

            with StepTimer("copy_visual_frames", args.slow_ms):
                vis_left = left_frame.copy()
                vis_right = right_frame.copy()

            with StepTimer("draw_boxes[left]", args.slow_ms):
                draw_boxes(vis_left, left_boxes, (0, 255, 0), "L")
            with StepTimer("draw_boxes[right]", args.slow_ms):
                draw_boxes(vis_right, right_boxes, (255, 0, 0), "R")

            with StepTimer("track_update", args.slow_ms):
                left_boxes_for_track = [dict(b) for b in left_boxes]
                right_boxes_for_track = [dict(b) for b in right_boxes]
                track_ok, track_mode = update_track_with_detections(
                    track,
                    left_boxes_for_track,
                    right_boxes_for_track,
                    max_miss=max_track_miss,
                )

            raw_distance_m = None
            disparity_px = None
            category = "unknown"

            if track_ok and track.left_box is not None and track.right_box is not None:
                with StepTimer("calc_distance_from_track", args.slow_ms):
                    lcx, _lcy = box_center(track.left_box)
                    rcx, _rcy = box_center(track.right_box)

                    result = calc_distance_from_points(
                        left_x=lcx,
                        right_x=rcx,
                        image_width_px=left_w,
                        h_fov_deg=args.h_fov_deg,
                        baseline_m=args.baseline_m,
                        min_disparity_px=args.min_disparity_pxx if hasattr(args, "min_disparity_pxx") else args.min_disparity_px,
                    )

                disparity_px = result["disparity_px"]
                raw_distance_m = result["distance_m"]
                category = result["category"]

                if raw_distance_m is not None:
                    if ema_distance is None:
                        ema_distance = raw_distance_m
                    else:
                        ema_distance = ema_alpha * raw_distance_m + (1.0 - ema_alpha) * ema_distance

                    track.last_distance_m = ema_distance
                    track.last_disparity_px = disparity_px

                with StepTimer("draw_track_boxes", args.slow_ms):
                    draw_track_boxes(vis_left, vis_right, track, track_mode)

                log(
                    f"[TRACE] track_mode={track_mode} miss_count={track.miss_count} "
                    f"disp={disparity_px:.2f} raw={raw_distance_m} smooth={ema_distance}"
                )
            else:
                log(f"[TRACE] track_mode={track_mode} miss_count={track.miss_count}")

            with StepTimer("hstack", args.slow_ms):
                combined = np.hstack([vis_left, vis_right])

            now = time.time()
            fps = 1.0 / max(now - prev_time, 1e-6)
            prev_time = now

            with StepTimer("draw_overlay", args.slow_ms):
                cv2.putText(combined, f"FPS: {fps:.1f}", (20, 40),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 255), 2, cv2.LINE_AA)
                cv2.putText(combined, f"L persons: {len(left_boxes)} / R persons: {len(right_boxes)}", (20, 75),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2, cv2.LINE_AA)
                cv2.putText(combined, f"CaptureMode: {capture_mode}", (20, 110),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2, cv2.LINE_AA)
                cv2.putText(combined, f"Window: {last_window_name[:70]}", (20, 140),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2, cv2.LINE_AA)
                cv2.putText(combined, f"Capture: {monitor['left']},{monitor['top']} {monitor['width']}x{monitor['height']}", (20, 170),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2, cv2.LINE_AA)

                if cached_hwnd is not None:
                    cv2.putText(combined, f"HWND: {cached_hwnd}", (20, 200),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2, cv2.LINE_AA)

                cv2.putText(combined, f"FrameDiff: {frame_diff_mean:.3f}", (20, 230),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2, cv2.LINE_AA)

                pw_text = f"PrintWindow: {printwindow_state}"
                cv2.putText(combined, pw_text, (20, 260),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2, cv2.LINE_AA)

                y_base = 290

                if track_ok and track.left_box is not None and track.right_box is not None:
                    dist_text = "N/A"
                    if raw_distance_m is not None and ema_distance is not None:
                        dist_text = f"raw={raw_distance_m:.2f}m smooth={ema_distance:.2f}m"
                    elif raw_distance_m is not None:
                        dist_text = f"raw={raw_distance_m:.2f}m"
                    elif track.last_distance_m is not None:
                        dist_text = f"hold={track.last_distance_m:.2f}m"

                    disp_text = "N/A"
                    if disparity_px is not None:
                        disp_text = f"{disparity_px:.2f}px"
                    elif track.last_disparity_px is not None:
                        disp_text = f"{track.last_disparity_px:.2f}px"

                    cv2.putText(
                        combined,
                        f"disp={disp_text}  {dist_text}  cat={category}",
                        (20, y_base),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.75,
                        (0, 255, 255),
                        2,
                        cv2.LINE_AA,
                    )
                else:
                    cv2.putText(
                        combined,
                        "No stereo pair / track lost",
                        (20, y_base),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.8,
                        (0, 100, 255),
                        2,
                        cv2.LINE_AA,
                    )

                cv2.putText(
                    combined,
                    f"TrackMode: {track_mode} miss={track.miss_count}",
                    (20, y_base + 30),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.7,
                    (0, 255, 255) if track_mode == "detected" else (0, 180, 255),
                    2,
                    cv2.LINE_AA,
                )

            with StepTimer("imshow", args.slow_ms):
                cv2.imshow(args.title, combined)

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

    if _LOG_FILE_HANDLE is not None:
        _LOG_FILE_HANDLE.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())