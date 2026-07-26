"""The harvester: fetch → signature → store. HALF of harvest.py, and only that half.

harvest.py does two jobs in one process: it FETCHES candidates (network-bound, politeness-bound)
and it SCORES them against the mysteries (local, CPU-bound). Splitting them means either half can
run, stop, or move without the other — the collector (collector.py) owns all scoring, all
excerpting, and ALL shared state; this file owns nothing but its own bookkeeping.

What this process writes — and, more importantly, what it does NOT:

  WRITES: .harvest/harvester_state.json   (its own hosts/session/progress bookkeeping)
          .harvest/jobs/<sigkey>/f1/      (one working dir per fetch: url.json, audio.flac,
                                           sig.npy, done — every file lands via .part + rename,
                                           so "final name exists" always means "complete")
          .harvest/results/<sigkey>.json  (a submission for the collector: append-only spool)
          the signature cache + store     (via harvest.stream_chroma / sigstore, as before)
  NEVER:  state.json, queue.json, scored, matches, excerpts. Those belong to the collector —
          one writer per file, or the file is eventually lost.

The queue is READ-ONLY here: the collector moves a URL from pending to done when it folds the
result. Until then a fetched URL's signature already exists in the store, so a restarted
harvester skips it by the fetched-once check rather than by queue position.

The audio is retained — as a WORKING COPY. harvest.py cuts excerpts in-memory at fetch time;
this process does not score, so it cannot know which ~30s matter. Instead the decoded audio
(mono, 16 kHz — the scoring format) is written into the job dir, the collector cuts anything it
needs when it scores, and the job dir is deleted at collection. Long-term we still keep only
signatures and short excerpts.

Politeness is IDENTICAL to harvest.py by construction: same constants, same jittered per-host
gaps, same strike/backoff/blocked ladder, same bot-wall hard stop — imported, not re-derived.

Enable with NETRADIO_HARVESTER=on. Never run this at the same time as `harvest.py --run`
(Mode A): they share the fetch role without sharing a lock. The player supervises one or the
other, not both.
"""

import argparse
import fcntl
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np                                   # noqa: E402
import soundfile as sf                               # noqa: E402

import harvest                                       # noqa: E402  (the constants + fetch core)
import sigstore                                      # noqa: E402
from harvest import (                                # noqa: E402
    BACKOFF_MAX_S, BACKOFF_START_S, BLOCK_AFTER, HOST_GAP_S, IDLE_S, SESSION_S,
    HOME, PAUSE, QUEUE, _load, _now, _save, _sig_key, host_of, is_bot_wall, sig_path,
)

STATE_DIR = os.path.join(HOME, ".harvest")
HSTATE = os.path.join(STATE_DIR, "harvester_state.json")
JOBS = os.path.join(STATE_DIR, "jobs")
RESULTS = os.path.join(STATE_DIR, "results")
LOCK = os.path.join(STATE_DIR, "harvester.lock")


def enabled():
    return os.environ.get("NETRADIO_HARVESTER", "").strip().lower() == "on"


def blank_hstate():
    return {"started": _now(), "updated": _now(), "analyzed": 0, "errors": 0,
            "hosts": {}, "session": {"phase": "idle", "until": 0}, "current": None,
            "issues": []}


def _atomic_write(path, data_bytes):
    """Every job-dir/spool file lands via .part + rename — final name means complete."""
    tmp = path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(data_bytes)
    os.replace(tmp, path)


def job_dir(url):
    return os.path.join(JOBS, _sig_key(url)[:-4], "f1")     # keyed like the cache, .npy stripped


def submit_result(url, ok, error=None):
    os.makedirs(RESULTS, exist_ok=True)
    rec = {"sigkey": _sig_key(url), "url": url, "ok": bool(ok), "at": _now()}
    if error:
        rec["error"] = str(error)[:200]
    _atomic_write(os.path.join(RESULTS, _sig_key(url) + ".json"),
                  json.dumps(rec, indent=1).encode())


def already_held(url):
    """Fetched-once, split-world edition: the cache or the store has the signature."""
    if os.path.exists(sig_path(url)):
        return True
    return sigstore.enabled() and sigstore.have_remote(_sig_key(url))


def _pick(q, hstate, done_set):
    """The first pending URL whose host is ready — harvest.py's selection, read-only."""
    now = time.time()
    best_wait = None
    for url in q.get("pending") or []:
        if url in done_set:
            continue
        host = host_of(url)
        hinfo = hstate["hosts"].setdefault(host, {})
        if hinfo.get("blocked"):
            continue
        wait = hinfo.get("next_ok", 0) - now
        if wait <= 0:
            return url, host, hinfo, 0
        best_wait = wait if best_wait is None else min(best_wait, wait)
    return None, None, None, best_wait


def work_once(hstate, q):
    """One fetch attempt. Public so tests can step the policy. Returns:
    'fetched' | 'skipped' | 'waiting' | 'idle' | 'halted'."""
    done_set = set(q.get("done") or [])
    url, host, hinfo, wait = _pick(q, hstate, done_set)
    if url is None:
        if wait is not None:
            hstate["session"] = {"phase": "waiting on hosts", "until": time.time() + wait}
            return "waiting"
        return "idle"

    if already_held(url):
        # The signature exists; only the collector may advance the queue, so just make sure a
        # result is on the spool (idempotent — one file per sigkey) and move on.
        submit_result(url, ok=True)
        return "skipped"

    hstate["current"] = url
    hstate["updated"] = _now()
    _save(HSTATE, hstate)

    c, samples, err = harvest.stream_chroma(url)     # caches + uploads the sig (sigstore)

    if is_bot_wall(err):
        hstate["halted"] = {"at": _now(), "host": host, "error": (err or "")[:200]}
        hstate["issues"] = (hstate["issues"] + [{"at": _now(), "host": host,
                                                 "issue": "HALTED: bot wall at %s" % host}])[-50:]
        hstate["errors"] += 1
        _save(HSTATE, hstate)
        return "halted"

    # Host pacing — harvest.py's ladder verbatim, on this process's own bookkeeping.
    gap = HOST_GAP_S.get(host, HOST_GAP_S["_default"]) * random.uniform(0.5, 2.0)
    if err and ("403" in err or "429" in err or "blocked" in err.lower()):
        hinfo["strikes"] = hinfo.get("strikes", 0) + 1
        back = min(BACKOFF_START_S * (2 ** (hinfo["strikes"] - 1)), BACKOFF_MAX_S)
        hinfo["next_ok"] = time.time() + back
        if hinfo["strikes"] >= BLOCK_AFTER:
            hinfo["blocked"] = True                  # it told us to go away. we listen.
            hstate["issues"].append({"at": _now(), "host": host,
                                     "issue": "blocked after %d refusals" % hinfo["strikes"]})
        hstate["errors"] += 1
        _save(HSTATE, hstate)
        return "waiting"
    hinfo["strikes"] = 0
    hinfo["next_ok"] = time.time() + gap

    if c is None:
        # A permanent per-URL failure (not a host refusal): tell the collector so it can
        # advance the queue and record the issue in the shared state.
        hstate["errors"] += 1
        _save(HSTATE, hstate)
        submit_result(url, ok=False, error=err or "no signature")
        return "fetched"

    jd = job_dir(url)
    os.makedirs(jd, exist_ok=True)
    _atomic_write(os.path.join(jd, "url.json"),
                  json.dumps({"url": url, "host": host, "at": _now()}, indent=1).encode())
    # The decoded audio, in the scoring format (mono float32 @ 16 kHz), FLAC-compressed.
    tmp = os.path.join(jd, "audio.flac.part")
    sf.write(tmp, np.asarray(samples, dtype="float32"), harvest._audio.SR, format="FLAC")
    os.replace(tmp, os.path.join(jd, "audio.flac"))
    with open(sig_path(url), "rb") as fh:
        _atomic_write(os.path.join(jd, "sig.npy"), fh.read())
    _atomic_write(os.path.join(jd, "done"), b"")
    submit_result(url, ok=True)

    hstate["analyzed"] += 1
    hstate["current"] = None
    hstate["updated"] = _now()
    _save(HSTATE, hstate)
    return "fetched"


def run():
    os.makedirs(STATE_DIR, exist_ok=True)
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another harvester holds %s -- refusing to double-fetch" % LOCK)
        return
    hstate = _load(HSTATE, blank_hstate())
    hstate["started"] = _now()
    session_end = time.time() + random.uniform(*SESSION_S)
    while True:
        if os.path.exists(PAUSE):
            hstate["session"] = {"phase": "paused", "until": 0}
            _save(HSTATE, hstate)
            time.sleep(20)
            continue
        if time.time() > session_end:
            rest = random.uniform(*IDLE_S)
            hstate["session"] = {"phase": "resting", "until": time.time() + rest}
            _save(HSTATE, hstate)
            time.sleep(rest)
            session_end = time.time() + random.uniform(*SESSION_S)
            continue
        hstate["session"] = {"phase": "working", "until": session_end}
        q = _load(QUEUE, {"pending": [], "done": []})
        outcome = work_once(hstate, q)
        _save(HSTATE, hstate)
        if outcome == "halted":
            print("!! HALTED -- bot wall; see harvester_state.json and harvest.py's banner advice")
            return
        if outcome in ("waiting", "idle"):
            time.sleep(30 if outcome == "waiting" else 120)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", action="store_true", help="fetch loop (runs for weeks)")
    ap.add_argument("--once", action="store_true", help="one work_once() pass (tests/cron)")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    if not enabled():
        print("harvester is dark -- set NETRADIO_HARVESTER=on (.env_vars) to enable the split "
              "harvester. Mode A (harvest.py --run) is unaffected.")
        return
    if args.status:
        h = _load(HSTATE, blank_hstate())
        print(json.dumps({k: h.get(k) for k in ("session", "current", "analyzed", "errors",
                                                "updated")}, indent=1))
        return
    if args.once:
        hstate = _load(HSTATE, blank_hstate())
        q = _load(QUEUE, {"pending": [], "done": []})
        print(work_once(hstate, q))
        _save(HSTATE, hstate)
        return
    if args.run:
        run()


if __name__ == "__main__":
    main()
