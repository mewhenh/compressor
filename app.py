import json
import os
import platform
import subprocess
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import ttk, messagebox, filedialog
import queue
import re
import time
import tempfile

try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    import tkinterdnd2 as tkdnd
    DND_AVAILABLE = True
except ImportError:
    DND_AVAILABLE = False


# ──────────────────────────────────────────────
#  Core encode logic
# ──────────────────────────────────────────────

@dataclass
class EncodeConfig:
    target_mb: float
    codec: str = "libx264"
    preset: str = "medium"
    container: str = "mp4"
    audio_codec: str = "aac"
    audio_kbps: int = 96
    mute: bool = False
    safety_margin: float = 0.94
    resolution: str = "original"


def run_cmd(cmd: list, log_fn=None) -> None:
    if log_fn:
        log_fn("$ " + " ".join(cmd))
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_cmd_progress(cmd: list, duration_s: float, progress_cb,
                     pct_start=0, pct_end=100, log_fn=None) -> None:
    """Run ffmpeg, writing -progress to a temp file we poll every 100ms."""
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False)
    tmp_path = tmp.name
    tmp.close()

    # Swap "pipe:1" placeholder for the real temp path
    cmd = [tmp_path if a == "__PROGRESS__" else a for a in cmd]

    if log_fn:
        log_fn("$ " + " ".join(cmd))

    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    pct_range = pct_end - pct_start
    last_pct = pct_start

    while proc.poll() is None:
        try:
            with open(tmp_path, "r") as f:
                content = f.read()
            for line in reversed(content.splitlines()):
                if line.startswith("out_time_ms="):
                    ms_str = line.split("=", 1)[1].strip()
                    if ms_str and ms_str != "N/A":
                        ms = int(ms_str)
                        ratio = min(1.0, ms / 1_000_000 / max(duration_s, 0.001))
                        pct = pct_start + int(ratio * pct_range)
                        if pct > last_pct:
                            last_pct = pct
                            progress_cb(pct)
                    break
        except Exception:
            pass
        time.sleep(0.1)

    progress_cb(pct_end)

    try:
        os.unlink(tmp_path)
    except OSError:
        pass

    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd)


def ffprobe_info(input_path: str) -> dict:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration:stream=width,height",
        "-of", "json", input_path
    ]
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
        data = json.loads(out)
        duration = float(data.get("format", {}).get("duration", 0) or 0)
        width, height = 0, 0
        for s in data.get("streams", []):
            if s.get("width"):
                width = s["width"]
                height = s["height"]
                break
        return {"duration": duration, "width": width, "height": height}
    except Exception:
        return {"duration": 0, "width": 0, "height": 0}


def null_device() -> str:
    return "NUL" if platform.system().lower().startswith("win") else "/dev/null"


def compute_video_bitrate_kbps(target_mb, duration_s, audio_kbps, mute, safety_margin):
    target_bytes = target_mb * 1024 * 1024
    target_bits = target_bytes * 8 * safety_margin
    total_bps = target_bits / max(duration_s, 0.001)
    audio_bps = 0 if mute else audio_kbps * 1000
    video_bps = max(total_bps - audio_bps, 100_000)
    return max(int(video_bps / 1000), 100)


def two_pass_encode(input_path, output_path, start_s, end_s, cfg: EncodeConfig,
                    log_fn=None, progress_cb=None):
    input_path = str(input_path)
    output_path = str(output_path)

    full_dur = ffprobe_info(input_path)["duration"]
    if start_s is None:
        start_s = 0.0
    if end_s is None or end_s > full_dur:
        end_s = full_dur
    seg_dur = max(end_s - start_s, 0.001)

    v_kbps = compute_video_bitrate_kbps(
        cfg.target_mb, seg_dur, cfg.audio_kbps, cfg.mute, cfg.safety_margin)
    logbase = str(Path(__file__).parent / (Path(output_path).stem + "_2pass"))
    trim_args = ["-ss", f"{start_s}", "-to", f"{end_s}"]

    vf_args = []
    if cfg.resolution != "original":
        h = cfg.resolution.replace("p", "")
        vf_args = ["-vf", f"scale=-2:{h}"]

    cb = progress_cb or (lambda v: None)

    pass1 = [
        "ffmpeg", "-y", "-i", input_path, *trim_args,
        "-c:v", cfg.codec, "-b:v", f"{v_kbps}k",
        "-preset", cfg.preset, "-pass", "1", "-passlogfile", logbase,
        *vf_args, "-an",
        "-progress", "__PROGRESS__",
        "-f", "mp4", null_device()
    ]
    pass2 = [
        "ffmpeg", "-y", "-i", input_path, *trim_args,
        "-c:v", cfg.codec, "-b:v", f"{v_kbps}k",
        "-preset", cfg.preset, "-pass", "2", "-passlogfile", logbase,
        *vf_args,
    ]
    if cfg.mute:
        pass2 += ["-an"]
    else:
        pass2 += ["-c:a", cfg.audio_codec, "-b:a", f"{cfg.audio_kbps}k"]
    pass2 += ["-movflags", "+faststart", "-progress", "__PROGRESS__", output_path]

    try:
        cb(0)
        run_cmd_progress(pass1, seg_dur, cb,
                         pct_start=0,  pct_end=50,  log_fn=log_fn)
        run_cmd_progress(pass2, seg_dur, cb,
                         pct_start=50, pct_end=100, log_fn=log_fn)
    finally:
        for suffix in ["-0.log", "-0.log.mbtree", ".log", ".log.mbtree"]:
            p = Path(logbase + suffix)
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass


def extract_thumbnail(video_path: str, size=(120, 68)):
    if not PIL_AVAILABLE:
        return None
    try:
        import tempfile
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp.close()
        cmd = [
            "ffmpeg", "-y", "-ss", "1", "-i", video_path,
            "-vframes", "1", "-q:v", "2",
            "-vf", (f"scale={size[0]}:{size[1]}:force_original_aspect_ratio=decrease,"
                    f"pad={size[0]}:{size[1]}:(ow-iw)/2:(oh-ih)/2:black"),
            tmp.name
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        img = Image.open(tmp.name)
        photo = ImageTk.PhotoImage(img)
        os.unlink(tmp.name)
        return photo
    except Exception:
        return None


# ──────────────────────────────────────────────
#  Theme constants
# ──────────────────────────────────────────────

BG       = "#111827"
PANEL    = "#1f2937"
CARD     = "#1a2332"
BORDER   = "#2d3748"
ACCENT   = "#6366f1"
RED      = "#ef4444"
GREEN    = "#22c55e"
ORANGE   = "#f97316"
TEXT     = "#f1f5f9"
SUBTEXT  = "#94a3b8"
INPUT_BG = "#0f172a"

RESOLUTIONS   = ["original", "2160p", "1440p", "1080p", "720p", "480p", "360p"]
CODECS_LABELS = ["H.264 (libx264)", "H.265 (libx265)"]
CODECS_MAP    = {"H.264 (libx264)": "libx264", "H.265 (libx265)": "libx265"}
PRESETS       = ["ultrafast", "superfast", "veryfast", "faster", "fast",
                 "medium", "slow", "slower", "veryslow"]
VIDEO_EXTS    = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".wmv", ".flv"}


# ──────────────────────────────────────────────
#  Video Card widget
# ──────────────────────────────────────────────

class VideoCard(tk.Frame):
    def __init__(self, parent, video_path: str, remove_cb, scroll_update, **kwargs):
        super().__init__(parent, bg=CARD, highlightthickness=1,
                         highlightbackground=BORDER, **kwargs)
        self.video_path = video_path
        self.remove_cb = remove_cb
        self.scroll_update = scroll_update
        self._photo = None
        self._build()
        self._load_info()

    # ── helpers ──────────────────────────────────────────────────

    def _lbl(self, parent, text, color=SUBTEXT, bold=False):
        font = ("Segoe UI", 8, "bold") if bold else ("Segoe UI", 8)
        return tk.Label(parent, text=text, bg=parent["bg"],
                        fg=color, font=font)

    def _entry(self, parent, var, w=7):
        return tk.Entry(parent, textvariable=var, width=w,
                        bg=INPUT_BG, fg=TEXT, insertbackground=TEXT,
                        relief="flat", font=("Segoe UI", 8),
                        highlightthickness=1, highlightbackground=BORDER)

    # ── build ─────────────────────────────────────────────────────

    def _build(self):
        self.columnconfigure(1, weight=1)

        # Thumbnail column
        self.thumb = tk.Label(self, bg="#0a1020", width=16, height=5,
                              text="🎬", fg=SUBTEXT, font=("Segoe UI", 16))
        self.thumb.grid(row=0, column=0, rowspan=3, padx=(8, 10),
                        pady=8, sticky="ns")

        # Header: filename + × button
        header = tk.Frame(self, bg=CARD)
        header.grid(row=0, column=1, sticky="ew", pady=(8, 2), padx=(0, 8))
        header.columnconfigure(0, weight=1)

        name = Path(self.video_path).name
        name_disp = name if len(name) <= 42 else name[:39] + "…"
        tk.Label(header, text=name_disp, bg=CARD, fg=TEXT,
                 font=("Segoe UI", 9, "bold"), anchor="w"
                 ).grid(row=0, column=0, sticky="ew")
        tk.Button(header, text="✕", bg=CARD, fg=RED, relief="flat", bd=0,
                  font=("Segoe UI", 10, "bold"), cursor="hand2",
                  activebackground=CARD, activeforeground=RED,
                  command=lambda: self.remove_cb(self)
                  ).grid(row=0, column=1)

        # Status line
        self.status_var = tk.StringVar(value="Loading info…")
        self.status_lbl = tk.Label(self, textvariable=self.status_var,
                                   bg=CARD, fg=SUBTEXT,
                                   font=("Segoe UI", 8), anchor="w")
        self.status_lbl.grid(row=1, column=1, sticky="ew", padx=(0, 8))

        # Controls row
        ctrl = tk.Frame(self, bg=CARD)
        ctrl.grid(row=2, column=1, sticky="ew", pady=(4, 8), padx=(0, 8))

        self._lbl(ctrl, "Start (s):").pack(side="left")
        self.start_var = tk.StringVar(value="0")
        self._entry(ctrl, self.start_var, 6).pack(side="left", padx=(3, 12))

        self._lbl(ctrl, "End (s):").pack(side="left")
        self.end_var = tk.StringVar(value="?")
        self._entry(ctrl, self.end_var, 8).pack(side="left", padx=(3, 12))

        self._lbl(ctrl, "Mute:").pack(side="left", padx=(4, 2))
        self.mute_var = tk.BooleanVar(value=False)
        tk.Checkbutton(ctrl, variable=self.mute_var,
                       bg=CARD, fg=TEXT, activebackground=CARD,
                       selectcolor=ACCENT, relief="flat",
                       cursor="hand2").pack(side="left")

        # Progress bar (hidden until encoding)
        self.prog = ttk.Progressbar(self, mode="determinate", length=10)
        self.prog.grid(row=3, column=0, columnspan=2,
                       sticky="ew", padx=8, pady=(0, 8))
        self.prog.grid_remove()

    # ── info loading ─────────────────────────────────────────────

    def _load_info(self):
        def worker():
            info = ffprobe_info(self.video_path)
            dur = info["duration"]
            w, h = info["width"], info["height"]
            dur_s = f"{dur:.2f}" if dur else "?"
            res = f"{w}×{h}  " if w else ""
            self.end_var.set(dur_s)
            self.status_var.set(f"{res}{dur_s}s")
            if PIL_AVAILABLE:
                photo = extract_thumbnail(self.video_path)
                if photo:
                    self._photo = photo
                    self.thumb.config(image=photo, text="",
                                      width=120, height=68)
            self.scroll_update()
        threading.Thread(target=worker, daemon=True).start()

    # ── public API ───────────────────────────────────────────────

    def set_status(self, text, color=SUBTEXT):
        self.status_var.set(text)
        self.status_lbl.config(fg=color)

    def show_progress(self, show: bool):
        if show:
            self.prog["value"] = 0
            self.prog.grid()
        else:
            self.prog.grid_remove()

    def set_progress(self, value: int):
        """Update progress bar (0-100) — safe to call from any thread."""
        self.prog["value"] = value

    @property
    def values(self):
        try:
            start = float(self.start_var.get())
        except ValueError:
            start = 0.0
        try:
            raw = self.end_var.get()
            end = float(raw) if raw not in ("?", "—", "") else None
        except ValueError:
            end = None
        return {"start": start, "end": end, "mute": self.mute_var.get()}


# ──────────────────────────────────────────────
#  Main application window
# ──────────────────────────────────────────────

_TkBase = tkdnd.TkinterDnD.Tk if DND_AVAILABLE else tk.Tk

class App(_TkBase):
    def __init__(self):
        super().__init__()
        self.title("Video Compressor")
        self.configure(bg=BG)
        self.geometry("980x680")
        self.minsize(800, 540)

        self._cards: list[VideoCard] = []
        self._running = False
        self._stop_flag = False
        self._log_q: queue.Queue = queue.Queue()

        self._apply_style()
        self._build_layout()
        self._poll_log()

    # ── style ────────────────────────────────────────────────────

    def _apply_style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("TProgressbar",
                     troughcolor=INPUT_BG, background=ACCENT,
                     borderwidth=0)
        s.configure("TCombobox",
                     fieldbackground=INPUT_BG, background=INPUT_BG,
                     foreground=TEXT, selectbackground=ACCENT,
                     borderwidth=0, relief="flat")
        s.map("TCombobox",
              fieldbackground=[("readonly", INPUT_BG)],
              foreground=[("readonly", TEXT)],
              selectbackground=[("readonly", ACCENT)])

    # ── layout ───────────────────────────────────────────────────

    def _build_layout(self):
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=0)
        self.rowconfigure(0, weight=1)

        # ── Left panel ──────────────────────────────────────────
        left = tk.Frame(self, bg=BG)
        left.grid(row=0, column=0, sticky="nsew", padx=(16, 8), pady=16)
        left.rowconfigure(2, weight=1)
        left.columnconfigure(0, weight=1)

        tk.Label(left, text="Queue", bg=BG, fg=TEXT,
                 font=("Segoe UI", 14, "bold")
                 ).grid(row=0, column=0, sticky="w", pady=(0, 10))

        # Drop zone
        dz = tk.Frame(left, bg=PANEL, highlightthickness=2,
                      highlightbackground=ACCENT, cursor="hand2")
        dz.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        dz.columnconfigure(0, weight=1)
        self._dz_inner = tk.Label(
            dz,
            text="⬇   Drop video files here   or   click to browse",
            bg=PANEL, fg=SUBTEXT,
            font=("Segoe UI", 10), pady=20, cursor="hand2")
        self._dz_inner.grid(row=0, column=0, sticky="ew")
        dz.bind("<Button-1>", self._browse)
        self._dz_inner.bind("<Button-1>", self._browse)
        dz.bind("<Enter>", lambda e: dz.config(highlightbackground=TEXT))
        dz.bind("<Leave>", lambda e: dz.config(highlightbackground=ACCENT))
        self._drop_zone = dz

        # Scrollable card area
        wrap = tk.Frame(left, bg=BG)
        wrap.grid(row=2, column=0, sticky="nsew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

        self._canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(wrap, orient="vertical",
                             command=self._canvas.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        self._canvas.configure(yscrollcommand=vsb.set)

        self._card_frame = tk.Frame(self._canvas, bg=BG)
        self._cf_id = self._canvas.create_window(
            (0, 0), window=self._card_frame, anchor="nw")

        self._card_frame.bind("<Configure>", self._sync_scroll)
        self._canvas.bind("<Configure>",
                          lambda e: self._canvas.itemconfig(
                              self._cf_id, width=e.width))
        for w in (self._canvas, self._card_frame):
            w.bind("<MouseWheel>",
                   lambda e: self._canvas.yview_scroll(
                       -1 * (e.delta // 120), "units"))

        # DnD wiring
        if DND_AVAILABLE:
            for w in (self, dz, self._dz_inner):
                w.drop_target_register(tkdnd.DND_FILES)
                w.dnd_bind("<<Drop>>", self._on_drop)

        # ── Thin divider ────────────────────────────────────────
        tk.Frame(self, bg=BORDER, width=1).grid(
            row=0, column=0, sticky="nse", pady=20)

        # ── Right panel ─────────────────────────────────────────
        right = tk.Frame(self, bg=PANEL, width=256)
        right.grid(row=0, column=1, sticky="nsew",
                   padx=(0, 16), pady=16)
        right.columnconfigure(0, weight=1)
        right.grid_propagate(False)
        self._build_settings(right)

    def _sync_scroll(self, e=None):
        self._canvas.configure(
            scrollregion=self._canvas.bbox("all"))

    # ── settings panel ───────────────────────────────────────────

    def _build_settings(self, p):
        P = 14  # horizontal padding

        def shead(text, top=14):
            tk.Label(p, text=text, bg=PANEL, fg=ACCENT,
                     font=("Segoe UI", 8, "bold")
                     ).pack(anchor="w", padx=P, pady=(top, 3))

        def combo(var, values):
            cb = ttk.Combobox(p, textvariable=var, values=values,
                              state="readonly", font=("Segoe UI", 9))
            cb.pack(fill="x", padx=P, pady=(0, 2))
            return cb

        tk.Label(p, text="Settings", bg=PANEL, fg=TEXT,
                 font=("Segoe UI", 14, "bold")
                 ).pack(anchor="w", padx=P, pady=(16, 12))

        # Target size
        shead("Target Size", top=0)
        row = tk.Frame(p, bg=PANEL)
        row.pack(fill="x", padx=P)
        self.size_var = tk.StringVar(value="50")
        tk.Entry(row, textvariable=self.size_var, width=8,
                 bg=INPUT_BG, fg=TEXT, insertbackground=TEXT,
                 relief="flat", font=("Segoe UI", 10),
                 highlightthickness=1, highlightbackground=BORDER
                 ).pack(side="left")
        tk.Label(row, text=" MB", bg=PANEL, fg=SUBTEXT,
                 font=("Segoe UI", 9)).pack(side="left")

        # Resolution
        shead("Resolution")
        self.res_var = tk.StringVar(value="original")
        combo(self.res_var, RESOLUTIONS)

        # Codec
        shead("Codec")
        self.codec_var = tk.StringVar(value="H.264 (libx264)")
        combo(self.codec_var, CODECS_LABELS)

        # Preset
        shead("Preset")
        self.preset_var = tk.StringVar(value="medium")
        combo(self.preset_var, PRESETS)

        # Spacer
        tk.Frame(p, bg=PANEL, height=20).pack()

        # Start button
        self.start_btn = tk.Button(
            p, text="▶  Start Encoding",
            bg=GREEN, fg="#000",
            activebackground="#16a34a", activeforeground="#000",
            font=("Segoe UI", 10, "bold"),
            relief="flat", bd=0, cursor="hand2", pady=11,
            command=self._start)
        self.start_btn.pack(fill="x", padx=P, pady=(0, 6))

        # Stop button
        self.stop_btn = tk.Button(
            p, text="■  Stop",
            bg=ORANGE, fg="#000",
            activebackground="#ea580c", activeforeground="#000",
            font=("Segoe UI", 10, "bold"),
            relief="flat", bd=0, cursor="hand2", pady=11,
            state="disabled", command=self._stop)
        self.stop_btn.pack(fill="x", padx=P)

        # Log area
        shead("Log")
        self.log_box = tk.Text(
            p, bg=INPUT_BG, fg=SUBTEXT,
            font=("Consolas", 7), relief="flat",
            wrap="word", state="disabled",
            highlightthickness=1, highlightbackground=BORDER)
        self.log_box.pack(fill="both", expand=True,
                          padx=P, pady=(2, 16))

    # ── file handling ────────────────────────────────────────────

    def _browse(self, event=None):
        files = filedialog.askopenfilenames(
            title="Select videos",
            filetypes=[
                ("Video files",
                 "*.mp4 *.mov *.avi *.mkv *.webm *.m4v *.wmv *.flv"),
                ("All files", "*.*")])
        for f in files:
            self._add_video(f)

    def _on_drop(self, event):
        for match in re.findall(r'\{([^}]+)\}|(\S+)', event.data):
            path = match[0] or match[1]
            if path and Path(path).is_file() \
                    and Path(path).suffix.lower() in VIDEO_EXTS:
                self._add_video(path)

    def _add_video(self, path: str):
        path = str(Path(path).resolve())
        if any(c.video_path == path for c in self._cards):
            return
        card = VideoCard(
            self._card_frame, path,
            remove_cb=self._remove_card,
            scroll_update=self._sync_scroll)
        card.pack(fill="x", pady=(0, 6), padx=4)
        self._cards.append(card)
        self._sync_scroll()

    def _remove_card(self, card: VideoCard):
        if self._running:
            return
        self._cards.remove(card)
        card.destroy()
        self._sync_scroll()

    # ── encoding ─────────────────────────────────────────────────

    def _start(self):
        if not self._cards:
            messagebox.showinfo(
                "No videos", "Add videos to the queue first.")
            return
        try:
            target_mb = float(self.size_var.get())
            assert target_mb > 0
        except (ValueError, AssertionError):
            messagebox.showerror(
                "Invalid", "Enter a valid positive number for target size.")
            return

        self._running = True
        self._stop_flag = False
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")

        cards   = list(self._cards)
        codec   = CODECS_MAP[self.codec_var.get()]
        res     = self.res_var.get()
        preset  = self.preset_var.get()

        def worker():
            for card in cards:
                if self._stop_flag:
                    self.after(0, card.set_status, "⏭ skipped", SUBTEXT)
                    continue

                self.after(0, card.set_status, "⏳ encoding…", ACCENT)
                self.after(0, card.show_progress, True)

                v   = card.values
                src = card.video_path
                out = str(Path(src).parent /
                           f"{Path(src).stem}_compressed{Path(src).suffix or '.mp4'}")
                cfg = EncodeConfig(
                    target_mb=target_mb, codec=codec,
                    preset=preset, mute=v["mute"], resolution=res)
                try:
                    two_pass_encode(
                        input_path=src, output_path=out,
                        start_s=v["start"], end_s=v["end"],
                        cfg=cfg,
                        log_fn=lambda msg: self._log_q.put(msg),
                        progress_cb=lambda pct: self.after(0, card.set_progress, pct),
                    )
                    size_mb = os.path.getsize(out) / (1024 * 1024)
                    self.after(0, card.set_status,
                               f"✓ done — {size_mb:.1f} MB", GREEN)
                except Exception as ex:
                    self.after(0, card.set_status, f"✗ {ex}", RED)
                    self._log_q.put(f"ERROR: {ex}")
                finally:
                    self.after(0, card.show_progress, False)

            self._running = False
            self._log_q.put("── All jobs finished ──")
            self.after(0, self.start_btn.config, {"state": "normal"})
            self.after(0, self.stop_btn.config,  {"state": "disabled"})

        threading.Thread(target=worker, daemon=True).start()

    def _stop(self):
        self._stop_flag = True
        self.stop_btn.config(state="disabled")
        self._log_q.put("Stop requested — will finish current job first…")

    # ── log polling ──────────────────────────────────────────────

    def _poll_log(self):
        try:
            while True:
                msg = self._log_q.get_nowait()
                self.log_box.config(state="normal")
                self.log_box.insert("end", msg + "\n")
                self.log_box.see("end")
                self.log_box.config(state="disabled")
        except queue.Empty:
            pass
        self.after(120, self._poll_log)


# ──────────────────────────────────────────────
#  Entry point
# ──────────────────────────────────────────────

def main():
    root = App()

    if not DND_AVAILABLE:
        root._log_q.put(
            "Tip: pip install tkinterdnd2  →  enables drag & drop")
    if not PIL_AVAILABLE:
        root._log_q.put(
            "Tip: pip install Pillow       →  enables video thumbnails")

    root.mainloop()


if __name__ == "__main__":
    main()