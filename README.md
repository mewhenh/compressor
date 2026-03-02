# Video Compressor (Tkinter + FFmpeg)

## Requirements
- Python 3.x
- **FFmpeg + FFprobe** binaries must be placed here after the exe is built:
  - `./_internal/ffmpeg/ffmpeg.exe`
  - `./_internal/ffmpeg/ffprobe.exe`

## Install (Python)
```bash
pip install -r requirements.txt
```
## Build
``` bash
python -m PyInstaller --windowed app.py
```