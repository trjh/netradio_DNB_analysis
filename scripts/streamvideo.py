#!/usr/bin/env python3
"""Render a "chunk of stream" video for a range of tracks from the 1998
Netradio D&B ISDN reconstruction. Companion to mkmysteryvideo.sh (the single-track
"Unknown Track N" mystery posts); this one does a continuous multi-track span.

  scripts/streamvideo.py <start> <end> [outdir] [--no-fetch]
  scripts/streamvideo.py 1 31

What it builds, from this repo's own data (never the CSV): the tracks and their master
positions from `track-metadata.json`, the capture files' positions on the master timeline
from `stream-files.json` (a mirror of the file the stream's file-ordering generator writes),
the album art from the art folder, and the audio from NETRADIO_MP3_DIR:
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

Album art: each track's cover is `<art folder>/<artwork>`, the file name `track-metadata.json`
records (the track's own, else its album's). The art folder is NETRADIO_ART_DIR, default `art/`
in this repo (gitignored). A cover that is not in the folder is downloaded from the artwork URL
the same entry records (`artwork_hires`, else `artwork_url`) and kept there; `--no-fetch` turns
that off. A track's own cover wins over its album's (the metadata's inheritance rule), so a
track that records only an artwork URL shows that image rather than the album cover. Only
http/https URLs whose host resolves to public addresses are fetched: each redirect and the
address each connection reached are checked the same way, and no proxy is used. A download has
a per-read timeout, an overall deadline and a size cap, and only a response that is an image is
kept. Nothing is ever generated: a track with no cover file and no URL, or whose download
fails, shows the netradio.com logo.

Settings come from the environment, else from this repo's `.env` (see `.env.example`).

Deps: python3 + Pillow (brew install pillow) for the stills, ffmpeg for audio+encode.
This Homebrew ffmpeg has no libfreetype (no drawtext) -- all text is drawn with Pillow.
"""
import argparse
import http.client
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict

try:                                    # the rendering needs Pillow; the planning does not
    from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance
except ImportError:                     # pragma: no cover - checked again in main()
    Image = ImageDraw = ImageFont = ImageFilter = ImageEnhance = None

# --- paths ------------------------------------------------------------------
SELF = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(SELF)
LOGO = os.path.join(REPO, "logo", "logo.jpg")
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


def setting(name, env=None):
    """The environment wins; this repo's `.env` fills a gap."""
    env = ENV if env is None else env
    return os.environ.get(name) or env.get(name) or None


ENV = read_env(os.path.join(REPO, ".env"))
MP3_DIR = setting("NETRADIO_MP3_DIR")
FFMPEG = setting("NETRADIO_FFMPEG") or "ffmpeg"
ART_DIR = os.path.expanduser(setting("NETRADIO_ART_DIR") or os.path.join(REPO, "art"))

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
def load_meta(repo=REPO):
    with open(os.path.join(repo, "track-metadata.json"), encoding="utf-8") as fh:
        d = json.load(fh)
    with open(os.path.join(repo, "stream-files.json"), encoding="utf-8") as fh:
        sf = json.load(fh)
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
        "begin": t["master_begin_seconds"],
        "end": t["master_end_seconds"],
    }


# --- album art ---------------------------------------------------------------
ART_MAX_BYTES = 10 * 1024 * 1024        # a cover is a few hundred KB; anything near this is not one
ART_TIMEOUT_S = 20                      # each connect and each read
ART_DEADLINE_S = 60                     # the whole download, however the bytes arrive
ART_CHUNK = 64 * 1024
ART_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
UA = "netradio-streamvideo/1.0 (+album art for a stream video)"


def _is_image(head):
    """JPEG, PNG, GIF or WebP, by the first bytes -- never by a header the server chose."""
    return (head[:3] == b"\xff\xd8\xff" or head[:8] == b"\x89PNG\r\n\x1a\n"
            or head[:6] in (b"GIF87a", b"GIF89a")
            or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"))


def _http_url(url):
    try:
        u = urllib.parse.urlsplit(url or "")
    except ValueError:
        return False
    return u.scheme in ("http", "https") and bool(u.hostname)


_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_IPV4_COMPAT = ipaddress.ip_network("::/96")


def _public_ip(addr):
    """True when `addr` (a string) is a public unicast address. An IPv6 address that carries
    an IPv4 address (mapped, compatible, NAT64 or 6to4) is judged by the IPv4 address too."""
    try:
        ip = ipaddress.ip_address(str(addr).split("%", 1)[0])
    except ValueError:
        return False
    if not ip.is_global or ip.is_multicast:
        return False
    if ip.version == 6:
        inner = ip.ipv4_mapped or ip.sixtofour
        if inner is None and (ip in _NAT64 or ip in _IPV4_COMPAT):
            inner = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if inner is not None and (not inner.is_global or inner.is_multicast):
            return False
    return True


def _public_host(host, resolve=None):
    """True when every address `host` resolves to is a public one. A private, loopback,
    link-local, multicast, reserved or unspecified address -- or a name that does not
    resolve -- is refused, so a recorded URL can never reach this machine or its network."""
    resolve = resolve or socket.getaddrinfo
    try:
        infos = resolve(host, None)
    except (OSError, UnicodeError, ValueError):
        return False
    addrs = {info[4][0] for info in infos}
    return bool(addrs) and all(_public_ip(a) for a in addrs)


def _safe_url(url, resolve=None):
    """http/https, with a host that resolves only to public addresses."""
    return _http_url(url) and _public_host(urllib.parse.urlsplit(url).hostname, resolve)


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    """Every redirect hop is checked as the first URL was, before it is followed."""

    def __init__(self, resolve=None):
        super().__init__()
        self._resolve = resolve

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _safe_url(newurl, self._resolve):
            raise urllib.error.URLError("redirect to a refused URL: %s" % newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _check_peer(sock):
    """The address the socket really connected to must be public too: the connection resolves
    the host again, and that answer may differ from the one `_public_host` checked."""
    peer = sock.getpeername()[0]
    if not _public_ip(peer):
        sock.close()
        raise OSError("connected to a non-public address: %s" % peer)


class _CheckedHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        super().connect()
        _check_peer(self.sock)


class _CheckedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        super().connect()
        _check_peer(self.sock)


class _CheckedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_CheckedHTTPConnection, req)


class _CheckedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_CheckedHTTPSConnection, req, context=self._context)


def _default_opener(resolve=None):
    """No proxy (a proxy would resolve the host itself), the peer checked on every connection,
    and every redirect hop checked before it is followed."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _CheckedHTTPHandler(),
                                       _CheckedHTTPSHandler(), _CheckedRedirect(resolve)).open


def _read_capped(resp, max_bytes, deadline_s, clock):
    """Read at most `max_bytes` + 1 bytes, giving up once `deadline_s` has passed."""
    end = clock() + deadline_s
    buf = bytearray()
    while len(buf) <= max_bytes:
        if clock() > end:
            raise TimeoutError("download took over %s s" % deadline_s)
        chunk = resp.read(min(ART_CHUNK, max_bytes + 1 - len(buf)))
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


def fetch_art(url, dest, opener=None, max_bytes=ART_MAX_BYTES, timeout=ART_TIMEOUT_S,
              resolve=None, deadline_s=ART_DEADLINE_S, clock=time.monotonic):
    """Download one cover to `dest`. Returns True when `dest` now holds an image.

    http/https only, to a host whose addresses are all public (checked after resolution, at
    every redirect hop, and on the address each connection reached; no proxy), `timeout`
    seconds per connect or read and `deadline_s` for the whole download, at most `max_bytes`,
    and the bytes must be an image. The file is written beside `dest` and renamed into place, so a failed download
    never leaves a half file to be read later."""
    if not _http_url(url):
        print(f"  art: not an http(s) URL, not fetched: {url!r}", file=sys.stderr)
        return False
    if not _public_host(urllib.parse.urlsplit(url).hostname, resolve):
        print(f"  art: host is not a public address, not fetched: {url}", file=sys.stderr)
        return False
    opener = opener or _default_opener(resolve)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with opener(req, timeout=timeout) as resp:
            final = resp.geturl() if hasattr(resp, "geturl") else url
            if final != url and not _safe_url(final, resolve):
                print(f"  art: redirected to a refused URL: {url}", file=sys.stderr)
                return False
            data = _read_capped(resp, max_bytes, deadline_s, clock)
    except Exception as exc:                       # network, HTTP status, timeout: no cover
        print(f"  art: download failed ({exc.__class__.__name__}): {url}", file=sys.stderr)
        return False
    if len(data) > max_bytes:
        print(f"  art: over {max_bytes} bytes, refused: {url}", file=sys.stderr)
        return False
    if not _is_image(data[:16]):
        print(f"  art: not an image, refused: {url}", file=sys.stderr)
        return False
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, dest)
    return True


def _plain_name(name):
    """A bare file name: no directory part, not `.` or `..`, no NUL. Anything else is refused,
    so a cover is never read or written outside the art folder."""
    return (isinstance(name, str) and name not in ("", ".", "..") and "\0" not in name
            and "/" not in name and "\\" not in name and os.path.basename(name) == name)


def _art_name(entry, fallback):
    """The cover's file name in the art folder: the recorded `artwork`, else `fallback` plus the
    URL's image extension. Either way the name must be a bare file name, or there is none."""
    name = entry.get("artwork")
    if not name:
        url = entry.get("artwork_hires") or entry.get("artwork_url") or ""
        ext = os.path.splitext(urllib.parse.urlsplit(url).path)[1].lower()
        name = fallback + (ext if ext in ART_EXTS else ".jpg")
    return name if _plain_name(name) else None


def entry_cover(entry, fallback, art_dir=None, fetch=True, opener=None, resolve=None):
    """The cover file for one track or album entry, fetching it if allowed; None if none."""
    if not entry:
        return None
    url = entry.get("artwork_hires") or entry.get("artwork_url")
    if not entry.get("artwork") and not url:
        return None
    name = _art_name(entry, fallback)
    if not name:
        return None
    path = os.path.join(art_dir or ART_DIR, name)
    if os.path.exists(path):
        return path
    if fetch and url and fetch_art(url, path, opener=opener, resolve=resolve):
        return path
    return None


def album_cover(albums, album_id, art_dir=None, fetch=True, opener=None, resolve=None):
    p = entry_cover(albums.get(album_id or "") or {}, "album-%s" % album_id, art_dir, fetch,
                    opener, resolve)
    return p or LOGO


def track_cover(tr, albums, n, art_dir=None, fetch=True, opener=None, resolve=None):
    """The track's own cover, else its album's, else the logo (the station promos say so).

    A track's value wins over its album's, as everywhere in `track-metadata.json`: a track that
    records its own artwork URL shows that image (downloaded when missing), even when its album
    has a cover. With `--no-fetch` a track cover that is not already in the folder is skipped
    and the album's is used."""
    t = tr[str(n)]
    if t.get("use_logo"):
        return LOGO
    p = entry_cover(t, "track-%s" % n, art_dir, fetch, opener, resolve)
    return p or album_cover(albums, t.get("album"), art_dir, fetch, opener, resolve)


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
        raise SystemExit("NETRADIO_MP3_DIR not set (this repo's .env, or the environment)")
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


def render_background(tr, albums, start, end, fetch=True):
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
        im = Image.open(album_cover(albums, album, fetch=fetch)).convert("RGB")
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
def render_card(bg, tr, albums, n, idx, count, header, fetch=True):
    F_TRACK = font("Futura.ttc", 30)
    F_ARTIST = font("Futura.ttc", 44)
    F_REL = font("Futura.ttc", 32)
    F_YEAR = font("Georgia Italic.ttf", 32)
    F_HEAD = font("Futura.ttc", 34)
    m = merged(tr, albums, n)
    img = bg.copy().convert("RGBA")
    draw = ImageDraw.Draw(img)

    CX, CY, COV = 90, 545, 360
    cov = Image.open(track_cover(tr, albums, n, fetch=fetch)).convert("RGB")
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
    ap = argparse.ArgumentParser(description="Render a stream video for tracks START..END.")
    ap.add_argument("start", type=int)
    ap.add_argument("end", type=int)
    ap.add_argument("outdir", nargs="?",
                    default=os.path.join(os.path.dirname(REPO), "stream-uploads"),
                    help="where the mp4 and chapters go (default: stream-uploads beside the repo)")
    ap.add_argument("--no-fetch", action="store_true",
                    help="never download a missing cover; use the logo instead")
    args = ap.parse_args()
    if Image is None:
        raise SystemExit("need Pillow (brew install pillow)")
    start, end, outdir, fetch = args.start, args.end, args.outdir, not args.no_fetch
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
        bg = render_background(tr, albums, start, end, fetch=fetch)

        print(f"rendering {count} track cards…")
        begins = [merged(tr, albums, n)["begin"] for n in range(start, end + 1)]
        durs = [(begins[i + 1] if i + 1 < count else T1) - begins[i] for i in range(count)]
        cards = []
        for i, n in enumerate(range(start, end + 1)):
            card = os.path.join(work, f"card{n:03d}.png")
            render_card(bg, tr, albums, n, i + 1, count, header, fetch=fetch).save(card)
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
