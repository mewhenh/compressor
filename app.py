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
    from PIL import Image, ImageTk, ImageDraw
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    import tkinterdnd2 as tkdnd
    DND_AVAILABLE = True
except ImportError:
    DND_AVAILABLE = False


# ──────────────────────────────────────────────
#  Core encode logic (unchanged)
# ──────────────────────────────────────────────

@dataclass
class EncodeConfig:
    target_mb: float
    codec: str = "libx264"
    preset: str = "slow"
    container: str = "mp4"
    audio_codec: str = "aac"
    audio_kbps: int = 128
    mute: bool = False
    safety_margin: float = 0.94
    resolution: str = "original"


def run_cmd(cmd: list, log_fn=None) -> None:
    if log_fn:
        log_fn("$ " + " ".join(cmd))
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_cmd_progress(cmd: list, duration_s: float, progress_cb,
                     pct_start=0, pct_end=100, log_fn=None) -> None:
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    tmp_path = tmp.name
    tmp.close()
    cmd = [tmp_path if a == "__PROGRESS__" else a for a in cmd]
    if log_fn:
        log_fn("$ " + " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        *vf_args, "-an", "-progress", "__PROGRESS__",
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
        run_cmd_progress(pass1, seg_dur, cb, pct_start=0, pct_end=50, log_fn=log_fn)
        run_cmd_progress(pass2, seg_dur, cb, pct_start=50, pct_end=100, log_fn=log_fn)
    finally:
        for suffix in ["-0.log", "-0.log.mbtree", ".log", ".log.mbtree"]:
            p = Path(logbase + suffix)
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass


def extract_thumbnail(video_path: str, size=(128, 72)):
    if not PIL_AVAILABLE:
        return None
    try:
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
        img = Image.open(tmp.name).convert("RGBA")
        mask = Image.new("L", img.size, 0)
        draw = ImageDraw.Draw(mask)
        draw.rounded_rectangle([0, 0, img.size[0] - 1, img.size[1] - 1], radius=6, fill=255)
        img.putalpha(mask)
        photo = ImageTk.PhotoImage(img)
        os.unlink(tmp.name)
        return photo
    except Exception:
        return None


# ──────────────────────────────────────────────
#  Design tokens — GitHub-dark inspired palette
# ──────────────────────────────────────────────

BG         = "#0d1117"
SURFACE    = "#161b22"
SURFACE2   = "#21262d"
BORDER     = "#30363d"
BORDER_SUB = "#21262d"
ACCENT     = "#7c3aed"
ACCENT_LT  = "#a78bfa"
ACCENT_DIM = "#2e1065"
ACCENT_MID = "#4c1d95"
GREEN      = "#3fb950"
RED        = "#f85149"
ORANGE     = "#e3b341"
TEXT       = "#e6edf3"
TEXT2      = "#8b949e"
TEXT3      = "#484f58"
INPUT_BG   = "#0d1117"

RESOLUTIONS   = ["original", "2160p", "1440p", "1080p", "720p", "480p", "360p"]

CODECS_LABELS = ["H.264 (nvenc)", 
                 "H.265 (nvenc)",
                 "H.264 (CPU)",
                 "H.265 (CPU)"]

CODECS_MAP    = {"H.264 (nvenc)": "h264_nvenc", 
                 "H.265 (nvenc)": "hevc_nvenc",
                 "H.264 (CPU)": "libx264",
                 "H.265 (CPU)": "libx265"}

CPU_PRESETS   = ["ultrafast", "superfast", "veryfast", "faster", "fast",
                 "medium", "slow", "slower", "veryslow"]

NVENC_PRESETS = ["p1", "p2", "p3", "p4", "p5", "p6", "p7"]

# Optional: keep the "feel" when switching CPU <-> NVENC
CPU_TO_NVENC = {
    "ultrafast": "p1",
    "superfast": "p2",
    "veryfast":  "p3",
    "faster":    "p3",
    "fast":      "p4",
    "medium":    "p4",
    "slow":      "p5",
    "slower":    "p6",
    "veryslow":  "p7",
}
NVENC_TO_CPU = {
    "p1": "ultrafast",
    "p2": "superfast",
    "p3": "veryfast",
    "p4": "medium",
    "p5": "slow",
    "p6": "slower",
    "p7": "veryslow",
}

VIDEO_EXTS    = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".wmv", ".flv"}


# ──────────────────────────────────────────────
#  Micro-widgets
# ──────────────────────────────────────────────

class HoverButton(tk.Label):
    """Flat clickable label that swaps colour on hover."""
    def __init__(self, parent, text, command, bg, fg,
                 hover_bg, hover_fg=None, font=None,
                 padx=16, pady=8, **kwargs):
        font = font or ("Segoe UI", 9, "bold")
        super().__init__(parent, text=text, bg=bg, fg=fg,
                         font=font, padx=padx, pady=pady,
                         cursor="hand2", **kwargs)
        self._bg, self._fg = bg, fg
        self._hbg = hover_bg
        self._hfg = hover_fg or fg
        self._cmd = command
        self._disabled = False
        self.bind("<Enter>",    self._enter)
        self.bind("<Leave>",    self._leave)
        self.bind("<Button-1>", self._click)

    def _enter(self, e):
        if not self._disabled:
            self.config(bg=self._hbg, fg=self._hfg)

    def _leave(self, e):
        if not self._disabled:
            self.config(bg=self._bg, fg=self._fg)

    def _click(self, e):
        if not self._disabled and self._cmd:
            self._cmd()

    def set_disabled(self, val: bool):
        self._disabled = val
        self.config(bg=SURFACE2 if val else self._bg,
                    fg=TEXT3    if val else self._fg,
                    cursor="arrow" if val else "hand2")


class StyledEntry(tk.Entry):
    def __init__(self, parent, var, width=9, font_size=9, **kwargs):
        super().__init__(parent, textvariable=var, width=width,
                         bg=INPUT_BG, fg=TEXT, insertbackground=TEXT,
                         relief="flat", font=("Segoe UI", font_size),
                         highlightthickness=1, highlightbackground=BORDER,
                         selectbackground=ACCENT_MID, selectforeground=TEXT,
                         **kwargs)
        self.bind("<FocusIn>",  lambda e: self.config(highlightbackground=ACCENT_LT))
        self.bind("<FocusOut>", lambda e: self.config(highlightbackground=BORDER))


def sep(parent, padx=16, pady=(8, 8)):
    tk.Frame(parent, bg=BORDER, height=1).pack(fill="x", padx=padx, pady=pady)


def section_head(parent, text, padx=16, top=12):
    tk.Label(parent, text=text.upper(), bg=SURFACE, fg=TEXT3,
             font=("Segoe UI", 7, "bold")
             ).pack(anchor="w", padx=padx, pady=(top, 4))


# ──────────────────────────────────────────────
#  Video Card
# ──────────────────────────────────────────────

class VideoCard(tk.Frame):
    def __init__(self, parent, video_path: str, remove_cb, scroll_update, **kwargs):
        super().__init__(parent, bg=SURFACE,
                         highlightthickness=1, highlightbackground=BORDER_SUB,
                         **kwargs)
        self.video_path    = video_path
        self.remove_cb     = remove_cb
        self.scroll_update = scroll_update
        self._photo        = None
        self._build()
        self._load_info()
        self.bind("<Enter>", lambda e: self.config(highlightbackground=ACCENT_MID))
        self.bind("<Leave>", lambda e: self.config(highlightbackground=BORDER_SUB))

    def _build(self):
        self.columnconfigure(1, weight=1)

        # Thumbnail
        self.thumb = tk.Label(self, bg="#0a0e17", width=17, height=5,
                              text="▶", fg=TEXT3, font=("Segoe UI", 18))
        self.thumb.grid(row=0, column=0, rowspan=3,
                        padx=(10, 12), pady=10, sticky="ns")

        # Header
        hdr = tk.Frame(self, bg=SURFACE)
        hdr.grid(row=0, column=1, sticky="ew", pady=(10, 1), padx=(0, 10))
        hdr.columnconfigure(0, weight=1)
        name = Path(self.video_path).name
        disp = name if len(name) <= 48 else name[:45] + "…"
        tk.Label(hdr, text=disp, bg=SURFACE, fg=TEXT,
                 font=("Segoe UI", 9, "bold"), anchor="w"
                 ).grid(row=0, column=0, sticky="ew")
        self._x = tk.Label(hdr, text="✕", bg=SURFACE, fg=TEXT3,
                            font=("Segoe UI", 10), cursor="hand2")
        self._x.grid(row=0, column=1)
        self._x.bind("<Enter>",    lambda e: self._x.config(fg=RED))
        self._x.bind("<Leave>",    lambda e: self._x.config(fg=TEXT3))
        self._x.bind("<Button-1>", lambda e: self.remove_cb(self))

        # Status
        self.status_var = tk.StringVar(value="Reading metadata…")
        self.status_lbl = tk.Label(self, textvariable=self.status_var,
                                   bg=SURFACE, fg=TEXT2,
                                   font=("Segoe UI", 8), anchor="w")
        self.status_lbl.grid(row=1, column=1, sticky="ew", padx=(0, 10))

        # Controls
        ctrl = tk.Frame(self, bg=SURFACE)
        ctrl.grid(row=2, column=1, sticky="ew", pady=(6, 10), padx=(0, 10))

        def mini_field(label, var, width):
            grp = tk.Frame(ctrl, bg=SURFACE)
            tk.Label(grp, text=label, bg=SURFACE, fg=TEXT3,
                     font=("Segoe UI", 7, "bold")).pack(anchor="w")
            StyledEntry(grp, var, width).pack()
            return grp

        self.start_var = tk.StringVar(value="0")
        self.end_var   = tk.StringVar(value="?")
        mini_field("START (s)", self.start_var, 7).pack(side="left", padx=(0, 10))
        mini_field("END (s)",   self.end_var,   8).pack(side="left", padx=(0, 14))

        # Mute pill
        mgrp = tk.Frame(ctrl, bg=SURFACE)
        tk.Label(mgrp, text="MUTE", bg=SURFACE, fg=TEXT3,
                 font=("Segoe UI", 7, "bold")).pack(anchor="w")
        self.mute_var = tk.BooleanVar(value=False)
        self._mpill = tk.Label(mgrp, text="OFF", bg=SURFACE2, fg=TEXT3,
                               font=("Segoe UI", 7, "bold"),
                               padx=10, pady=4, cursor="hand2")
        self._mpill.pack()
        self._mpill.bind("<Button-1>", self._toggle_mute)
        mgrp.pack(side="left")

        # Thin progress bar at card bottom
        self._pb_frame = tk.Frame(self, bg=INPUT_BG, height=3)
        self._pb_frame.grid(row=3, column=0, columnspan=2,
                            sticky="ew", padx=0, pady=0)
        self._pb_frame.grid_propagate(False)
        self._pb_canvas = tk.Canvas(self._pb_frame, bg=INPUT_BG,
                                    height=3, highlightthickness=0)
        self._pb_canvas.pack(fill="x", expand=True)
        self._pb_frame.grid_remove()

    def _toggle_mute(self, e=None):
        self.mute_var.set(not self.mute_var.get())
        if self.mute_var.get():
            self._mpill.config(text="ON", bg=ACCENT_MID, fg=ACCENT_LT)
        else:
            self._mpill.config(text="OFF", bg=SURFACE2, fg=TEXT3)

    def _load_info(self):
        def worker():
            info = ffprobe_info(self.video_path)
            dur  = info["duration"]
            w, h = info["width"], info["height"]
            dur_s = f"{dur:.2f}" if dur else "?"
            self.end_var.set(dur_s)
            parts = []
            if w:
                parts.append(f"{w}×{h}")
            if dur:
                s = int(dur)
                hh, rem = divmod(s, 3600)
                mm, ss = divmod(rem, 60)
                parts.append(f"{hh}:{mm:02}:{ss:02}" if hh else f"{mm}:{ss:02}")
            self.status_var.set("  ·  ".join(parts) if parts else "?")
            if PIL_AVAILABLE:
                photo = extract_thumbnail(self.video_path, (128, 72))
                if photo:
                    self._photo = photo
                    self.thumb.config(image=photo, text="",
                                      width=128, height=72, bg="#000")
            self.scroll_update()
        threading.Thread(target=worker, daemon=True).start()

    def set_status(self, text, color=TEXT2):
        self.status_var.set(text)
        self.status_lbl.config(fg=color)

    def show_progress(self, show: bool):
        if show:
            self._pb_frame.grid()
            self._draw_pb(0)
        else:
            self._pb_frame.grid_remove()

    def set_progress(self, value: int):
        self._draw_pb(value)

    def _draw_pb(self, pct):
        self._pb_canvas.update_idletasks()
        total = self._pb_canvas.winfo_width()
        self._pb_canvas.delete("all")
        fill = int(total * pct / 100)
        if fill > 0:
            self._pb_canvas.create_rectangle(
                0, 0, fill, 3, fill=ACCENT_LT, outline="")

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
#  Main application
# ──────────────────────────────────────────────

_TkBase = tkdnd.TkinterDnD.Tk if DND_AVAILABLE else tk.Tk


class App(_TkBase):
    def __init__(self):
        super().__init__()
        self.title("Video Compressor")
        self.configure(bg=BG)
        self.geometry("1020x700")
        self.minsize(820, 560)

        self._cards: list[VideoCard] = []
        self._running   = False
        self._stop_flag = False
        self._log_q: queue.Queue = queue.Queue()

        self._style_setup()
        self._build()
        self._poll_log()

    def _style_setup(self):
        s = ttk.Style(self)
        s.theme_use("clam")

        # -------------------------------
        # Scrollbar: remove bright edges
        # -------------------------------
        s.configure(
            "Dark.Vertical.TScrollbar",
            troughcolor=SURFACE,
            background=SURFACE2,
            bordercolor=BORDER,   # <- key: kill bright border
            lightcolor=BORDER,    # <- kill 3D highlight
            darkcolor=BORDER,     # <- kill 3D shadow
            arrowcolor=TEXT3,
            relief="flat",
            borderwidth=0,
            arrowsize=11,
        )
        s.map(
            "Dark.Vertical.TScrollbar",
            background=[("active", BORDER)],
            arrowcolor=[("active", TEXT2)]
        )

        # ---------------------------------------
        # Combobox field itself (the entry area)
        # ---------------------------------------
        s.configure(
            "V.TCombobox",
            fieldbackground=INPUT_BG,
            bordercolor=BORDER,   # <- key: kill bright border
            lightcolor=BORDER,    # <- kill 3D highlight
            darkcolor=BORDER,     # <- kill 3D shadow
            background=INPUT_BG,
            foreground=TEXT,
            selectbackground=ACCENT_MID,
            selectforeground=TEXT,
            arrowcolor=TEXT2,
            borderwidth=0,
            relief="flat",
            padding=(8, 6),
        )

        s.map(
            "V.TCombobox",
            fieldbackground=[("readonly", INPUT_BG), ("focus", INPUT_BG), ("active", INPUT_BG)],
            background=[("readonly", INPUT_BG), ("focus", INPUT_BG), ("active", INPUT_BG)],
            foreground=[("readonly", TEXT)],
            selectbackground=[("readonly", ACCENT_MID)],
        )

        # ----------------------------------------------------------
        # Dropdown list (popdown Listbox): THIS fixes the white menu
        # ----------------------------------------------------------
        # These option database keys are what Tk uses for the popdown list.
        self.option_add("*TCombobox*Listbox.background", INPUT_BG)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT_MID)
        self.option_add("*TCombobox*Listbox.selectForeground", TEXT)
        self.option_add("*TCombobox*Listbox.borderWidth", 0)
        self.option_add("*TCombobox*Listbox.highlightThickness", 0)
        self.option_add("*TCombobox*Listbox.relief", "flat")

    def _build(self):
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, minsize=272, weight=0)
        self.rowconfigure(1, weight=1)
        self._build_topbar()
        self._build_left()
        self._build_right()

    # ── top bar ──────────────────────────────────────────────────

    def _build_topbar(self):
        bar = tk.Frame(self, bg=SURFACE,
                       highlightthickness=1, highlightbackground=BORDER)
        bar.grid(row=0, column=0, columnspan=2,
                 sticky="ew", padx=16, pady=(16, 10))
        bar.columnconfigure(1, weight=1)

        # Colour accent strip
        tk.Frame(bar, bg=ACCENT, width=4).grid(row=0, column=0, sticky="ns")

        title_wrap = tk.Frame(bar, bg=SURFACE)
        title_wrap.grid(row=0, column=1, sticky="w", padx=14, pady=10)
        tk.Label(title_wrap, text="Video Compressor", bg=SURFACE, fg=TEXT,
                 font=("Segoe UI", 12, "bold")).pack(anchor="w")
        tk.Label(title_wrap, text="Two-pass H.264 / H.265  ·  target file size",
                 bg=SURFACE, fg=TEXT2, font=("Segoe UI", 8)).pack(anchor="w")

        self._badge = tk.Label(bar, text="No files queued",
                               bg=SURFACE, fg=TEXT3, font=("Segoe UI", 8))
        self._badge.grid(row=0, column=2, padx=16)

    # ── left / queue ─────────────────────────────────────────────

    def _build_left(self):
        left = tk.Frame(self, bg=BG)
        left.grid(row=1, column=0, sticky="nsew",
                  padx=(16, 8), pady=(0, 16))
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)

        # Drop zone with dashed canvas border
        self._dz = tk.Frame(left, bg=SURFACE,
                             highlightthickness=2, highlightbackground=BORDER,
                             cursor="hand2")
        self._dz.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self._dz.columnconfigure(0, weight=1)

        self._dz_cv = tk.Canvas(self._dz, bg=SURFACE, height=100,
                                highlightthickness=0)
        self._dz_cv.grid(row=0, column=0, sticky="ew")
        self._dz_cv.bind("<Configure>", self._redraw_dz)

        self._dz_icon  = tk.Label(self._dz_cv, text="⬆", bg=SURFACE,
                                   fg=TEXT3, font=("Segoe UI", 22))
        self._dz_icon.place(relx=0.5, rely=0.25, anchor="center")

        self._dz_line1 = tk.Label(self._dz_cv, text="Drop video files here",
                                   bg=SURFACE, fg=TEXT2,
                                   font=("Segoe UI", 10, "bold"))
        self._dz_line1.place(relx=0.5, rely=0.58, anchor="center")

        self._dz_line2 = tk.Label(self._dz_cv,
                                   text="or click to browse  ·  MP4 MOV AVI MKV WebM and more",
                                   bg=SURFACE, fg=TEXT3, font=("Segoe UI", 8))
        self._dz_line2.place(relx=0.5, rely=0.80, anchor="center")

        for w in (self._dz, self._dz_cv, self._dz_icon,
                  self._dz_line1, self._dz_line2):
            w.bind("<Button-1>", self._browse)
            w.bind("<Enter>",    self._dz_on)
            w.bind("<Leave>",    self._dz_off)

        # Scrollable list
        wrap = tk.Frame(left, bg=BG)
        wrap.grid(row=1, column=0, sticky="nsew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

        self._canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(wrap, orient="vertical",
                             command=self._canvas.yview,
                             style="Dark.Vertical.TScrollbar")
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

        self._empty = tk.Label(self._card_frame,
                               text="Your queue is empty.\nAdd videos using the drop zone above.",
                               bg=BG, fg=TEXT3,
                               font=("Segoe UI", 9), justify="center")
        self._empty.pack(pady=50)

        if DND_AVAILABLE:
            for w in (self, self._dz, self._dz_cv,
                      self._dz_icon, self._dz_line1, self._dz_line2):
                w.drop_target_register(tkdnd.DND_FILES)
                w.dnd_bind("<<Drop>>", self._on_drop)

    def _redraw_dz(self, e=None):
        c = self._dz_cv
        c.update_idletasks()
        w, h = c.winfo_width(), c.winfo_height()
        c.delete("dash")
        pad = 8
        c.create_rectangle(pad, pad, w - pad, h - pad,
                            dash=(6, 4), outline=TEXT3,
                            fill="", tags="dash")

    def _dz_on(self, e=None):
        self._dz.config(highlightbackground=ACCENT)
        for w in (self._dz, self._dz_cv, self._dz_icon,
                  self._dz_line1, self._dz_line2):
            w.config(bg=ACCENT_DIM)
        self._dz_icon.config(fg=ACCENT_LT)
        self._dz_line1.config(fg=TEXT)

    def _dz_off(self, e=None):
        self._dz.config(highlightbackground=BORDER)
        for w in (self._dz, self._dz_cv, self._dz_icon,
                  self._dz_line1, self._dz_line2):
            w.config(bg=SURFACE)
        self._dz_icon.config(fg=TEXT3)
        self._dz_line1.config(fg=TEXT2)

    def _sync_scroll(self, e=None):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    # ── right / settings ─────────────────────────────────────────

    def _build_right(self):
        p = tk.Frame(self, bg=SURFACE,
                     highlightthickness=1, highlightbackground=BORDER)
        p.grid(row=1, column=1, sticky="nsew",
               padx=(0, 16), pady=(0, 16))
        p.columnconfigure(0, weight=1)
        p.grid_propagate(False)
        P = 16

        tk.Label(p, text="Settings", bg=SURFACE, fg=TEXT,
                 font=("Segoe UI", 12, "bold")
                 ).pack(anchor="w", padx=P, pady=(16, 4))

        # Target size
        section_head(p, "Target size", P, top=8)
        size_row = tk.Frame(p, bg=SURFACE)
        size_row.pack(fill="x", padx=P)
        size_row.columnconfigure(0, weight=1)
        self.size_var = tk.StringVar(value="50")
        size_e = StyledEntry(size_row, self.size_var, width=8,
                             font_size=20, justify="right")
        size_e.grid(row=0, column=0, sticky="ew", ipady=4)
        tk.Label(size_row, text=" MB", bg=SURFACE, fg=TEXT2,
                 font=("Segoe UI", 14, "bold")).grid(row=0, column=1)

        # Quick-pick chips
        qp = tk.Frame(p, bg=SURFACE)
        qp.pack(fill="x", padx=P, pady=(6, 0))
        for mb in (8, 16, 25, 50, 100, 200):
            chip = tk.Label(qp, text=f"{mb}", bg=SURFACE2, fg=TEXT2,
                            font=("Segoe UI", 7, "bold"),
                            padx=7, pady=4, cursor="hand2")
            chip.pack(side="left", padx=(0, 4))
            chip.bind("<Button-1>", lambda e, v=mb: self.size_var.set(str(v)))
            chip.bind("<Enter>", lambda e, w=chip: w.config(bg=ACCENT_MID, fg=ACCENT_LT))
            chip.bind("<Leave>", lambda e, w=chip: w.config(bg=SURFACE2, fg=TEXT2))
        tk.Label(qp, text="MB", bg=SURFACE, fg=TEXT3,
                 font=("Segoe UI", 7)).pack(side="left", padx=(2, 0))

        sep(p, padx=P, pady=(12, 0))

        # Resolution
        section_head(p, "Resolution", P, top=10)
        self.res_var = tk.StringVar(value="original")
        ttk.Combobox(p, textvariable=self.res_var, values=RESOLUTIONS,
                     state="readonly", style="V.TCombobox",
                     font=("Segoe UI", 9)).pack(fill="x", padx=P)

        # Codec
        section_head(p, "Codec", P, top=10)
        self.codec_var = tk.StringVar(value="H.264 (nvenc)")
        self.codec_cb = ttk.Combobox(
            p, textvariable=self.codec_var, values=CODECS_LABELS,
            state="readonly", style="V.TCombobox",
            font=("Segoe UI", 9)
        )
        self.codec_cb.pack(fill="x", padx=P)

        # Preset
        section_head(p, "Encoding preset", P, top=10)
        self.preset_var = tk.StringVar(value="p4")  # sensible default for nvenc
        self.preset_cb = ttk.Combobox(
            p, textvariable=self.preset_var, values=NVENC_PRESETS,
            state="readonly", style="V.TCombobox",
            font=("Segoe UI", 9)
        )
        self.preset_cb.pack(fill="x", padx=P)

        tk.Label(p, text="Slower preset → better quality, smaller file",
                 bg=SURFACE, fg=TEXT3,
                 font=("Segoe UI", 7)).pack(anchor="w", padx=P, pady=(3, 0))

        # Bind codec changes -> update preset list
        self.codec_cb.bind("<<ComboboxSelected>>", self._on_codec_change)

        # Set correct preset options on startup too
        self._on_codec_change()

        sep(p, padx=P, pady=(12, 8))

        # Buttons
        self._start_btn = HoverButton(
            p, text="▶   Start Encoding",
            command=self._start,
            bg=ACCENT, fg="#fff",
            hover_bg="#6d28d9",
            font=("Segoe UI", 10, "bold"),
            pady=12, padx=0)
        self._start_btn.pack(fill="x", padx=P, pady=(0, 6))

        self._stop_btn = HoverButton(
            p, text="■   Stop after current",
            command=self._stop,
            bg=SURFACE2, fg=ORANGE,
            hover_bg="#2d2115",
            font=("Segoe UI", 9),
            pady=9, padx=0)
        self._stop_btn.pack(fill="x", padx=P)
        self._stop_btn.set_disabled(True)

        sep(p, padx=P, pady=(12, 0))

        section_head(p, "Activity log", P, top=8)
        self.log_box = tk.Text(
            p, bg=INPUT_BG, fg=TEXT3,
            font=("Consolas", 7),
            relief="flat", wrap="word", state="disabled",
            highlightthickness=1, highlightbackground=BORDER,
            padx=6, pady=6)
        self.log_box.pack(fill="both", expand=True, padx=P, pady=(0, 16))

    # ── file handling ────────────────────────────────────────────

    def _browse(self, event=None):
        files = filedialog.askopenfilenames(
            title="Select videos",
            filetypes=[("Video files",
                        "*.mp4 *.mov *.avi *.mkv *.webm *.m4v *.wmv *.flv"),
                       ("All files", "*.*")])
        for f in files:
            self._add_video(f)

    def _on_drop(self, event):
        for m in re.findall(r'\{([^}]+)\}|(\S+)', event.data):
            path = m[0] or m[1]
            if path and Path(path).is_file() \
                    and Path(path).suffix.lower() in VIDEO_EXTS:
                self._add_video(path)

    def _add_video(self, path: str):
        path = str(Path(path).resolve())
        if any(c.video_path == path for c in self._cards):
            return
        if self._empty.winfo_manager():
            self._empty.pack_forget()
        card = VideoCard(self._card_frame, path,
                         remove_cb=self._remove_card,
                         scroll_update=self._sync_scroll)
        card.pack(fill="x", pady=(0, 6), padx=4)
        self._cards.append(card)
        self._sync_scroll()
        self._update_badge()

    def _remove_card(self, card: VideoCard):
        if self._running:
            return
        self._cards.remove(card)
        card.destroy()
        if not self._cards:
            self._empty.pack(pady=50)
        self._sync_scroll()
        self._update_badge()

    def _update_badge(self):
        n = len(self._cards)
        if n == 0:
            self._badge.config(text="No files queued", fg=TEXT3)
        else:
            self._badge.config(
                text=f"{n} file{'s' if n > 1 else ''} queued", fg=TEXT2)

    # ── encoding ─────────────────────────────────────────────────

    def _start(self):
        if not self._cards:
            messagebox.showinfo("No videos", "Add videos to the queue first.")
            return
        try:
            target_mb = float(self.size_var.get())
            assert target_mb > 0
        except (ValueError, AssertionError):
            messagebox.showerror("Invalid",
                                 "Enter a valid positive number for target size.")
            return

        self._running   = True
        self._stop_flag = False
        self._start_btn.set_disabled(True)
        self._stop_btn.set_disabled(False)

        cards  = list(self._cards)
        codec  = CODECS_MAP[self.codec_var.get()]
        res    = self.res_var.get()
        preset = self.preset_var.get()

        def worker():
            for card in cards:
                if self._stop_flag:
                    self.after(0, card.set_status, "⏭  skipped", TEXT3)
                    continue
                self.after(0, card.set_status, "⏳  encoding…", ACCENT_LT)
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
                        progress_cb=lambda pct: self.after(
                            0, card.set_progress, pct),
                    )
                    size_mb = os.path.getsize(out) / (1024 * 1024)
                    self.after(0, card.set_status,
                               f"✓  done  ·  {size_mb:.1f} MB", GREEN)
                except Exception as ex:
                    self.after(0, card.set_status, f"✗  {ex}", RED)
                    self._log_q.put(f"ERROR: {ex}")
                finally:
                    self.after(0, card.show_progress, False)

            self._running = False
            self._log_q.put("── All jobs finished ──")
            self.after(0, self._start_btn.set_disabled, False)
            self.after(0, self._stop_btn.set_disabled, True)

        threading.Thread(target=worker, daemon=True).start()

    def _stop(self):
        self._stop_flag = True
        self._stop_btn.set_disabled(True)
        self._log_q.put("Stop requested — finishing current job…")

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

    def _is_nvenc_selected(self) -> bool:
        try:
            codec = CODECS_MAP[self.codec_var.get()]
        except Exception:
            return False
        return codec in ("h264_nvenc", "hevc_nvenc")

    def _on_codec_change(self, event=None):
        # Choose preset list based on codec
        is_nvenc = self._is_nvenc_selected()
        new_values = NVENC_PRESETS if is_nvenc else CPU_PRESETS

        # Remember current value and try to "translate" it
        current = self.preset_var.get().strip()

        if is_nvenc:
            translated = CPU_TO_NVENC.get(current, None)
            fallback = "p4"
        else:
            translated = NVENC_TO_CPU.get(current, None)
            fallback = "medium"

        # Apply new dropdown values
        self.preset_cb.configure(values=new_values)

        # Pick the best preset to keep UX smooth
        if translated in new_values:
            self.preset_var.set(translated)
        elif current in new_values:
            self.preset_var.set(current)
        else:
            self.preset_var.set(fallback)


# ──────────────────────────────────────────────
#  Entry point
# ──────────────────────────────────────────────

def main():
    root = App()
    if not DND_AVAILABLE:
        root._log_q.put("Tip: pip install tkinterdnd2  →  drag & drop support")
    if not PIL_AVAILABLE:
        root._log_q.put("Tip: pip install Pillow       →  video thumbnails")
    root.mainloop()


if __name__ == "__main__":
    main()