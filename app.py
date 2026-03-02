import json
import os
import platform
import re
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from PIL import Image, ImageTk
except ImportError:  # Pillow is optional, thumbnails will fallback to text-only rows.
    Image = None
    ImageTk = None

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:
    DND_FILES = None
    TkinterDnD = None


@dataclass
class EncodeConfig:
    target_mb: float
    codec: str = "libx264"
    preset: str = "medium"
    container: str = "mp4"
    audio_codec: str = "aac"
    audio_kbps: int = 96
    mute: bool = False
    safety_margin: float = 0.98
    scale_filter: str | None = None


@dataclass
class VideoJob:
    input_path: str
    start_s: float = 0.0
    end_s: float | None = None
    mute: bool = False


def ffprobe_duration_seconds(input_path: str) -> float:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        input_path,
    ]
    out = subprocess.check_output(cmd, text=True)
    data = json.loads(out)
    return float(data["format"]["duration"])


def null_device() -> str:
    return "NUL" if platform.system().lower().startswith("win") else "/dev/null"


def compute_video_bitrate_kbps(
    target_mb: float,
    duration_s: float,
    audio_kbps: int,
    mute: bool,
    safety_margin: float,
) -> int:
    target_bytes = target_mb * 1024 * 1024
    target_bits = target_bytes * 8 * safety_margin
    total_bps = target_bits / max(duration_s, 0.001)

    audio_bps = 0 if mute else audio_kbps * 1000
    video_bps = max(total_bps - audio_bps, 100_000)
    video_kbps = int(video_bps / 1000)
    return max(video_kbps, 100)


def parse_ffmpeg_time_to_seconds(line: str) -> float | None:
    match = re.search(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", line)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def run_ffmpeg_with_progress(cmd: list[str], pass_weight: float, progress_callback, stop_event, seg_dur: float):
    process = subprocess.Popen(
        cmd,
        stderr=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    try:
        while True:
            if stop_event.is_set():
                process.terminate()
                raise RuntimeError("Stopped by user")

            line = process.stderr.readline() if process.stderr else ""
            if not line:
                if process.poll() is not None:
                    break
                continue

            sec = parse_ffmpeg_time_to_seconds(line)
            if sec is not None and progress_callback:
                progress_callback(min(sec / max(seg_dur, 0.001), 1.0) * pass_weight)

        code = process.wait()
        if code != 0:
            raise RuntimeError(f"ffmpeg failed with exit code {code}")
    finally:
        if process.stderr:
            process.stderr.close()


def two_pass_encode(
    input_path: str,
    output_path: str,
    start_s: float | None,
    end_s: float | None,
    cfg: EncodeConfig,
    progress_callback=None,
    stop_event: threading.Event | None = None,
) -> None:
    stop_event = stop_event or threading.Event()

    full_dur = ffprobe_duration_seconds(input_path)
    if start_s is None:
        start_s = 0.0
    if end_s is None or end_s > full_dur:
        end_s = full_dur
    seg_dur = max(end_s - start_s, 0.001)

    v_kbps = compute_video_bitrate_kbps(cfg.target_mb, seg_dur, cfg.audio_kbps, cfg.mute, cfg.safety_margin)
    logbase = str(Path(output_path).with_suffix("")) + "_2pass"

    trim_args = ["-ss", f"{start_s}", "-to", f"{end_s}"]
    vf_args = ["-vf", cfg.scale_filter] if cfg.scale_filter else []

    pass1 = [
        "ffmpeg", "-y",
        "-i", input_path,
        *trim_args,
        *vf_args,
        "-c:v", cfg.codec,
        "-b:v", f"{v_kbps}k",
        "-preset", cfg.preset,
        "-pass", "1",
        "-passlogfile", logbase,
        "-an",
        "-f", "mp4",
        null_device(),
    ]

    pass2 = [
        "ffmpeg", "-y",
        "-i", input_path,
        *trim_args,
        *vf_args,
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

    pass2 += ["-movflags", "+faststart", output_path]

    try:
        run_ffmpeg_with_progress(pass1, 0.5, progress_callback, stop_event, seg_dur)
        if progress_callback:
            progress_callback(0.5)
        run_ffmpeg_with_progress(pass2, 0.5, lambda p: progress_callback(0.5 + p) if progress_callback else None, stop_event, seg_dur)
        if progress_callback:
            progress_callback(1.0)
    finally:
        for suffix in ["-0.log", "-0.log.mbtree", ".log", ".log.mbtree"]:
            p = Path(logbase + suffix)
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass


class VideoRow(ttk.Frame):
    def __init__(self, parent, app, file_path: str):
        super().__init__(parent, padding=6, relief="solid", borderwidth=1)
        self.app = app
        self.file_path = file_path
        self.duration = ffprobe_duration_seconds(file_path)
        self.thumbnail_ref = None

        thumb = ttk.Label(self, text="No preview", width=18)
        thumb.grid(row=0, column=0, rowspan=3, padx=(0, 8), sticky="n")
        self._load_thumbnail(thumb)

        ttk.Label(self, text=Path(file_path).name).grid(row=0, column=1, columnspan=5, sticky="w")

        self.mute_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(self, text="Mute", variable=self.mute_var).grid(row=1, column=1, sticky="w")

        ttk.Label(self, text="Start (s)").grid(row=1, column=2, sticky="e", padx=(6, 2))
        self.start_var = tk.StringVar(value="0")
        ttk.Entry(self, textvariable=self.start_var, width=8).grid(row=1, column=3, sticky="w")

        ttk.Label(self, text="End (s)").grid(row=1, column=4, sticky="e", padx=(6, 2))
        self.end_var = tk.StringVar(value=f"{self.duration:.2f}")
        ttk.Entry(self, textvariable=self.end_var, width=8).grid(row=1, column=5, sticky="w")

        ttk.Label(self, text=f"Length: {self.duration:.2f}s").grid(row=2, column=1, columnspan=5, sticky="w")

    def _load_thumbnail(self, label_widget: ttk.Label):
        if not Image or not ImageTk:
            return
        try:
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                thumb_path = tmp.name
            cmd = [
                "ffmpeg", "-y", "-ss", "0", "-i", self.file_path,
                "-frames:v", "1", "-q:v", "2", thumb_path,
            ]
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            img = Image.open(thumb_path)
            img.thumbnail((120, 68))
            self.thumbnail_ref = ImageTk.PhotoImage(img)
            label_widget.configure(image=self.thumbnail_ref, text="")
        except Exception:
            pass
        finally:
            try:
                os.remove(thumb_path)
            except Exception:
                pass

    def to_job(self) -> VideoJob:
        start_s = float(self.start_var.get() or 0)
        end_s = float(self.end_var.get() or self.duration)
        if end_s <= start_s:
            raise ValueError(f"End time must be greater than start for {Path(self.file_path).name}")
        return VideoJob(
            input_path=self.file_path,
            start_s=max(0.0, start_s),
            end_s=min(end_s, self.duration),
            mute=self.mute_var.get(),
        )


class CompressorApp:
    RESOLUTION_OPTIONS = {
        "Original": None,
        "1080p": "scale=-2:1080",
        "720p": "scale=-2:720",
        "480p": "scale=-2:480",
    }

    CODEC_OPTIONS = {
        "H.264": "libx264",
        "H.265": "libx265",
    }

    def __init__(self, root):
        self.root = root
        self.root.title("Video Compressor")
        self.rows: list[VideoRow] = []
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None

        self._build_ui()

    def _build_ui(self):
        container = ttk.Frame(self.root, padding=10)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=3)
        container.columnconfigure(1, weight=2)

        left = ttk.LabelFrame(container, text="Queue", padding=10)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)

        self.drop_zone = tk.Text(left, height=4)
        self.drop_zone.insert("1.0", "Drag and drop videos here\n(or click 'Add Videos')")
        self.drop_zone.configure(state="disabled")
        self.drop_zone.grid(row=0, column=0, sticky="ew", pady=(0, 8))

        if TkinterDnD and DND_FILES:
            self.drop_zone.drop_target_register(DND_FILES)
            self.drop_zone.dnd_bind("<<Drop>>", self._on_drop)

        ttk.Button(left, text="Add Videos", command=self._select_files).grid(row=0, column=1, padx=(8, 0), sticky="n")

        self.rows_frame = ttk.Frame(left)
        self.rows_frame.grid(row=1, column=0, columnspan=2, sticky="nsew")

        right = ttk.LabelFrame(container, text="Options", padding=10)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(1, weight=1)

        ttk.Label(right, text="Goal Size (MB)").grid(row=0, column=0, sticky="w")
        self.target_mb_var = tk.StringVar(value="50")
        ttk.Entry(right, textvariable=self.target_mb_var).grid(row=0, column=1, sticky="ew")

        ttk.Label(right, text="Resolution").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.resolution_var = tk.StringVar(value="Original")
        ttk.Combobox(right, textvariable=self.resolution_var, values=list(self.RESOLUTION_OPTIONS.keys()), state="readonly").grid(row=1, column=1, sticky="ew", pady=(8, 0))

        ttk.Label(right, text="Video Encoder").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.codec_var = tk.StringVar(value="H.264")
        ttk.Combobox(right, textvariable=self.codec_var, values=list(self.CODEC_OPTIONS.keys()), state="readonly").grid(row=2, column=1, sticky="ew", pady=(8, 0))

        self.progress_var = tk.DoubleVar(value=0)
        ttk.Progressbar(right, variable=self.progress_var, mode="determinate", maximum=100).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(16, 8))

        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(right, textvariable=self.status_var).grid(row=4, column=0, columnspan=2, sticky="w")

        buttons = ttk.Frame(right)
        buttons.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        buttons.columnconfigure(0, weight=1)
        buttons.columnconfigure(1, weight=1)
        ttk.Button(buttons, text="Start", command=self.start).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(buttons, text="Stop", command=self.stop).grid(row=0, column=1, sticky="ew", padx=(4, 0))

    def _select_files(self):
        files = filedialog.askopenfilenames(
            filetypes=[("Video Files", "*.mp4 *.mov *.mkv *.avi *.webm"), ("All Files", "*.*")]
        )
        self._add_files(list(files))

    def _on_drop(self, event):
        files = self.root.tk.splitlist(event.data)
        self._add_files(list(files))

    def _add_files(self, files: list[str]):
        for file_path in files:
            if not Path(file_path).exists():
                continue
            row = VideoRow(self.rows_frame, self, file_path)
            row.pack(fill="x", pady=4)
            self.rows.append(row)

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        if not self.rows:
            messagebox.showwarning("No videos", "Please add at least one video.")
            return

        try:
            jobs = [row.to_job() for row in self.rows]
            target_mb = float(self.target_mb_var.get())
        except ValueError as exc:
            messagebox.showerror("Invalid input", str(exc))
            return

        self.stop_event.clear()
        self.progress_var.set(0)
        self.status_var.set("Starting...")

        cfg = EncodeConfig(
            target_mb=target_mb,
            codec=self.CODEC_OPTIONS[self.codec_var.get()],
            preset="medium",
            scale_filter=self.RESOLUTION_OPTIONS[self.resolution_var.get()],
        )

        self.worker = threading.Thread(target=self._run_queue, args=(jobs, cfg), daemon=True)
        self.worker.start()

    def stop(self):
        self.stop_event.set()
        self.status_var.set("Stopping...")

    def _set_progress(self, value: float):
        self.progress_var.set(max(0.0, min(100.0, value)))

    def _run_queue(self, jobs: list[VideoJob], cfg: EncodeConfig):
        total = len(jobs)
        out_dir = Path.cwd() / "outputs"
        out_dir.mkdir(exist_ok=True)

        try:
            for index, job in enumerate(jobs):
                if self.stop_event.is_set():
                    self.root.after(0, lambda: self.status_var.set("Stopped"))
                    return

                name = Path(job.input_path).stem
                out_name = f"{name}_compressed_{uuid.uuid4().hex[:6]}.mp4"
                out_path = str(out_dir / out_name)

                local_cfg = EncodeConfig(**{**cfg.__dict__, "mute": job.mute})

                def progress_update(job_progress: float):
                    global_progress = ((index + job_progress) / total) * 100
                    self.root.after(0, lambda v=global_progress: self._set_progress(v))
                    self.root.after(0, lambda: self.status_var.set(f"Compressing {index + 1}/{total}: {Path(job.input_path).name}"))

                two_pass_encode(
                    input_path=job.input_path,
                    output_path=out_path,
                    start_s=job.start_s,
                    end_s=job.end_s,
                    cfg=local_cfg,
                    progress_callback=progress_update,
                    stop_event=self.stop_event,
                )

            self.root.after(0, lambda: self.status_var.set("Done"))
            self.root.after(0, lambda: self._set_progress(100))
        except Exception as exc:
            self.root.after(0, lambda: messagebox.showerror("Compression error", str(exc)))
            self.root.after(0, lambda: self.status_var.set("Error"))


def main():
    root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")

    CompressorApp(root)
    root.minsize(1000, 550)
    root.mainloop()


if __name__ == "__main__":
    main()
