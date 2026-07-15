#!/usr/bin/env python3
"""Identify a mystery record by acoustic-fingerprint APIs (ACRCloud + AudD).

    set -a && . ./.env_vars && set +a          # needs the API keys below
    python3 scripts/identify_by_api.py --query "Mystery Track 4.wav"
    python3 scripts/identify_by_api.py --all-mystery
    python3 scripts/identify_by_api.py --query "Mystery Track 4.wav" --windows 6 --json out.json

Why this exists, and how it differs from `identify_by_chroma.py`
---------------------------------------------------------------
`identify_by_chroma.py` matches against a LOCAL pool -- it can only find a record you already
have on disk. This asks the big commercial catalogues (ACRCloud ~150M, AudD ~160M tracks)
instead, so it can name a record nobody here owns. That is the whole reason to use it.

The catch, stated honestly. This project already proved (`Archive/LESSON_acoustid_stream.md`)
that Chromaprint FAILS on this material: a 1998 broadcast capture scores 0.511 against its own
clean original, and 0.50 is random. ACRCloud and AudD are ALSO spectral-peak fingerprinters, so
the ISDN/RealAudio codec + the DJ's EQ may defeat them the same way. This tool does not assume
they win -- it is the experiment that finds out. Treat every hit as a lead to CONFIRM BY EAR.

Clean windows, all hits
-----------------------
Each service reads only ~10-12 s per request, so we cut several short windows from the
**interior** of the clip (the edges are where a neighbouring record bleeds in) and submit each.
Every result each service returns is printed -- no consensus, no gate; you audition them all,
the way the listen/harvest queues are triaged.

Credentials (in `.env_vars`, gitignored)
-----------------------------------------
  ACRCLOUD_HOST           e.g. identify-eu-west-1.acrcloud.com   (from your ACRCloud project)
  ACRCLOUD_ACCESS_KEY
  ACRCLOUD_ACCESS_SECRET
  AUDD_API_TOKEN          from audd.io (300 free requests, no card)
A service is used only if its keys are present; with none set, this explains how to get them.
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

WINDOW_S = 12.0          # ACRCloud/AudD analyse ~10-12s; keep the clip at/under that
DEFAULT_WINDOWS = 4      # how many interior windows per clip
EDGE_FRAC = 0.10         # skip this fraction at each end (mix bleed / talkover live at the edges)
THROTTLE_S = 1.0         # be polite to the free tiers


# --- carving -------------------------------------------------------------------------------

def probe_duration(path):
    """Clip length in seconds via ffprobe, or None."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30).stdout.strip()
        return float(out) if out else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def window_starts(duration, n, length=WINDOW_S):
    """`n` window start-times spread across the clip's interior, avoiding both ends.

    A record pulled from a continuous mix is cleanest in the middle; the first and last
    stretch is where the previous/next track is fading in or out, or the DJ is talking.
    """
    edge = min(duration * EDGE_FRAC, 10.0)
    lo, hi = edge, duration - edge - length
    if hi <= lo:                      # clip too short to trim -- use the whole thing once
        return [max(0.0, (duration - length) / 2.0)]
    if n == 1:
        return [(lo + hi) / 2.0]
    step = (hi - lo) / (n - 1)
    return [lo + i * step for i in range(n)]


def carve(path, start, length=WINDOW_S):
    """Return `length` seconds of `path` from `start` as WAV bytes (mono 44.1k), via ffmpeg.

    Piped, so no temp files. Mono keeps the upload small; 44.1k preserves the spectral band the
    fingerprint keys on.
    """
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", "%.3f" % start, "-t", "%.3f" % length,
         "-i", path, "-ac", "1", "-ar", "44100", "-f", "wav", "pipe:1"],
        capture_output=True, timeout=60)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError("ffmpeg failed to carve %s @ %.1fs: %s"
                           % (os.path.basename(path), start, proc.stderr.decode()[:200]))
    return proc.stdout


# --- HTTP (stdlib multipart, like acoustid_check.py stays on urllib) -----------------------

def _multipart(fields, sample_bytes, file_field):
    """Encode `fields` (str->str) plus the WAV under `file_field` into multipart/form-data.

    The file field name differs by service -- ACRCloud reads `sample`, AudD reads `file` (and
    rejects the request with error 700 if it is named anything else).
    """
    boundary = "----netradioMusicId"
    crlf = b"\r\n"
    body = bytearray()
    for key, value in fields.items():
        body += b"--" + boundary.encode() + crlf
        body += ('Content-Disposition: form-data; name="%s"' % key).encode() + crlf + crlf
        body += str(value).encode() + crlf
    body += b"--" + boundary.encode() + crlf
    body += ('Content-Disposition: form-data; name="%s"; filename="sample.wav"'
             % file_field).encode() + crlf
    body += b"Content-Type: audio/wav" + crlf + crlf
    body += sample_bytes + crlf
    body += b"--" + boundary.encode() + b"--" + crlf
    return "multipart/form-data; boundary=" + boundary, bytes(body)


def _post(url, content_type, body, timeout=45):
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": content_type})
    with urllib.request.urlopen(req, timeout=timeout) as resp:   # noqa: S310 (trusted vendor)
        return json.loads(resp.read().decode("utf-8", "replace"))


# --- services ------------------------------------------------------------------------------

def acrcloud_creds():
    host = os.environ.get("ACRCLOUD_HOST")
    key = os.environ.get("ACRCLOUD_ACCESS_KEY")
    secret = os.environ.get("ACRCLOUD_ACCESS_SECRET")
    return (host, key, secret) if (host and key and secret) else None


def acrcloud_identify(sample_bytes, creds, now=None):
    """POST one sample to ACRCloud /v1/identify. Returns a list of hit dicts (may be several)."""
    host, key, secret = creds
    ts = str(int(now if now is not None else time.time()))
    to_sign = "\n".join(["POST", "/v1/identify", key, "audio", "1", ts])
    sig = base64.b64encode(hmac.new(secret.encode(), to_sign.encode(),
                                    hashlib.sha1).digest()).decode()
    fields = {"access_key": key, "data_type": "audio", "signature_version": "1",
              "signature": sig, "sample_bytes": str(len(sample_bytes)), "timestamp": ts}
    ctype, body = _multipart(fields, sample_bytes, file_field="sample")
    data = _post("https://%s/v1/identify" % host, ctype, body)
    code = data.get("status", {}).get("code")
    if code == 1001:
        return []                       # genuine "no result"
    if code != 0:                       # auth / quota / bad-request -> surface, don't mask
        raise RuntimeError("ACRCloud %s: %s" % (code, data.get("status", {}).get("msg")))
    hits = []
    for m in data.get("metadata", {}).get("music", []):
        hits.append(_hit("ACRCloud",
                         artist=", ".join(a.get("name", "") for a in m.get("artists", [])),
                         title=m.get("title"),
                         album=(m.get("album") or {}).get("name"),
                         label=m.get("label"),
                         year=(m.get("release_date") or "")[:4],
                         score=m.get("score"),
                         link=_first_link(m.get("external_metadata", {}))))
    return hits


def audd_creds():
    return os.environ.get("AUDD_API_TOKEN")


def audd_identify(sample_bytes, token):
    """POST one sample to AudD. Returns a list with 0 or 1 hit (AudD gives a single best)."""
    fields = {"api_token": token or "", "return": "apple_music,spotify"}
    ctype, body = _multipart(fields, sample_bytes, file_field="file")
    data = _post("https://api.audd.io/", ctype, body)
    if data.get("status") == "error":   # bad request / quota / no-token -> surface, don't mask
        raise RuntimeError("AudD %s: %s" % (data.get("error", {}).get("error_code"),
                                            data.get("error", {}).get("error_message", "")[:120]))
    if not data.get("result"):
        return []                       # genuine no-match (result: null)
    r = data["result"]
    return [_hit("AudD", artist=r.get("artist"), title=r.get("title"),
                 album=r.get("album"), label=r.get("label"),
                 year=(r.get("release_date") or "")[:4], score=None,
                 link=r.get("song_link"))]


def _hit(service, **kw):
    kw["service"] = service
    return kw


def _first_link(external):
    for k in ("spotify", "youtube", "deezer"):
        node = external.get(k) or {}
        if k == "spotify" and node.get("track", {}).get("id"):
            return "https://open.spotify.com/track/" + node["track"]["id"]
        if node.get("vid"):
            return "https://youtu.be/" + node["vid"]
    return None


# --- driver --------------------------------------------------------------------------------

def identify_clip(path, services, n_windows, throttle=THROTTLE_S):
    """Carve `n_windows` interior windows and submit each to every service. Returns
    [{start, hits:[...]}] -- every hit from every window, nothing filtered."""
    duration = probe_duration(path)
    if not duration:
        raise RuntimeError("could not read duration of %s" % path)
    results = []
    for start in window_starts(duration, n_windows):
        sample = carve(path, start)
        hits = []
        for name, fn in services:
            try:
                hits.extend(fn(sample))
            except Exception as exc:                             # network/parse: report, go on
                hits.append(_hit(name, title="(error: %s)" % str(exc)[:80]))
            if throttle:
                time.sleep(throttle)
        results.append({"start": start, "hits": hits})
    return results


def _fmt_hit(h):
    who = " - ".join(x for x in (h.get("artist"), h.get("title")) if x) or "(no title)"
    extra = ", ".join(x for x in (
        h.get("album"), h.get("label"),
        h.get("year") or None,
        ("score %s" % h["score"]) if h.get("score") is not None else None) if x)
    line = "%-9s %s" % (h["service"], who)
    if extra:
        line += "  [%s]" % extra
    if h.get("link"):
        line += "  " + h["link"]
    return line


def resolve_services(which):
    """[(name, fn)] for the services asked for that actually have credentials."""
    out = []
    acr, audd = acrcloud_creds(), audd_creds()
    if which in ("all", "acrcloud") and acr:
        out.append(("ACRCloud", lambda s, c=acr: acrcloud_identify(s, c)))
    if which in ("all", "audd") and audd is not None:
        out.append(("AudD", lambda s, t=audd: audd_identify(s, t)))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--query", action="append", help="clip to identify (repeatable)")
    ap.add_argument("--all-mystery", action="store_true",
                    help="identify every unsolved Mystery Track clip (via track-metadata.json)")
    ap.add_argument("--windows", type=int, default=DEFAULT_WINDOWS,
                    help="interior windows to submit per clip (default %d)" % DEFAULT_WINDOWS)
    ap.add_argument("--service", choices=("all", "acrcloud", "audd"), default="all")
    ap.add_argument("--json", default=None, help="also write machine-readable results here")
    args = ap.parse_args(argv)

    services = resolve_services(args.service)
    if not services:
        sys.exit(
            "No music-ID credentials set. Add to .env_vars (gitignored):\n"
            "  AUDD_API_TOKEN=...            # audd.io -- 300 free requests, no card\n"
            "  ACRCLOUD_HOST=...             # identify-<region>.acrcloud.com\n"
            "  ACRCLOUD_ACCESS_KEY=...       # from your ACRCloud project\n"
            "  ACRCLOUD_ACCESS_SECRET=...\n"
            "then: set -a && . ./.env_vars && set +a")

    sources = os.environ.get("NETRADIO_SOURCES_DIR")
    queries = list(args.query or [])
    if args.all_mystery:
        if not sources:
            sys.exit("--all-mystery needs NETRADIO_SOURCES_DIR")
        from streamalign import mystery as _mystery
        queries += [e["clip"] for e in _mystery.searchable(sources)]
    if not queries:
        sys.exit("nothing to identify: pass --query or --all-mystery")

    print("# music-ID via %s  (%d window(s)/clip, ~%.0fs each)\n"
          % (" + ".join(n for n, _ in services), args.windows, WINDOW_S))

    report = []
    for q in queries:
        path = q if os.path.exists(q) else os.path.join(sources or "", q)
        name = os.path.basename(path)
        if not os.path.exists(path):
            print("  %s: not found\n" % q)
            continue
        print("  %s" % name)
        try:
            windows = identify_clip(path, services, args.windows)
        except RuntimeError as exc:
            print("    (%s)\n" % exc)
            continue
        any_hit = False
        for w in windows:
            for h in w["hits"]:
                any_hit = True
                print("    @%5.1fs  %s" % (w["start"], _fmt_hit(h)))
        if not any_hit:
            print("    ==> no match from any service, any window.")
        print()
        report.append({"clip": name, "windows": windows})

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print("# wrote %s" % args.json)

    print("# Every hit is a LEAD -- confirm by ear. Acoustic fingerprinting may be defeated by\n"
          "# the 1998 ISDN/EQ damage (see Archive/LESSON_acoustid_stream.md); no match is not\n"
          "# proof the record is obscure, only that the catalogue+codec did not line up.")


if __name__ == "__main__":
    sys.exit(main())
