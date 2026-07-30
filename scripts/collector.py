"""The harvest-collector: THE one writer of the shared harvest state.

The other half of the harvest.py split (see harvester.py). The harvester fetches and submits;
this process folds. Everything that used to make state.json dangerous to share — scored
pairings, the match boards, excerpts, queue advancement — happens HERE and only here:

  * drain `.harvest/results/` (the spool the harvester — or any future submitter — appends to),
  * score each new signature against every current mystery (from the cache, else the store),
  * cut the ~30 s excerpt for a near-miss FROM THE RETAINED JOB AUDIO (mono 16 kHz FLAC —
    the same samples harvest.py would have held in memory),
  * move the URL from pending to done in queue.json,
  * delete the folded result and, once nothing more is wanted from it, the job dir,
  * work the rescan backlog a chunk at a time, and — when it is empty — let sigstore evict
    cold signatures (the step-1c rule: verified remote AND fully scored).

Scoring and match/board semantics are harvest.py's own functions and constants, imported —
the split moves the work, it does not re-derive it.

Enable with NETRADIO_COLLECTOR=on. Never run at the same time as `harvest.py --run` (Mode A):
both write state.json and queue.json. One writer, or the file is eventually lost — and this is
now ENFORCED, not just documented: every writer (this run(), Mode A's run(), and the on-demand
--requeue-missing-sigs) takes the same flock (harvest.WRITER_LOCK) for its lifetime, so
whichever starts second refuses loudly.
"""

import argparse
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np                                   # noqa: E402
import soundfile as sf                               # noqa: E402

import harvest                                       # noqa: E402
import sigstore                                      # noqa: E402
from harvest import (                                # noqa: E402
    KEEP, KEEP_CEILING, KEEP_TOP, MATCH_COST, QUEUE, RESCAN_PER_PASS, STATE,
    _load, _now, _save, blank_state, evict_overfull, listen_queue_split, queries,
    write_excerpt,
)
from harvest import _cm                              # noqa: E402  (the matcher)
from harvester import JOBS, RESULTS, STATE_DIR       # noqa: E402  (the shared layout)

LOCK = harvest.WRITER_LOCK           # THE queue/state writer lock, shared with harvest.py
                                     # (historic path name collector.lock; see harvest.py)
JOB_TTL_S = 7 * 24 * 3600          # orphaned job dirs are swept after a week
WATCH_IDLE_S = float(os.environ.get("NETRADIO_WATCH_IDLE_S", "900"))   # watcher hold freshness


def _watched_worker():
    """The worker under the watcher's hold, iff `<jobs>/_watching.json` is FRESH (touched
    within WATCH_IDLE_S). The watcher plan's rotation rule (player repo,
    PLAN_edge_chroma_watcher.md): while a fresh watching file names a worker, that worker's
    jobs keep their WHOLE evidence set — retained audio, attempt sig, grant record — so the
    watcher can verify them; a stale or absent file self-heals to normal cleanup (watcher
    died or rotated). JOB_TTL_S stays as the max-hold backstop. Path resolved at call time
    because tests re-point JOBS."""
    path = os.path.join(JOBS, "_watching.json")
    try:
        if time.time() - os.path.getmtime(path) > WATCH_IDLE_S:
            return None
        with open(path) as fh:
            return (json.load(fh) or {}).get("worker") or None
    except (OSError, ValueError):
        return None


def enabled():
    return os.environ.get("NETRADIO_COLLECTOR", "").strip().lower() == "on"


def _job_audio(sigkey):
    path = os.path.join(JOBS, sigkey[:-4], "f1", "audio.flac")
    if not os.path.exists(path):
        return None
    samples, sr = sf.read(path, dtype="float32")
    return samples if sr == harvest._audio.SR else None


def _score_new(state, url, c, samples, qs):
    """harvest.py's fetch-time scoring block, verbatim in behaviour: board displacement,
    excerpt from the samples in hand, match rows, per-mystery eviction."""
    state["analyzed"] += 1
    key = harvest._sig_key(url)
    for num, qc, qkey in qs:
        cost, shift, at = _cm.match(qc, c)
        state.setdefault("scored", {}).setdefault(qkey, []).append(key)
        if cost is None or cost > KEEP_CEILING:
            continue
        board = [m for m in state["matches"] if m["mystery"] == num]
        board.sort(key=lambda m: m["cost"])
        if len(board) >= KEEP_TOP and cost >= board[-1]["cost"]:
            continue                       # not good enough to displace anyone
        excerpt = os.path.join(KEEP, "MT%d-%.4f-%s.wav"
                               % (num, cost, hashlib.sha1(url.encode()).hexdigest()[:8]))
        if not os.path.exists(excerpt):
            if samples is None:            # no retained audio -> a lead without a clip
                continue
            write_excerpt(samples, at or 0, excerpt)
            state["kept"] += 1
        hit = {"at": _now(), "mystery": num, "cost": round(cost, 4),
               "semitones": shift, "at_s": round(at or 0, 1), "url": url,
               "audio": excerpt,
               "verdict": "MATCH" if cost <= MATCH_COST else "near"}
        state["matches"].append(hit)
        evict_overfull(state, num)
        print("  %s  MT%d  cost %.4f  %s" % (hit["verdict"], num, cost, url))


def collect_once(state, q, qs):
    """Fold every result currently on the spool. Public so tests can step it.
    Returns how many results were folded."""
    try:
        names = sorted(os.listdir(RESULTS))
    except OSError:
        return 0
    folded = 0
    for name in names:
        if not name.endswith(".json"):
            continue
        rpath = os.path.join(RESULTS, name)
        try:
            with open(rpath) as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        url, sigkey = rec.get("url"), rec.get("sigkey")
        if not url or not sigkey:
            os.unlink(rpath)
            continue

        # Idempotent replay: scoring and the queue live in DIFFERENT durable files, so the
        # fold marker lives in state.json ITSELF, written in the same atomic save as the
        # scores. Marker present (or URL already done) => this record was scored-and-persisted;
        # a crash merely interrupted the queue save or the cleanup. Finish those, never
        # re-score — that would double analyzed/scored/match rows.
        if sigkey in state.get("folded", {}) or url in (q.get("done") or []):
            if url in (q.get("pending") or []):
                q["pending"].remove(url)
                if url not in (q.get("done") or []):
                    q.setdefault("done", []).append(url)
                _save(QUEUE, q)
            if rec.get("worker") and rec["worker"] == _watched_worker():
                continue          # watched: hold the evidence set; a later pass cleans up
            _cleanup(rpath, sigkey)
            folded += 1
            continue

        if rec.get("ok"):
            c = harvest._load_sig(url)
            if c is None:
                # Submitted but the signature is nowhere (upload failed AND cache lost). Leave
                # the result for a later pass rather than silently declaring the URL done.
                continue
            _score_new(state, url, c, _job_audio(sigkey), qs)
        else:
            state["errors"] += 1
            state["issues"] = (state.get("issues", []) + [{"at": _now(), "url": url,
                                                           "issue": rec.get("error")}])[-50:]

        if url in (q.get("pending") or []):
            q["pending"].remove(url)
        q.setdefault("done", []).append(url)

        # DURABILITY BEFORE CLEANUP, exactly-once across TWO files: the fold marker rides in
        # the SAME atomic state write as the scores, so a crash between the state save and the
        # queue save replays as marker-present => queue-reconcile + cleanup only. A crash
        # before the state save leaves no marker and all recovery inputs (audio included)
        # intact. Cleanup is strictly last. A hit and its excerpt can never be lost, and can
        # never be doubled, at any boundary.
        state.setdefault("folded", {})[sigkey] = _now()
        state["updated"] = _now()
        _save(STATE, state)
        _save(QUEUE, q)
        # The watcher's hold, checked strictly AFTER durability: scoring + queue are saved, so
        # deferring cleanup costs nothing — the record replays as marker-present until the
        # watch ends, then the evidence set (audio + attempt sig + grants) retires as one.
        if rec.get("worker") and rec["worker"] == _watched_worker():
            folded += 1
            continue
        _cleanup(rpath, sigkey)
        folded += 1

    retry_prunes()          # grant retirements owed from earlier passes/outages

    # Prune fold markers whose spool record is gone — the record can never replay again.
    stale = [k for k in state.get("folded", {})
             if not os.path.exists(os.path.join(RESULTS, k + ".json"))]
    if stale:
        for k in stale:
            del state["folded"][k]
        _save(STATE, state)
    return folded


def _fleet():
    """(controller, fleet_token, collector_token) when ALL are configured, else None."""
    base = os.environ.get("NETRADIO_CONTROLLER_URL", "").rstrip("/")
    fleet = os.environ.get("NETRADIO_FLEET_TOKEN", "")
    coll = os.environ.get("NETRADIO_COLLECTOR_TOKEN", "")
    return (base, fleet, coll) if (base and fleet and coll) else None


def _prunes_path():
    return os.path.join(os.path.dirname(STATE), "pending_prunes.json")


def _pending_prunes():
    try:
        with open(_prunes_path()) as fh:
            return list(json.load(fh))
    except (OSError, ValueError):
        return []


def _prune_grants(sigkey):
    """One prune attempt against the controller. True = nothing further owed (acknowledged,
    or the fleet is dark so no grant record exists); False = owed and must retry — the
    caller keeps the sigkey in pending_prunes.json. Never raises: a controller outage must
    not kill the fold loop."""
    cfg = _fleet()
    if not cfg:
        return True
    base, fleet, coll = cfg
    import urllib.request
    req = urllib.request.Request(base + "/lease/prune-grants",
                                 data=json.dumps({"sigkey": sigkey}).encode(),
                                 headers={"content-type": "application/json",
                                          "x-fleet-token": fleet,
                                          "x-collector-token": coll})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
        return True
    except Exception:
        return False


def retry_prunes():
    """Drain pending_prunes.json — the durable retry key for grant retirement (the spool
    record is gone by then, so THIS file is what makes a missed prune re-prunable). Called
    every fold pass; returns how many were acknowledged."""
    pend = _pending_prunes()
    if not pend or not _fleet():
        return 0
    left = [k for k in pend if not _prune_grants(k)]
    if left != pend:
        _save(_prunes_path(), left)
    return len(pend) - len(left)


def _cleanup(rpath, sigkey):
    """Best-effort: a cleanup failure must not kill the loop — the done[] replay key makes
    the next pass finish it. Grant retirement is durably owed BEFORE the evidence is
    destroyed: sigkey lands in pending_prunes.json first, so a crash or controller outage
    anywhere below still retires the grants on a later pass (retry_prunes)."""
    import shutil
    if _fleet():
        pend = _pending_prunes()
        if sigkey not in pend:
            _save(_prunes_path(), pend + [sigkey])
    try:
        os.unlink(rpath)
    except OSError:
        pass
    jd = os.path.join(JOBS, sigkey[:-4])
    if os.path.isdir(jd):
        shutil.rmtree(jd, ignore_errors=True)         # excerpt (if any) is already cut
    if _prune_grants(sigkey):                         # grants die with the evidence set
        pend = _pending_prunes()
        if sigkey in pend:
            _save(_prunes_path(), [k for k in pend if k != sigkey])


def sweep_jobs():
    """Orphaned job dirs (no result ever arrived) go after JOB_TTL_S."""
    now = time.time()
    try:
        names = os.listdir(JOBS)
    except OSError:
        return 0
    swept = 0
    for name in names:
        path = os.path.join(JOBS, name)
        try:
            if now - os.path.getmtime(path) > JOB_TTL_S:
                import shutil
                shutil.rmtree(path, ignore_errors=True)
                swept += 1
        except OSError:
            continue
    return swept


def run():
    os.makedirs(STATE_DIR, exist_ok=True)
    lock = harvest.acquire_writer_lock()
    if lock is None:
        print("another queue/state writer holds %s (a collector, or harvest.py --run) -- "
              "ONE writer, always" % LOCK)
        return
    # LOST SIGNATURES REGENERATE in split mode too: this process is the split runtime's one
    # queue/state writer, so the startup recovery harvest.run() does must happen HERE as
    # well -- same function, same policy (requeue small losses; past the cap, report via
    # sig_alert and stand still). No live state is held yet, so let it load-and-save.
    harvest.recover_missing_sigs_at_start()
    while True:
        state = _load(STATE, blank_state())
        q = _load(QUEUE, {"pending": [], "done": []})
        qs = queries()
        # Bucket sig count for /harvest (≤15-min cached listing). Persist only when the
        # count moved -- an idle collector pass has no other reason to write state.json.
        if harvest.stamp_pool(state):
            _save(STATE, state)
        n = collect_once(state, q, qs)

        _, retired = listen_queue_split()
        dropped = harvest.drop_ruled_excerpts(state, retired)
        todo = len(harvest.unscored_pairs(state, q, retired, qs))
        if todo:
            state["rescan_pending"] = todo
            done = harvest.rescan(state, q, retired, qs, limit=RESCAN_PER_PASS)
            state["rescan_pending"] = max(0, todo - done)
            _save(STATE, state)
        elif sigstore.enabled():
            n_ev, freed = sigstore.evict_cold(harvest.CACHE, state.get("scored") or {},
                                              [qk for _, _, qk in qs])
            if n_ev:
                print("evicted %d cold signature(s) (%.1f MB freed)" % (n_ev, freed / 1e6))
        if dropped:
            _save(STATE, state)
        sweep_jobs()
        if not n and not todo:
            time.sleep(30)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", action="store_true", help="fold loop")
    ap.add_argument("--once", action="store_true", help="one collect_once() pass (tests/cron)")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    if not enabled():
        print("collector is dark -- set NETRADIO_COLLECTOR=on (.env_vars) to enable the split "
              "collector. Mode A (harvest.py --run) is unaffected.")
        return
    if args.status:
        s = _load(STATE, blank_state())
        pending = len([n for n in (os.listdir(RESULTS) if os.path.isdir(RESULTS) else [])
                       if n.endswith(".json")])
        print(json.dumps({"results_pending": pending, "analyzed": s.get("analyzed"),
                          "matches": len(s.get("matches") or []),
                          "rescan_pending": s.get("rescan_pending", 0),
                          "updated": s.get("updated")}, indent=1))
        return
    if args.once:
        state = _load(STATE, blank_state())
        q = _load(QUEUE, {"pending": [], "done": []})
        print(collect_once(state, q, queries()))
        return
    if args.run:
        run()


if __name__ == "__main__":
    main()
