#!/usr/bin/env bash
# Build a "chunk of stream" video for a RANGE of tracks from the 1998 Netradio
# D&B ISDN reconstruction -- the multi-track companion to mkmysteryvideo.sh.
#
#   scripts/mkstreamvideo.sh <start> <end> [outdir]
#   scripts/mkstreamvideo.sh 1 31
#
# Assembles one continuous audio file for the span (stitching the primary capture
# files by MASTER TIME, seams snapped to track boundaries), draws a static album-cover
# treemap background + a dynamic per-track foreground + blue sound bars, encodes an
# mp4, and writes a YouTube-ready chapters.txt. See streamvideo.py for the details.
#
# Deps: ffmpeg, python3, Pillow (brew install pillow). Reads the player repo's committed
# metadata (metadata/track-metadata.json + stream-files.json) and NETRADIO_MP3_DIR.
set -euo pipefail

START="${1:?usage: mkstreamvideo.sh <start> <end> [outdir]}"
END="${2:?usage: mkstreamvideo.sh <start> <end> [outdir]}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

command -v ffmpeg >/dev/null || { echo "need ffmpeg (brew install ffmpeg)"; exit 1; }
python3 -c 'import PIL' 2>/dev/null || { echo "need Pillow (brew install pillow)"; exit 1; }

exec python3 "$HERE/streamvideo.py" "$START" "$END" ${3:+"$3"}
