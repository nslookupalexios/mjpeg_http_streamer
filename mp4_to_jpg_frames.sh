#!/usr/bin/env bash

set -euo pipefail

# =============================================================================
# Configuration
# =============================================================================
INPUT_MP4="$1"
OUTPUT_DIR="$2"
FPS="${3:-20}"          # default 20 fps
JPEG_QUALITY="${4:-2}"  # ffmpeg scale: 2 = high quality, 31 = low quality

# =============================================================================
# Checks
# =============================================================================
if [[ ! -f "$INPUT_MP4" ]]; then
    echo "ERROR: input file does not exist: $INPUT_MP4" >&2
    exit 1
fi

if [[ ! -d "$OUTPUT_DIR" ]]; then
    echo "INFO: output directory does not exist, creating: $OUTPUT_DIR"
    mkdir -p "$OUTPUT_DIR"
fi

# =============================================================================
# Frame extraction
# =============================================================================
ffmpeg -loglevel error \
       -re \
       -i "$INPUT_MP4" \
       -vf "fps=${FPS}" \
       -q:v "${JPEG_QUALITY}" \
       "${OUTPUT_DIR}/frame_%06d.jpg"

echo "Done. Frames written to: $OUTPUT_DIR"