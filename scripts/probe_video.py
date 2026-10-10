#!/usr/bin/env python3
"""Probe a video and extract deterministic analysis frames."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path


PTS_RE = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=check)


def parse_rate(value: str | None) -> float | None:
    if not value or value in {"0/0", "N/A"}:
        return None
    if "/" in value:
        numerator, denominator = value.split("/", 1)
        if float(denominator) == 0:
            return None
        return float(numerator) / float(denominator)
    return float(value)


def bounded_timestamps(values: list[float], duration: float, max_frames: int) -> list[float]:
    cleaned = sorted({round(max(0.0, min(value, max(0.0, duration - 0.001))), 3) for value in values})
    if len(cleaned) <= max_frames:
        return cleaned
    indexes = [round(i * (len(cleaned) - 1) / (max_frames - 1)) for i in range(max_frames)]
    return [cleaned[index] for index in indexes]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=1.0, help="Regular sampling interval in seconds")
    parser.add_argument("--scene-threshold", type=float, default=0.30, help="FFmpeg scene-change threshold")
    parser.add_argument("--max-frames", type=int, default=48)
    parser.add_argument("--extract-audio", action="store_true", help="Extract 16 kHz mono WAV when audio exists")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    video = args.video.resolve()
    output_dir = args.output_dir.resolve()
    manifest_path = output_dir / "video_manifest.json"

    if not video.is_file():
        parser.error(f"video not found: {video}")
    if args.interval <= 0 or args.max_frames < 2 or not 0 <= args.scene_threshold <= 1:
        parser.error("interval must be > 0, max-frames >= 2, and scene-threshold within [0, 1]")
    if manifest_path.exists() and not args.overwrite:
        parser.error(f"manifest already exists: {manifest_path}; use --overwrite for a deliberate rerun")

    ffprobe = shutil.which("ffprobe")
    ffmpeg = shutil.which("ffmpeg")
    if not ffprobe or not ffmpeg:
        parser.error("ffprobe and ffmpeg must both be available on PATH")

    output_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = output_dir / "frames"
    frames_dir.mkdir(exist_ok=True)

    probe = run([
        ffprobe,
        "-v", "error",
        "-show_streams",
        "-show_format",
        "-of", "json",
        str(video),
    ])
    metadata = json.loads(probe.stdout)
    video_stream = next((item for item in metadata.get("streams", []) if item.get("codec_type") == "video"), None)
    if video_stream is None:
        parser.error("input contains no video stream")
    audio_streams = [item for item in metadata.get("streams", []) if item.get("codec_type") == "audio"]
    duration = float(video_stream.get("duration") or metadata.get("format", {}).get("duration") or 0)
    if duration <= 0:
        parser.error("could not determine a positive duration")

    # Sample only up to the last decodable frame. A container duration may extend
    # past the final frame, and seeking beyond it yields no packet at all.
    sampling_duration = duration
    frame_rate = parse_rate(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate"))
    nb_frames = video_stream.get("nb_frames")
    if frame_rate and str(nb_frames or "").isdigit() and int(nb_frames) > 0:
        sampling_duration = min(sampling_duration, max(0.0, (int(nb_frames) - 1) / frame_rate))

    regular = [min(index * args.interval, sampling_duration - 0.001) for index in range(math.ceil(sampling_duration / args.interval) + 1)]
    scene_filter = f"select=gt(scene\\,{args.scene_threshold}),showinfo"
    scene_run = run([
        ffmpeg, "-hide_banner", "-nostdin", "-i", str(video),
        "-filter:v", scene_filter, "-an", "-f", "null", "-",
    ], check=False)
    scene_times = [float(match.group(1)) for match in PTS_RE.finditer(scene_run.stderr)]
    timestamps = bounded_timestamps(
        [0.0, sampling_duration - 0.001, *regular, *scene_times], sampling_duration, args.max_frames
    )

    extracted = []
    for index, timestamp in enumerate(timestamps, start=1):
        frame_path = frames_dir / f"frame_{index:04d}_{timestamp:010.3f}s.jpg"
        # -strict -2 keeps ffmpeg from rejecting full-range (non-standard) YUV
        # when encoding evidence JPEGs.
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-ss", f"{timestamp:.3f}", "-i", str(video), "-frames:v", "1",
            "-vf", "scale=1280:-2:force_original_aspect_ratio=decrease",
            "-strict", "-2", "-q:v", "2", str(frame_path),
        ]
        result = run(command, check=False)
        if result.returncode != 0 or not frame_path.is_file():
            # Retry one frame earlier: some containers report a duration that lands
            # past the last decodable packet.
            fallback = max(0.0, timestamp - (1.0 / frame_rate if frame_rate else 0.04))
            fallback_path = frames_dir / f"frame_{index:04d}_{fallback:010.3f}s.jpg"
            if fallback != timestamp:
                retry = [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                    "-ss", f"{fallback:.3f}", "-i", str(video), "-frames:v", "1",
                    "-vf", "scale=1280:-2:force_original_aspect_ratio=decrease",
                    "-strict", "-2", "-q:v", "2", str(fallback_path),
                ]
                retry_result = run(retry, check=False)
                if retry_result.returncode == 0 and fallback_path.is_file():
                    timestamp, frame_path = fallback, fallback_path
                else:
                    raise RuntimeError(f"frame extraction failed at {timestamp:.3f}s: {result.stderr.strip()}")
            else:
                raise RuntimeError(f"frame extraction failed at {timestamp:.3f}s: {result.stderr.strip()}")
        extracted.append({"timestamp": timestamp, "path": str(frame_path), "sha256": sha256_file(frame_path)})

    audio_path = None
    if args.extract_audio and audio_streams:
        audio_file = output_dir / "audio_16khz_mono.wav"
        audio_run = run([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(audio_file),
        ], check=False)
        if audio_run.returncode != 0:
            raise RuntimeError(f"audio extraction failed: {audio_run.stderr.strip()}")
        audio_path = {"path": str(audio_file), "sha256": sha256_file(audio_file)}

    manifest = {
        "manifest_version": "1.0",
        "source": {
            "path": str(video),
            "sha256": sha256_file(video),
            "duration_seconds": round(duration, 6),
            "fps": parse_rate(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")),
            "width": video_stream.get("width"),
            "height": video_stream.get("height"),
            "video_codec": video_stream.get("codec_name"),
            "has_audio": bool(audio_streams),
            "audio_codecs": [item.get("codec_name") for item in audio_streams],
        },
        "sampling": {
            "interval_seconds": args.interval,
            "scene_threshold": args.scene_threshold,
            "sampling_duration_seconds": round(sampling_duration, 6),
            "scene_timestamps": sorted({round(value, 3) for value in scene_times}),
            "selected_timestamps": timestamps,
            "max_frames": args.max_frames,
        },
        "frames": extracted,
        "audio": audio_path,
    }
    temporary = manifest_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(manifest_path)
    print(manifest_path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)

