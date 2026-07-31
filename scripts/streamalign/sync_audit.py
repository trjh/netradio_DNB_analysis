"""Audit every hand `origNNN sync:` point against the audio (align-tool calibration).

The hand sync pairs are the G2 ground truth, but they were seated by ear and eye in
Audacity, where exact seating edits the original's clip head -- so the label-file
bookkeeping (`origNNN start:` rows) can sit hundreds of ms from the audio-exact seat,
and a mislabeled point has nothing checking it at all. This walks EVERY sync pair in
every label file and grades it against the audio itself:

  1. reconstruct the claimed seat: original-native instant = orig_ts - start_ts (the
     matching `origNNN start:` row -- exact label match first, else the nearest start
     row for that original in that file), rate from `track_sync_groundtruth` (inverted
     into original-seconds-per-stream-second);
  2. GCC-PHAT the seat within +-SEARCH_S (the hand label guarantees the right MOMENT,
     so a short search cannot loop-trap; PHAT restores the sample-exactness the clip
     bookkeeping lost);
  3. measure what the /align inspector will show there: whole-window and 10%-point
     RMS(diff)/RMS(stream) at the best polarity, plus the refine confidence;
  4. verdict: STRONG (seat_conf >= 0.5) / ok (>= 0.15) / NOT-FOUND -- the last means
     the audio does not corroborate the label within the search radius: a candidate
     for re-checking by ear, or simply a heavily layered moment.

With --background N it also samples the same original window against N wrong places
in the capture per audited point -- the mismatch distribution that, together with the
true-seat numbers, calibrates the inspector's residual colors (they cannot be
absolute: verified seats range ~28%..~110% whole-window depending on how exposed the
record is in the mix).

Read-only: writes nothing but the optional --json report.
"""

import json

import numpy as np

from . import audio as _audio
from . import matchconv as _mc
from . import track_mix as _tm

SR = _audio.SR
WIN_S = 6.0
CORE = 0.10
SEARCH_S = 1.5
BG_STEP_S = 7.0
STRONG_CONF = 0.5
FOUND_CONF = 0.15


def parse_orig_starts(labels_dir=None):
    """{(stem, orig_num): [(label, t)]} from `origNNN start: <label> ...` rows."""
    import re
    labels_dir = labels_dir or _tm._gt.LABELS_DIR
    rows = _tm._read_rows(labels_dir)
    out = {}
    pat = re.compile(r"orig(\d+)\s+start:\s*(\S*)", re.IGNORECASE)
    for r in rows:
        m = pat.match(r["text"].strip())
        if m:
            stem = r["file"].replace(".labels.tsv", "")
            out.setdefault((stem, int(m.group(1))), []).append((m.group(2), r["t"]))
    return out


def start_for(starts, stem, num, label, orig_ts):
    """The clip-head time for a sync point: nearest exact-label match, else nearest
    of any label. Re-seating in Audacity leaves DUPLICATE `start: <label>` rows (an
    old head and the current one), so among exact matches the one nearest the sync
    row's own time is the live bookkeeping -- an arbitrary first-match can be
    seconds of stale clip head."""
    rows = starts.get((stem, num))
    if not rows:
        return None
    exact = [t for lab, t in rows if lab == label]
    pool = exact or [t for _, t in rows]
    return min(pool, key=lambda t: abs(t - orig_ts))


def seat_metrics(stream, orig2, a_s, b2_s, win_s=WIN_S):
    """(whole%, point%, inverted) of the inspector's subtraction at one seat, or None.

    Mirrors inspect_slice: RMS-gain-matched, best polarity, 16 kHz band.
    """
    n = int(win_s * SR)
    a0, b0 = int((a_s - win_s / 2) * SR), int((b2_s - win_s / 2) * SR)
    if a0 < 0 or b0 < 0 or a0 + n > len(stream) or b0 + n > len(orig2):
        return None
    s, o = stream[a0:a0 + n], orig2[b0:b0 + n]
    rs, ro = np.sqrt(np.mean(s ** 2)), np.sqrt(np.mean(o ** 2))
    if rs < 1e-6 or ro < 1e-6:
        return None
    o = o * (rs / ro)
    best = None
    for sign in (1.0, -1.0):
        d = s - sign * o
        whole = 100.0 * np.sqrt(np.mean(d ** 2)) / rs
        c0, c1 = int(n * (0.5 - CORE / 2)), int(n * (0.5 + CORE / 2))
        point = 100.0 * np.sqrt(np.mean(d[c0:c1] ** 2)) \
            / max(1e-9, np.sqrt(np.mean(s[c0:c1] ** 2)))
        if best is None or whole < best[0]:
            best = (float(whole), float(point), sign < 0)
    return best


def _inside(a_s, b2_s, stream_len_s, orig2_len_s, win_s=WIN_S, search_s=SEARCH_S):
    """Shift the probe (same amount both signals -- correspondence preserved) so the
    refine window (win + search radius + slack) fits inside both files; None if it
    can't. A sync point at a clip's very edge gets probed slightly inward rather
    than skipped -- constant-offset correspondence holds locally."""
    margin = win_s / 2 + search_s + 1.1
    lo_shift = max(0.0, margin - min(a_s, b2_s))
    hi_shift = min(0.0, min(stream_len_s - a_s, orig2_len_s - b2_s) - margin)
    shift = lo_shift if lo_shift > 0 else hi_shift
    a_s, b2_s = a_s + shift, b2_s + shift
    if min(a_s, b2_s) < margin or a_s > stream_len_s - margin \
            or b2_s > orig2_len_s - margin:
        return None
    return a_s, b2_s


def audit(labels_dir=None, sources_dir="sources_local", audio_dir=None,
          tracks=None, background=0, search_s=SEARCH_S):
    """Grade every hand sync pair. Returns {"points": [...], "background": [...]}.

    `tracks`: optional iterable of track numbers to limit to. `background`: mismatch
    samples per audited point (0 = skip; they cost one metrics() each).
    """
    gt = _tm.track_sync_groundtruth(labels_dir)
    starts = parse_orig_starts(labels_dir)
    streams, origs = {}, {}
    points, bg = [], []
    for num in sorted(gt):
        if tracks and num not in set(int(t) for t in tracks):
            continue
        info = gt[num]
        if not info["rate"] or info["rate"] <= 0:
            continue
        rate_mc = 1.0 / float(info["rate"])
        for pr in info["pairs"]:
            stem = pr["file"].replace(".labels.tsv", "")
            rec = {"track": num, "label": pr["label"], "stem": stem,
                   "stream_t": pr["track_ts"], "verdict": "SKIPPED", "why": None}
            points.append(rec)
            start_t = start_for(starts, stem, num, pr["label"], pr["orig_ts"])
            if start_t is None:
                rec["why"] = "no origNNN start: row"
                continue
            b_native = pr["orig_ts"] - start_t
            if b_native < 0:
                rec["why"] = "sync before clip head"
                continue
            if not _audio.find_audio_file(stem, audio_dir):
                rec["why"] = "capture audio missing"
                continue
            orig_path = _tm.find_original(num, sources_dir)
            if not orig_path:
                rec["why"] = "original missing"
                continue
            if stem not in streams:
                streams[stem] = _audio.load_audio(stem, audio_dir=audio_dir)
            if num not in origs:
                origs[num] = _audio.load_audio(orig_path, use_cache=False)
            stream = streams[stem]
            orig2 = _mc.resample_by_rate(origs[num], rate_mc)
            placed = _inside(pr["track_ts"], b_native / rate_mc,
                             len(stream) / SR, len(orig2) / SR, search_s=search_s)
            if placed is None:
                rec["why"] = "window does not fit (clip edge)"
                continue
            a_s, b2_hand = placed
            got = _mc._refine_peaks(stream, orig2, a_s, b2_hand, WIN_S, search_s,
                                    n_peaks=1)
            if not got:
                rec["why"] = "refine window out of bounds"
                continue
            off_s, seat_conf, _ = got[0]
            b2 = a_s - off_s
            m = seat_metrics(stream, orig2, a_s, b2)
            if m is None:
                rec["why"] = "metrics window out of bounds"
                continue
            rec.update({
                "seat_conf": float(seat_conf),
                "hand_err_ms": float((b2 - b2_hand) * 1000.0),
                "whole": m[0], "point": m[1], "inverted": m[2],
                "verdict": ("STRONG" if seat_conf >= STRONG_CONF
                            else "ok" if seat_conf >= FOUND_CONF else "NOT-FOUND"),
                "why": None,
            })
            if background:
                a, made = WIN_S, 0
                while made < background and a < len(stream) / SR - WIN_S:
                    if abs(a - a_s) > 3.0:
                        mm = seat_metrics(stream, orig2, a, b2)
                        if mm:
                            bg.append(mm[0])
                            made += 1
                    a += BG_STEP_S
    return {"points": points, "background": bg}


def render(result):
    """The human-readable table (one line per point) + summary counts."""
    lines = []
    counts = {}
    for p in result["points"]:
        counts[p["verdict"]] = counts.get(p["verdict"], 0) + 1
        if p["verdict"] == "SKIPPED":
            lines.append("track %03d %-2s %-14s SKIPPED: %s"
                         % (p["track"], p["label"], p["stem"], p["why"]))
        else:
            lines.append(
                "track %03d %-2s %-14s @%8.1fs  %-9s conf %.2f  hand_err %+8.1fms  "
                "whole %5.1f%%  point %5.1f%%  %s"
                % (p["track"], p["label"], p["stem"], p["stream_t"], p["verdict"],
                   p["seat_conf"], p["hand_err_ms"], p["whole"], p["point"],
                   "inv" if p["inverted"] else "   "))
    lines.append("# " + "  ".join("%s: %d" % kv for kv in sorted(counts.items())))
    if result["background"]:
        b = np.array(result["background"])
        lines.append("# background (wrong-place) whole-window residual: "
                     "min %.1f%%  p5 %.1f%%  median %.1f%%  n=%d"
                     % (b.min(), np.percentile(b, 5), np.median(b), len(b)))
    return "\n".join(lines)
