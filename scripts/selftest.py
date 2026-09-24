#!/usr/bin/env python3
"""Canary self-tests — is the matcher still RIGHT, and does the pool still hold the canary?

    . .venv/bin/activate && python scripts/selftest.py --offline
    . .venv/bin/activate && python scripts/selftest.py --live

`--offline` re-runs one calibration case from local files (no network). `--live` re-scores
the canary's STORED signature by hand -- the canary's file arrives through the feeder like any
entry, is signed once, and its signature lives in the signature bucket keyed by
`NETRADIO_CANARY_KEY` -- against the canary's mix and the current mysteries, demanding the known
cost, rank and margin. The by-hand check needs no file and no fetch. The harvester's own canary
pass goes further: whenever the feeder puts the canary's file back, it re-signs the file as if
new, compares the new signature with the stored one, and hands the NEW signature to `live()`
(see `harvest.canary_pass`).

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

**live()** — the canary's signature, scored. The canary is one known track whose file arrives
through the feeder; its signature sits in the bucket under its key (`NETRADIO_CANARY_KEY`). The
check scores a signature of it against the canary's own mix (the query a calibration case would
build from it) and against the current mystery queries, demanding the same three gates as
offline: cost in the true-match range, rank first among the rivals, and a real margin. The
harvester hands it the signature it has just re-signed from the canary's file; `--live` hands it
the stored one. No network, no fetch inside the check itself.

The trap, and the canary URL
----------------------------
The re-score proves the matcher and the pool are still working. It does NOT prove the canary's
signature is the right record: the canary's file is fed through the directories like any entry,
and a feed that delivers the wrong upload files a signature the matcher will fail against the
canary's mix — and the canary cries wolf, saying "matcher broken" when the matcher is fine. A
canary that cries wolf gets ignored, and then it is worse than no canary at all.

So the canary's track is the **first calibration case** by default (`cases()[0]`), the same track
`--offline` uses: feed that track's source URL through the queue as the canary, and the re-score
has the mix it expects. For naming a DIFFERENT calibration case as the canary, `establish_canary`
(by hand) searches for a stream of a solved track, fetches it, and scores what it fetched against
the original held on disk — only if that is a true match is it saved as the canary. This is the
one place a fetch still lives; the harvester's canary pass never fetches. The canary's key is the
pool's own rule applied to that URL, and the harvester compares its re-sign with the signature
filed under it.

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


# A STOP IS NEVER A VERDICT.
#
# A fetch callable returns exactly this error when a signal interrupted the decode. It says
# nothing about the matcher, so it must not become a canary result: recorded as a FAILURE it
# would sit on the harvest page saying the matcher is broken until the next daily
# run, and recorded at all -- even as "not checked" -- it would satisfy a due-date check and
# stand the canary down for 24 hours because somebody pressed Ctrl-C. So it is reported and
# NOT recorded, and the next start asks again.
STOPPED = "stopped"


def was_stopped(err):
    """True when a fetch came back because it was interrupted, not because anything failed.

    Matched exactly, or as the tail of `establish_canary`'s "could not fetch <url>: <err>", so a
    real error that happens to contain the word is not mistaken for a stop. `harvest.was_stopped`
    is the same predicate; it is duplicated rather than imported because this module must never
    import the harvester -- that is why `fetch` is injected.
    """
    e = (err or "").strip().lower()
    return e == STOPPED or e.endswith(": " + STOPPED)


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
    if was_stopped(err):
        # A stop is never a verdict: a Ctrl-C during the fetch is not "the upload is bad",
        # so it is reported as a skip (ok: None), not a failure, and no canary is saved.
        return {"ok": None, "url": url, "why": "stopped before the fetch finished"}
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


def best_rival_cost(mystery_queries, c_fetched):
    """Best (lowest) cost any MYSTERY query scores against the fetched canary.

    The canary's own mix must beat every mystery on this same candidate. Cost alone would let a
    degenerate matcher -- one that scores everything low -- sail straight through; the margin over
    the best rival is what actually carries the gate.

    Lifted out of live() because it was UNREACHABLE without a network fetch, and that is precisely
    how it broke. `queries()` grew a third field (a query key fingerprinting the clip) and the loop
    here still said `for num, q in ...` -- two names, three values -- so the next daily canary would
    have raised ValueError and killed the harvester. Nothing caught it: no test drove live() (a
    fake fetch returns an error and live() returns long before this point), and pyflakes is blind to
    an unpack arity error. Same shape as the KeyError that killed it before: a branch no test
    exercises is a branch that fails in production. So the branch is now a function, and the
    function is tested.

    Takes the first two fields POSITIONALLY: the contract is "(mystery number, chroma), and I do
    not care what else you carry".

    Returns (best_cost, how_many_scored). The COUNT matters: with no rival, there is nothing to
    beat, and the margin gate must not hand the canary a win by default.
    """
    rivals = []
    for entry in (mystery_queries or []):
        rc, _s, _a = _cm.match(entry[1], c_fetched)
        if rc is not None:
            rivals.append(float(rc))
    return (min(rivals) if rivals else 1.0), len(rivals)


def live(c_canary, mystery_queries=None):
    """Score a signature of the canary against the canary's mix and the mysteries.

    `c_canary` is the canary's chroma, injected by the caller: the harvester's canary pass
    hands in the signature it has just re-signed from the canary's file (see
    `harvest.canary_pass`), the `--live` command hands in the STORED signature it loads
    through sigstore by `NETRADIO_CANARY_KEY`, and a test hands in a fake. This module never
    imports the harvester, and the check needs no fetch.

    The canary is one known track. The check scores its stored signature against the
    canary's own mix (the query a calibration case would build from the canary's track)
    and against the current mystery queries, demanding the same three gates as offline:
    cost in the true-match range, rank first among the rivals, and a real margin. A
    degenerate matcher -- one that scores everything low, or everything the same -- fails
    one of the three, the same way it fails offline.

    The canary's track is the first calibration case by default (`cases()[0]`), so the
    re-score works on a fresh machine once `NETRADIO_CANARY_KEY` is set and the canary's
    signature is in the bucket -- no `canary.json` needed. A `canary.json` written by
    `establish_canary` (or by hand) overrides the default, naming a different calibration
    case as the canary. The default matches `establish_canary`'s own default
    (`subject = subject or cs[0]`), so a canary established under the old fetch-based
    contract and a fresh canary under the new re-score contract agree on the same track.
    """
    cs = cases()
    canary = _read(CANARY, {})
    track = canary.get("track")
    if track is None:
        # No canary.json: default to the first calibration case, the same track
        # `establish_canary` would pick. This makes the re-score work end-to-end on a
        # fresh machine once the key is set and the canary's signature is in the bucket,
        # with no manual `canary.json` step.
        if not cs:
            return record({"kind": "live", "ok": None, "when": _now(),
                           "why": "no calibration cases -- NETRADIO_SOURCES_DIR unset or empty"})
        track = cs[0]["num"]
    subject = next((c for c in cs if c["num"] == track), None)
    if subject is None:
        return record({"kind": "live", "ok": None, "when": _now(),
                       "why": "the canary's track is no longer in the calibration set"})

    if c_canary is None:
        # The caller could not load the signature: the cache is dark, the bucket listing
        # failed, or the canary's key points at an object that is not there. None of those
        # is a verdict on the matcher, so this is "not checked" rather than a failure -- the
        # same state a missing calibration case reads, and the one a reader can tell apart
        # from a PASS or a FAIL.
        return record({"kind": "live", "ok": None, "when": _now(),
                       "track": track, "name": canary.get("name") or subject["name"],
                       "why": "the canary's signature is not available -- set "
                              "NETRADIO_CANARY_KEY to the canary's key (see .env.example) and "
                              "ensure its signature is in the bucket"})

    t0 = time.time()
    mix = _cal.mix_query(subject)
    if mix is None:
        return record({"kind": "live", "ok": None, "when": _now(),
                       "why": "the canary's mix is too short to query with"})
    q_own = _cal.chroma(mix)

    cost, shift, at = _cm.match(q_own, c_canary)

    # RANK: the canary's own mix must beat the mystery queries on this same candidate. Cost
    # alone would let a degenerate matcher -- one that scores everything low -- sail through.
    best_rival, n_rivals = best_rival_cost(mystery_queries, c_canary)

    # Same three gates as offline: in range, first, and by a real margin. `rivals` are the
    # mystery queries scored against this same candidate -- if the canary's own mix cannot
    # beat them comfortably on a record we KNOW is the answer, nothing the harvester reports
    # means anything.
    ok = (cost is not None and cost <= TRUE_MATCH_MAX
          and (not n_rivals or best_rival - cost >= MIN_MARGIN))
    return record({"kind": "live", "ok": bool(ok), "when": _now(),
                   "track": track, "name": canary.get("name") or subject["name"],
                   "cost": None if cost is None else round(float(cost), 4),
                   "rival": round(best_rival, 4) if n_rivals else None,
                   "semitones": shift, "at_s": None if at is None else round(float(at), 1),
                   "took_s": round(time.time() - t0, 1),
                   "why": None if ok else
                          ("a KNOWN record's stored signature did not come back as a match "
                           "(cost %s) -- the matcher or the signature is broken"
                           % ("none" if cost is None else "%.4f" % cost))})


def _load_canary_signature():
    """The canary's stored chroma, pulled from the working cache or the bucket by its key.

    `NETRADIO_CANARY_KEY` names the canary (the pool's own key rule, `u` + sha1(url)[:20]).
    The signature sits in the bucket under `<key>.npy` and is pulled back into the chroma
    cache on demand. This is the one place the `--live` command reaches for the store, and
    it imports `harvest` and `sigstore` lazily so the library paths (the tests, the
    harvester's own loop) never pay for them and never depend on them.

    The cache directory is resolved through `harvest._chroma_dir()` -- the SAME seam the
    harvester's loop uses (`_load_sig`) -- so the by-hand `--live` command reads from the
    same directory every pass does. The harvester's `_chroma_dir()` resolves through the
    cache policy's registry (which reads `$NETRADIO_CHROMA_CACHE_DIR` or
    `$NETRADIO_CACHE_ROOT/chroma`), and importing `harvest` registers the chroma cache.
    A `harvest` import that fails (a dependency the CLI does not own) or a dark policy
    leaves `_chroma_cache_dir()` None, and the caller reports the misconfiguration rather
    than scoring from a directory the harvester's loop never reads.
    """
    key = (os.environ.get("NETRADIO_CANARY_KEY") or "").strip()
    if not key:
        return None, "NETRADIO_CANARY_KEY is not set -- the canary is not configured"
    d = _chroma_cache_dir()
    if d is None:
        return None, ("the chroma cache is dark -- set NETRADIO_CACHE_ROOT (or "
                      "NETRADIO_CHROMA_CACHE_DIR) in .env, see .env.example")
    import sigstore                                     # lazy: the library path does not import it
    if not os.path.isdir(d) and not sigstore.enabled():
        return None, "the canary's signature is nowhere to be found -- no chroma cache and " \
                     "no bucket configured"
    path = os.path.join(d, key + ".npy")
    if not os.path.exists(path) and sigstore.enabled():
        os.makedirs(d, exist_ok=True)
        if not sigstore.fetch(key + ".npy", d):
            return None, ("the canary's signature (%s.npy) is not in the bucket -- has the "
                          "canary been signed and uploaded?" % key)
    try:
        return np.load(path).astype("float32"), None
    except (OSError, ValueError) as exc:
        return None, "the canary's signature could not be read: %s" % exc


def _chroma_cache_dir():
    """The chroma cache's directory, resolved the same way the harvester resolves it.

    Imports `harvest` lazily (which registers the chroma cache with the policy via
    `harvest.register_caches()`, run at import) and reads `harvest._chroma_dir()`. If the
    import fails (a dependency the CLI does not own) or the policy is dark, the cache is
    dark: return None and let the caller (`_load_canary_signature`) report the
    misconfiguration, rather than scoring from a legacy directory the harvester's loop
    never reads. The harvester itself refuses to start on a dark policy, so a by-hand
    `--live` CLI that silently fell back to `<repo>/.chroma-cache` would read a directory
    no current writer of the pool's signatures produces: `match_queue.py` does cache its
    own content-addressed signatures there, but under sha1(basename|size|mtime) keys,
    never the pool's `u` + sha1(url)[:20] keys, so the canary's `.npy` would never be
    found and the loop never reads the directory.
    """
    try:
        import harvest                          # lazy: registers the chroma cache
        d = harvest._chroma_dir()
        if d is not None:
            return d
    except Exception:
        pass
    return None


def _mystery_queries_for_live():
    """The current mystery queries for the `--live` re-score, the same set the harvester's
    loop passes to `live()`. Built through `harvest.queries()` (imported lazily here, inside
    the CLI, so the library path -- `live()`, `offline()` -- never imports the harvester and
    the tests never pay for it). Returns an empty list when the queries cannot be built
    (no unsolved mysteries, no usable clips, librosa absent): an empty rival set is
    `live()`'s "no rivals to beat" case, not a skip -- the canary still has to score in
    range against its own mix, which is the check the CLI runs by hand when the loop is
    not feeding it mysteries.

    A failure that is NOT one of the expected "no queries" cases (a bug in
    `harvest.queries()`, a malformed `track-metadata.json`, a broken clip) would silently
    weaken the by-hand check to "no rivals to beat", which passes the canary on cost alone.
    So the exception is printed to stderr -- the check still runs (the canary has to score
    in range against its own mix either way), but a silent degradation is not silent."""
    try:
        import harvest                          # lazy: the library path does not import it
    except Exception as exc:
        print("selftest --live: could not import harvest for the mystery queries (%s); "
              "running with no rivals -- the canary still has to score in range against its "
              "own mix" % exc, file=sys.stderr)
        return []
    try:
        return harvest.queries()
    except Exception as exc:
        print("selftest --live: harvest.queries() raised (%s); running with no rivals -- "
              "the canary still has to score in range against its own mix" % exc,
              file=sys.stderr)
        return []


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true", help="matching maths, local files, no network")
    ap.add_argument("--live", action="store_true",
                    help="re-score the canary's STORED signature (named by NETRADIO_CANARY_KEY) "
                         "against the canary's mix and the current mysteries")
    args = ap.parse_args()

    if not args.offline and not args.live:
        ap.error("choose --offline and/or --live")
    if args.offline:
        print(json.dumps(offline(), indent=2))
    if args.live:
        # The by-hand re-score reads the canary's STORED signature, named by
        # NETRADIO_CANARY_KEY, through the chroma cache and sigstore -- no file, no fetch.
        # (The harvester's canary pass scores a fresh re-sign instead, and compares it with
        # this stored one.) The current mystery queries are built
        # the same way the loop builds them, so the rank/margin gate runs against the same
        # rivals -- a canary that only barely beats its own mix cannot pass the CLI when a
        # close current mystery should reject it.
        c_canary, why = _load_canary_signature()
        if c_canary is None:
            print(json.dumps({"kind": "live", "ok": None, "when": _now(), "why": why},
                              indent=2))
            return
        print(json.dumps(live(c_canary, mystery_queries=_mystery_queries_for_live()),
                         indent=2))


if __name__ == "__main__":
    main()
