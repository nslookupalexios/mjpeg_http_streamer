#!/usr/bin/env bash

set -euo pipefail

# =============================================================================
# Configuration
# =============================================================================
INPUT_MP4="$1"
OUTPUT_DIR="$2"
FPS="${3:-20}"                # default 20 fps
PNG_COMPRESSION="${4:-6}"     # ffmpeg scale: 0 = no compression, 9 = max compression

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
       -compression_level "${PNG_COMPRESSION}" \
       "${OUTPUT_DIR}/frame_%06d.png"

echo "Done. Frames written to: $OUTPUT_DIR"