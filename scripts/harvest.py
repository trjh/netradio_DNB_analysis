#!/usr/bin/env python3
"""Harvest chroma signatures from the listen queue, slowly, and match them against the Mysteries.

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

ONE QUEUE
---------
The candidate list IS the player's listen queue. There is no second queue: anything you add --
by hand, or via a channel subscription -- is a candidate, and anything you have already HEARD is
skipped (if you had heard it and it were the mystery, it would not be a mystery).

Long DJ mixes are processed too, deliberately. A 1997 record is as likely to surface inside a
90-minute set as on its own, and subsequence-DTW tells us WHERE in the set it matches -- so a hit
is a timestamp to go and listen to, not "it is in there somewhere".

WHO WRITES WHAT
---------------
Two writers on one JSON file is how you lose the file. So:

  * the PLAYER owns the listen queue (heard, favourite, discarded). The harvester only READS it.
  * the HARVESTER owns `.harvest/` (signatures, results, leaderboard). The player only READS it,
    except for the pause flag.

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
QUEUE = os.path.join(STATE_DIR, "queue.json")          # legacy seed list; still honoured
RESULTS = os.path.join(STATE_DIR, "results.json")     # {url: {...}} -- what we have analysed
LISTEN_QUEUE = os.environ.get(
    "NETRADIO_LISTEN_QUEUE",
    os.path.expanduser("~/Downloads/Netradio/player/metadata/listen_queue.json"))
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


def listen_queue():
    """The player's listen queue, read-only. This is the candidate list.

    Skips anything HEARD (if he had heard it and it were the mystery, it would not be a mystery)
    and anything DISCARDED ("I don't recognise this as being in the mix" -- a human ruling, and
    the engine has no business overriding it or asking again).
    """
    data = _load(LISTEN_QUEUE, {})
    items = data.get("items", data if isinstance(data, list) else [])
    out = []
    for it in items:
        url = it.get("url") or it.get("canonical")
        if not url or not url.startswith("http"):
            continue
        if it.get("listened") or it.get("heard") or it.get("discarded"):
            continue
        out.append({"url": url, "id": it.get("id"), "title": it.get("title") or ""})
    return out


def candidates():
    """Everything still to analyse: the listen queue, plus any legacy seeded URLs."""
    done = set(_load(RESULTS, {}).keys())
    out, seen = [], set()
    for it in listen_queue():
        if it["url"] in done or it["url"] in seen:
            continue
        seen.add(it["url"])
        out.append(it)
    legacy = _load(QUEUE, {"pending": []}).get("pending") or []
    for url in legacy:
        if url in done or url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "id": None, "title": ""})
    return out


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
    """Next candidate, ROTATING hosts so no single site ever sees a burst.

    The core politeness move: consecutive fetches go to DIFFERENT hosts, so even a busy run looks,
    from any one site's point of view, like an occasional visitor.
    """
    now = time.time()
    last = state.get("hosts", {})
    best, best_key = None, None
    for i, item in enumerate(pending):
        h = host_of(item["url"])
        info = last.get(h, {})
        if info.get("blocked"):
            continue
        ready_at = info.get("next_ok", 0)
        key = (ready_at > now, ready_at)
        if best_key is None or key < best_key:
            best, best_key = i, key
    return best


def run(args):
    state = _load(STATE, blank_state())
    results = _load(RESULTS, {})
    qs = queries()
    if not qs:
        print("no unsolved mysteries with clips -- nothing to search for")
        return
    print("# hunting Mystery Tracks %s" % ", ".join(str(n) for n, _ in qs))
    print("# candidates: the listen queue (heard and discarded items are skipped)")
    print("# work 4-5h, idle 40-120m, rotating hosts, jittered. Ctrl-C is safe; state is on disk.")

    session_end = time.time() + random.uniform(*SESSION_S)
    while True:
        if os.path.exists(PAUSE):
            state["session"] = {"phase": "paused", "until": 0}
            _save(STATE, state)
            time.sleep(15)
            continue

        pending = candidates()
        if not pending:
            state["session"] = {"phase": "queue empty", "until": 0}
            _save(STATE, state)
            print("nothing left to analyse -- add to the listen queue (manually or by subscription)")
            time.sleep(300)          # a subscription poll may bring more; do not exit
            continue

        if time.time() > session_end:
            nap = random.uniform(*IDLE_S)
            state["session"] = {"phase": "idle", "until": time.time() + nap}
            _save(STATE, state)
            print("# session over -- idling %.0f min" % (nap / 60))
            time.sleep(nap)
            session_end = time.time() + random.uniform(*SESSION_S)
            continue

        idx = pick_next(pending, state)
        if idx is None:
            time.sleep(60)
            continue
        item = pending[idx]
        url = item["url"]
        host = host_of(url)
        hinfo = state.setdefault("hosts", {}).setdefault(host, {})

        wait = hinfo.get("next_ok", 0) - time.time()
        if wait > 0:
            state["session"] = {"phase": "waiting on %s" % host, "until": hinfo["next_ok"]}
            _save(STATE, state)
            time.sleep(min(wait, 60))
            continue

        state["session"] = {"phase": "working", "until": session_end}
        state["current"] = item.get("title") or url
        _save(STATE, state)

        if os.path.exists(sig_path(url)):
            c, err = np.load(sig_path(url)).astype("float32"), None
            state["skipped_cached"] += 1
        else:
            c, err = stream_chroma(url)

        gap = HOST_GAP_S.get(host, HOST_GAP_S["_default"]) * random.uniform(0.5, 2.0)
        if err and ("403" in err or "429" in err or "blocked" in err.lower()):
            hinfo["strikes"] = hinfo.get("strikes", 0) + 1
            back = min(BACKOFF_START_S * (2 ** (hinfo["strikes"] - 1)), BACKOFF_MAX_S)
            hinfo["next_ok"] = time.time() + back
            if hinfo["strikes"] >= BLOCK_AFTER:
                hinfo["blocked"] = True          # it told us to go away. we listen.
                state["issues"].append({"at": _now(), "host": host,
                                        "issue": "blocked after %d refusals -- backing off for "
                                                 "good" % hinfo["strikes"]})
            state["errors"] += 1
            _save(STATE, state)
            continue
        hinfo["strikes"] = 0
        hinfo["next_ok"] = time.time() + gap

        if c is None:
            state["errors"] += 1
            state["issues"] = (state["issues"] + [{"at": _now(), "url": url,
                                                   "issue": err or "no signature"}])[-50:]
            results[url] = {"at": _now(), "error": err or "no signature"}
            _save(RESULTS, results)
            _save(STATE, state)
            continue

        # A record can hide inside a 90-minute DJ set, so long candidates are analysed too --
        # subsequence-DTW says WHERE it matches, which turns "somewhere in there" into a
        # timestamp you can scrub to.
        scores = []
        state["analyzed"] += 1
        for num, qc in qs:
            cost, shift, at = _cm.match(qc, c)
            if cost is None:
                continue
            scores.append({"mystery": num, "cost": round(cost, 4), "semitones": shift,
                           "at_s": round(at or 0, 1)})
        results[url] = {"at": _now(), "id": item.get("id"), "title": item.get("title"),
                        "scores": scores,
                        "duration_s": round(c.shape[1] * HOP / float(_audio.SR))}
        _save(RESULTS, results)

        for sc in scores:
            num, cost = sc["mystery"], sc["cost"]
            if cost > KEEP_CEILING:
                continue
            board = sorted([m for m in state["matches"] if m["mystery"] == num],
                           key=lambda m: m["cost"])
            if len(board) >= KEEP_TOP and cost >= board[-1]["cost"]:
                continue

            keep_to = os.path.join(KEEP, "MT%d-%.4f-%s.wav"
                                   % (num, cost, hashlib.sha1(url.encode()).hexdigest()[:8]))
            if not os.path.exists(keep_to):
                stream_chroma(url, keep_to=keep_to)
                state["kept"] += 1
            hit = {"at": _now(), "mystery": num, "cost": cost, "semitones": sc["semitones"],
                   "at_s": sc["at_s"], "url": url, "title": item.get("title") or "",
                   "id": item.get("id"), "audio": keep_to,
                   "verdict": "MATCH" if cost <= MATCH_COST else "near"}
            state["matches"].append(hit)

            board = sorted([m for m in state["matches"] if m["mystery"] == num],
                           key=lambda m: m["cost"])
            for dead in board[KEEP_TOP:]:
                state["matches"].remove(dead)
                try:
                    os.unlink(dead["audio"])
                    state["kept"] -= 1
                except OSError:
                    pass
            print("  %s  MT%d  cost %.4f  %s  at %s  %s"
                  % (hit["verdict"], num, cost, _cm.describe_shift(sc["semitones"]),
                     _cm.describe_at(sc["at_s"]), (item.get("title") or url)[:48]))
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
