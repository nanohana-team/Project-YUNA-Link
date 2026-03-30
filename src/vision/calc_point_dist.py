# src/vision/calc_point_dist.py
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass
class StereoDistanceResult:
    disparity_px: float
    distance_m: Optional[float]
    category: str
    focal_px: float


def focal_length_px_from_fov(width_px: int, fov_deg: float) -> float:
    """
    画像幅と水平FOVから焦点距離[pixel]を求める
    """
    if width_px <= 0:
        raise ValueError("width_px must be > 0")
    if fov_deg <= 0.0 or fov_deg >= 179.0:
        raise ValueError("fov_deg must be in (0, 179)")
    fov_rad = math.radians(fov_deg)
    return (width_px / 2.0) / math.tan(fov_rad / 2.0)


def calc_disparity_px(left_x: float, right_x: float) -> float:
    """
    視差 = 左画像上のx - 右画像上のx
    """
    return float(left_x) - float(right_x)


def calc_distance_from_disparity(
    disparity_px: float,
    focal_px: float,
    baseline_m: float,
    min_disparity_px: float = 1.0,
) -> Optional[float]:
    """
    Z = f * B / d
    disparityが小さすぎると不安定なので None を返す
    """
    d = abs(float(disparity_px))
    if d < float(min_disparity_px):
        return None
    if focal_px <= 0.0:
        return None
    if baseline_m <= 0.0:
        return None
    return (float(focal_px) * float(baseline_m)) / d


def categorize_distance(distance_m: Optional[float]) -> str:
    if distance_m is None:
        return "unknown"
    if distance_m < 1.0:
        return "near"
    if distance_m < 2.5:
        return "mid"
    return "far"


def calc_distance_from_points(
    left_x: float,
    right_x: float,
    image_width_px: int,
    h_fov_deg: float,
    baseline_m: float,
    min_disparity_px: float = 1.0,
) -> StereoDistanceResult:
    focal_px = focal_length_px_from_fov(image_width_px, h_fov_deg)
    disparity_px = calc_disparity_px(left_x, right_x)
    distance_m = calc_distance_from_disparity(
        disparity_px=disparity_px,
        focal_px=focal_px,
        baseline_m=baseline_m,
        min_disparity_px=min_disparity_px,
    )
    category = categorize_distance(distance_m)
    return StereoDistanceResult(
        disparity_px=disparity_px,
        distance_m=distance_m,
        category=category,
        focal_px=focal_px,
    )


def box_center(box_xyxy) -> Tuple[float, float]:
    x1, y1, x2, y2 = box_xyxy
    return ((float(x1) + float(x2)) * 0.5, (float(y1) + float(y2)) * 0.5)


def box_size(box_xyxy) -> Tuple[float, float]:
    x1, y1, x2, y2 = box_xyxy
    return (max(0.0, float(x2) - float(x1)), max(0.0, float(y2) - float(y1)))


def box_area(box_xyxy) -> float:
    w, h = box_size(box_xyxy)
    return w * h


def vertical_overlap_ratio(box_a, box_b) -> float:
    ay1 = float(box_a[1])
    ay2 = float(box_a[3])
    by1 = float(box_b[1])
    by2 = float(box_b[3])

    inter = max(0.0, min(ay2, by2) - max(ay1, by1))
    ha = max(1e-6, ay2 - ay1)
    hb = max(1e-6, by2 - by1)
    return inter / min(ha, hb)


def size_similarity(box_a, box_b) -> float:
    aw, ah = box_size(box_a)
    bw, bh = box_size(box_b)
    if aw <= 0 or ah <= 0 or bw <= 0 or bh <= 0:
        return 0.0

    wr = min(aw, bw) / max(aw, bw)
    hr = min(ah, bh) / max(ah, bh)
    return (wr + hr) * 0.5


def select_best_stereo_pair(
    left_boxes,
    right_boxes,
    max_vertical_center_diff_px: float = 120.0,
    min_vertical_overlap_ratio: float = 0.25,
    min_size_similarity: float = 0.35,
):
    """
    左右の人物bbox群から、同一人物らしいペアを1組選ぶ
    スコアは:
      - y中心が近い
      - 縦重なりが大きい
      - サイズが近い
      - x視差が正方向にある（左x > 右x を優先）
    """
    best = None
    best_score = None

    for li, lbox in enumerate(left_boxes):
        lcx, lcy = box_center(lbox)

        for ri, rbox in enumerate(right_boxes):
            rcx, rcy = box_center(rbox)

            dy = abs(lcy - rcy)
            if dy > max_vertical_center_diff_px:
                continue

            vo = vertical_overlap_ratio(lbox, rbox)
            if vo < min_vertical_overlap_ratio:
                continue

            ss = size_similarity(lbox, rbox)
            if ss < min_size_similarity:
                continue

            disparity = lcx - rcx

            # disparityが正の方を強く優先、負でも一応候補に残せるよう軽いペナルティ
            disparity_penalty = 0.0 if disparity > 0 else 2000.0

            score = (
                dy * 2.0
                - vo * 120.0
                - ss * 80.0
                + abs(disparity) * 0.15
                + disparity_penalty
            )

            if best_score is None or score < best_score:
                best_score = score
                best = {
                    "left_index": li,
                    "right_index": ri,
                    "left_box": lbox,
                    "right_box": rbox,
                    "left_center": (lcx, lcy),
                    "right_center": (rcx, rcy),
                    "disparity_px": disparity,
                    "score": score,
                    "vertical_diff_px": dy,
                    "vertical_overlap_ratio": vo,
                    "size_similarity": ss,
                }

    return best