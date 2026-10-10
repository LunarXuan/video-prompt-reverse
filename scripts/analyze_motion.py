#!/usr/bin/env python3
"""Measure motion signals in a video and infer the body mechanics behind them.

Two layers:
  signal layer      - frame differences, dense optical flow, grid hotspots, ROI
                      displacement series, silhouette tracking, rigid-fit score;
  attribution layer - turns those signals into candidate body-mechanism
                      hypotheses with supporting and opposing evidence.

Optical flow is only an observation. It never goes into a prompt as an action
description; `action_mechanics` is what may be described, and only after a
human confirms it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

try:  # dependency gate, resolved in main()
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover - exercised only without deps
    cv2 = None
    np = None

GRID = 16
SILHOUETTE_THRESHOLD = 110
CYCLE_MIN_HZ, CYCLE_MAX_HZ = 0.35, 3.0
AUTOCORR_MIN, POWER_MIN, MIN_CYCLES = 0.45, 0.30, 2.5
RECIPROCAL_MAX, UNIDIRECTIONAL_MIN = 0.25, 0.65
IN_PHASE_MIN, INDEPENDENT_MAX = 0.65, 0.35
TREND_MIN_PX_PER_S, TREND_MIN_R2 = 3.0, 0.60

# Normalised (x0, y0, x1, y1) body partition; override with --roi-config.
DEFAULT_ROIS = {
    "head": (0.30, 0.10, 0.75, 0.40),
    "face": (0.375, 0.16, 0.64, 0.33),
    "shoulders": (0.22, 0.40, 0.78, 0.53),
    "chest": (0.25, 0.50, 0.75, 0.62),
    "hip": (0.25, 0.62, 0.75, 0.74),
    "arms_front": (0.10, 0.55, 0.55, 0.85),
    "legs": (0.30, 0.78, 0.70, 0.97),
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=check)


def atomic_json(path: Path, data: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


# --------------------------------------------------------------------------- #
# signal layer
# --------------------------------------------------------------------------- #

def extract_frames(ffmpeg: str, video: Path, frames_dir: Path, fps: float) -> list[Path]:
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(video), "-vf", f"fps={fps:g}", "-pix_fmt", "gray",
        str(frames_dir / "f_%05d.png"),
    ]
    result = run(command, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"frame extraction failed: {result.stderr.strip()}")
    frames = sorted(frames_dir.glob("f_*.png"))
    if len(frames) < 2:
        raise RuntimeError(f"not enough frames extracted: {len(frames)}")
    return frames


def load_frames(paths: list[Path]) -> list["np.ndarray"]:
    # uint8 grayscale is mandatory: Farneback silently returns all-zero flow
    # when fed float32 images.
    frames = []
    for path in paths:
        image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise RuntimeError(f"could not read frame: {path}")
        frames.append(image)
    return frames


def frame_differences(frames: list["np.ndarray"]) -> dict[int, float]:
    stack = [frame.astype(np.float32) for frame in frames]
    result = {}
    for step in (1, 5, 15, 30):
        if step >= len(stack):
            continue
        values = [float(np.mean(np.abs(stack[i + step] - stack[i]))) for i in range(len(stack) - step)]
        result[step] = round(float(np.mean(values)), 3)
    return result


def dense_flow(frames: list["np.ndarray"]) -> list["np.ndarray"]:
    return [
        cv2.calcOpticalFlowFarneback(frames[i], frames[i + 1], None, 0.5, 3, 21, 3, 5, 1.2, 0)
        for i in range(len(frames) - 1)
    ]


def grid_maps(flows: list["np.ndarray"], shape: tuple[int, int]) -> dict:
    height, width = shape
    cell_h, cell_w = height // GRID, width // GRID
    mag = np.stack([np.linalg.norm(f, axis=-1) for f in flows])
    global_x = np.array([float(np.median(f[..., 0])) for f in flows])
    global_y = np.array([float(np.median(f[..., 1])) for f in flows])
    local = np.stack([
        np.linalg.norm([flows[i][..., 0] - global_x[i], flows[i][..., 1] - global_y[i]], axis=0)
        for i in range(len(flows))
    ])

    def grid(cube: "np.ndarray") -> list[list[float]]:
        return [[round(float(cube[:, r * cell_h:(r + 1) * cell_h, c * cell_w:(c + 1) * cell_w].mean()), 3)
                 for c in range(GRID)] for r in range(GRID)]

    local_grid = np.array(grid(local))
    top = sorted(
        [{"x_px": int(c * cell_w), "y_px": int(r * cell_h),
          "x_pct": round(100 * c * cell_w / width, 1), "y_pct": round(100 * r * cell_h / height, 1),
          "local_px_per_frame": float(local_grid[r, c])}
         for r in range(GRID) for c in range(GRID)],
        key=lambda item: -item["local_px_per_frame"])[:8]
    return {
        "cell_px": [cell_h, cell_w],
        "raw_flow": grid(mag),
        "local_magnitude": local_grid.round(3).tolist(),
        "local_dx": grid(np.stack([flows[i][..., 0] - global_x[i] for i in range(len(flows))])),
        "local_dy": grid(np.stack([flows[i][..., 1] - global_y[i] for i in range(len(flows))])),
        "top_cells": top,
    }


def reciprocity(net_dx: float, net_dy: float, path_len: float) -> float:
    if path_len <= 1e-6:
        return 0.0
    return round(float(math.hypot(net_dx, net_dy) / path_len), 3)


def roi_series(flows: list["np.ndarray"], global_xy: list[tuple[float, float]],
               rois: dict[str, tuple[float, float, float, float]],
               shape: tuple[int, int]) -> list[dict]:
    height, width = shape
    series = []
    for name, (x0, y0, x1, y1) in rois.items():
        box = (slice(int(y0 * height), int(y1 * height)), slice(int(x0 * width), int(x1 * width)))
        dx, dy, mag, abs_x, abs_y = [], [], [], [], []
        for i, flow in enumerate(flows):
            gx, gy = global_xy[i]
            patch_x = flow[..., 0][box] - gx
            patch_y = flow[..., 1][box] - gy
            dx.append(float(patch_x.mean()))
            dy.append(float(patch_y.mean()))
            mag.append(float(np.mean(np.linalg.norm([patch_x, patch_y], axis=0))))
            # Absolute motion keeps whole-body movement that the camera shares.
            # In a selfie the body carries the camera, so subtracting the global
            # flow would also remove a genuine body bounce.
            abs_x.append(float(flow[..., 0][box].mean()))
            abs_y.append(float(flow[..., 1][box].mean()))
        cum_x, cum_y = np.cumsum(dx), np.cumsum(dy)
        cum_abs_x, cum_abs_y = np.cumsum(abs_x), np.cumsum(abs_y)
        path = float(np.sum(np.hypot(dx, dy)))
        record = {
            "id": name,
            "box_norm": [x0, y0, x1, y1],
            "local_mean_px_per_frame": round(float(np.mean(mag)), 3),
            "local_peak_px_per_frame": round(float(np.max(mag)), 3),
            "peak_index": int(np.argmax(mag)),
            "net_dx_px": round(float(cum_x[-1]), 2),
            "net_dy_px": round(float(cum_y[-1]), 2),
            "path_len_px": round(path, 2),
            "reciprocity": reciprocity(float(cum_x[-1]), float(cum_y[-1]), path),
            "displacement_x": [round(float(v), 3) for v in cum_x],
            "displacement_y": [round(float(v), 3) for v in cum_y],
            "absolute_displacement_x": [round(float(v), 3) for v in cum_abs_x],
            "absolute_displacement_y": [round(float(v), 3) for v in cum_abs_y],
            "magnitude": [round(float(v), 3) for v in mag],
        }
        series.append(record)
    return series


def silhouette_tracking(frames: list["np.ndarray"], fps: float) -> dict:
    tops, centroids, bottoms, widths, areas = [], [], [], [], []
    height, width = frames[0].shape
    for frame in frames:
        mask = frame < SILHOUETTE_THRESHOLD
        band = mask[: int(height * 0.92)]
        rows = np.where(band.sum(axis=1) >= 3)[0]
        tops.append(int(rows.min()) if len(rows) else -1)
        ys = np.where(band)[0]
        centroids.append(float(ys.mean()) if len(ys) else float("nan"))
        bottoms.append(round(100 * float(mask[int(height * 0.9):].mean()), 2))
        leg = mask[int(height * 0.78): int(height * 0.97)]
        widths.append(int(leg.sum(axis=1).max()) if leg.size else 0)
        areas.append(int(band.sum()))
    slope, r2 = linear_trend([float(v) for v in tops], fps)
    result = {
        "head_top_row": [min(tops), max(tops)],
        "head_top_range_px": max(tops) - min(tops),
        "centroid_y": [round(float(np.nanmin(centroids)), 1), round(float(np.nanmax(centroids)), 1)],
        "bottom_ratio_percent": [min(bottoms), max(bottoms)],
        "leg_width_px": [min(widths), max(widths)],
        "area_px": [min(areas), max(areas)],
        "head_top_trend_px_per_s": slope,
        "head_top_trend_r2": r2,
        "series": {"head_top": tops, "centroid_y": [round(float(v), 2) for v in centroids],
                   "bottom_ratio": bottoms, "leg_width": widths, "area": areas},
    }
    # Silhouette positions are absolute: they keep whole-body motion that a
    # selfie camera shares, which residual flow would discard.
    result["cycle"] = {
        "head_top": detect_cycle([float(v) for v in tops], fps),
        "centroid_y": detect_cycle(centroids, fps),
        "leg_width": detect_cycle([float(v) for v in widths], fps),
        "area": detect_cycle([float(v) for v in areas], fps),
    }
    return result


def linear_trend(values: list[float], fps: float) -> tuple[float, float]:
    y = np.asarray(values, dtype=float)
    x = np.arange(len(y)) / fps
    if len(y) < 3 or np.std(y) < 1e-9:
        return 0.0, 0.0
    slope, intercept = np.polyfit(x, y, 1)
    predicted = slope * x + intercept
    ss_res = float(np.sum((y - predicted) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-9 else 0.0
    return round(float(slope), 3), round(float(r2), 3)


def rigid_fit(frames: list["np.ndarray"]) -> dict:
    first = frames[0]
    points = cv2.goodFeaturesToTrack(first, maxCorners=300, qualityLevel=0.01, minDistance=8)
    if points is None or len(points) < 12:
        return {"inlier_ratio": None, "rotation_deg": None, "scale": None,
                "note": "not enough trackable features for a rigid fit"}
    current = points.reshape(-1, 2).astype(np.float32)
    last = frames[-1]
    next_points, status, _ = cv2.calcOpticalFlowPyrLK(first, last, current, None,
                                                      winSize=(21, 21), maxLevel=3)
    status = status.reshape(-1).astype(bool)
    src, dst = current[status], next_points[status]
    if len(src) < 12:
        return {"inlier_ratio": None, "rotation_deg": None, "scale": None,
                "note": "tracks lost before the last frame"}
    matrix, inliers = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC,
                                                  ransacReprojThreshold=2.0)
    if matrix is None:
        return {"inlier_ratio": None, "rotation_deg": None, "scale": None,
                "note": "rigid fit did not converge"}
    ratio = float(inliers.mean())
    scale = float(math.hypot(matrix[0][0], matrix[1][0]))
    rotation = float(math.degrees(math.atan2(matrix[1][0], matrix[0][0])))
    note = ("low inlier ratio: the clip is not one rigid transform, so camera motion, "
            "subject pose change and non-rigid deformation all contribute"
            if ratio < 0.5 else "mostly rigid: one transform explains most of the motion")
    return {"inlier_ratio": round(ratio, 3), "rotation_deg": round(rotation, 3),
            "scale": round(scale, 4), "note": note}


# --------------------------------------------------------------------------- #
# attribution layer
# --------------------------------------------------------------------------- #

def detrend(values: list[float]) -> "np.ndarray":
    x = np.asarray(values, dtype=float)
    if len(x) < 4 or np.std(x) < 1e-9:
        return x - x.mean()
    t = np.arange(len(x))
    return x - np.polyval(np.polyfit(t, x, 1), t)


def detect_cycle(values: list[float], fps: float) -> dict:
    x = detrend(values)
    if len(x) < 8 or float(np.std(x)) < 1e-6:
        return {"cyclic": False, "hz": 0.0, "amplitude_px": 0.0, "autocorr_peak": 0.0,
                "power_ratio": 0.0, "cycles_covered": 0.0, "note": "series too short or flat"}
    windowed = x * np.hanning(len(x))
    spectrum = np.abs(np.fft.rfft(windowed)) ** 2
    freqs = np.fft.rfftfreq(len(x), d=1.0 / fps)
    band = (freqs >= CYCLE_MIN_HZ) & (freqs <= CYCLE_MAX_HZ)
    if not band.any():
        return {"cyclic": False, "hz": 0.0, "amplitude_px": 0.0, "autocorr_peak": 0.0,
                "power_ratio": 0.0, "cycles_covered": 0.0, "note": "no frequency band available"}
    band_spectrum = np.where(band, spectrum, 0.0)
    peak_index = int(np.argmax(band_spectrum))
    hz = float(freqs[peak_index])
    total = float(spectrum[1:].sum())
    power_ratio = float(band_spectrum[peak_index] / total) if total > 1e-9 else 0.0
    autocorrelation = np.correlate(x, x, mode="full")[len(x) - 1:]
    if autocorrelation[0] > 1e-9:
        autocorrelation = autocorrelation / autocorrelation[0]
    search = autocorrelation[2: max(3, len(x) // 2)]
    autocorr_peak = float(search.max()) if len(search) else 0.0
    peak_lag = int(np.argmax(search)) + 2 if len(search) else 0
    cycles = (len(x) / fps) * hz if hz > 0 else 0.0
    cyclic = bool(autocorr_peak >= AUTOCORR_MIN and power_ratio >= POWER_MIN
                  and cycles >= MIN_CYCLES)
    return {"cyclic": cyclic, "hz": round(hz, 3), "amplitude_px": round(2.0 * float(np.std(x)), 2),
            "autocorr_peak": round(autocorr_peak, 3), "power_ratio": round(power_ratio, 3),
            "cycles_covered": round(cycles, 2), "period_frames": peak_lag,
            "note": "cycle accepted" if cyclic else "thresholds not met; treat as non-cyclic"}


def correlation_and_lag(a: list[float], b: list[float], fps: float, max_lag_s: float = 0.6) -> dict:
    x, y = detrend(a), detrend(b)
    if len(x) < 6 or float(np.std(x)) < 1e-9 or float(np.std(y)) < 1e-9:
        return {"r": 0.0, "lag_s": 0.0, "in_phase": False, "independent": True}
    max_lag = max(1, int(max_lag_s * fps))
    best_r, best_lag = 0.0, 0
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            xs, ys = x[lag:], y[: len(y) - lag]
        else:
            xs, ys = x[: len(x) + lag], y[-lag:]
        if len(xs) < 6:
            continue
        denom = float(np.std(xs) * np.std(ys))
        r = float(np.mean((xs - xs.mean()) * (ys - ys.mean())) / denom) if denom > 1e-9 else 0.0
        if abs(r) > abs(best_r):
            best_r, best_lag = r, lag
    return {"r": round(best_r, 3), "lag_s": round(best_lag / fps, 3),
            "in_phase": bool(best_r >= IN_PHASE_MIN),
            "independent": bool(abs(best_r) < INDEPENDENT_MAX)}


def build_hypotheses(signals: dict, fps: float) -> list[dict]:
    rois = {item["id"]: item for item in signals["rois"]}
    global_info = signals["global"]
    silhouette = signals["silhouette"]
    grid_top = signals["grid"]["top_cells"]

    def cycle_of(name: str) -> dict:
        return rois[name]["cycle"] if name in rois and "cycle" in rois[name] else {"cyclic": False}

    hypotheses: list[dict] = []

    # h1: sustained pan / tilt - the interpretation most often asserted wrongly.
    slope, r2 = silhouette["head_top_trend_px_per_s"], silhouette["head_top_trend_r2"]
    monotonic = abs(slope) >= TREND_MIN_PX_PER_S and r2 >= TREND_MIN_R2
    if monotonic:
        verdict, confidence = "supported", 0.75
        support = [f"head-top row drifts {slope:+.2f} px/s with r2={r2:.2f}"]
        against = []
        if global_info["reciprocity"] <= RECIPROCAL_MAX:
            against.append(f"net/path ratio {global_info['reciprocity']:.2f} indicates oscillation, not travel")
    else:
        verdict, confidence = "refuted", 0.05
        support = []
        against = [f"head-top trend only {slope:+.2f} px/s with r2={r2:.2f} (needs "
                   f">={TREND_MIN_PX_PER_S} px/s and r2>={TREND_MIN_R2})",
                   f"net/path ratio {global_info['reciprocity']:.2f}: motion oscillates instead of travelling"]
    hypotheses.append({
        "id": "h_sustained_pan_tilt",
        "label_zh": "镜头持续平移/俯仰，取景逐渐变宽或变窄",
        "label_en": "sustained camera pan or tilt that progressively reframes the subject",
        "verdict": verdict, "confidence": confidence,
        "chain": [{"region": "camera", "role": "driver", "joint_action": "translate_or_tilt"}],
        "support": support, "against": against,
        "prompt_hint_zh": "取景范围全程基本不变，不得写成推近、拉远或持续俯仰",
        "prompt_hint_en": "framing stays constant; never write it as a zoom or a sustained pan",
    })

    # h2: handheld reciprocating micro-sway.
    edge_cells = [cell for cell in grid_top if cell["x_pct"] <= 12 or cell["x_pct"] >= 88]
    if global_info["reciprocity"] <= RECIPROCAL_MAX:
        verdict, confidence = "supported", 0.70
        support = [f"net/path ratio {global_info['reciprocity']:.2f} (<= {RECIPROCAL_MAX})",
                   f"net drift {global_info['net_dx_px']:+.1f} x / {global_info['net_dy_px']:+.1f} y px "
                   f"against a {global_info['path_len_px']:.0f} px accumulated path",
                   f"head-top row oscillates within {silhouette['head_top_range_px']} px"]
        against = []
        if not edge_cells:
            against.append("no high-motion cell near the frame edges, so part of the sway may be subject motion")
    else:
        verdict, confidence = "inconclusive", 0.35
        support = []
        against = [f"net/path ratio {global_info['reciprocity']:.2f} is above the reciprocating threshold"]
    hypotheses.append({
        "id": "h_handheld_sway",
        "label_zh": "手持往复微晃：整体来回摆动而非单向位移，全片无静止帧",
        "label_en": "handheld reciprocating micro-sway: oscillation rather than travel, never fully still",
        "verdict": verdict, "confidence": confidence,
        "chain": [{"region": "camera", "role": "driver", "joint_action": "handheld_oscillation"}],
        "support": support, "against": against,
        "prompt_hint_zh": "画面全程保持轻微而持续的手持微晃，晃幅往复、不累积成位移",
        "prompt_hint_en": "keep a slight continuous handheld sway throughout, oscillating without accumulating into travel",
    })

    # h3: cyclic vertical body bounce driven by hip push-pull with knee flexion.
    # Judged on absolute signals: in a selfie the body carries the camera, so
    # residual (camera-removed) flow would erase a real whole-body bounce.
    sil_cycle = silhouette.get("cycle", {})
    head_cycle = sil_cycle.get("head_top", {})
    centroid_cycle = sil_cycle.get("centroid_y", {})
    leg_cycle = sil_cycle.get("leg_width", {})
    area_cycle = sil_cycle.get("area", {})
    torso_phase = correlation_and_lag(rois["head"]["absolute_displacement_y"],
                                      rois["shoulders"]["absolute_displacement_y"], fps) \
        if "head" in rois and "shoulders" in rois else {"r": 0.0, "lag_s": 0.0, "in_phase": False}
    hip_phase = correlation_and_lag(rois["head"]["absolute_displacement_y"],
                                    rois["hip"]["absolute_displacement_y"], fps) \
        if "head" in rois and "hip" in rois else {"r": 0.0, "lag_s": 0.0, "in_phase": False}
    leg_width = silhouette["leg_width_px"]
    width_change = (leg_width[1] - leg_width[0]) / leg_width[1] * 100 if leg_width[1] else 0.0
    area = silhouette["area_px"]
    area_change = (area[1] - area[0]) / area[1] * 100 if area[1] else 0.0
    bounce_cyclic = bool(head_cycle.get("cyclic") or centroid_cycle.get("cyclic"))
    if bounce_cyclic and torso_phase.get("in_phase"):
        verdict = "supported"
        confidence = 0.35 + 0.20 + 0.20
        support = [
            f"head-top row oscillates cyclically at {head_cycle.get('hz', centroid_cycle.get('hz', 0)):.2f} Hz "
            f"(autocorr {head_cycle.get('autocorr_peak', centroid_cycle.get('autocorr_peak', 0)):.2f}, "
            f"amplitude {head_cycle.get('amplitude_px', centroid_cycle.get('amplitude_px', 0)):.1f} px)",
            f"head and shoulders move in phase (r={torso_phase.get('r', 0):.2f}, "
            f"lag {torso_phase.get('lag_s', 0):.2f}s): the whole torso rises and falls together",
        ]
        if leg_cycle.get("cyclic") and width_change >= 3.0:
            confidence += 0.15
            support.append(f"leg silhouette width oscillates {width_change:.1f}% at "
                           f"{leg_cycle.get('hz', 0):.2f} Hz (knee flexion absorbing the bounce)")
        if area_cycle.get("cyclic") and area_change >= 1.5:
            confidence += 0.10
            support.append(f"silhouette area oscillates {area_change:.1f}% (toward/away depth change)")
        confidence = min(0.85, confidence)
    elif bounce_cyclic or torso_phase.get("in_phase"):
        verdict, confidence = "inconclusive", 0.40
        support = ["partial periodic evidence only"]
    else:
        verdict, confidence = "refuted", 0.10
        support = []
    against = [] if verdict == "supported" else [
        f"head-top cycle: cyclic={head_cycle.get('cyclic')}, hz={head_cycle.get('hz', 0):.2f}, "
        f"autocorr={head_cycle.get('autocorr_peak', 0):.2f}",
        f"head/shoulders in-phase: r={torso_phase.get('r', 0):.2f} (needs >= {IN_PHASE_MIN})",
    ]
    if verdict == "supported":
        if not (leg_cycle.get("cyclic") and width_change >= 3.0):
            against.append(f"leg width varies {width_change:.1f}% without a clean cycle; knee flexion unconfirmed")
        if not (area_cycle.get("cyclic") and area_change >= 1.5):
            against.append(f"silhouette area varies {area_change:.1f}%; front-back hip drive is only indirectly "
                           "observable and capped by monocular depth ambiguity")
        against.append(f"head-hip vertical correlation r={hip_phase.get('r', 0):.2f} at lag "
                       f"{hip_phase.get('lag_s', 0):.2f}s; hip drive itself stays indirect")
    hypotheses.append({
        "id": "h_vertical_bounce",
        "label_zh": "身体轻微上下起伏的循环动作（胯部前后推拉带动、膝盖轻弯配合）",
        "label_en": "gentle cyclic vertical bounce of the body, driven by the hips and absorbed by the knees",
        "verdict": verdict, "confidence": round(float(confidence), 2),
        "chain": [{"region": "hip", "role": "driver", "joint_action": "ant_post_push_pull"},
                  {"region": "knee", "role": "follower", "joint_action": "flex_extend"},
                  {"region": "torso", "role": "follower", "joint_action": "vertical_bounce"}],
        "support": support, "against": against,
        "prompt_hint_zh": "身体随胯部前后推拉做持续、轻微的上下起伏，膝盖随之轻弯缓冲",
        "prompt_hint_en": "the body keeps a gentle continuous vertical bounce driven by the hips, knees softly absorbing it",
    })

    # h4: arm reach into the lower foreground.
    arm = rois.get("arms_front")
    if arm and arm["local_peak_px_per_frame"] >= 1.8 * max(arm["local_mean_px_per_frame"], 1e-6):
        verdict, confidence = "supported", 0.65
        support = [f"arm-region local motion peaks at {arm['local_peak_px_per_frame']:.2f} px/frame, "
                   f"{arm['local_peak_px_per_frame'] / max(arm['local_mean_px_per_frame'], 1e-6):.1f}x its own mean",
                   f"peak at frame {arm['peak_index']} (~{arm['peak_index'] / fps:.2f}s)"]
        against = []
    else:
        verdict, confidence = "inconclusive", 0.30
        support = []
        against = ["arm-region motion has no distinct peak over its own baseline"]
    hypotheses.append({
        "id": "h_arm_reach",
        "label_zh": "手臂沿画面对角线向前下方缓慢伸出（持机/调整姿态）",
        "label_en": "one arm reaching slowly forward and down along the frame diagonal",
        "verdict": verdict, "confidence": confidence,
        "chain": [{"region": "arm", "role": "driver", "joint_action": "reach_forward_down"}],
        "support": support, "against": against,
        "prompt_hint_zh": "手臂沿画面对角线缓慢前伸至镜头前下方",
        "prompt_hint_en": "the arm reaches slowly forward and downward along the frame diagonal",
    })

    # h5: head sway plus non-rigid beauty-filter deformation.
    head = rois.get("head")
    face = rois.get("face")
    shoulders = rois.get("shoulders")
    if head and shoulders and head["local_mean_px_per_frame"] > shoulders["local_mean_px_per_frame"]:
        verdict, confidence = "supported", 0.55
        support = [f"head local motion {head['local_mean_px_per_frame']:.2f} px/frame exceeds shoulders "
                   f"{shoulders['local_mean_px_per_frame']:.2f} px/frame",
                   f"reciprocity in the head region {head['reciprocity']:.2f} fits sway rather than travel"]
        if face and face["local_mean_px_per_frame"] >= head["local_mean_px_per_frame"]:
            support.append(f"face-region motion {face['local_mean_px_per_frame']:.2f} px/frame is at or above "
                           "the head average, consistent with filter micro-deformation")
        against = []
    else:
        verdict, confidence = "inconclusive", 0.30
        support = []
        against = ["head motion does not exceed shoulder motion"]
    hypotheses.append({
        "id": "h_head_sway_filter",
        "label_zh": "头部轻微摆动，叠加美颜滤镜带来的面部非刚性微形变",
        "label_en": "slight head sway combined with non-rigid beauty-filter micro-deformation on the face",
        "verdict": verdict, "confidence": confidence,
        "chain": [{"region": "head", "role": "driver", "joint_action": "sway_lateral"},
                  {"region": "face", "role": "follower", "joint_action": "filter_micro_warp"}],
        "support": support, "against": against,
        "prompt_hint_zh": "头部轻微摆动，面部保留美颜滤镜带来的细微非刚性形变",
        "prompt_hint_en": "subtle head sway with slight non-rigid beauty-filter deformation on the face",
    })
    return hypotheses


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #

def ascii_grid(grid: list[list[float]], title: str, unit: str, cell_px: list[int]) -> list[str]:
    ramp = " .:-=+*#%@"
    values = np.array(grid, dtype=float)
    maximum = float(values.max()) + 1e-9
    lines = [f"### {title}", "", f"max {maximum:.2f} {unit}；cell {cell_px[0]}x{cell_px[1]} px", "",
             "```",
             "      " + "".join(f"{c % 10}" for c in range(GRID))]
    for r in range(GRID):
        row = "".join(ramp[min(9, int(values[r, c] / maximum * 9.99))] for c in range(GRID))
        lines.append(f"y{r * cell_px[0]:>3}  {row}")
    lines.append("```")
    return lines


def ascii_direction(grid: dict, cell_px: list[int]) -> list[str]:
    dx, dy = np.array(grid["local_dx"]), np.array(grid["local_dy"])
    lines = ["### 局部运动方向（已扣除全局运动）", "", "```"]
    for r in range(GRID):
        cells = []
        for c in range(GRID):
            symbol = "→" if abs(dx[r, c]) > abs(dy[r, c]) else ("↓" if dy[r, c] >= 0 else "↑")
            if symbol == "→" and dx[r, c] < 0:
                symbol = "←"
            magnitude = max(abs(dx[r, c]), abs(dy[r, c]))
            cells.append(f"{symbol}{int(min(9, round(magnitude * 10)))}")
        lines.append(f"y{r * cell_px[0]:>3}  " + " ".join(cells))
    lines.append("```")
    return lines


def render_report(data: dict, fps: float) -> str:
    motion = data["motion_analysis"]
    mechanics = data["action_mechanics"]
    lines = ["# 运动信号与动作机理分析", "",
             "差分与光流只是**观测**；动作描述必须来自下面的机理假设，且需人工确认后才能写入提示词。", ""]
    lines += ["## 1. 信号概览", "",
              "| 指标 | 数值 |", "| --- | --- |",
              f"| 分析帧率 / 帧数 | {motion['sampling']['analysis_fps']} fps / {motion['sampling']['frame_count']} 帧 |",
              f"| 相邻帧平均差 | {motion['frame_difference'].get(1, 'n/a')} / 255 灰度 |",
              f"| 多帧平均差 (5/15/30) | {motion['frame_difference'].get(5, '-')} / "
              f"{motion['frame_difference'].get(15, '-')} / {motion['frame_difference'].get(30, '-')} |",
              f"| 整体光流 | 均值 {motion['global']['flow_mean_px_per_frame']} px/帧，"
              f"峰值 {motion['global']['flow_peak_px_per_frame']} px/帧 |",
              f"| 净位移 | dx {motion['global']['net_dx_px']:+.2f} px，dy {motion['global']['net_dy_px']:+.2f} px |",
              f"| 累计路径长 | {motion['global']['path_len_px']:.1f} px |",
              f"| 往复率 R | {motion['global']['reciprocity']} → {motion['global']['character']} |",
              f"| 刚体拟合内点率 | {motion['rigid_fit']['inlier_ratio']} |", ""]

    lines += ["## 2. 区域运动", "",
              "| 区域 | 局部均值 px/帧 | 峰值 px/帧 | 峰值时刻 s | 净位移 dx/dy px | 往复率 R | 周期性 |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for roi in motion["rois"]:
        cycle = roi.get("cycle", {})
        cycle_text = f"{cycle.get('hz', 0):.2f} Hz" if cycle.get("cyclic") else "非周期"
        peak_time = roi["peak_index"] / fps
        lines.append(f"| {roi['id']} | {roi['local_mean_px_per_frame']:.2f} | "
                     f"{roi['local_peak_px_per_frame']:.2f} | {peak_time:.2f} | "
                     f"{roi['net_dx_px']:+.1f} / {roi['net_dy_px']:+.1f} | {roi['reciprocity']:.2f} | {cycle_text} |")
    lines.append("")

    lines += ascii_grid(motion["grid"]["local_magnitude"], "局部运动热点（已扣除全局运动）", "px/帧", motion["grid"]["cell_px"])
    lines.append("")
    lines += ascii_direction(motion["grid"], motion["grid"]["cell_px"])
    lines.append("")
    lines += ["最热单元：" + "、".join(
        f"(x {cell['x_pct']}%, y {cell['y_pct']}%) {cell['local_px_per_frame']:.2f} px/帧"
        for cell in motion["grid"]["top_cells"][:4]), ""]

    silhouette = motion["silhouette"]
    lines += ["## 3. 剪影追踪（绝对坐标，保留整体身体运动）", "",
              f"- 头顶行范围 {silhouette['head_top_row'][0]}–{silhouette['head_top_row'][1]} px"
              f"（摆动幅度 {silhouette['head_top_range_px']} px）",
              f"- 暗部质心 y {silhouette['centroid_y'][0]}–{silhouette['centroid_y'][1]} px",
              f"- 剪影面积 {silhouette['area_px'][0]}–{silhouette['area_px'][1]} px²",
              f"- 腿部宽度 {silhouette['leg_width_px'][0]}–{silhouette['leg_width_px'][1]} px",
              f"- 画面底部暗部占比 {silhouette['bottom_ratio_percent'][0]}%–{silhouette['bottom_ratio_percent'][1]}%",
              f"- 头顶行趋势 {silhouette['head_top_trend_px_per_s']:+.2f} px/s，r²={silhouette['head_top_trend_r2']}"]
    for key, label in (("head_top", "头顶行"), ("centroid_y", "质心 y"),
                       ("leg_width", "腿部宽度"), ("area", "剪影面积")):
        cycle = silhouette.get("cycle", {}).get(key, {})
        state = f"{cycle.get('hz', 0):.2f} Hz / 幅度 {cycle.get('amplitude_px', 0):.1f}" \
            if cycle.get("cyclic") else "非周期"
        lines.append(f"- {label}周期性：{state}"
                     f"（自相关 {cycle.get('autocorr_peak', 0):.2f}，功率占比 {cycle.get('power_ratio', 0):.2f}）")
    lines.append("")

    lines += ["## 4. 动作机理假设", ""]
    for item in mechanics["hypotheses"]:
        verdict_zh = {"supported": "支持", "refuted": "证伪", "inconclusive": "证据不足"}[item["verdict"]]
        lines += [f"### {item['id']}｜{item['label_zh']}｜{verdict_zh}｜置信度 {item['confidence']:.2f}", "",
                  f"- 英文：{item['label_en']}",
                  "- 发力链：" + " → ".join(f"{link['region']}({link['role']}/{link['joint_action']})"
                                            for link in item["chain"])]
        if item["support"]:
            lines.append("- 支持证据：" + "；".join(item["support"]))
        if item["against"]:
            lines.append("- 反对证据：" + "；".join(item["against"]))
        lines += [f"- 提示词写法（中）：{item['prompt_hint_zh']}",
                  f"- 提示词写法（英）：{item['prompt_hint_en']}", ""]

    lines += ["## 5. 局限", ""]
    lines += [f"- {item}" for item in motion["limitations"]]
    lines.append("")
    lines.append("> 未经人工确认（`analyst_confirmed`）的假设不得当作事实写入提示词。")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--probe-manifest", type=Path, help="video_manifest.json from probe_video.py")
    parser.add_argument("--roi-config", type=Path, help="JSON object of normalised ROI boxes")
    parser.add_argument("--analysis-fps", type=float, default=15.0)
    parser.add_argument("--silhouette-threshold", type=int, default=SILHOUETTE_THRESHOLD)
    parser.add_argument("--confirm-hypothesis", help="mark one hypothesis id as analyst-confirmed")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    if np is None or cv2 is None:
        parser.error("motion analysis requires numpy and opencv; install with: pip install -r requirements.txt")
    video = args.video.resolve()
    output_dir = args.output_dir.resolve()
    if not video.is_file():
        parser.error(f"video not found: {video}")
    if args.analysis_fps <= 0:
        parser.error("analysis-fps must be positive")
    if output_dir.exists() and any(output_dir.iterdir()) and not args.overwrite:
        parser.error(f"output directory is not empty: {output_dir}; use --overwrite deliberately")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        parser.error("ffmpeg must be available on PATH")

    rois = dict(DEFAULT_ROIS)
    if args.roi_config:
        supplied = json.loads(args.roi_config.read_text(encoding="utf-8-sig"))
        if not isinstance(supplied, dict):
            parser.error("roi-config must be a JSON object of normalised ROI boxes")
        rois.update({key: tuple(value) for key, value in supplied.items()})

    frames_dir = output_dir / "motion_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    frame_paths = extract_frames(ffmpeg, video, frames_dir, args.analysis_fps)
    frames = load_frames(frame_paths)
    height, width = frames[0].shape
    fps = args.analysis_fps

    flows = dense_flow(frames)
    global_xy = [(float(np.median(f[..., 0])), float(np.median(f[..., 1]))) for f in flows]
    magnitudes = [float(np.mean(np.linalg.norm(f, axis=-1))) for f in flows]
    net_dx = float(np.sum([gx for gx, _ in global_xy]))
    net_dy = float(np.sum([gy for _, gy in global_xy]))
    path_len = float(np.sum([math.hypot(gx, gy) for gx, gy in global_xy]))
    ratio = reciprocity(net_dx, net_dy, path_len)
    character = ("reciprocating" if ratio <= RECIPROCAL_MAX
                 else "unidirectional" if ratio >= UNIDIRECTIONAL_MIN else "mixed")

    roi_records = roi_series(flows, global_xy, rois, (height, width))
    for record in roi_records:
        record["cycle"] = detect_cycle(record["displacement_y"], fps)
        record["cycle_x"] = detect_cycle(record["displacement_x"], fps)
        record["cycle_absolute_y"] = detect_cycle(record["absolute_displacement_y"], fps)

    signals = {
        "sampling": {"analysis_fps": fps, "frame_count": len(frames), "size": [width, height]},
        "frame_difference": frame_differences(frames),
        "global": {"flow_mean_px_per_frame": round(float(np.mean(magnitudes)), 3),
                   "flow_peak_px_per_frame": round(float(np.max(magnitudes)), 3),
                   "net_dx_px": round(net_dx, 2), "net_dy_px": round(net_dy, 2),
                   "path_len_px": round(path_len, 2), "reciprocity": ratio, "character": character},
        "grid": grid_maps(flows, (height, width)),
        "rois": roi_records,
        "silhouette": silhouette_tracking(frames, fps),
        "rigid_fit": rigid_fit(frames),
    }

    limitations = [
        f"分析帧率为 {fps:g} fps，低于源帧率时高频细节会被平滑",
        "稠密光流与剪影阈值无法区分真实位移与美颜滤镜造成的形变",
        "单目视频存在深度歧义：前后方向（推拉）只能间接推断，相关结论置信度上限 0.85",
    ]
    if signals["silhouette"]["head_top_row"][0] < 0:
        limitations.append("剪影追踪未找到主体轮廓，剪影指标不可用")
    if signals["rigid_fit"]["inlier_ratio"] is not None and signals["rigid_fit"]["inlier_ratio"] < 0.5:
        limitations.append("刚体拟合内点率低：相机、主体姿态与非刚性形变叠加，无法用单一变换分离")
    signals["limitations"] = limitations

    hypotheses = build_hypotheses(signals, fps)
    hypotheses.sort(key=lambda item: (-item["confidence"], item["id"]))
    supported = [item for item in hypotheses if item["verdict"] == "supported"]
    status = "auto_hypothesis"
    primary = supported[0]["id"] if supported else hypotheses[0]["id"]
    if args.confirm_hypothesis:
        known = {item["id"] for item in hypotheses}
        if args.confirm_hypothesis not in known:
            parser.error(f"unknown hypothesis id: {args.confirm_hypothesis}; known: {sorted(known)}")
        status = "analyst_confirmed"
        primary = args.confirm_hypothesis

    source = {"path": str(video), "sha256": sha256_file(video)}
    if args.probe_manifest:
        probe = json.loads(args.probe_manifest.read_text(encoding="utf-8-sig"))
        source["probe_manifest"] = str(args.probe_manifest)
        source["probe_duration_seconds"] = probe.get("source", {}).get("duration_seconds")

    data = {
        "motion_analysis": {"version": "1.0", "source": source, **signals},
        "action_mechanics": {"status": status, "primary_id": primary, "hypotheses": hypotheses},
    }
    json_path = output_dir / "motion_analysis.json"
    atomic_json(json_path, data)
    (output_dir / "motion_report.md").write_text(render_report(data, fps), encoding="utf-8")
    print(json_path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, json.JSONDecodeError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
