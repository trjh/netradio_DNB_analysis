#!/usr/bin/env bash
# Build a "chunk of stream" video for a RANGE of tracks from the 1998 Netradio
# D&B ISDN reconstruction -- the multi-track companion to mkmysteryvideo.sh.
#
#   scripts/mkstreamvideo.sh <start> <end> [outdir] [--no-fetch]
#   scripts/mkstreamvideo.sh 1 31
#
# Assembles one continuous audio file for the span (stitching the primary capture
# files by MASTER TIME, seams snapped to track boundaries), draws a static album-cover
# treemap background + a dynamic per-track foreground + blue sound bars, encodes an
# mp4, and writes a YouTube-ready chapters.txt. See streamvideo.py for the details.
#
# Deps: ffmpeg, python3, Pillow (brew install pillow). Reads this repo's
# track-metadata.json + stream-files.json, NETRADIO_MP3_DIR and NETRADIO_ART_DIR (the
# environment, else this repo's .env). Extra arguments (e.g. --no-fetch) pass through.
set -euo pipefail

START="${1:?usage: mkstreamvideo.sh <start> <end> [outdir]}"
END="${2:?usage: mkstreamvideo.sh <start> <end> [outdir]}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

command -v ffmpeg >/dev/null || { echo "need ffmpeg (brew install ffmpeg)"; exit 1; }
python3 -c 'import PIL' 2>/dev/null || { echo "need Pillow (brew install pillow)"; exit 1; }

shift 2
exec python3 "$HERE/streamvideo.py" "$START" "$END" "$@"
