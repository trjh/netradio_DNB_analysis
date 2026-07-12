#!/usr/bin/env bash
# Build an "Unknown Track N" video for a track-ID post -- locally, no website.
#
#   scripts/mkvideo.sh "~/Downloads/Netradio/mystery-uploads/Mystery Track 6.wav" 6
#   scripts/mkvideo.sh <audio> <n> [outdir]
#
# Reproduces the format of the Mystery Track 3 upload
# (https://www.youtube.com/watch?v=jKEt_2jLzYo) from primitives:
#
#   * 1920x1080 black starfield, generated here, with a slow twinkle so it is not a dead still
#   * blue log-scale frequency bars (showfreqs) -- the moving part, and the RIGHT visual for a
#     track-ID post: the shape of the bassline and the drum pattern are themselves a clue
#
#     Two non-obvious tricks here. showfreqs draws ONE BAR PER FFT BIN, so at 1200px wide it is
#     a solid mass, not bars: render it 56px wide (=56 bars) and upscale with NEAREST-NEIGHBOUR
#     to get chunky ones. And it has no notion of a gap between bars, so `geq` punches the alpha
#     out on a 21px pitch (13px bar, 8px gap). Without both, you get a filled blob.
#   * the netradio.com logo, bottom-left
#   * "Unknown Track N" + "from Netradio Drum & Bass ISDN / 1998 Stream"
#
# "Unknown", not "Mystery" -- that is what the audience is being asked, and it matches the
# existing upload so the set reads as one series.
#
# NOTE: this Homebrew ffmpeg is built WITHOUT libfreetype, so `drawtext` does not exist. The
# text is rendered to a transparent PNG with ImageMagick and composited instead. That is more
# robust anyway: the card is a reusable artefact you can eyeball before spending an encode on it.
set -euo pipefail

AUDIO="${1:?usage: mkvideo.sh <audio> <n> [outdir]}"
N="${2:?usage: mkvideo.sh <audio> <n> [outdir]}"
OUTDIR="${3:-$HOME/Downloads/Netradio/mystery-uploads}"
FPS="${FPS:-30}"     # the reference is 60; 30 looks identical for bars and halves the encode.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOGO="$REPO/logo/logo.jpg"
FONT="${FONT:-/System/Library/Fonts/Supplemental/Futura.ttc}"

command -v magick >/dev/null || { echo "need ImageMagick (brew install imagemagick)"; exit 1; }

mkdir -p "$OUTDIR"
OUT="$OUTDIR/Unknown Track $N.mp4"
STARS="$OUTDIR/.starfield.png"
CARD="$OUTDIR/.card-$N.png"

# --- the starfield (generated once, reused) -------------------------------------------------
# Sparse points on black, weighted so MOST stars are faint: a uniform sprinkle reads as sensor
# noise, not a sky. Seeded, so the sky is the same in every video of the series.
if [ ! -f "$STARS" ]; then
  python3 - "$STARS" <<'PY'
import random, sys, zlib, struct
W, H = 1920, 1080
random.seed(1998)
px = bytearray(W * H * 3)
for _ in range(2600):
    x, y = random.randrange(W), random.randrange(H)
    v = random.choice([40, 60, 80, 110, 150, 200, 255])
    i = (y * W + x) * 3
    px[i] = px[i+1] = px[i+2] = v
    if v > 180 and 0 < x < W-1 and 0 < y < H-1:
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            j = ((y+dy) * W + (x+dx)) * 3
            px[j] = px[j+1] = px[j+2] = v // 4
raw = b"".join(b"\x00" + bytes(px[y*W*3:(y+1)*W*3]) for y in range(H))
def chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))
open(sys.argv[1], "wb").write(
    b"\x89PNG\r\n\x1a\n"
    + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
    + chunk(b"IDAT", zlib.compress(raw, 9))
    + chunk(b"IEND", b""))
PY
fi

# --- the title card (transparent overlay: text + logo) --------------------------------------
magick -size 1920x1080 xc:none \
  -font "$FONT" -fill white \
  -pointsize 118 -annotate +500+680 "Unknown Track $N" \
  -pointsize 52  -annotate +500+745 "from Netradio Drum & Bass ISDN" \
  -pointsize 52  -annotate +500+801 "1998 Stream" \
  \( "$LOGO" -resize 190x190 \) -geometry +270+596 -composite \
  "$CARD"

# --- the video -------------------------------------------------------------------------------
ffmpeg -v error -stats -y \
  -loop 1 -framerate "$FPS" -i "$STARS" \
  -i "$AUDIO" \
  -loop 1 -framerate "$FPS" -i "$CARD" \
  -filter_complex "
    [0:v]eq=brightness='0.03*sin(2*PI*t/7)':eval=frame,format=rgba[bg];
    [1:a]showfreqs=s=56x240:mode=bar:ascale=log:fscale=log:win_size=1024:averaging=3,
         scale=1176:240:flags=neighbor,
         colorize=hue=232:saturation=0.62,format=rgba,
         geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='if(lt(mod(X\,21),13),alpha(X\,Y),0)',
         colorchannelmixer=aa=0.95,gblur=sigma=1.0[bars];
    [bg][bars]overlay=x=(W-w)/2:y=330:format=auto[v1];
    [v1][2:v]overlay=0:0:format=auto,format=yuv420p[v]
  " \
  -map "[v]" -map 1:a \
  -c:v libx264 -preset medium -crf 20 -r "$FPS" \
  -c:a aac -b:a 192k -shortest \
  "$OUT"

echo
echo "wrote: $OUT  ($(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUT" | cut -d. -f1)s, $(du -h "$OUT" | cut -f1))"
echo
echo "Posting it: say the things only you know -- they are what narrows the search."
echo "  * a 1998 netradio.com D&B ISDN broadcast, so the record is 1998 or earlier"
echo "  * the tracks either side of it on the master timeline (TRACKLIST.md)"
echo "  * it is a continuous DJ mix, so the intro may be buried under the previous record"
echo "  * where to post: FINDING_MYSTERY_TRACKS.md"
