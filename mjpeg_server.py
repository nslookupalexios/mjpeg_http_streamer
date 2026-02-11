from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Generator, Optional

from fastapi import FastAPI, Response
from fastapi.responses import StreamingResponse
from PIL import Image
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import os

# =============================================================================
# Configuration
# =============================================================================
FRAMES_OJBECT_DIR_ABS: str = os.getenv("FRAMES_OJBECT_DIR_ABS", "/tmp/mjpeg_frames1")
FRAMES_ROCK_DIR_ABS: str = os.getenv("FRAMES_ROCK_DIR_ABS", "/tmp/mjpeg_frames2")

TARGET_FPS: float = float(os.getenv("TARGET_FPS", "20.0"))
MAX_FRAME_AGE_S: float = float(os.getenv("MAX_FRAME_AGE_S", "10.0"))

# Placeholder frame resolution (shown when no real frames exist yet)
NO_FRAME_WIDTH: int = 640
NO_FRAME_HEIGHT: int = 480

# =============================================================================
# Shared state (latest JPEG) - one per source
# =============================================================================
@dataclass
class LatestFrame:
    lock: threading.Lock
    jpeg_bytes: bytes
    seq: int
    path: Optional[Path]


latest1 = LatestFrame(lock=threading.Lock(), jpeg_bytes=b"", seq=0, path=None)
latest2 = LatestFrame(lock=threading.Lock(), jpeg_bytes=b"", seq=0, path=None)


def _is_candidate_image(path: Path) -> bool:
    suffix = path.suffix.lower()
    return (suffix == ".jpg") or (suffix == ".jpeg") or (suffix == ".png")


def _try_load_as_jpeg_bytes(path: Path, max_attempts: int = 5, sleep_s: float = 0.02) -> Optional[bytes]:
    """
    Defensive load to handle partially written files.
    Reads the file, validates by decoding, then re-encodes to JPEG for streaming consistency.
    Supports JPEG and PNG inputs; PNG with alpha is composited to RGB.
    """
    for _ in range(max_attempts):
        try:
            with path.open("rb") as f:
                raw = f.read()

            bio_in = BytesIO(raw)
            img = Image.open(bio_in)
            img.load()

            # Ensure JPEG-compatible mode (no alpha)
            if img.mode in ("RGBA", "LA"):
                background = Image.new("RGB", img.size, (0, 0, 0))
                alpha = img.split()[-1]
                background.paste(img.convert("RGB"), mask=alpha)
                img = background
            elif img.mode != "RGB":
                img = img.convert("RGB")

            bio_out = BytesIO()
            img.save(bio_out, format="JPEG", quality=85, optimize=True)
            return bio_out.getvalue()
        except Exception:
            time.sleep(sleep_s)

    return None


def _generate_no_frame_jpeg(width: int, height: int, text: str = "NO FRAME") -> bytes:
    """
    Generate an in-memory JPEG placeholder with large white text on black background.
    """
    from PIL import ImageDraw, ImageFont

    img = Image.new("RGB", (width, height), color=(0, 0, 0))
    draw = ImageDraw.Draw(img)

    font_size = max(24, int(height * 0.15))

    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            font_size,
        )
    except Exception:
        font = ImageFont.load_default()

    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
    except Exception:
        (text_w, text_h) = draw.textsize(text, font=font)

    pos = ((width - text_w) // 2, (height - text_h) // 2)
    draw.text(pos, text, fill=(255, 255, 255), font=font)

    bio = BytesIO()
    img.save(bio, format="JPEG", quality=85, optimize=True)
    return bio.getvalue()


NO_FRAME_JPEG: bytes = _generate_no_frame_jpeg(NO_FRAME_WIDTH, NO_FRAME_HEIGHT)


def prune_older_than_current(folder: Path, current_path: Optional[Path], max_age_s: float) -> None:
    """
    Delete images older than (mtime(current_path) - max_age_s).
    Best-effort deletion: failures do not affect streaming.
    """
    if (current_path is None) or (max_age_s <= 0.0):
        return

    try:
        current_mtime = current_path.stat().st_mtime
    except FileNotFoundError:
        return
    except Exception:
        return

    threshold = current_mtime - max_age_s

    for p in folder.iterdir():
        if (not p.is_file()) or (not _is_candidate_image(p)):
            continue
        try:
            if p.stat().st_mtime < threshold:
                p.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass


def _update_latest(latest: LatestFrame, jpeg: bytes, path: Path) -> None:
    with latest.lock:
        latest.jpeg_bytes = jpeg
        latest.seq += 1
        latest.path = path


# =============================================================================
# Watchdog handler (parametric on "latest")
# =============================================================================
class NewImageHandler(FileSystemEventHandler):
    def __init__(self, folder: Path, latest: LatestFrame) -> None:
        super().__init__()
        self._folder = folder
        self._latest = latest

    def on_created(self, event) -> None:
        if event.is_directory:
            return
        self._handle(Path(event.src_path))

    def on_moved(self, event) -> None:
        if getattr(event, "is_directory", False):
            return
        self._handle(Path(event.dest_path))

    def on_modified(self, event) -> None:
        if event.is_directory:
            return
        self._handle(Path(event.src_path))

    def _handle(self, path: Path) -> None:
        if not _is_candidate_image(path):
            return
        try:
            path.relative_to(self._folder)
        except ValueError:
            return

        jpeg = _try_load_as_jpeg_bytes(path)
        if jpeg is not None:
            _update_latest(self._latest, jpeg, path)
            prune_older_than_current(self._folder, path, MAX_FRAME_AGE_S)


def _start_watcher(folder: Path, latest: LatestFrame) -> Observer:
    handler = NewImageHandler(folder, latest)
    observer = Observer()
    observer.schedule(handler, str(folder), recursive=False)
    observer.start()
    return observer


def _bootstrap_folder(folder: Path, latest: LatestFrame) -> None:
    """
    Load newest existing frame from folder (if any) and prune older frames.
    """
    candidates = sorted(
        (p for p in folder.iterdir() if p.is_file() and _is_candidate_image(p)),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if candidates:
        jpeg = _try_load_as_jpeg_bytes(candidates[0])
        if jpeg is not None:
            _update_latest(latest, jpeg, candidates[0])
        prune_older_than_current(folder, candidates[0], MAX_FRAME_AGE_S)


# =============================================================================
# FastAPI app
# =============================================================================
app = FastAPI()


@app.on_event("startup")
def _startup() -> None:
    folder1 = Path(FRAMES_OJBECT_DIR_ABS)
    folder2 = Path(FRAMES_ROCK_DIR_ABS)

    for folder, name in ((folder1, "FRAMES_OJBECT_DIR_ABS"), (folder2, "FRAMES_ROCK_DIR_ABS")):
        if folder.exists() and (not folder.is_dir()):
            print(f"ERROR: {name} exists but is not a directory: {folder}", file=sys.stderr)
            raise SystemExit(1)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            print(f"ERROR: cannot create/access {name} directory: {folder} ({e})", file=sys.stderr)
            raise SystemExit(1)

    _bootstrap_folder(folder1, latest1)
    _bootstrap_folder(folder2, latest2)

    app.state.folder1 = folder1
    app.state.folder2 = folder2
    app.state.observer1 = _start_watcher(folder1, latest1)
    app.state.observer2 = _start_watcher(folder2, latest2)


@app.on_event("shutdown")
def _shutdown() -> None:
    obs1: Observer = app.state.observer1
    obs2: Observer = app.state.observer2

    obs1.stop()
    obs2.stop()

    obs1.join()
    obs2.join()


def _mjpeg_generator(
    target_fps: float,
    folder: Path,
    latest: LatestFrame,
) -> Generator[bytes, None, None]:
    boundary = b"--frame\r\n"
    header_ct = b"Content-Type: image/jpeg\r\n"
    frame_interval = 1.0 / target_fps if target_fps > 0.0 else 0.05

    prune_every_n: int = 20
    i: int = 0

    while True:
        with latest.lock:
            jpeg = latest.jpeg_bytes
            path = latest.path

        frame = jpeg if jpeg else NO_FRAME_JPEG

        part = (
            boundary
            + header_ct
            + f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii")
            + frame
            + b"\r\n"
        )
        yield part

        i += 1
        if (i % prune_every_n) == 0:
            prune_older_than_current(folder, path, MAX_FRAME_AGE_S)

        time.sleep(frame_interval)


@app.get("/stream/1")
def stream1() -> Response:
    folder1: Path = app.state.folder1
    return StreamingResponse(
        _mjpeg_generator(TARGET_FPS, folder1, latest1),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/stream/2")
def stream2() -> Response:
    folder2: Path = app.state.folder2
    return StreamingResponse(
        _mjpeg_generator(TARGET_FPS, folder2, latest2),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )

@app.get("/")
def index() -> Response:
    html = """
    <!doctype html>
    <html>
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>MJPEG Streams (2 sources)</title>
      </head>
      <body style="margin:0; background:#111; color:#eee; font-family: sans-serif;">
        <div style="display:flex; flex-direction:row; gap:8px; padding:8px; height:100vh; box-sizing:border-box;">
          
          <!-- STREAM 1 -->
          <div style="flex:1; display:flex; flex-direction:column; gap:10px; min-width:0;">
            
            <!-- Titolo migliorato -->
            <div style="
              display:flex; align-items:center; gap:10px;
              padding:10px 14px;
              border-radius:12px;
              background: linear-gradient(180deg, rgba(255,255,255,0.08), rgba(255,255,255,0.03));
              border: 1px solid rgba(255,255,255,0.10);
              box-shadow: 0 6px 18px rgba(0,0,0,0.35);
              min-width:0;
            ">
              <span style="
                width:10px; height:10px; border-radius:999px;
                background: rgba(255,0,0,0.95);
                box-shadow: 0 0 0 3px rgba(255,0,0,0.18);
                flex:0 0 auto;
              "></span>

              <span style="
                font-size:18px;
                font-weight:800;
                letter-spacing:0.2px;
                color:#f3f4f6;
                line-height:1.2;
                text-shadow: 0 1px 0 rgba(0,0,0,0.35);
                white-space:normal;
              ">
                Identificazione di: persone, automobili, bici/motocicli, bus/camion, animali
              </span>
            </div>

            <div style="flex:1; display:flex; align-items:center; justify-content:center; background:#000; border-radius:12px; overflow:hidden;">
              <img src="/stream/1" style="max-width:100%; max-height:100%; object-fit:contain;" />
            </div>
          </div>

          <!-- STREAM 2 -->
          <div style="flex:1; display:flex; flex-direction:column; gap:10px; min-width:0;">
            
            <!-- Titolo migliorato -->
            <div style="
              display:flex; align-items:center; gap:10px;
              padding:10px 14px;
              border-radius:12px;
              background: linear-gradient(180deg, rgba(255,255,255,0.08), rgba(255,255,255,0.03));
              border: 1px solid rgba(255,255,255,0.10);
              box-shadow: 0 6px 18px rgba(0,0,0,0.35);
              min-width:0;
            ">
              <span style="
                width:10px; height:10px; border-radius:999px;
                background: rgba(255,0,0,0.95);
                box-shadow: 0 0 0 3px rgba(255,0,0,0.18);
                flex:0 0 auto;
              "></span>

              <span style="
                font-size:18px;
                font-weight:800;
                letter-spacing:0.2px;
                color:#f3f4f6;
                line-height:1.2;
                text-shadow: 0 1px 0 rgba(0,0,0,0.35);
                white-space:normal;
              ">
                Identificazione di rocce
              </span>
            </div>

            <div style="flex:1; display:flex; align-items:center; justify-content:center; background:#000; border-radius:12px; overflow:hidden;">
              <img src="/stream/2" style="max-width:100%; max-height:100%; object-fit:contain;" />
            </div>
          </div>
        </div>
      </body>
    </html>
    """
    return Response(content=html, media_type="text/html; charset=utf-8")