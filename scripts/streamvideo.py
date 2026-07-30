#!/usr/bin/env python3
"""Render a "chunk of stream" video for a range of tracks from the 1998
Netradio D&B ISDN reconstruction. Companion to mkmysteryvideo.sh (the single-track
"Unknown Track N" mystery posts); this one does a continuous multi-track span.

  scripts/streamvideo.py <start> <end> [outdir]
  scripts/streamvideo.py 1 31

What it builds, from the player repo's committed metadata (never the CSV):
  * ONE continuous audio file for master span [begin(start) .. end(end)], stitched
    from the PRIMARY capture files by MASTER TIME (filenames are hints, not authority).
    Every file->file seam is snapped to a TRACK BOUNDARY that lies inside the overlap
    of the two captures, so the join hides under a DJ transition instead of a phrase.
    Lossless: real between-track audio (e.g. the 2.2s gap before the track-9 promo) is
    kept, unlike a naive per-track concat.
  * a STATIC background: a proportional squarified treemap of the album covers in the
    span (cell area proportional to how many tracks come from that album), dimmed.
    Albums with no artwork (the Net Radio promos) fall back to the netradio.com logo.
  * a DYNAMIC foreground: one card per track (cover, index, title, artist, year, and
    the source capture file), baked over the dimmed mosaic and shown for the track's
    duration. Since the audio starts at master 0-of-span, video time == span time, so
    each card simply switches at the track's master_begin.
  * blue showfreqs sound bars along the bottom (same recipe as mkmysteryvideo.sh).
  * chapters.txt: a YouTube-ready timestamped tracklist (paste into the description;
    YouTube renders it as clickable scrubber chapters).

Deps: python3 + Pillow (brew install pillow) for the stills, ffmpeg for audio+encode.
This Homebrew ffmpeg has no libfreetype (no drawtext) -- all text is drawn with Pillow.
"""
import json, os, subprocess, sys, tempfile, shutil
from collections import OrderedDict
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance

# --- paths ------------------------------------------------------------------
SELF = os.path.dirname(os.path.abspath(__file__))
LOCALDISK = os.path.dirname(SELF)
PLAYER = os.environ.get("NETRADIO_PLAYER_DIR",
                        os.path.normpath(os.path.join(LOCALDISK, "..", "player")))
LOGO = os.path.join(LOCALDISK, "logo", "logo.jpg")
FONTDIR = "/System/Library/Fonts/Supplemental"


def read_env(path):
    env = {}
    try:
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env


ENV = read_env(os.path.join(PLAYER, ".env"))
MP3_DIR = os.environ.get("NETRADIO_MP3_DIR") or ENV.get("NETRADIO_MP3_DIR")
FFMPEG = os.environ.get("NETRADIO_FFMPEG", "ffmpeg")

W, H = 1920, 1080
FPS = int(os.environ.get("FPS", "30"))
ACCENT = (120, 180, 255)


# --- fonts ------------------------------------------------------------------
def font(name, size):
    p = os.path.join(FONTDIR, name)
    if not os.path.exists(p):
        p = os.path.join("/System/Library/Fonts", name)
    return ImageFont.truetype(p, size)


# --- metadata ---------------------------------------------------------------
def load_meta():
    d = json.load(open(os.path.join(PLAYER, "metadata/track-metadata.json")))
    sf = json.load(open(os.path.join(PLAYER, "metadata/stream-files.json")))
    return d["tracks"], d["albums"], sf


def merged(tr, albums, n):
    t = tr[str(n)]
    alb = albums.get(t.get("album") or "", {})
    tf, af = t.get("fields", {}), alb.get("fields", {})
    return {
        "title": t.get("title") or "",
        "artist": t.get("artist") or alb.get("artist") or "",
        "year": af.get("year") or tf.get("year") or "",   # prefer the album's year
        "album_id": t.get("album"),
        "release": alb.get("title") or "",          # album/release name
        "artwork": t.get("artwork") or alb.get("artwork"),
        "begin": t["master_begin_seconds"],
        "end": t["master_end_seconds"],
    }


def cover_path(albums, album_id, artwork):
    if artwork:
        p = os.path.join(PLAYER, "art", artwork)
        if os.path.exists(p):
            return p
    return LOGO


# --- audio plan (stitch primaries by master time, seams on track boundaries) -
def plan_audio(tr, sf, start, end):
    prim = [f for f in sf["files"]
            if f.get("is_primary") and f.get("master_start_seconds") is not None]
    prim.sort(key=lambda f: f["master_start_seconds"])
    T0 = tr[str(start)]["master_begin_seconds"]
    T1 = tr[str(end)]["master_end_seconds"]
    boundaries = sorted(t["master_begin_seconds"] for t in tr.values()
                        if t.get("master_begin_seconds") is not None)

    def covering(c):
        cs = [f for f in prim
              if f["master_start_seconds"] <= c < f["master_end_seconds"]]
        return max(cs, key=lambda f: f["master_end_seconds"]) if cs else None

    cur, segs = T0, []
    while cur < T1 - 1e-6:
        f = covering(cur)
        if not f:
            raise SystemExit(f"no primary file covers master {cur:.1f}s")
        seg_end = min(f["master_end_seconds"], T1)
        if seg_end < T1 - 1e-6:                       # a file->file handoff
            succ = covering(seg_end)
            if succ:
                ov = max(succ["master_start_seconds"], cur)
                snap = [b for b in boundaries if ov < b <= seg_end + 1e-6]
                if snap:
                    seg_end = max(snap)               # hide seam on a boundary
        segs.append({"file": f, "m_start": cur, "m_end": seg_end,
                     "l_start": cur - f["master_start_seconds"],
                     "l_end": seg_end - f["master_start_seconds"]})
        cur = seg_end
    return T0, T1, segs


def lms(t):
    """m:ss.mmm (h:mm:ss.mmm past the hour) — cut points deserve milliseconds, not rounding."""
    ms = int(round(t * 1000))
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h}:{m:02d}:{s:02d}.{ms:03d}" if h else f"{m}:{s:02d}.{ms:03d}"


def describe_plan(segs):
    """Say exactly what is about to be cut from where, BEFORE any ffmpeg runs.

    Each line is the source file and the file-LOCAL range being taken from it. The summary
    line is the no-silence proof: the segments are contiguous on the master timeline by
    construction (each starts where the previous ended), so the components' summed length
    equals the span and there is nowhere a pad could hide. If coverage ever had a hole,
    plan_audio() refuses with SystemExit rather than padding — but a claim like that should
    be checkable by eye, which is what this printout is for.
    """
    print("Assembling audio from these components:")
    total = 0.0
    for i, s in enumerate(segs):
        f = s["file"]
        eof = abs(s["l_end"] - (f["master_end_seconds"] - f["master_start_seconds"])) < 0.0005
        gap = "" if i == 0 or abs(s["m_start"] - segs[i - 1]["m_end"]) < 0.0005 \
            else "  !! NOT CONTIGUOUS with previous segment"
        print(f"- {f['mp3_filename']} {lms(s['l_start'])} - {lms(s['l_end'])}"
              f"{' (end of file)' if eof else ''}{gap}")
        total += s["l_end"] - s["l_start"]
    span = segs[-1]["m_end"] - segs[0]["m_start"]
    print(f"  = {len(segs)} segment(s), total {lms(total)} for a span of {lms(span)} — "
          "contiguous on the master timeline; nothing padded, no silence inserted")


def assemble_audio(segs, workdir):
    if not MP3_DIR:
        raise SystemExit("NETRADIO_MP3_DIR not set (player/.env or env)")
    describe_plan(segs)
    parts = []
    for i, s in enumerate(segs):
        src = os.path.join(MP3_DIR, s["file"]["mp3_filename"])
        out = os.path.join(workdir, f"seg{i:02d}.wav")
        # decode then trim (-ss/-to AFTER -i) => sample-accurate cut
        subprocess.run([FFMPEG, "-v", "error", "-y", "-i", src,
                        "-ss", f"{s['l_start']:.3f}", "-to", f"{s['l_end']:.3f}",
                        "-ac", "2", "-ar", "44100", out], check=True)
        parts.append(out)
    listf = os.path.join(workdir, "audio.txt")
    with open(listf, "w") as fh:
        for p in parts:
            fh.write(f"file '{p}'\n")
    audio = os.path.join(workdir, "assembled.wav")
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "concat", "-safe", "0",
                    "-i", listf, "-c", "copy", audio], check=True)
    return audio


# --- background treemap -----------------------------------------------------
def squarify(items, x, y, w, h):
    total = sum(v for _, v in items)
    vals = [(k, v * (w * h) / total) for k, v in items]
    out = []

    def worst(row, side):
        s = sum(a for _, a in row); mx = max(a for _, a in row); mn = min(a for _, a in row)
        return max(side * side * mx / (s * s), s * s / (side * side * mn))

    while vals:
        side = min(w, h)
        row, i = [vals[0]], 1
        while i < len(vals) and worst(row, side) >= worst(row + [vals[i]], side):
            row.append(vals[i]); i += 1
        vals = vals[i:]
        s = sum(a for _, a in row)
        if w <= h:
            rh = s / w; cx = x
            for k, a in row:
                cw = a / rh; out.append((k, cx, y, cw, rh)); cx += cw
            y += rh; h -= rh
        else:
            rw = s / h; cy = y
            for k, a in row:
                ch = a / rw; out.append((k, x, cy, rw, ch)); cy += ch
            x += rw; w -= rw
    return out


def render_background(tr, albums, start, end):
    weight = OrderedDict()
    order = {}
    for n in range(start, end + 1):
        a = tr[str(n)].get("album")
        weight[a] = weight.get(a, 0) + 1
        order.setdefault(a, n)
    items = sorted(weight.items(), key=lambda kv: (-kv[1], order[kv[0]]))
    rects = squarify(items, 0, 0, W, H)

    bg = Image.new("RGB", (W, H), (10, 10, 12))
    for album, rx, ry, rw, rh in rects:
        iw, ih = max(1, round(rw)), max(1, round(rh))
        alb = albums.get(album, {})
        im = Image.open(cover_path(albums, album, alb.get("artwork"))).convert("RGB")
        sc = max(iw / im.width, ih / im.height)
        im = im.resize((round(im.width * sc), round(im.height * sc)), Image.LANCZOS)
        L, T = (im.width - iw) // 2, (im.height - ih) // 2
        bg.paste(im.crop((L, T, L + iw, T + ih)), (round(rx), round(ry)))
    gd = ImageDraw.Draw(bg)
    for album, rx, ry, rw, rh in rects:
        gd.rectangle([rx, ry, rx + rw - 1, ry + rh - 1], outline=(0, 0, 0), width=3)
    bg = ImageEnhance.Brightness(bg).enhance(0.34)
    bg = Image.alpha_composite(bg.convert("RGBA"),
                               Image.new("RGBA", (W, H), (8, 10, 20, 90))).convert("RGB")
    return bg


# --- per-track foreground card (baked over a copy of the dimmed mosaic) ------
F_TRACK = font("Futura.ttc", 30)
F_ARTIST = font("Futura.ttc", 44)
F_REL = font("Futura.ttc", 32)
F_YEAR = font("Georgia Italic.ttf", 32)
F_HEAD = font("Futura.ttc", 34)


def render_card(bg, tr, albums, n, idx, count, header):
    m = merged(tr, albums, n)
    img = bg.copy().convert("RGBA")
    draw = ImageDraw.Draw(img)

    CX, CY, COV = 90, 545, 360
    cov = Image.open(cover_path(albums, m["album_id"], m["artwork"])).convert("RGB")
    cov = cov.resize((COV, COV), Image.LANCZOS)
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rectangle([CX + 10, CY + 14, CX + COV + 10, CY + COV + 14],
                                 fill=(0, 0, 0, 170))
    img = Image.alpha_composite(img, sh.filter(ImageFilter.GaussianBlur(12)))
    img.paste(cov, (CX, CY))
    draw = ImageDraw.Draw(img)
    draw.rectangle([CX, CY, CX + COV - 1, CY + COV - 1], outline=(255, 255, 255, 60), width=2)

    # Text block right of the cover, bottom-justified against the cover's base:
    #   TRACK n • idx OF N  /  title (one line, shrunk to fit)  /  artist  /  release + year.
    # The release line is dropped for the Net Radio promos (no commercial release).
    tx = CX + COV + 46
    maxw = W - tx - 90
    tsize = 74                                   # shrink the title until it fits on one line
    while tsize > 40 and draw.textlength(m["title"], font=font("Futura.ttc", tsize)) > maxw:
        tsize -= 2
    f_title = font("Futura.ttc", tsize)
    release = m["release"] if (m["album_id"] and m["album_id"] != "net-radio") else ""

    h_track, h_title, h_artist = 48, tsize + 12, 56
    h_rel = 44 if release else 0
    ty = (CY + COV) - (h_track + h_title + h_artist + h_rel)

    draw.text((tx, ty), f"TRACK {n}  •  {idx} OF {count}", font=F_TRACK, fill=ACCENT)
    ty += h_track
    draw.text((tx, ty), m["title"], font=f_title, fill=(255, 255, 255))
    ty += h_title
    draw.text((tx, ty), m["artist"], font=F_ARTIST, fill=(228, 233, 242))
    ty += h_artist
    if release:
        draw.text((tx, ty), release, font=F_REL, fill=(170, 180, 195))
        if m["year"]:
            rw = draw.textlength(release, font=F_REL)
            draw.text((tx + rw + 16, ty + 3), m["year"], font=F_YEAR, fill=(150, 160, 175))

    draw.text((90, 70), header, font=F_HEAD, fill=(210, 220, 235))
    logo = Image.open(LOGO).convert("RGB")
    lw = 150; logo = logo.resize((lw, round(lw * logo.height / logo.width)), Image.LANCZOS)
    img.paste(logo, (W - lw - 80, 60))
    return img.convert("RGB")


def hms(t):
    t = int(round(t)); h, m, s = t // 3600, (t % 3600) // 60, t % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# --- bars filter (from mkmysteryvideo.sh, moved to a bottom strip) ------------------
BW, BH, BX, BY = 1740, 120, 90, H - 150
def bars_filter(ain):
    cols = BW // 20                       # ~20px pitch after neighbour-upscale
    return (
        f"[{ain}]showfreqs=s={cols}x{BH}:mode=bar:ascale=log:fscale=log:"
        f"win_size=1024:averaging=3,"
        f"scale={BW}:{BH}:flags=neighbor,"
        f"colorize=hue=232:saturation=0.62,format=rgba,"
        f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
        f"a='if(lt(mod(X\\,20),12),alpha(X\\,Y),0)',"
        f"colorchannelmixer=aa=0.95,gblur=sigma=1.0[bars]"
    )


def main():
    if len(sys.argv) < 3:
        raise SystemExit("usage: streamvideo.py <start> <end> [outdir]")
    start, end = int(sys.argv[1]), int(sys.argv[2])
    outdir = sys.argv[3] if len(sys.argv) > 3 else os.path.join(
        os.path.expanduser("~"), "Downloads/Netradio/stream-uploads")
    os.makedirs(outdir, exist_ok=True)
    tr, albums, sf = load_meta()
    count = end - start + 1
    header = f"netradio.com  Drum & Bass ISDN — 1998 Stream — Tracks {start}–{end}"

    T0, T1, segs = plan_audio(tr, sf, start, end)
    print(f"span {T0:.1f}..{T1:.1f}s ({(T1-T0)/60:.1f} min), {len(segs)} audio segments")

    work = tempfile.mkdtemp(prefix="streamvid.")
    try:
        audio = assemble_audio(segs, work)

        print("rendering background treemap…")
        bg = render_background(tr, albums, start, end)

        print(f"rendering {count} track cards…")
        begins = [merged(tr, albums, n)["begin"] for n in range(start, end + 1)]
        durs = [(begins[i + 1] if i + 1 < count else T1) - begins[i] for i in range(count)]
        cards = []
        for i, n in enumerate(range(start, end + 1)):
            card = os.path.join(work, f"card{n:03d}.png")
            render_card(bg, tr, albums, n, i + 1, count, header).save(card)
            cards.append(card)

        # Foreground = the cards dissolved into each other. Each card input is padded by
        # XFADE seconds so the running xfade overlaps don't compress the timeline: the k-th
        # dissolve is centred on the track's real master_begin, keeping cards synced to the
        # audio (and to the chapters). D<=0 => hard cuts.
        D = max(0.0, float(os.environ.get("XFADE", "0.6")))
        aidx = count                       # audio is the input after the N card images
        vinputs = []
        for i, c in enumerate(cards):
            vinputs += ["-loop", "1", "-framerate", str(FPS),
                        "-t", f"{durs[i] + (D if count > 1 else 0):.3f}", "-i", c]

        chain = [f"[{i}:v]fps={FPS},scale={W}:{H},format=yuv420p,setsar=1[c{i}]"
                 for i in range(count)]
        if count == 1 or D <= 0:
            if count > 1:                  # hard-cut concat of the prepared cards
                chain.append("".join(f"[c{i}]" for i in range(count))
                             + f"concat=n={count}:v=1:a=0[fg]")
                last = "fg"
            else:
                last = "c0"
        else:
            prev = "c0"
            for k in range(1, count):
                off = max(0.0, (begins[k] - T0) - D / 2)
                chain.append(f"[{prev}][c{k}]xfade=transition=fade:"
                             f"duration={D}:offset={off:.3f}[x{k}]")
                prev = f"x{k}"
            last = prev

        filt = (";".join(chain) + ";" + bars_filter(f"{aidx}:a")
                + f";[{last}][bars]overlay={BX}:{BY}:format=auto,format=yuv420p[v]")

        out = os.path.join(outdir, f"Netradio Stream — Tracks {start}-{end}.mp4")
        print(f"encoding {out} …")
        subprocess.run([
            FFMPEG, "-v", "error", "-stats", "-y", *vinputs, "-i", audio,
            "-filter_complex", filt,
            "-map", "[v]", "-map", f"{aidx}:a",
            "-c:v", "libx264", "-preset", os.environ.get("PRESET", "medium"),
            "-crf", "20", "-r", str(FPS),
            "-c:a", "aac", "-b:a", "192k", "-shortest", out,
        ], check=True)

        chapters = os.path.join(outdir, f"chapters — tracks {start}-{end}.txt")
        with open(chapters, "w") as fh:
            for n in range(start, end + 1):
                m = merged(tr, albums, n)
                fh.write(f"{hms(m['begin'] - T0)}  {m['artist']} — {m['title']}\n")
        print(f"\nwrote: {out}")
        print(f"wrote: {chapters}  (paste into the YouTube description)")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
