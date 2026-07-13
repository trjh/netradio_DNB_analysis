#!/usr/bin/env python3
"""Canary self-tests — is the matcher still RIGHT, and is the live pipeline still WORKING?

    PYTHONPATH=scripts .venv/bin/python scripts/selftest.py --offline
    PYTHONPATH=scripts .venv/bin/python scripts/selftest.py --live

Why this exists
---------------
A broken harvester and a pool that does not contain the answer produce the **identical** output:
zero matches. Week after week of "0 found" looks the same either way. The dashboard proves the
process is *alive*; it proves nothing at all about whether the matching is *correct*.

So we hold out a track we have already SOLVED — we have its mix, its original, and the answer —
and check that the machinery still finds it.

Two checks, because they fail differently
-----------------------------------------
**offline()** — re-run one calibration case from local files: take a solved track's mix, score it
against a pool of originals, and require its OWN original to come **first**. No network, a couple
of seconds. This proves the matching *maths* — chroma, subsequence-DTW, the twelve transpositions
— still works. If someone breaks `chroma_match.py`, this goes red immediately.

**live()** — the streaming path (`yt-dlp` → `ffmpeg` → chroma → match) is exactly what offline()
and the unit tests do **not** exercise, and it is the part with moving dependencies: yt-dlp breaks
when YouTube changes, ffmpeg flags rot, a decode silently yields noise. So: fetch a **fresh
stream** of a solved track's original, off the internet, right now, and require the harvester's
real query path to flag it at a true-match cost.

The trap, and the canary URL
----------------------------
If the stream we fetch is the *wrong upload*, the match fails — and the canary cries wolf, saying
"matcher broken" when the matcher is fine. A canary that cries wolf gets ignored, and then it is
worse than no canary at all.

So the canary URL is **established by validation, once**: we search for the record, fetch it, and
score it against the **local original we already hold**. Only if that is a true match do we know
the upload really is the record — and only then is it saved as the canary. From then on the URL is
known-good, so a later failure is unambiguous: it is the *pipeline*, not the pick.

Rank, never a bare cost
-----------------------
Both checks require the right answer to **win**, not merely to clear a bar. A short query drives
every cost down until unrelated tracks tie for first (the Mystery Track 7 lesson: 23 seconds, five
confident false positives), so both use a **full-length** query and both check rank. A self-test
that a broken matcher could pass is not a self-test.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np                                  # noqa: E402

import calibrate as _cal                            # noqa: E402
from streamalign import audio as _audio             # noqa: E402
from streamalign import chroma_match as _cm         # noqa: E402
from streamalign import groundtruth as _gt          # noqa: E402

STATE_DIR = os.path.join(_gt.REPO_ROOT, ".harvest")
RESULT = os.path.join(STATE_DIR, "selftest.json")
CANARY = os.path.join(STATE_DIR, "canary.json")

# A true match runs 0.004-0.03; an unrelated record sits around 0.095. The populations OVERLAP, so
# this bar alone is not proof -- it is paired with a rank AND a margin check everywhere it is used.
TRUE_MATCH_MAX = 0.050
POOL_N = 8                  # decoys for the offline check: enough to make rank #1 mean something
LIVE_EVERY_S = 24 * 3600

# THE MARGIN, and why rank alone is not enough.
#
# A degenerate matcher -- one that returns the same cost for everything -- produces a table of
# ties. Sorting ties falls back to the track number, so the subject (the lowest-numbered case)
# lands at rank 1 and a rank-only check waves it through. I know because I wrote the rank-only
# check first, sabotaged the matcher to prove it would catch it, and it did not.
#
# This is the Mystery Track 7 lesson again, from the other side: what made those five false
# positives false was not their cost, it was that they were all within 0.0007 of each other.
# Winning by nothing is not winning. Observed true-match margins are 0.045-0.088; this is set well
# below that, but lethal to a tie.
MIN_MARGIN = 0.010


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def _read(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def last():
    return _read(RESULT, {})


def record(result):
    """Keep the latest of each kind, so one offline pass cannot hide a live failure."""
    all_ = _read(RESULT, {})
    all_[result["kind"]] = result
    _save(RESULT, all_)
    return result


def cases():
    """Solved tracks where we hold BOTH the mix and the original. The calibration set."""
    src = os.environ.get("NETRADIO_SOURCES_DIR")
    if not src or not os.path.isdir(src):
        return []
    meta = json.load(open(os.path.join(_gt.REPO_ROOT, "track-metadata.json")))
    tracks = meta.get("tracks", meta)
    return _cal.build_cases(tracks, _cal.positions(), src)


# --- offline: does the matching maths still work? ------------------------------------------------

def offline():
    """Re-run one calibration case from local files. No network."""
    cs = cases()
    if not cs:
        return record({"kind": "offline", "ok": None, "when": _now(),
                       "why": "no calibration cases -- NETRADIO_SOURCES_DIR unset or empty"})

    # Deterministic: always the same case, so a change in the RESULT means a change in the CODE.
    subject = cs[0]
    pool_cases = cs[:POOL_N] if len(cs) >= POOL_N else cs
    if subject not in pool_cases:
        pool_cases = [subject] + pool_cases[:POOL_N - 1]

    t0 = time.time()
    mix = _cal.mix_query(subject)
    if mix is None:
        return record({"kind": "offline", "ok": None, "when": _now(),
                       "why": "the subject's mix is too short to query with"})
    q = _cal.chroma(mix)

    scored = []
    for c in pool_cases:
        cost, shift, _at = _cm.match(q, _cal.chroma(_audio.load_audio(c["orig"])))
        if cost is not None:
            scored.append((cost, c["num"], shift))
    scored.sort()
    if not scored:
        return record({"kind": "offline", "ok": False, "when": _now(),
                       "why": "the matcher returned nothing at all"})

    own = next((s for s in scored if s[1] == subject["num"]), None)
    if own is None:
        return record({"kind": "offline", "ok": False, "when": _now(),
                       "why": "the subject's own original did not score at all"})
    rank = [s[1] for s in scored].index(subject["num"]) + 1
    rival = next((s[0] for s in scored if s[1] != subject["num"]), 1.0)
    margin = float(rival - own[0])

    # Cost, rank AND margin. Cost alone passes a matcher that scores everything low; rank alone
    # passes one that scores everything the SAME (ties sort by track number, and the subject is
    # the lowest -- see MIN_MARGIN). It has to win, and win by something.
    ok = rank == 1 and own[0] <= TRUE_MATCH_MAX and margin >= MIN_MARGIN
    why = None
    if not ok:
        if rank != 1:
            why = "its own original did not win (rank %d of %d, cost %.4f)" % (rank, len(scored), own[0])
        elif own[0] > TRUE_MATCH_MAX:
            why = "won, but at %.4f -- outside the true-match range" % own[0]
        else:
            why = ("won by only %.4f -- a tie is not a win, and a matcher that scores everything "
                   "the same would look exactly like this" % margin)
    return record({"kind": "offline", "ok": ok, "when": _now(),
                   "track": subject["num"], "name": subject["name"],
                   "cost": round(float(own[0]), 4), "rival": round(float(rival), 4),
                   "margin": round(margin, 4), "rank": rank,
                   "pool": len(scored), "took_s": round(time.time() - t0, 1), "why": why})


# --- live: does the streaming pipeline still work? -----------------------------------------------

def _search(name):
    """Find a stream of a record by name. Only ever used to ESTABLISH a canary, and whatever it
    returns is then validated against the original we already hold -- so a bad search result is
    rejected, not trusted."""
    try:
        out = subprocess.run(
            ["yt-dlp", "--no-warnings", "--skip-download", "--no-playlist",
             "--print", "%(webpage_url)s", "ytsearch1:%s" % name],
            capture_output=True, text=True, timeout=120).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out.split("\n")[0].strip() or None if out.startswith("http") else None


def establish_canary(fetch, subject=None):
    """Pick a stream of a SOLVED track and prove it really is that record before trusting it.

    This is the step that stops the canary crying wolf. We search, we fetch, and we score what we
    fetched against the original we already hold on disk. If it is not a true match, the upload is
    not the record -- so we refuse it rather than enshrine it as the thing we measure against.
    """
    cs = cases()
    if not cs:
        return {"ok": False, "why": "no calibration cases to build a canary from"}
    subject = subject or cs[0]

    # A HAND-PICKED URL, when the search keeps finding the wrong upload.
    #
    # `_search()` takes YouTube's first hit for the track name, and for the current subject
    # (track 3, Jamie Myerson - Sky Blue) that hit is not the record: it scores 0.0867 against our
    # own copy, so the guard below refuses it and no canary is ever established. Correct behaviour
    # -- a canary that cries wolf is worse than none -- but it leaves the live check permanently
    # "not checked", and there was no way to break the deadlock by hand.
    #
    # Set NETRADIO_CANARY_URL to a stream you know IS the record. It is still validated against our
    # own copy, exactly like a searched one: a hand-picked URL is a hint, never an override.
    url = os.environ.get("NETRADIO_CANARY_URL", "").strip() or _search(subject["name"])
    if not url:
        return {"ok": False, "why": "found no stream for %r -- set NETRADIO_CANARY_URL to a "
                                    "stream of it, and it will be validated before use"
                                    % subject["name"]}

    c_fetched, _samples, err = fetch(url)
    if err or c_fetched is None:
        return {"ok": False, "why": "could not fetch %s: %s" % (url, err)}

    # THE VALIDATION: what we fetched, against the original we already hold.
    local = _cal.chroma(_audio.load_audio(subject["orig"]))
    cost, _shift, _at = _cm.match(local, c_fetched)
    if cost is None or cost > TRUE_MATCH_MAX:
        return {"ok": False, "url": url,
                "why": "the stream we found is not the record (cost %s vs our own copy)"
                       % ("none" if cost is None else "%.4f" % cost)}

    canary = {"url": url, "track": subject["num"], "name": subject["name"],
              "control_cost": round(float(cost), 4), "established": _now()}
    _save(CANARY, canary)
    return {"ok": True, **canary}


def live(fetch, mystery_queries=None):
    """Fetch the canary FRESH off the internet and require the real query path to flag it.

    `fetch(url) -> (chroma, samples, error)` is injected (harvest.stream_chroma) so this module
    never imports the harvester -- and so the tests can drive it without a network.
    """
    canary = _read(CANARY, {})
    if not canary.get("url"):
        est = establish_canary(fetch)
        if not est.get("ok"):
            return record({"kind": "live", "ok": None, "when": _now(),
                           "why": "no canary yet: %s" % est.get("why")})
        canary = est

    subject = next((c for c in cases() if c["num"] == canary.get("track")), None)
    if subject is None:
        return record({"kind": "live", "ok": None, "when": _now(),
                       "why": "the canary's track is no longer in the calibration set"})

    t0 = time.time()
    c_fetched, _samples, err = fetch(canary["url"])          # the REAL path: yt-dlp -> ffmpeg -> chroma
    if err or c_fetched is None:
        return record({"kind": "live", "ok": False, "when": _now(), "url": canary["url"],
                       "track": canary["track"], "name": canary["name"],
                       "why": "the live pipeline could not fetch a KNOWN-GOOD url: %s" % err})

    mix = _cal.mix_query(subject)
    if mix is None:
        return record({"kind": "live", "ok": None, "when": _now(),
                       "why": "the canary's mix is too short to query with"})
    q_own = _cal.chroma(mix)

    cost, shift, at = _cm.match(q_own, c_fetched)

    # RANK: the canary's own mix must beat the mystery queries on this same candidate. Cost alone
    # would let a degenerate matcher -- one that scores everything low -- sail through.
    rivals = []
    for num, q in (mystery_queries or []):
        rc, _s, _a = _cm.match(q, c_fetched)
        if rc is not None:
            rivals.append(float(rc))
    best_rival = min(rivals) if rivals else 1.0

    # Same three gates as offline: in range, first, and by a real margin. `rivals` are the mystery
    # queries scored against this same candidate -- if the canary's own mix cannot beat them
    # comfortably on a record we KNOW is the answer, nothing the harvester reports means anything.
    ok = (cost is not None and cost <= TRUE_MATCH_MAX
          and (not rivals or best_rival - cost >= MIN_MARGIN))
    return record({"kind": "live", "ok": bool(ok), "when": _now(), "url": canary["url"],
                   "track": canary["track"], "name": canary["name"],
                   "cost": None if cost is None else round(float(cost), 4),
                   "rival": round(best_rival, 4) if rivals else None,
                   "semitones": shift, "at_s": None if at is None else round(float(at), 1),
                   "took_s": round(time.time() - t0, 1),
                   "why": None if ok else
                          "a KNOWN record, fetched live, did not come back as a match "
                          "(cost %s) -- the streaming path or the matcher is broken"
                          % ("none" if cost is None else "%.4f" % cost)})


def due_for_live(every_s=LIVE_EVERY_S):
    prev = _read(RESULT, {}).get("live") or {}
    when = prev.get("when")
    if not when:
        return True
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(when)).total_seconds()
    except (ValueError, TypeError):
        return True
    return age >= every_s


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true", help="matching maths, local files, no network")
    ap.add_argument("--live", action="store_true", help="the real streaming path, against a canary")
    args = ap.parse_args()

    if not args.offline and not args.live:
        ap.error("choose --offline and/or --live")
    if args.offline:
        print(json.dumps(offline(), indent=2))
    if args.live:
        import harvest                                  # only here: it pulls in the whole harvester
        print(json.dumps(live(harvest.stream_chroma, harvest.queries()), indent=2))


if __name__ == "__main__":
    main()
