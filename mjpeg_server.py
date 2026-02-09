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
FRAMES_DIR_ABS: str = "/home/alexios/Projects/mjpeg_http_streamer/images"  # <-- set your directory here
TARGET_FPS: float = 20.0


# =============================================================================
# Shared state (latest JPEG)
# =============================================================================
@dataclass
class LatestFrame:
    lock: threading.Lock
    jpeg_bytes: bytes
    seq: int


latest = LatestFrame(lock=threading.Lock(), jpeg_bytes=b"", seq=0)


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


def _update_latest(jpeg: bytes) -> None:
    with latest.lock:
        latest.jpeg_bytes = jpeg
        latest.seq += 1


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
            _update_latest(jpeg)


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

    # Hard requirement: directory must exist already, otherwise exit immediately.
    if (not folder.exists()) or (not folder.is_dir()):
        print(f"ERROR: FRAMES_DIR_ABS does not exist or is not a directory: {folder}", file=sys.stderr)
        raise SystemExit(1)

    # Bootstrap: load the newest existing frame by mtime if any
    candidates = sorted(
        (p for p in folder.iterdir() if p.is_file() and _is_candidate_image(p)),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if candidates:
        jpeg = _try_load_as_jpeg_bytes(candidates[0])
        if jpeg is not None:
            _update_latest(jpeg)

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

    while True:
        with latest.lock:
            jpeg = latest.jpeg_bytes

        if jpeg:
            part = (
                boundary
                + header_ct
                + f"Content-Length: {len(jpeg)}\r\n\r\n".encode("ascii")
                + jpeg
                + b"\r\n"
            )
            yield part

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