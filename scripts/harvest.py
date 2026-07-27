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
import urllib.error
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np                                   # noqa: E402

from streamalign import audio as _audio              # noqa: E402
from streamalign import chroma_match as _cm          # noqa: E402
from streamalign import groundtruth as _gt           # noqa: E402
from streamalign import mystery as _mystery          # noqa: E402

import chroma_recipe                                 # noqa: E402  (THE recipe, single source)
import selftest                                      # noqa: E402  (the canary; see run())
import sigstore                                      # noqa: E402  (bucket = the pool's only home)

HOME = _gt.REPO_ROOT
STATE_DIR = os.path.join(HOME, ".harvest")
STATE = os.path.join(STATE_DIR, "state.json")
QUEUE = os.path.join(STATE_DIR, "queue.json")
PAUSE = os.path.join(STATE_DIR, "PAUSED")
CACHE = os.path.join(HOME, ".chroma-cache")
KEEP = os.path.join(os.path.expanduser("~"), "media", "netradio-candidates")

HOP = chroma_recipe.HOP          # single source: chroma_recipe.py
QUERY_S = 120.0
# The shortest clip we will search WITH. MT7's is 23 seconds, and it produced five confident false
# positives all within 0.0007 of each other: a short query drives every cost down until the matcher
# can no longer tell records apart, and a degenerate ranking looks exactly like a real one. Better
# to search for nothing than to search for everything. A mystery below this floor leaves the query
# set and comes back by itself once a longer clip is cut.
MIN_QUERY_S = 60.0

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
# How many cached signatures to re-score per pass of the loop. Small on purpose: rescanning is CPU
# and fetching is network, so a modest chunk each pass rides along in the gaps (host backoff, the
# polite delay between fetches) instead of stalling the search. A full rescan of ~900 signatures
# against 3 mysteries is only ~3 minutes of CPU, so there is no hurry.
RESCAN_PER_PASS = 25
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
    if len(y) < chroma_recipe.MIN_SECONDS * _audio.SR:
        return None, None, "too short (%.0fs)" % (len(y) / _audio.SR)

    c = chroma_recipe.compute_chroma(y)          # THE recipe, in one place (chroma_recipe.py)
    os.makedirs(CACHE, exist_ok=True)
    np.save(sig_path(url), c.astype("float16"))
    # The bucket is the signature's long-term home (see sigstore). Upload now, verified; on
    # failure the local file simply stays -- eviction never fires for an unverified key, so a
    # flaky upload costs disk space, never data.
    if sigstore.enabled():
        sigstore.put(sig_path(url), _sig_key(url))
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


def _sig_key(url):
    """The signature's filename — a stable id for "this candidate's chroma", 20 chars not a URL."""
    return os.path.basename(sig_path(url))


_REMOTE_KEYS = {"at": 0.0, "keys": None}     # session cache of the bucket's key listing


def _remote_keys(max_age_s=900):
    """The bucket's signature keys, cached for a while -- None when the store is dark or the
    listing failed (callers must not treat that as 'empty')."""
    if not sigstore.enabled():
        return None
    now = time.time()
    if _REMOTE_KEYS["keys"] is not None and now - _REMOTE_KEYS["at"] < max_age_s:
        return _REMOTE_KEYS["keys"]
    keys = sigstore.list_keys()
    if keys is not None:
        _REMOTE_KEYS.update(at=now, keys=keys)
    return _REMOTE_KEYS["keys"]


def _load_sig(url):
    """A signature by hook or by crook: the working cache first, then the bucket. None if it
    exists in neither (i.e. this URL genuinely needs its audio fetched)."""
    path = sig_path(url)
    if not os.path.exists(path) and sigstore.enabled():
        if not sigstore.fetch(_sig_key(url), CACHE):
            return None
    try:
        return np.load(path).astype("float32")
    except (OSError, ValueError):
        return None


def unscored_pairs(state, q, retired, qs, limit=None):
    """Every (mystery, cached-signature) pair we have NOT scored yet.

    The harvester only ever walked `pending`. Once a URL reached `done` it was never looked at
    again -- so a mystery whose clip arrives LATER was scored only against candidates fetched after
    it. Every signature gathered before that point, which is the entire corpus built over weeks,
    was silently never tested against it. Tim assumed the opposite, reasonably.

    A chroma signature is not tied to the question you asked of it: the same 12xN matrix answers
    MT4 today and MT8 next month, for free and with no network. So the pairing is what we track --
    `state["scored"][mystery] = [signature keys]` -- and anything unpaired is work to do.

    Skips anything ruled on: a `not_a_match` is not a match for ANYTHING we are waiting for.
    """
    scored = state.setdefault("scored", {})
    out = []
    for num, qc, qkey in qs:
        seen = set(scored.get(qkey, []))
        for url in q.get("done") or []:
            if url in retired:
                continue
            key = _sig_key(url)
            if key in seen:
                continue
            # A signature counts as HELD if it is in the working cache OR the bucket -- eviction
            # (sigstore) moves cold ones out of the cache, and _load_sig pulls them back to score.
            if not os.path.exists(sig_path(url)):
                remote = _remote_keys()
                if remote is None or key not in remote:
                    continue
            out.append((num, qc, qkey, url, key))
            if limit and len(out) >= limit:
                return out
    return out


def score_cached(state, num, qc, qkey, url, key):
    """Score one cached signature against one mystery. No network, ~0.06s. Returns a hit or None.

    Updates an existing row rather than duplicating it -- which is also how the missing `at_s`
    gets filled in. Every match saved before the harvester recorded WHERE it hit has `at_s: None`,
    so `/harvest` could not cue the link and Tim had to hunt through a 108-minute mix by hand.
    The position was never lost: it is recomputable from the signature we already hold.
    """
    c = _load_sig(url)
    if c is None:
        return None
    cost, shift, at = _cm.match(qc, c)
    state.setdefault("scored", {}).setdefault(qkey, []).append(key)

    if cost is None or cost > KEEP_CEILING:
        return None
    verdict = "MATCH" if cost <= MATCH_COST else "near"
    for m in state["matches"]:
        if m.get("url") == url and m.get("mystery") == num:
            m.update(cost=round(float(cost), 4), semitones=shift,
                     at_s=round(float(at or 0), 1), verdict=verdict)
            return m
    hit = {"at": _now(), "mystery": num, "cost": round(float(cost), 4),
           "semitones": shift, "at_s": round(float(at or 0), 1), "url": url,
           "verdict": verdict}
    state["matches"].append(hit)
    evict_overfull(state, num)
    return hit


def forget(state, num):
    """Drop everything we have learned about one mystery: its leads, and its scored pairings.

    For when the QUESTION was bad, not the answers. MT7's clip is 23 seconds; every lead it
    produced is suspect, and leaving them on the board invites a human to rule on evidence gathered
    with a broken instrument. Clearing the scored pairings means the next clip starts from a clean
    slate against the whole corpus.

    (Re-cutting the clip alone would also force a full re-score -- `scored` is keyed on the clip's
    fingerprint -- but the stale LEADS would survive, and they are the part that misleads you.)
    """
    before = len(state.get("matches") or [])
    state["matches"] = [m for m in (state.get("matches") or []) if m.get("mystery") != num]
    scored = state.setdefault("scored", {})
    dropped_keys = [k for k in scored if k.split(":", 1)[0] == str(num)]
    for k in dropped_keys:
        del scored[k]
    return before - len(state["matches"]), len(dropped_keys)


def rescan(state, q, retired, qs, limit=None, verbose=True):
    """Work through the unscored pairs. Returns how many were scored."""
    pairs = unscored_pairs(state, q, retired, qs, limit=limit)
    for num, qc, qkey, url, key in pairs:
        hit = score_cached(state, num, qc, qkey, url, key)
        if hit and verbose:
            a = int(hit.get("at_s") or 0)
            print("  %s  MT%d  cost %.4f  at %d:%02d  %s  (from cache -- no fetch)"
                  % (hit["verdict"], num, hit["cost"], a // 60, a % 60, url))
    return len(pairs)


def evict_overfull(state, num):
    """Trim mystery `num`'s board back to KEEP_TOP, keeping the best (lowest cost).

    A row's "audio" key is OPTIONAL, and its absence is the normal case: `purge_audio()` above pops
    it from every match, and a lead is a URL -- not a copy of a record. So an evicted row with no
    file is evicted quietly, and only a row that still points at one decrements `kept`.

    This must never raise. It runs on the hot path of every hit, and it used to do
    `os.unlink(dead["audio"])` catching only OSError -- so once the audio was purged, the first
    match in any full board raised KeyError and killed the harvester. Every board was already at
    KEEP_TOP, so that was every start, forever, and the watchdog restarted it into the same wall.
    """
    board = sorted([m for m in state["matches"] if m["mystery"] == num],
                   key=lambda m: m["cost"])
    for dead in board[KEEP_TOP:]:
        state["matches"].remove(dead)
        path = dead.get("audio")
        if not path:
            continue
        try:
            os.unlink(path)
            state["kept"] -= 1
        except OSError:
            pass


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


def drop_ruled_excerpts(state, retired):
    """A ruled-on lead loses its audio; the numbers stay. Returns how many were dropped.

    The excerpt exists for exactly one purpose: to let a human confirm or reject the lead by ear.
    Once the ruling is made -- match, not-a-match, heard, any of RULED_ON -- that purpose is spent,
    and holding the audio a day longer serves nothing. The lead itself survives whole (url, cost,
    mystery, key, at_s, verdict): the SCORE is the record; the audio was only ever the evidence.

    Runs on every pass, right after the listen queue is re-read, so a ruling made at /harvest
    takes effect within one loop iteration. The TTL sweep above remains the backstop for anything
    ruled while the harvester was off.
    """
    dropped = 0
    for m in state.get("matches") or []:
        path = m.pop("audio", None) if m.get("url") in retired else None
        if not path:
            continue
        try:
            os.unlink(path)
        except OSError:
            pass                       # already gone -- the row still stops carrying it
        state["kept"] = max(0, state.get("kept", 0) - 1)
        dropped += 1
    return dropped


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

# The player, which is the SINGLE WRITER of the listen queue.
PLAYER_URL = os.environ.get("NETRADIO_PLAYER_URL", "http://127.0.0.1:8765")


def add_to_queue(urls, source):
    """Queue candidates by handing them to the PLAYER, not by writing our own queue.

    This used to append straight into `.harvest/queue.json`, and that was the bug. It gave the
    harvester a second, private door that the listen queue knew nothing about: 400 candidates got
    in that way, invisible at /queue, untagged, and impossible to remove when a channel turned out
    to be feeding us noise. Tim found it by adding a video by hand and getting no duplicate warning
    for a record we had already analysed.

    So there is now ONE door. Everything enters through the listen queue, tagged with where it came
    from, and `sync_listen_queue()` folds it back into our working queue on the next pass. We only
    ever READ that file -- the player owns it, and two writers on one JSON file is how you lose the
    file -- so we ask the player over HTTP and let it do the write.

    A dead player is a hard failure, not a silent fallback to the private queue: falling back is
    precisely how the candidates went dark in the first place.
    """
    if any(c in (source or "") for c in EXCLUDE_CHANNELS):
        print("refusing to queue %s -- it publishes the mystery clips themselves" % source)
        return 0

    origin = "seed-channel:%s" % (source or "unknown")
    added = 0
    for url in dict.fromkeys(urls):
        body = json.dumps({"url": url, "origin": origin}).encode()
        req = urllib.request.Request(PLAYER_URL.rstrip("/") + "/api/queue/add", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                for res in (json.loads(r.read()) or {}).get("results") or []:
                    if res.get("status") == "new":
                        added += 1
                    elif res.get("status") == "refused":
                        print("  refused: %s -- %s" % (url, res.get("why")))
        except urllib.error.URLError as e:
            raise SystemExit(
                "cannot reach the player at %s (%s).\n"
                "Candidates are queued THROUGH the player now -- it owns the listen queue and is\n"
                "its only writer. Start it (scripts/run_player.sh start), or set NETRADIO_PLAYER_URL."
                % (PLAYER_URL, e))
    return added


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
#
# NETRADIO_LISTEN_QUEUE may name any of three layouts -- we read all of them, still read-only:
#   * the legacy single `listen_queue.json` (a {"items": [...]} object), or a rendered merged
#     single-file view of the same shape;
#   * the player's sharded DIRECTORY (the queue the player migrated to), or that directory's
#     `index.json` manifest named directly -- a manifest listing `shard-NNNN.json` files, each a
#     bare JSON array of items, which we read in manifest order and concatenate.
# We tell them apart by inspecting the path (a dir, or a basename of `index.json`), not a new env
# var -- the player owns the layout, and the harvester should follow it wherever it goes.

LISTEN_QUEUE = os.environ.get("NETRADIO_LISTEN_QUEUE", "")

# A human ruling retires an entry from the search. `duplicate` is the same audio as another entry;
# `ignored` was rejected outright.
#
# `not_a_match` is the important one, and it is DELIBERATELY GLOBAL: it means "this record is not
# any Mystery Track" -- including the mysteries whose clips do not exist yet. That is what makes
# `rescan()` below safe. Without it, the day MT8's clip lands, every record Tim has already
# listened to and rejected would be scored again and handed straight back to him.
#
# Note `not_a_match` does NOT imply `listened`: you can rule a record out as a match and still want
# to hear it. The player keeps those two verdicts apart (see listen_queue_store.mark_not_a_match).
RULED_ON = ("listened", "discarded", "ignored", "duplicate", "not_a_match")


# Tim's own channel, as it appears in a listen-queue entry's `origin`.
OWN_ORIGINS = ("tim hunter", "trjh", "UCuYTatE2k5dOV8J8Bi3rK0g")


def _is_own_clip(item):
    """Ours? Then never analyse it: we would rediscover our own question and report ~0.00.

    This used to match ONLY the title `Mystery Track N`, and justified that by claiming
    "listen-queue entries carry no channel or uploader field, only a title." That was false --
    they carry `origin`, which names the subscription that produced them -- and the cost of the
    mistake was real: NINE of his uploads sat in the pending queue, uncaught, because they are
    titled "ID #1", "ID #2" and "Wave Forms [in the mix, low quality]". Not one of them contains
    the word "mystery". The last is an excerpt of the mix itself.

    So check WHO uploaded it first, and keep the title check only as a second net -- narrowly,
    because real records are called things like "No Mystery" and "Mystery Blend".
    """
    origin = (item.get("origin") or "").strip().lower()
    if any(o.lower() in origin for o in OWN_ORIGINS):
        return True
    title = (item.get("title") or "").strip().lower()
    return title.startswith("mystery track") or title.startswith("netradio mystery")


def _load_queue_items():
    """The listen queue's flat items list, whatever layout NETRADIO_LISTEN_QUEUE points at.

    Single file -> read its `items`. Sharded (a directory, or an `index.json` manifest named
    directly) -> read the manifest and concatenate the items of each `shard-NNNN.json` it names,
    in manifest order. A shard is a bare JSON array (the object form is tolerated in case the
    layout ever wraps them). Raises OSError/ValueError on a missing/torn/mid-write file -- the
    caller turns that into "empty, try again next pass".
    """
    if os.path.isdir(LISTEN_QUEUE):
        manifest_path = os.path.join(LISTEN_QUEUE, "index.json")
    elif os.path.basename(LISTEN_QUEUE) == "index.json":
        manifest_path = LISTEN_QUEUE
    else:
        manifest_path = None             # a plain file -> the legacy single-file layout

    if manifest_path is None:
        with open(LISTEN_QUEUE, "r", encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("items") or []

    shard_dir = os.path.dirname(manifest_path)
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh) or {}
    items = []
    for entry in manifest.get("shards") or []:
        name = (entry or {}).get("name")
        if not name:
            continue
        with open(os.path.join(shard_dir, name), "r", encoding="utf-8") as fh:
            chunk = json.load(fh)
        if isinstance(chunk, dict):      # tolerate a {"items": [...]} shard wrapper
            chunk = chunk.get("items") or []
        items.extend(chunk or [])
    return items


def _is_cooling(item):
    """True while the item's `retry_after` (ISO `YYYY-MM-DD`) is still in the FUTURE.

    Mirrors the player's rule: a URL a recent fetch failed on is held back from the network until
    its date passes, and NOTHING else changes. Lexical compare works because ISO dates sort as
    text; a non-string/absent value is not cooling.
    """
    ra = item.get("retry_after")
    if not isinstance(ra, str):
        return False
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return ra > today


def listen_queue_split():
    """(candidates, retired) from the player's listen queue. Read-only; never raises.

    Reads whichever layout NETRADIO_LISTEN_QUEUE names (single file, merged view, or sharded
    dir/manifest -- see _load_queue_items).
    """
    if not LISTEN_QUEUE or not os.path.exists(LISTEN_QUEUE):
        return [], set()
    try:
        items = _load_queue_items()
    except (OSError, ValueError):
        return [], set()                 # the player may be mid-write; try again next pass

    candidates, retired = [], set()
    for it in items:
        url = (it.get("url") or "").strip()
        if not url.startswith("http"):
            continue
        if any(it.get(f) for f in RULED_ON) or _is_own_clip(it):
            retired.add(url)             # a ruling wins over cooling: retirement is permanent-ish
        elif _is_cooling(it):
            continue                     # cooling gates the network only -- hold the URL back, but
                                         # do NOT retire it: it rejoins on its own once the date
                                         # passes, so nothing here drops it from pending.
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

def clip_fingerprint(path):
    """A short hash of the clip's CONTENTS. Changes the moment the clip is re-cut."""
    h = hashlib.sha1()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return "missing"
    return h.hexdigest()[:12]


def queries(state=None):
    """The unsolved mysteries, as chroma. From track-metadata.json -- never from filenames.

    Returns (mystery_number, chroma, query_key) triples. The QUERY KEY is the mystery number plus a
    fingerprint of the clip's contents, and it is what `state["scored"]` is keyed on -- so a clip
    that is RE-CUT invalidates every pairing made against the old one, and every cached signature is
    scored again automatically. Keying on the number alone would have meant a better MT7 clip
    inherited the old clip's verdicts and was never actually asked: exactly the false negatives Tim
    was worried about.

    A clip shorter than MIN_QUERY_S is REFUSED, and this is why: MT7's clip is 23 seconds, and it
    produced five confident false positives all within 0.0007 of each other. A short query drives
    every cost down until the matcher cannot tell anything apart -- the ranking goes degenerate, and
    a degenerate match looks exactly like a real one. Better to search for nothing than to search
    for everything. The mystery comes back into the query set by itself once a longer clip is cut.
    """
    import librosa
    out, skipped = [], []
    for e in _mystery.searchable():
        secs = _audio.duration(e["clip"]) if hasattr(_audio, "duration") else None
        if secs is None:
            y_full = _audio.load_audio(e["clip"])
            secs = len(y_full) / float(_audio.SR)
        if secs < MIN_QUERY_S:
            skipped.append({"mystery": e["number"], "clip_s": round(secs),
                            "why": "the clip is %ds, below the %ds floor -- a query that short "
                                   "drives every cost down and the matcher stops being able to "
                                   "tell records apart (MT7's 23s clip produced five 'confident' "
                                   "false positives within 0.0007 of each other). Cut a longer one "
                                   "and it re-enters the search by itself."
                                   % (round(secs), MIN_QUERY_S)})
            continue
        y = _audio.load_audio(e["clip"])[:int(QUERY_S * _audio.SR)]
        c = chroma_recipe.compute_chroma(y)          # same recipe as the candidates it scores
        qkey = "%d:%s" % (e["number"], clip_fingerprint(e["clip"]))
        out.append((e["number"], c, qkey))

    if state is not None:                     # so /harvest can say what it is NOT asking, and why
        state["searching"] = [n for n, _, _ in out]
        state["skipped_queries"] = skipped
    for s in skipped:
        print("# NOT searching MT%d -- %s" % (s["mystery"], s["why"]))
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
    qs = queries(state)
    if not qs:
        print("no unsolved mysteries with a usable clip -- nothing to search for")
        return
    print("# searching for Mystery Tracks %s" % ", ".join(str(n) for n, _, _ in qs))
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
            # Pass (number, chroma) PAIRS -- selftest's contract. `qs` carries a third field (the
            # query key, which fingerprints the clip) that is ours alone, and leaking it across the
            # boundary is what broke this: selftest unpacked two and got three.
            lv = selftest.live(stream_chroma, [(n, qc) for n, qc, _ in qs])
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

        # Score cached signatures against any mystery they have not met yet -- a bounded chunk per
        # pass, so it rides along with the fetching instead of blocking it. This is CPU only
        # (~0.06s each, no network), and it is what makes a NEW mystery see the WHOLE corpus: the
        # day MT8's clip lands, all ~900 signatures already on disk get scored against it, without
        # re-downloading a single track. Positions (`at_s`) on old rows get filled in on the way.
        _, retired = listen_queue_split()
        # A ruling spends the excerpt: the audio existed to let the human make the call, and the
        # call has been made. Drop it now, not at the 30-day sweep.
        n_dropped = drop_ruled_excerpts(state, retired)
        if n_dropped:
            _save(STATE, state)
            print("dropped %d ruled-on excerpt(s) -- the leads keep their numbers" % n_dropped)
        todo = len(unscored_pairs(state, q, retired, qs))
        if todo:
            state["rescan_pending"] = todo
            n = rescan(state, q, retired, qs, limit=RESCAN_PER_PASS)
            state["rescan_pending"] = max(0, todo - n)
            _save(STATE, state)
        elif sigstore.enabled():
            # Rescan backlog empty = every cached signature is scored vs every current mystery,
            # which is exactly when cold ones may leave the disk (verified-remote only).
            n_ev, freed = sigstore.evict_cold(CACHE, state.get("scored") or {},
                                              [qk for _, _, qk in qs])
            if n_ev:
                print("evicted %d cold signature(s) to the bucket (%.1f MB freed)"
                      % (n_ev, freed / 1e6))

        if not q["pending"]:
            # Nothing to fetch -- but a rescan backlog is still real work, so do it flat out rather
            # than declaring the queue empty and going home.
            if todo:
                state["session"] = {"phase": "rescanning cached signatures", "until": 0}
                _save(STATE, state)
                continue
            state["session"] = {"phase": "queue empty", "until": 0}
            _save(STATE, state)
            print("queue empty -- add more at /queue, or subscribe to a channel")
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
        c = _load_sig(url)                 # working cache, else the bucket -- no audio either way
        cached = c is not None
        if cached:
            err = None
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
        for num, qc, _qkey in qs:
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

            evict_overfull(state, num)
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
    ap.add_argument("--forget", type=int, metavar="N",
                    help="drop every lead for Mystery Track N, and every scored pairing against it. "
                         "For when the QUESTION was bad -- a clip too short to distinguish records "
                         "with. The next clip then starts clean against the whole corpus.")
    ap.add_argument("--migrate-sigs", action="store_true",
                    help="move the signature archive fully into the bucket: upload+verify every "
                         "local signature, then evict the cold ones (verified remote AND scored "
                         "against every current mystery). Run with the harvester STOPPED or "
                         "paused. After this, local disk holds only work in progress.")
    ap.add_argument("--rescan", action="store_true",
                    help="score every cached signature against every mystery it has not met yet, "
                         "in one go. No network. The running harvester does this by itself, a "
                         "chunk at a time -- this is for when you want it finished NOW (e.g. you "
                         "have just added a Mystery Track clip).")
    args = ap.parse_args()

    os.makedirs(STATE_DIR, exist_ok=True)
    if args.purge_audio:
        print(purge_audio())
        return
    if args.forget:
        state = _load(STATE, blank_state())
        leads, pairs = forget(state, args.forget)
        _save(STATE, state)
        print("# forgot MT%d: dropped %d lead(s) and %d scored pairing(s)."
              % (args.forget, leads, pairs))
        print("# Cut a better clip and it re-enters the search by itself, against every cached "
              "signature.")
        return
    if args.migrate_sigs:
        if not sigstore.enabled():
            print("# sigstore is dark -- set NETRADIO_SIG_BUCKET (and profile/endpoint) in "
                  ".env_vars first.")
            return
        state = _load(STATE, blank_state())
        qs = queries()
        names = sorted(n for n in os.listdir(CACHE)
                       if n.startswith("u") and n.endswith(".npy")) if os.path.isdir(CACHE) else []
        up = failed = 0
        for name in names:
            path = os.path.join(CACHE, name)
            try:
                local = os.path.getsize(path)
            except OSError:
                continue
            if sigstore.remote_size(name) == local:
                continue                        # already there, verified
            if sigstore.put(path, name):
                up += 1
            else:
                failed += 1
        n_ev, freed = sigstore.evict_cold(CACHE, state.get("scored") or {},
                                          [qk for _, _, qk in qs])
        left = len(names) - n_ev
        print("# migrate: %d uploaded, %d upload failure(s); %d evicted (%.1f MB freed); "
              "%d signature(s) still local (unscored vs a current mystery, or unverified)."
              % (up, failed, n_ev, freed / 1e6, left))
        if failed:
            print("# NOTHING that failed to upload was deleted. Fix the store config and re-run.")
        return
    if args.rescan:
        state = _load(STATE, blank_state())
        q = _load(QUEUE, {"pending": [], "done": []})
        qs = queries()
        _, retired = listen_queue_split()
        todo = len(unscored_pairs(state, q, retired, qs))
        print("# rescanning %d (signature, mystery) pair(s) against MT%s -- no network, ~%.0f min"
              % (todo, "/MT".join(str(n) for n, _, _ in qs), todo * 0.06 / 60))
        n = rescan(state, q, retired, qs)
        state["rescan_pending"] = 0
        _save(STATE, state)
        print("# scored %d. Every cached signature has now met every mystery." % n)
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
