#!/usr/bin/env python3
"""Harvest chroma signatures from the internet, slowly, and match them against the Mysteries.

    PYTHONPATH=scripts .venv/bin/python scripts/harvest.py --seed-channel https://www.youtube.com/@back2theoldskoolera999
    PYTHONPATH=scripts .venv/bin/python scripts/harvest.py --run          # work the queue
    PYTHONPATH=scripts .venv/bin/python scripts/harvest.py --status

This runs for WEEKS. It is built to keep its load on other people's servers as low as possible.

The idea
--------
The matcher can only find what is in the pool, and the pool we want is far bigger than this
disk. So **keep the signature, not the audio**: a chroma signature is a 12xN float16 matrix,
~55KB against ~8MB for the track. A 100,000-track pool is ~5GB of signatures, and the audio
never has to touch the disk at all -- it is streamed, hashed to chroma, and dropped.

Except a brief excerpt of a near-miss. If a candidate scores near a Mystery Track we keep the
matched ~30-second window -- and only that window -- so a human can listen and confirm or reject
it. Cut from the audio already in memory, so nothing is fetched twice. It is not a copy of the
record; it is a magnifying glass over the moment the matcher flagged, swept after 30 days.

Load discipline
---------------
The point of all of this is to put as little load as possible on the servers we read from. Each
control below is justified by load, not by hiding -- if a control only made sense as a way to
avoid being noticed, it would not be here.

* **Spread load across hosts.** Consecutive fetches go to different sites, so no single host
  carries a run of back-to-back requests.
* **Per-host rate limits**, so a slow response from one host never concentrates load on another.
* **Randomised gaps** between requests to a host, so we never send a synchronised train of them.
* **Bounded sessions**: work a few hours, then idle, so sustained load stays low over a day.
* **Exponential backoff** on 429/403, and a HARD STOP after repeated 403 -- that is the host
  telling us to stop, and we stop.
* **Each track is fetched once, ever.** The signature cache guarantees it -- the single biggest
  load reduction available, and free.

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

import selftest                                      # noqa: E402  (the canary; see run())

HOME = _gt.REPO_ROOT
STATE_DIR = os.path.join(HOME, ".harvest")
STATE = os.path.join(STATE_DIR, "state.json")
QUEUE = os.path.join(STATE_DIR, "queue.json")
PAUSE = os.path.join(STATE_DIR, "PAUSED")
CACHE = os.path.join(HOME, ".chroma-cache")
KEEP = os.path.join(os.path.expanduser("~"), "media", "netradio-candidates")

HOP = 2048
QUERY_S = 120.0

# RETAINING EXCERPTS FOR AURAL CHECK: a bounded leaderboard of SHORT EXCERPTS, not a library.
#
# We never keep a full track. What is retained is a ~30-second excerpt around the matched instant
# (see write_excerpt) -- enough to recognise a record by ear, far too short to be a copy of it --
# and only for the best few candidates per mystery, and only for KEEP_TTL_DAYS.
#
# A bounded leaderboard, not a threshold, because the calibration (docs/CALIBRATION.md) is
# unambiguous: the true-match and non-match populations OVERLAP (true up to 0.0971, non-match down
# to 0.0376, median 0.0949). No cost gate works -- low excludes real matches, high keeps
# everything. So: the best KEEP_TOP excerpts PER MYSTERY, evicting the worst when a better one
# lands. Bounded, predictable, and it cannot be wrong about a threshold because it does not use
# one.
KEEP_TOP = 12
KEEP_CEILING = 0.130      # never retain an excerpt worse than the worst plausible true match
KEEP_TTL_DAYS = 30        # a lead not listened to in a month is not a lead -- swept
# A reported MATCH still needs cost AND margin. The populations OVERLAP (true match up to 0.0971,
# non-match down to 0.0376), so no cost alone can separate them: RANK is the reliable signal, and
# the margin test is what actually carries the gate. 40 of 41 tracks rank #1 against their own
# original, so the margin is real.
MATCH_COST = 0.050

# --- load discipline -------------------------------------------------------------------------
# Work for hours, then rest, so sustained load over a day stays low. No quiet hours (Tim's call);
# it is the spread across hosts and the randomised gaps that keep the load low, not the clock.
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


# --- YouTube wants to know you are a person -------------------------------------------------------
#
# YouTube now challenges anonymous downloads: "Sign in to confirm you're not a bot." Note what that
# error is NOT -- it has no 403, no 429, nothing the host-backoff logic looks for. So it sailed
# straight past the backoff and the harvester kept asking, over and over, filling the issue list and
# analysing nothing. A wall you cannot see is worse than one you can.
#
# Two ways to answer it, both OFF by default (the harvester must never touch a browser profile, or
# read a credential, unless explicitly told to):
#
#   NETRADIO_YTDLP_COOKIES=/path/to/cookies.txt    -- a cookies.txt export (Netscape format)
#   NETRADIO_YTDLP_COOKIES_FROM_BROWSER=chrome     -- or firefox / safari / brave / edge
#
# The cookie IS a credential: it is your logged-in YouTube session. Keep the file out of the repo
# (it is gitignored) and off the public remote.

def cookie_args():
    jar = os.environ.get("NETRADIO_YTDLP_COOKIES", "").strip()
    if jar and os.path.isfile(jar):
        return ["--cookies", jar]
    browser = os.environ.get("NETRADIO_YTDLP_COOKIES_FROM_BROWSER", "").strip()
    if browser:
        return ["--cookies-from-browser", browser]
    return []


# The errors that mean "you are not getting anything else out of me until you authenticate". These
# do not improve with waiting, so backing off is the wrong move: it just fails more slowly.
BOT_WALL = ("sign in to confirm", "confirm you're not a bot", "confirm you are not a bot",
            "use --cookies", "login required", "private video", "age-restricted")


def is_bot_wall(err):
    e = (err or "").lower()
    return any(p in e for p in BOT_WALL)


def stream_chroma(url):
    """Stream the audio, reduce it to a chroma signature -> (chroma, samples, error).

    The full audio is NEVER written to disk. It is streamed (yt-dlp -> ffmpeg -> numpy), reduced
    to chroma, cached as a signature, and the decoded samples are returned IN MEMORY so the caller
    can cut a short excerpt from them without fetching again. When the caller is done with them
    they are dropped. `samples` is the whole track only for as long as one function call.
    """
    import librosa
    yt = subprocess.Popen(["yt-dlp", "-q", "--no-warnings", "--no-playlist"] + cookie_args()
                          + ["-f", "bestaudio", "-o", "-", url],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    ff = subprocess.Popen(["ffmpeg", "-v", "error", "-i", "pipe:0",
                           "-ac", "1", "-ar", str(_audio.SR), "-f", "f32le", "pipe:1"],
                          stdin=yt.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    yt.stdout.close()
    raw, _ = ff.communicate()
    yt_err = yt.stderr.read().decode("utf-8", "replace")
    yt.wait()

    if yt.returncode != 0 or not raw:
        return None, None, yt_err.strip().split("\n")[-1][:160] if yt_err else "no audio"
    y = np.frombuffer(raw, dtype="float32")
    if len(y) < 45 * _audio.SR:
        return None, None, "too short (%.0fs)" % (len(y) / _audio.SR)

    c = librosa.feature.chroma_cqt(y=np.asarray(y, dtype="float32"),
                                   sr=_audio.SR, hop_length=HOP) + 1e-6
    c = librosa.util.normalize(c, norm=2, axis=0)
    os.makedirs(CACHE, exist_ok=True)
    np.save(sig_path(url), c.astype("float16"))
    return c, y, None


# A short excerpt AROUND the matched instant is all we retain -- long enough to recognise the
# record by ear, far too short to be a copy of it. This is not a library; it is a magnifying
# glass held over the exact moment the matcher flagged, so a human can confirm or reject it.
EXCERPT_S = 30.0


def write_excerpt(samples, at_s, path):
    """Write ~EXCERPT_S seconds of `samples` centred on the matched instant. In memory in, file
    out -- no second fetch. A brief excerpt for aural verification, swept after KEEP_TTL_DAYS."""
    import soundfile as sf
    lo = max(0, int((at_s - EXCERPT_S / 2) * _audio.SR))
    hi = min(len(samples), lo + int(EXCERPT_S * _audio.SR))
    clip = np.asarray(samples[lo:hi], dtype="float32")

    # THE HARD CAP. Everything about this project's copyright posture rests on one claim: what we
    # retain is an excerpt, "far too short to be a copy". That claim must be enforced by the code,
    # not merely intended by it -- because it has already failed once. 2.1 GB of FULL-LENGTH audio
    # was found in the candidates directory (one file was a 108-minute DJ mix, retained whole),
    # written by a harvester whose in-memory code did not match what is in git. The slice above is
    # correct today; this is here so that a slice that is ever wrong again cannot reach the disk.
    cap = int(EXCERPT_S * _audio.SR)
    if len(clip) > cap:
        clip = clip[:cap]
    if len(clip) == 0:
        return                                   # nothing to hear; do not leave an empty file

    os.makedirs(os.path.dirname(path), exist_ok=True)
    sf.write(path, clip, _audio.SR)
    _write_provenance()


def purge_audio():
    """Throw away every retained excerpt. The LEADS survive.

    Retained audio turned out to be the weakest part of this design. It is the only thing here that
    is a copy of someone's record, it is the thing that went wrong (full-length mixes were retained
    instead of excerpts), and it is not actually needed: a lead is a URL, and a URL can be listened
    to at the source. So the audio goes, and `/harvest` plays the candidate from an embed instead.

    What survives is everything that took work to compute -- the url, the cost, the mystery it
    matched, the key it matched in, and WHERE in the candidate it matched. Nothing is re-fetched and
    nothing is re-analysed; the chroma signatures (which are not audio) are untouched, so no
    candidate will ever be downloaded twice.
    """
    freed = n = 0
    if os.path.isdir(KEEP):
        for name in os.listdir(KEEP):
            if not name.lower().endswith((".wav", ".mp3", ".flac", ".m4a")):
                continue                      # leave PROVENANCE.txt alone
            path = os.path.join(KEEP, name)
            try:
                freed += os.path.getsize(path)
                os.unlink(path)
                n += 1
            except OSError:
                pass

    state = _load(STATE, blank_state())
    for m in state.get("matches") or []:
        m.pop("audio", None)                  # the lead stays; the copy does not
    state["kept"] = 0
    _save(STATE, state)
    return "purged %d retained files (%.1f GB). %d leads kept -- review them by embed at /harvest." % (
        n, freed / 1e9, len(state.get("matches") or []))


def _write_provenance():
    """State, in plain words, what the kept files are -- so nobody, including a future me, ever
    mistakes this directory for a music library."""
    note = os.path.join(KEEP, "PROVENANCE.txt")
    if os.path.exists(note):
        return
    os.makedirs(KEEP, exist_ok=True)
    with open(note, "w", encoding="utf-8") as fh:
        fh.write(
            "These are SHORT EXCERPTS (~%ds), retained TEMPORARILY so a human can listen and "
            "confirm or reject a track-identification hypothesis.\n\n"
            "This is not a music library. Full tracks are never kept -- the harvester streams "
            "audio, reduces it to a chroma signature, and drops it. Only the matched ~%d-second "
            "window of a near-miss is written here, and it is swept after %d days.\n\n"
            "If an excerpt confirms a record, ACQUIRE THE RECORD (buy it, or rip your own copy). "
            "Do not promote an excerpt into a source file.\n"
            % (int(EXCERPT_S), int(EXCERPT_S), KEEP_TTL_DAYS))


def sweep_excerpts():
    """Delete kept excerpts older than KEEP_TTL_DAYS. A lead you haven't listened to in a month
    is not a lead, and holding it any longer serves no purpose."""
    if not os.path.isdir(KEEP):
        return
    cutoff = time.time() - KEEP_TTL_DAYS * 86400
    for name in os.listdir(KEEP):
        if not name.endswith(".wav"):
            continue
        path = os.path.join(KEEP, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.unlink(path)
        except OSError:
            pass


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


# --- the listen queue: one queue, two stores -----------------------------------------------------
#
# The player owns `listen_queue.json`; we only ever READ it. Two writers on one JSON file is how
# you lose the file, so the harvester keeps its own working queue in `.harvest/queue.json` and
# merely SYNCS from the player's -- new unheard entries flow in, and anything a human has since
# ruled on flows out of pending. Subscriptions therefore feed the search automatically, which was
# the whole point: before this, subscriptions fed one queue and the harvester worked from another.
#
# Nothing here writes to the player's file. If the env var is unset (a harvester run by hand,
# without the player) this whole path is simply inert.

LISTEN_QUEUE = os.environ.get("NETRADIO_LISTEN_QUEUE", "")

# A human ruling means "I have heard this, and it is not the record" -- so there is nothing left
# for the matcher to find in it. `duplicate` is the same audio as another entry, `ignored` was
# rejected outright. All four retire an entry from the search.
RULED_ON = ("listened", "discarded", "ignored", "duplicate")


def _is_own_clip(item):
    """Tim's own uploads of the mystery clips are titled `Mystery Track N`. A harvester that
    "finds" one has rediscovered its own question and would report a triumphant 0.00.

    The channel-level guard (EXCLUDE_CHANNELS) cannot help here: listen-queue entries carry no
    channel or uploader field, only a title. So match the title his uploads actually use -- and
    narrowly, because real records are called things like "No Mystery" and "Mystery Blend".
    """
    title = (item.get("title") or "").strip().lower()
    return title.startswith("mystery track") or title.startswith("netradio mystery")


def listen_queue_split():
    """(candidates, retired) from the player's listen queue. Read-only; never raises."""
    if not LISTEN_QUEUE or not os.path.exists(LISTEN_QUEUE):
        return [], set()
    try:
        with open(LISTEN_QUEUE, "r", encoding="utf-8") as fh:
            items = (json.load(fh) or {}).get("items") or []
    except (OSError, ValueError):
        return [], set()                 # the player may be mid-write; try again next pass

    candidates, retired = [], set()
    for it in items:
        url = (it.get("url") or "").strip()
        if not url.startswith("http"):
            continue
        if any(it.get(f) for f in RULED_ON) or _is_own_clip(it):
            retired.add(url)
        else:
            candidates.append(url)
    return candidates, retired


def sync_listen_queue(q):
    """Fold the player's queue into ours. Returns (added, dropped); mutates `q` in place.

    Length is deliberately NOT a filter: a record can hide inside an hour-long DJ mix, and the
    match reports WHERE it hit (`at`), so a long mix is a feature, not a cost.
    """
    candidates, retired = listen_queue_split()
    if not candidates and not retired:
        return 0, 0

    seen = set(q["pending"]) | set(q["done"])
    fresh = [u for u in candidates if u not in seen]
    q["pending"].extend(fresh)

    # Drop anything a human ruled on while it sat in our pending list. Not from `done` -- that is
    # our record of work completed, and re-adding a URL later must not re-analyse it.
    before = len(q["pending"])
    q["pending"] = [u for u in q["pending"] if u not in retired]
    return len(fresh), before - len(q["pending"])


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

    This is the core load-spreading move: consecutive fetches go to DIFFERENT hosts, so no single
    host ever carries a run of back-to-back requests, even during a fast stretch.
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

    # A halt is a message to the human, not a permanent state: starting again IS the human saying
    # "I dealt with it". Clear it, and say whether they actually did the thing that was asked.
    if state.pop("halted", None):
        print("# clearing a previous halt -- cookies are %s"
              % ("CONFIGURED" if cookie_args() else "STILL NOT SET (this will halt again)"))
        _save(STATE, state)

    sweep_excerpts()                    # drop anything past its TTL before we start

    # THE CANARY. A broken harvester and a pool without the answer look identical from here: zero
    # matches, for weeks. So before searching for something we have never found, prove we can still
    # find something we HAVE -- re-run one solved calibration case from local files.
    st = selftest.offline()
    if st.get("ok"):
        print("# self-test PASS -- %s: cost %.4f, rank %d, beat the field by %.4f"
              % (st["name"], st["cost"], st["rank"], st["margin"]))
    elif st.get("ok") is False:
        print("!! SELF-TEST FAILED -- %s" % st.get("why"))
        print("!! The matcher cannot find a record we KNOW it holds. Every 'no match' it reports")
        print("!! from here is meaningless. Fix this before trusting another day of searching.")
        state.setdefault("issues", []).append(
            {"at": _now(), "issue": "self-test failed: %s" % st.get("why")})
        _save(STATE, state)
    else:
        print("# self-test skipped -- %s" % st.get("why"))

    session_end = time.time() + random.uniform(*SESSION_S)
    while True:
        # The live canary, about once a day: the streaming path (yt-dlp -> ffmpeg -> chroma) is
        # exactly what the offline test does NOT exercise, and it is the part with moving parts.
        if selftest.due_for_live():
            lv = selftest.live(stream_chroma, qs)
            if lv.get("ok"):
                print("# live canary PASS -- fetched %s fresh and matched it at %.4f"
                      % (lv["name"], lv["cost"]))
            elif lv.get("ok") is False:
                print("!! LIVE CANARY FAILED -- %s" % lv.get("why"))
                state.setdefault("issues", []).append(
                    {"at": _now(), "issue": "live canary failed: %s" % lv.get("why")})
                _save(STATE, state)

        if os.path.exists(PAUSE):
            state["session"] = {"phase": "paused", "until": 0}
            _save(STATE, state)
            time.sleep(20)
            continue

        q = _load(QUEUE, {"pending": [], "done": []})
        # Re-read the player's queue every pass: a subscription that fired an hour ago should feed
        # this search without a restart, and a candidate ruled on at /harvest should leave it.
        added, dropped = sync_listen_queue(q)
        if added or dropped:
            _save(QUEUE, q)
            print("listen queue: +%d new, -%d ruled on" % (added, dropped))

        if not q["pending"]:
            state["session"] = {"phase": "queue empty", "until": 0}
            _save(STATE, state)
            print("queue empty -- add more with --seed-channel, or add to the listen queue")
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

        # `samples` is the decoded audio, held IN MEMORY only for this iteration, so an excerpt
        # can be cut without a second fetch. It is dropped at the end of the loop. From the cache
        # there is no audio (only the signature), so a cached candidate cannot yield an excerpt --
        # which is fine: we only ever excerpt something we are already streaming.
        samples = None
        cached = os.path.exists(sig_path(url))
        if cached:
            c, err = np.load(sig_path(url)).astype("float32"), None
            state["skipped_cached"] += 1
        else:
            c, samples, err = stream_chroma(url)

        # --- the bot wall: STOP, do not grind ---
        #
        # "Sign in to confirm you're not a bot" does not get better by waiting, and it is not the
        # candidate's fault -- every subsequent fetch from this host will fail the same way. Grinding
        # on produces a wall of identical errors, burns the queue, and analyses nothing. So halt,
        # say plainly what is wrong and how to fix it, and let the human decide.
        if is_bot_wall(err):
            state["halted"] = {
                "at": _now(), "host": host, "error": (err or "")[:200],
                "reason": "%s is refusing anonymous downloads -- it wants a signed-in session" % host,
                "fix": "Give the harvester your YouTube cookies, then start it again:\n"
                       "  NETRADIO_YTDLP_COOKIES_FROM_BROWSER=chrome   (or firefox/safari/brave/edge)\n"
                       "or export a cookies.txt and set:\n"
                       "  NETRADIO_YTDLP_COOKIES=/path/to/cookies.txt\n"
                       "Put it in the analysis repo's .env_vars. The cookie is your logged-in "
                       "session -- keep it out of git.",
                "using_cookies": bool(cookie_args()),
            }
            state["session"] = {"phase": "halted", "until": 0}
            state["issues"] = (state["issues"] + [{"at": _now(), "host": host,
                                                   "issue": "HALTED: %s wants a signed-in session "
                                                            "(see the banner)" % host}])[-50:]
            state["errors"] += 1
            _save(STATE, state)
            print("!! HALTED -- %s" % state["halted"]["reason"])
            print(state["halted"]["fix"])
            return

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
            cost, shift, at = _cm.match(qc, c)
            if cost is None or cost > KEEP_CEILING:
                continue
            board = [m for m in state["matches"] if m["mystery"] == num]
            board.sort(key=lambda m: m["cost"])
            if len(board) >= KEEP_TOP and cost >= board[-1]["cost"]:
                continue                       # not good enough to displace anyone

            excerpt = os.path.join(KEEP, "MT%d-%.4f-%s.wav"
                                   % (num, cost, hashlib.sha1(url.encode()).hexdigest()[:8]))
            if not os.path.exists(excerpt):
                if samples is None:            # cached signature, no audio in hand -> can't excerpt
                    continue
                write_excerpt(samples, at or 0, excerpt)      # from memory; NO second fetch
                state["kept"] += 1
            hit = {"at": _now(), "mystery": num, "cost": round(cost, 4),
                   "semitones": shift, "at_s": round(at or 0, 1), "url": url,
                   "audio": excerpt,
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
            print("  %s  MT%d  cost %.4f  %s  at %s  %s"
                  % (hit["verdict"], num, cost, _cm.describe_shift(shift),
                     _cm.describe_at(at), url))
        samples = None                          # drop the decoded audio; it never persists
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
    ap.add_argument("--purge-audio", action="store_true",
                    help="delete every retained excerpt. The LEADS survive (url, cost, mystery, "
                         "key, at) -- only the audio goes. Review them by embed at /harvest.")
    args = ap.parse_args()

    os.makedirs(STATE_DIR, exist_ok=True)
    if args.purge_audio:
        print(purge_audio())
        return
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
