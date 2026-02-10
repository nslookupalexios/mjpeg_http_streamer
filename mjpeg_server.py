from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Generator, Optional

from fastapi import FastAPI, Response
from fastapi.responses import StreamingResponse
from PIL import Image
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


# =============================================================================
# Configuration (edit here)
# =============================================================================
FRAMES_DIR_ABS: str = "/home/alexios/Projects/mjpeg_http_streamer/images"
TARGET_FPS: float = 20.0

# Delete frames older than (current_frame_mtime - MAX_FRAME_AGE_S)
MAX_FRAME_AGE_S: float = 10.0

# Placeholder frame resolution (shown when no real frames exist yet)
NO_FRAME_WIDTH: int = 640
NO_FRAME_HEIGHT: int = 480


# =============================================================================
# Shared state (latest JPEG)
# =============================================================================
@dataclass
class LatestFrame:
    lock: threading.Lock
    jpeg_bytes: bytes
    seq: int
    path: Optional[Path]


latest = LatestFrame(lock=threading.Lock(), jpeg_bytes=b"", seq=0, path=None)


def _is_candidate_image(path: Path) -> bool:
    suffix = path.suffix.lower()
    return (suffix == ".jpg") or (suffix == ".jpeg")


def _try_load_as_jpeg_bytes(path: Path, max_attempts: int = 5, sleep_s: float = 0.02) -> Optional[bytes]:
    """
    Defensive load to handle partially written files.
    Reads the file, validates by decoding, then re-encodes to JPEG for consistency.
    """
    for _ in range(max_attempts):
        try:
            with path.open("rb") as f:
                raw = f.read()

            from io import BytesIO
            bio_in = BytesIO(raw)
            img = Image.open(bio_in)
            img.load()  # force decode, raises on truncated/corrupt

            bio_out = BytesIO()
            img.save(bio_out, format="JPEG", quality=85, optimize=True)
            return bio_out.getvalue()
        except Exception:
            time.sleep(sleep_s)

    return None


def _generate_no_frame_jpeg(width: int, height: int, text: str = "NO FRAME") -> bytes:
    """
    Generate an in-memory JPEG placeholder with large white text on black background.
    Used when no real frames are available.

    Implementation notes:
    - Uses a TrueType font (DejaVu Sans) with explicit size.
    - Falls back to default bitmap font if TTF is unavailable.
    - Text is centered both horizontally and vertically.
    """
    from io import BytesIO
    from PIL import ImageDraw, ImageFont

    img = Image.new("RGB", (width, height), color=(0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Font size proportional to image height (e.g. 15%)
    font_size = max(24, int(height * 0.15))

    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            font_size,
        )
    except Exception:
        # Fallback: small bitmap font (should never happen on Ubuntu)
        font = ImageFont.load_default()

    try:
        # Preferred for TrueType fonts
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
    except Exception:
        # Safe fallback
        (text_w, text_h) = draw.textsize(text, font=font)

    pos = ((width - text_w) // 2, (height - text_h) // 2)
    draw.text(pos, text, fill=(255, 255, 255), font=font)

    bio = BytesIO()
    img.save(bio, format="JPEG", quality=85, optimize=True)
    return bio.getvalue()


# Compute once; safe after fixing text size calculation
NO_FRAME_JPEG: bytes = _generate_no_frame_jpeg(NO_FRAME_WIDTH, NO_FRAME_HEIGHT)


def prune_older_than_current(folder: Path, current_path: Optional[Path], max_age_s: float) -> None:
    """
    Delete images older than (mtime(current_path) - max_age_s).

    Notes:
    - Uses filesystem mtime as time base.
    - If current_path is None or missing, nothing is deleted.
    - Best-effort deletion: failures do not affect streaming.
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


def _update_latest(jpeg: bytes, path: Path) -> None:
    with latest.lock:
        latest.jpeg_bytes = jpeg
        latest.seq += 1
        latest.path = path


# =============================================================================
# Watchdog handler
# =============================================================================
class NewImageHandler(FileSystemEventHandler):
    def __init__(self, folder: Path) -> None:
        super().__init__()
        self._folder = folder

    def on_created(self, event) -> None:
        if event.is_directory:
            return
        self._handle(Path(event.src_path))

    def on_moved(self, event) -> None:
        # Useful if producer writes to temp file then atomically renames.
        if getattr(event, "is_directory", False):
            return
        self._handle(Path(event.dest_path))

    def on_modified(self, event) -> None:
        # Optional: helps if producer writes in place.
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
            _update_latest(jpeg, path)
            prune_older_than_current(self._folder, path, MAX_FRAME_AGE_S)


def _start_watcher(folder: Path) -> Observer:
    handler = NewImageHandler(folder)
    observer = Observer()
    observer.schedule(handler, str(folder), recursive=False)
    observer.start()
    return observer


# =============================================================================
# FastAPI app
# =============================================================================
app = FastAPI()


@app.on_event("startup")
def _startup() -> None:
    folder = Path(FRAMES_DIR_ABS)

    # If path exists but is not a directory -> unrecoverable configuration error
    if folder.exists() and (not folder.is_dir()):
        print(f"ERROR: FRAMES_DIR_ABS exists but is not a directory: {folder}", file=sys.stderr)
        raise SystemExit(1)

    # If directory does not exist -> create it and continue
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"ERROR: cannot create/access FRAMES_DIR_ABS directory: {folder} ({e})", file=sys.stderr)
        raise SystemExit(1)

    # Bootstrap: load newest existing frame (may be empty -> ok)
    candidates = sorted(
        (p for p in folder.iterdir() if p.is_file() and _is_candidate_image(p)),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if candidates:
        jpeg = _try_load_as_jpeg_bytes(candidates[0])
        if jpeg is not None:
            _update_latest(jpeg, candidates[0])
        prune_older_than_current(folder, candidates[0], MAX_FRAME_AGE_S)

    app.state.folder = folder
    app.state.observer = _start_watcher(folder)


@app.on_event("shutdown")
def _shutdown() -> None:
    obs: Observer = app.state.observer
    obs.stop()
    obs.join()


def _mjpeg_generator(target_fps: float) -> Generator[bytes, None, None]:
    boundary = b"--frame\r\n"
    header_ct = b"Content-Type: image/jpeg\r\n"
    frame_interval = 1.0 / target_fps if target_fps > 0.0 else 0.05

    # Periodic prune even if watchdog misses events
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
            folder = getattr(app.state, "folder", None)
            if folder is not None:
                prune_older_than_current(folder, path, MAX_FRAME_AGE_S)

        time.sleep(frame_interval)


@app.get("/stream")
def stream() -> Response:
    return StreamingResponse(
        _mjpeg_generator(TARGET_FPS),
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
        <title>MJPEG Stream</title>
      </head>
      <body style="margin:0; background:#111; display:flex; align-items:center; justify-content:center; height:100vh;">
        <img src="/stream" style="max-width:100vw; max-height:100vh;" />
      </body>
    </html>
    """
    return Response(content=html, media_type="text/html; charset=utf-8")