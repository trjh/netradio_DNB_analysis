#!/usr/bin/env python3
"""Harvest chroma signatures from the internet, slowly, and match them against the Mysteries.

    PYTHONPATH=scripts .venv/bin/python scripts/harvest.py --seed-channel https://www.youtube.com/@back2theoldskoolera999
    PYTHONPATH=scripts .venv/bin/python scripts/harvest.py --run          # work the queue
    PYTHONPATH=scripts .venv/bin/python scripts/harvest.py --status

This runs for WEEKS. It is built to be a good citizen, and to survive being one.

The idea
--------
The matcher can only find what is in the pool, and the pool we want is far bigger than this
disk. So **keep the signature, not the audio**: a chroma signature is a 12xN float16 matrix,
~55KB against ~8MB for the track. A 100,000-track pool is ~5GB of signatures, and the audio
never has to touch the disk at all -- it is streamed, hashed to chroma, and dropped.

Except when it matters: **a near-match is KEPT**. If a candidate scores anywhere near a Mystery
Track we hold on to the audio, because that is the whole point and a re-download is exactly the
sort of request that gets you blocked.

Being a good citizen (and not getting blocked)
----------------------------------------------
* **Rotate hosts between tracks.** The strongest defence, and Tim's idea: no single site ever
  sees a burst, because consecutive fetches go to different places.
* **Per-host token buckets**, so YouTube being busy never makes us hammer SoundCloud.
* **Jittered delays, never fixed.** A metronome is the most bot-like thing there is.
* **Sessions**: work 4-5h, then idle 40-120 min. Humans browse in bursts.
* **Exponential backoff** on 429/403, and a HARD STOP on repeated 403 -- that is the site
  telling us to go away, and we listen.
* **Each track is fetched once, ever.** The signature cache guarantees it. That is the single
  biggest politeness win available, and it is free.

Pause/resume and progress live in `.harvest/` so the player's dashboard can drive them.
"""

import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np                                   # noqa: E402

from streamalign import audio as _audio              # noqa: E402
from streamalign import chroma_match as _cm          # noqa: E402
from streamalign import groundtruth as _gt           # noqa: E402
from streamalign import mystery as _mystery          # noqa: E402

HOME = _gt.REPO_ROOT
STATE_DIR = os.path.join(HOME, ".harvest")
STATE = os.path.join(STATE_DIR, "state.json")
QUEUE = os.path.join(STATE_DIR, "queue.json")
PAUSE = os.path.join(STATE_DIR, "PAUSED")
CACHE = os.path.join(HOME, ".chroma-cache")
KEEP = os.path.join(os.path.expanduser("~"), "media", "netradio-candidates")

HOP = 2048
QUERY_S = 120.0

# KEEPING AUDIO: a bounded LEADERBOARD, not a threshold.
#
# The calibration (docs/CALIBRATION.md) is unambiguous: the true-match and non-match populations
# OVERLAP (true up to 0.0971, non-match down to 0.0376, non-match MEDIAN 0.0949). So no cost gate
# works. Set it low enough to exclude non-matches and it throws away real ones; set it high enough
# to catch every real one (0.12) and it keeps essentially EVERYTHING -- which is what happened:
# 90 files kept out of 73 tracks analysed, defeating the entire point of not storing audio.
#
# I drew exactly the wrong conclusion from my own data. The calibration said "cost alone cannot
# separate these; rank is the signal" and I then set a cost-only gate.
#
# So: keep the best KEEP_TOP candidates PER MYSTERY, evicting the worst when a better one lands.
# Storage is bounded and predictable (7 mysteries x 12 x ~8MB ~ 700MB, once, not per week), the
# best candidates are always on disk to listen to, and it cannot be wrong about a threshold
# because it does not use one.
KEEP_TOP = 12
KEEP_CEILING = 0.130      # never keep something worse than the worst plausible true match
# A reported MATCH still needs cost AND margin. The populations OVERLAP (true match up to 0.0971,
# non-match down to 0.0376), so no cost alone can separate them: RANK is the reliable signal, and
# the margin test is what actually carries the gate. 40 of 41 tracks rank #1 against their own
# original, so the margin is real.
MATCH_COST = 0.050

# --- politeness ------------------------------------------------------------------------------
# Work for hours, then rest for a while. No quiet hours (Tim's call) -- the rotation and the
# jitter are what keep this civil, not the clock.
SESSION_S = (4 * 3600, 5 * 3600)
IDLE_S = (40 * 60, 120 * 60)
# Per-host: the MEAN gap between fetches to THAT host. Actual gaps are jittered 0.5x-2.0x, so a
# host never sees an even cadence.
HOST_GAP_S = {"youtube.com": 60.0, "youtu.be": 60.0, "soundcloud.com": 75.0,
              "bandcamp.com": 90.0, "_default": 90.0}
BACKOFF_START_S = 300.0
BACKOFF_MAX_S = 6 * 3600
BLOCK_AFTER = 5          # consecutive 403s from one host -> stop touching it


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def host_of(url):
    h = url.split("//", 1)[-1].split("/", 1)[0].lower()
    for known in HOST_GAP_S:
        if known != "_default" and known in h:
            return known
    return h or "_default"


def blank_state():
    return {"started": _now(), "updated": _now(), "analyzed": 0, "kept": 0, "errors": 0,
            "skipped_cached": 0, "matches": [], "issues": [], "hosts": {},
            "session": {"phase": "idle", "until": 0}, "current": None}


# --- the signature ----------------------------------------------------------------------------

def sig_path(url):
    return os.path.join(CACHE, "u" + hashlib.sha1(url.encode()).hexdigest()[:20] + ".npy")


def stream_chroma(url, keep_to=None):
    """Stream the audio, reduce it to a chroma signature, and DROP it -- unless `keep_to`.

    The audio never lands on disk: yt-dlp writes to stdout, ffmpeg decodes to 16kHz mono WAV on
    stdout, and we read it into numpy. That is what makes a pool of any size affordable here.
    """
    import librosa
    yt = subprocess.Popen(["yt-dlp", "-q", "--no-warnings", "--no-playlist",
                           "-f", "bestaudio", "-o", "-", url],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    ff = subprocess.Popen(["ffmpeg", "-v", "error", "-i", "pipe:0",
                           "-ac", "1", "-ar", str(_audio.SR), "-f", "f32le", "pipe:1"],
                          stdin=yt.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    yt.stdout.close()
    raw, _ = ff.communicate()
    yt_err = yt.stderr.read().decode("utf-8", "replace")
    yt.wait()

    if yt.returncode != 0 or not raw:
        return None, yt_err.strip().split("\n")[-1][:160] if yt_err else "no audio"
    y = np.frombuffer(raw, dtype="float32")
    if len(y) < 45 * _audio.SR:
        return None, "too short (%.0fs)" % (len(y) / _audio.SR)

    c = librosa.feature.chroma_cqt(y=np.asarray(y, dtype="float32"),
                                   sr=_audio.SR, hop_length=HOP) + 1e-6
    c = librosa.util.normalize(c, norm=2, axis=0)
    os.makedirs(CACHE, exist_ok=True)
    np.save(sig_path(url), c.astype("float16"))

    if keep_to:                       # a near match: keep the audio, we will want to hear it
        os.makedirs(os.path.dirname(keep_to), exist_ok=True)
        import soundfile as sf
        sf.write(keep_to, y, _audio.SR)
    return c, None


# --- the queue ---------------------------------------------------------------------------------

def enumerate_channel(url, limit=None):
    """Track URLs on a channel/playlist -- metadata only, NO audio. One cheap request."""
    cmd = ["yt-dlp", "-q", "--no-warnings", "--flat-playlist", "--print", "%(url)s", url]
    if limit:
        cmd += ["--playlist-end", str(limit)]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    return [u.strip() for u in out.stdout.split("\n") if u.strip().startswith("http")]


# Tim's own channel publishes the Mystery Track clips themselves. A harvester that "finds" one
# there has found nothing -- it has rediscovered its own question, and would report a triumphant
# 0.00 match. Never queue it.
EXCLUDE_CHANNELS = ("UCuYTatE2k5dOV8J8Bi3rK0g",)   # Tim Hunter


def add_to_queue(urls, source):
    if any(c in (source or "") for c in EXCLUDE_CHANNELS):
        print("refusing to queue %s -- it publishes the mystery clips themselves" % source)
        return 0
    q = _load(QUEUE, {"pending": [], "done": []})
    seen = set(q["pending"]) | set(q["done"])
    fresh = [u for u in urls if u not in seen]
    q["pending"].extend(fresh)
    _save(QUEUE, q)
    return len(fresh)


# --- the run ------------------------------------------------------------------------------------

def queries():
    """The unsolved mysteries, as chroma. From track-metadata.json -- never from filenames."""
    import librosa
    out = []
    for e in _mystery.searchable():
        y = _audio.load_audio(e["clip"])[:int(QUERY_S * _audio.SR)]
        c = librosa.feature.chroma_cqt(y=np.asarray(y, dtype="float32"),
                                       sr=_audio.SR, hop_length=HOP) + 1e-6
        out.append((e["number"], librosa.util.normalize(c, norm=2, axis=0)))
    return out


def pick_next(pending, state):
    """Next URL, ROTATING hosts so no single site ever sees a burst.

    This is the core politeness move: consecutive fetches go to DIFFERENT hosts, so even a fast
    run looks, from any one site's perspective, like an occasional visitor.
    """
    now = time.time()
    last = state.get("hosts", {})
    best, best_key = None, None
    for i, url in enumerate(pending):
        h = host_of(url)
        info = last.get(h, {})
        if info.get("blocked"):
            continue
        ready_at = info.get("next_ok", 0)
        # prefer the host we have left alone longest, and never one that isn't ready
        key = (ready_at > now, ready_at)
        if best_key is None or key < best_key:
            best, best_key = i, key
    return best


def run(args):
    state = _load(STATE, blank_state())
    qs = queries()
    if not qs:
        print("no unsolved mysteries with clips -- nothing to search for")
        return
    print("# searching for Mystery Tracks %s" % ", ".join(str(n) for n, _ in qs))
    print("# work %s, idle %s, rotating hosts, jittered. Ctrl-C is safe (state is on disk)."
          % ("4-5h", "40-120m"))

    session_end = time.time() + random.uniform(*SESSION_S)
    while True:
        if os.path.exists(PAUSE):
            state["session"] = {"phase": "paused", "until": 0}
            _save(STATE, state)
            time.sleep(20)
            continue

        q = _load(QUEUE, {"pending": [], "done": []})
        if not q["pending"]:
            state["session"] = {"phase": "queue empty", "until": 0}
            _save(STATE, state)
            print("queue empty -- add more with --seed-channel")
            return

        if time.time() > session_end:                 # rest
            nap = random.uniform(*IDLE_S)
            state["session"] = {"phase": "idle", "until": time.time() + nap}
            _save(STATE, state)
            print("# session over -- idling %.0f min" % (nap / 60))
            time.sleep(nap)
            session_end = time.time() + random.uniform(*SESSION_S)
            continue

        idx = pick_next(q["pending"], state)
        if idx is None:
            time.sleep(60)
            continue
        url = q["pending"][idx]
        host = host_of(url)
        hinfo = state.setdefault("hosts", {}).setdefault(host, {})

        wait = hinfo.get("next_ok", 0) - time.time()
        if wait > 0:
            state["session"] = {"phase": "waiting on %s" % host, "until": hinfo["next_ok"]}
            _save(STATE, state)
            time.sleep(min(wait, 60))
            continue

        state["session"] = {"phase": "working", "until": session_end}
        state["current"] = url
        _save(STATE, state)

        cached = os.path.exists(sig_path(url))
        if cached:
            c = np.load(sig_path(url)).astype("float32")
            err = None
            state["skipped_cached"] += 1
        else:
            c, err = stream_chroma(url)

        # --- host pacing: jittered, so the cadence is never even ---
        gap = HOST_GAP_S.get(host, HOST_GAP_S["_default"]) * random.uniform(0.5, 2.0)
        if err and ("403" in err or "429" in err or "blocked" in err.lower()):
            hinfo["strikes"] = hinfo.get("strikes", 0) + 1
            back = min(BACKOFF_START_S * (2 ** (hinfo["strikes"] - 1)), BACKOFF_MAX_S)
            hinfo["next_ok"] = time.time() + back
            if hinfo["strikes"] >= BLOCK_AFTER:
                hinfo["blocked"] = True               # it told us to go away. we listen.
                state["issues"].append({"at": _now(), "host": host,
                                        "issue": "blocked after %d refusals -- backing off for "
                                                 "good" % hinfo["strikes"]})
            state["errors"] += 1
            _save(STATE, state)
            continue
        hinfo["strikes"] = 0
        hinfo["next_ok"] = time.time() + gap

        q["pending"].pop(idx)
        q["done"].append(url)
        _save(QUEUE, q)

        if c is None:
            state["errors"] += 1
            state["issues"] = (state["issues"] + [{"at": _now(), "url": url,
                                                   "issue": err or "no signature"}])[-50:]
            _save(STATE, state)
            continue

        state["analyzed"] += 1
        for num, qc in qs:
            cost, shift = _cm.match(qc, c)
            if cost is None or cost > KEEP_CEILING:
                continue
            board = [m for m in state["matches"] if m["mystery"] == num]
            board.sort(key=lambda m: m["cost"])
            if len(board) >= KEEP_TOP and cost >= board[-1]["cost"]:
                continue                       # not good enough to displace anyone

            keep_to = os.path.join(KEEP, "MT%d-%.4f-%s.wav"
                                   % (num, cost, hashlib.sha1(url.encode()).hexdigest()[:8]))
            if not os.path.exists(keep_to):
                stream_chroma(url, keep_to=keep_to)
                state["kept"] += 1
            hit = {"at": _now(), "mystery": num, "cost": round(cost, 4),
                   "semitones": shift, "url": url, "audio": keep_to,
                   "verdict": "MATCH" if cost <= MATCH_COST else "near"}
            state["matches"].append(hit)

            # evict the worst if the board is now over-full -- bounded storage, best kept
            board = sorted([m for m in state["matches"] if m["mystery"] == num],
                           key=lambda m: m["cost"])
            for dead in board[KEEP_TOP:]:
                state["matches"].remove(dead)
                try:
                    os.unlink(dead["audio"])
                    state["kept"] -= 1
                except OSError:
                    pass
            print("  %s  MT%d  cost %.4f  %s  %s"
                  % (hit["verdict"], num, cost, _cm.describe_shift(shift), url))
        state["updated"] = _now()
        _save(STATE, state)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed-channel", action="append", default=[],
                    help="enumerate a YouTube/SoundCloud channel or playlist into the queue")
    ap.add_argument("--limit", type=int, default=None, help="cap how many to take from a channel")
    ap.add_argument("--run", action="store_true", help="work the queue (runs for weeks)")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--pause", action="store_true")
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    os.makedirs(STATE_DIR, exist_ok=True)
    if args.pause:
        open(PAUSE, "w").close()
        print("paused (the runner will notice within ~20s)")
        return
    if args.resume:
        if os.path.exists(PAUSE):
            os.unlink(PAUSE)
        print("resumed")
        return
    if args.status:
        s = _load(STATE, blank_state())
        q = _load(QUEUE, {"pending": [], "done": []})
        print(json.dumps({"analyzed": s["analyzed"], "kept": s["kept"], "errors": s["errors"],
                          "pending": len(q["pending"]), "matches": len(s["matches"]),
                          "phase": s.get("session", {}).get("phase"),
                          "paused": os.path.exists(PAUSE)}, indent=2))
        return
    for ch in args.seed_channel:
        urls = enumerate_channel(ch, args.limit)
        n = add_to_queue(urls, ch)
        print("seeded %d new track(s) from %s (%d found)" % (n, ch, len(urls)))
    if args.run:
        run(args)


if __name__ == "__main__":
    main()
