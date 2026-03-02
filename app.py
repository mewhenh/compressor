import json
import platform
import subprocess
import threading
import webbrowser
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from flask import Flask, jsonify, render_template, request
from werkzeug.utils import secure_filename

app = Flask(__name__)
BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)


@dataclass
class EncodeConfig:
    target_mb: float
    codec: str = "libx264"
    preset: str = "medium"
    audio_codec: str = "aac"
    audio_kbps: int = 96
    mute: bool = False
    safety_margin: float = 0.98
    scale_filter: str | None = None


state_lock = threading.Lock()
state = {
    "running": False,
    "progress": 0.0,
    "message": "Idle",
    "total": 0,
    "completed": 0,
}
stop_event = threading.Event()
current_process: subprocess.Popen | None = None


def ffprobe_duration_seconds(input_path: str) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        input_path,
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
    video_bps = max(total_bps - audio_bps, 100_000)
    return max(int(video_bps / 1000), 100)


def run_ffmpeg_with_progress(
    cmd: list[str],
    duration_s: float,
    pass_offset: float,
    pass_weight: float,
    overall_callback: Callable[[float], None],
) -> None:
    global current_process
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    current_process = process

    try:
        while True:
            if stop_event.is_set() and process.poll() is None:
                process.terminate()
                raise RuntimeError("Compression stopped by user")

            line = process.stdout.readline() if process.stdout else ""
            if not line and process.poll() is not None:
                break

            if line.startswith("out_time_ms="):
                out_time_ms = float(line.split("=", 1)[1].strip() or 0)
                progress = min((out_time_ms / 1_000_000) / max(duration_s, 0.001), 1.0)
                overall_callback(pass_offset + progress * pass_weight)

        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, cmd)
    finally:
        current_process = None


def two_pass_encode(
    input_path: str,
    output_path: str,
    start_s: float | None,
    end_s: float | None,
    cfg: EncodeConfig,
    progress_callback: Callable[[float], None],
) -> None:
    full_dur = ffprobe_duration_seconds(input_path)
    start_s = 0.0 if start_s is None else max(0.0, start_s)
    end_s = full_dur if end_s is None else min(end_s, full_dur)
    seg_dur = max(end_s - start_s, 0.001)

    v_kbps = compute_video_bitrate_kbps(cfg.target_mb, seg_dur, cfg.audio_kbps, cfg.mute, cfg.safety_margin)
    logbase = str(Path(output_path).with_suffix("")) + "_2pass"
    trim_args = ["-ss", f"{start_s}", "-to", f"{end_s}"]
    scale_args = ["-vf", cfg.scale_filter] if cfg.scale_filter else []

    pass1 = [
        "ffmpeg",
        "-y",
        "-i",
        input_path,
        *trim_args,
        *scale_args,
        "-c:v",
        cfg.codec,
        "-b:v",
        f"{v_kbps}k",
        "-preset",
        cfg.preset,
        "-pass",
        "1",
        "-passlogfile",
        logbase,
        "-an",
        "-f",
        "mp4",
        "-progress",
        "pipe:1",
        "-nostats",
        null_device(),
    ]

    pass2 = [
        "ffmpeg",
        "-y",
        "-i",
        input_path,
        *trim_args,
        *scale_args,
        "-c:v",
        cfg.codec,
        "-b:v",
        f"{v_kbps}k",
        "-preset",
        cfg.preset,
        "-pass",
        "2",
        "-passlogfile",
        logbase,
    ]

    if cfg.mute:
        pass2 += ["-an"]
    else:
        pass2 += ["-c:a", cfg.audio_codec, "-b:a", f"{cfg.audio_kbps}k"]

    pass2 += ["-movflags", "+faststart", "-progress", "pipe:1", "-nostats", output_path]

    try:
        run_ffmpeg_with_progress(pass1, seg_dur, 0.0, 0.5, progress_callback)
        run_ffmpeg_with_progress(pass2, seg_dur, 0.5, 0.5, progress_callback)
    finally:
        for suffix in ["-0.log", "-0.log.mbtree", ".log", ".log.mbtree"]:
            p = Path(logbase + suffix)
            if p.exists():
                p.unlink(missing_ok=True)




def build_output_path(input_path: str) -> str:
    src = Path(input_path)
    candidate = src.with_name(f"{src.stem}_compressed{src.suffix}")
    if not candidate.exists():
        return str(candidate)

    idx = 1
    while True:
        numbered = src.with_name(f"{src.stem}_compressed_{idx}{src.suffix}")
        if not numbered.exists():
            return str(numbered)
        idx += 1

def update_state(**kwargs):
    with state_lock:
        state.update(kwargs)


def resolution_to_filter(resolution: str) -> str | None:
    mapping = {
        "original": None,
        "1080p": "scale=-2:1080",
        "720p": "scale=-2:720",
        "480p": "scale=-2:480",
    }
    return mapping.get(resolution, None)


def worker(jobs: list[dict], target_mb: float, codec: str, resolution: str):
    update_state(running=True, progress=0.0, message="Starting...", total=len(jobs), completed=0)
    stop_event.clear()

    for index, job in enumerate(jobs):
        if stop_event.is_set():
            update_state(running=False, message="Stopped", progress=state["progress"])
            return

        input_path = job["input_path"]
        output_path = build_output_path(input_path)
        update_state(message=f"Compressing {job['filename']} ({index + 1}/{len(jobs)})")

        cfg = EncodeConfig(
            target_mb=target_mb,
            codec=codec,
            preset="medium",
            mute=job["mute"],
            scale_filter=resolution_to_filter(resolution),
        )

        def item_progress(value: float):
            overall = (index + value) / max(len(jobs), 1)
            update_state(progress=round(overall * 100, 2))

        try:
            two_pass_encode(
                input_path=input_path,
                output_path=output_path,
                start_s=job["start"],
                end_s=job["end"],
                cfg=cfg,
                progress_callback=item_progress,
            )
        except Exception as exc:
            update_state(running=False, message=f"Error: {exc}")
            return

        update_state(completed=index + 1)

    update_state(running=False, progress=100.0, message="All videos compressed")


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/compress")
def start_compression():
    with state_lock:
        if state["running"]:
            return jsonify({"error": "Compression already running"}), 409

    metadata = json.loads(request.form.get("metadata", "[]"))
    files = request.files.getlist("videos")
    if not files:
        return jsonify({"error": "No videos uploaded"}), 400

    target_mb = float(request.form.get("target_mb", "50"))
    codec = request.form.get("codec", "libx264")
    resolution = request.form.get("resolution", "original")

    jobs = []
    for idx, uploaded in enumerate(files):
        safe_name = secure_filename(uploaded.filename or f"video_{idx}.mp4")
        info = metadata[idx] if idx < len(metadata) else {}

        source_path = info.get("source_path")
        if isinstance(source_path, str) and source_path and Path(source_path).exists():
            input_path = source_path
        else:
            stored = UPLOAD_DIR / f"{uuid.uuid4().hex}_{safe_name}"
            uploaded.save(stored)
            input_path = str(stored)

        jobs.append(
            {
                "filename": safe_name,
                "input_path": input_path,
                "start": float(info.get("start", 0)),
                "end": info.get("end"),
                "mute": bool(info.get("mute", False)),
            }
        )

    thread = threading.Thread(target=worker, args=(jobs, target_mb, codec, resolution), daemon=True)
    thread.start()
    return jsonify({"ok": True})


@app.post("/api/stop")
def stop_compression():
    stop_event.set()
    global current_process
    if current_process and current_process.poll() is None:
        current_process.terminate()
    update_state(running=False, message="Stopping...")
    return jsonify({"ok": True})


@app.get("/api/status")
def get_status():
    with state_lock:
        return jsonify(state)


if __name__ == "__main__":
    threading.Timer(0.6, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
    app.run(debug=True, use_reloader=False)
