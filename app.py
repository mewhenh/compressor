import json
import math
import os
import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path

@dataclass
class EncodeConfig:
    target_mb: float
    codec: str = "libx264"          # "libx265" also works (two-pass), AV1 varies by encoder
    preset: str = "medium"
    container: str = "mp4"
    audio_codec: str = "aac"
    audio_kbps: int = 96            # if keeping audio
    mute: bool = False              # if True, drop audio entirely for best size predictability
    safety_margin: float = 0.98     # reserve ~2% for overhead / rounding

def run(cmd: list[str]) -> None:
    print(" ".join(cmd))
    subprocess.run(cmd, check=True)

def ffprobe_duration_seconds(input_path: str) -> float:
    # Probe the *container* duration; good enough for most cases.
    # For tricky files, you can probe stream duration or use ffprobe -show_entries format=duration.
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        input_path
    ]
    out = subprocess.check_output(cmd, text=True)
    data = json.loads(out)
    return float(data["format"]["duration"])

def null_device() -> str:
    return "NUL" if platform.system().lower().startswith("win") else "/dev/null"

def compute_video_bitrate_kbps(target_mb: float, duration_s: float, audio_kbps: int, mute: bool, safety_margin: float) -> int:
    target_bytes = target_mb * 1024 * 1024
    target_bits = target_bytes * 8 * safety_margin
    total_bps = target_bits / max(duration_s, 0.001)

    audio_bps = 0 if mute else audio_kbps * 1000
    video_bps = max(total_bps - audio_bps, 100_000)  # clamp to something nonzero
    video_kbps = int(video_bps / 1000)
    return max(video_kbps, 100)  # final clamp

def two_pass_encode(
    input_path: str,
    output_path: str,
    start_s: float | None,
    end_s: float | None,
    cfg: EncodeConfig
) -> None:
    input_path = str(input_path)
    output_path = str(output_path)

    # Use trims to estimate duration better than full duration
    full_dur = ffprobe_duration_seconds(input_path)
    if start_s is None:
        start_s = 0.0
    if end_s is None or end_s > full_dur:
        end_s = full_dur
    seg_dur = max(end_s - start_s, 0.001)

    v_kbps = compute_video_bitrate_kbps(cfg.target_mb, seg_dur, cfg.audio_kbps, cfg.mute, cfg.safety_margin)
    logbase = str(Path(output_path).with_suffix("")) + "_2pass"

    # Common input trim args
    trim_args = []
    # For accurate cuts, place -ss AFTER -i (decode-accurate).
    # For faster but less accurate, put -ss before -i.
    # Here we choose accurate.
    # We'll use -ss and -to (end timestamp), not -t.
    trim_args = ["-ss", f"{start_s}", "-to", f"{end_s}"]

    # PASS 1: analysis only (no audio), write stats to passlog
    pass1 = [
        "ffmpeg", "-y",
        "-i", input_path,
        *trim_args,
        "-c:v", cfg.codec,
        "-b:v", f"{v_kbps}k",
        "-preset", cfg.preset,
        "-pass", "1",
        "-passlogfile", logbase,
        "-an",
        "-f", "mp4",
        null_device()
    ]

    # PASS 2: real output, include or drop audio
    pass2 = [
        "ffmpeg", "-y",
        "-i", input_path,
        *trim_args,
        "-c:v", cfg.codec,
        "-b:v", f"{v_kbps}k",
        "-preset", cfg.preset,
        "-pass", "2",
        "-passlogfile", logbase,
    ]

    if cfg.mute:
        pass2 += ["-an"]
    else:
        pass2 += ["-c:a", cfg.audio_codec, "-b:a", f"{cfg.audio_kbps}k"]

    # Nice UX for MP4
    pass2 += ["-movflags", "+faststart", output_path]

    try:
        run(pass1)
        run(pass2)
    finally:
        # Clean up 2-pass logs (FFmpeg may create several files depending on encoder)
        for suffix in ["-0.log", "-0.log.mbtree", ".log", ".log.mbtree"]:
            p = Path(logbase + suffix)
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass

if __name__ == "__main__":
    cfg = EncodeConfig(
        target_mb=50,
        codec="libx264",
        preset="slow",     # slower = better efficiency (smaller for same quality)
        audio_kbps=96,
        mute=False
    )

    two_pass_encode(
        input_path="input.mp4",
        output_path="output_50mb.mp4",
        start_s=5,
        end_s=65,
        cfg=cfg
    )
    print("Done.")