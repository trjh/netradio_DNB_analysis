"""Original-track ↔ mix ground truth from Tim's sync points (G2 / T0).

Tim hand-marked where each original source track lines up with the mix using paired
labels: `origNNN sync: <label>` (a moment in original track NNN) and
`track sync: <label>` (the same moment in the mix), both in the owning label file's
Audacity timeline. This module parses those pairs and, per track, fits the **rate**
(how mix time advances per original-track time) — the quantity the chroma+DTW
aligner (T1) is graded against. (The sheet uses the same idea:
`speed = (trackB − trackA) / (origB − origA)`, sheetscript/Code.js.)

Caveats baked into the parsing: the labels (`2`, `A`, `B`, …) repeat across sections,
timestamps are ranges (we take the start column), and pairing is by matching label
within a file at the nearest timestamp. So this captures the clean, adjacent
`orig/track` pairs — enough for rate validation — not every annotation.
"""

import os
import re

import numpy as np

from . import groundtruth as _gt

# A primary sync row STARTS with `orig<NNN> sync:` or `track[<NNN>] sync:` (the
# track number is optional — `track sync: L` and `track015 sync: L` both occur).
# Anchored at the start so carried-forward note rows like `note d336-355: track
# sync: 9` (which reference ANOTHER file) are NOT consumed as current-file pairs.
_SYNC_RE = re.compile(r"^(orig|track)(\d*)\s+sync:\s*(\S+)", re.I)
_MAX_PAIR_GAP_S = 30.0   # an orig/track pair must sit within this in the same file


def _read_rows(labels_dir):
    rows = []
    if not os.path.isdir(labels_dir):
        return rows
    for fn in sorted(n for n in os.listdir(labels_dir) if n.endswith(".labels.tsv")):
        try:
            with open(os.path.join(labels_dir, fn), "r", encoding="utf-8",
                      errors="replace") as handle:
                for line in handle:
                    parts = line.rstrip("\n").split("\t", 2)
                    if len(parts) < 3:
                        continue
                    try:
                        t = float(parts[0])   # start column
                    except ValueError:
                        continue
                    rows.append({"file": fn, "t": t, "text": parts[2].strip()})
        except OSError:
            pass
    return rows


def parse_sync_points(labels_dir=None):
    """{track_num: [{label, orig_ts, track_ts, file}]} — matched orig/track pairs.

    Each `origNNN sync: L` is paired with the nearest (same file, same label `L`)
    `track sync: L` within _MAX_PAIR_GAP_S.
    """
    labels_dir = labels_dir or _gt.LABELS_DIR
    rows = _read_rows(labels_dir)
    origs, tracks = [], []
    for r in rows:
        m = _SYNC_RE.match(r["text"].strip())
        if not m:
            continue
        kind = m.group(1).lower()
        num = int(m.group(2)) if m.group(2) else None
        rec = {"num": num, "label": m.group(3), "t": r["t"], "file": r["file"]}
        if kind == "orig":
            if num is not None:   # an orig sync must name its track
                origs.append(rec)
        else:
            tracks.append(rec)   # track sync; num may be explicit (trackNNN) or None
    out = {}
    for o in origs:
        # a track row matches by label, same file; if it names a track number it
        # must equal the orig's, otherwise (plain `track sync`) any number is ok.
        cands = [tk for tk in tracks
                 if tk["file"] == o["file"] and tk["label"] == o["label"]
                 and (tk["num"] is None or tk["num"] == o["num"])]
        if not cands:
            continue
        best = min(cands, key=lambda tk: abs(tk["t"] - o["t"]))
        if abs(best["t"] - o["t"]) > _MAX_PAIR_GAP_S:
            continue
        out.setdefault(o["num"], []).append({
            "label": o["label"], "orig_ts": o["t"], "track_ts": best["t"],
            "file": o["file"]})
    return out


def track_sync_groundtruth(labels_dir=None):
    """{track_num: {pairs, n, rate, rate_method, files}} for tracks with sync pairs.

    `rate` = mix seconds per original second (~1.0 same speed, <1 slowed, >1 sped
    up). Computed from the designated **A and B** sync points the way the sheet does
    (`(trackB − trackA) / (origB − origA)`, sheetscript/Code.js) when both exist;
    otherwise (no A/B) a least-squares slope over all pairs as a fallback. The A/B
    points are deliberate speed endpoints, so a fit over every point — which mixes
    separate sections — is wrong; use A/B when present.
    """
    points = parse_sync_points(labels_dir)
    gt = {}
    for num, pairs in points.items():
        pairs = sorted(pairs, key=lambda p: p["orig_ts"])
        # A/B endpoints must come from the SAME file (a coherent original/mix
        # segment): an A in one label file and a B in another are different sections
        # and pairing them gives a meaningless rate. Compute an A/B rate per file.
        by_file = {}
        for p in pairs:   # pairs sorted by orig_ts; last-wins keeps the later (e.g.
            by_file.setdefault(p["file"], {})[p["label"]] = p   # post-skip) A/B point
        seg_rates = []
        for fn, lab in by_file.items():
            a, b = lab.get("A"), lab.get("B")
            if a and b and abs(b["orig_ts"] - a["orig_ts"]) > 1e-3:
                seg_rates.append({"file": fn,
                                  "rate": (b["track_ts"] - a["track_ts"])
                                          / (b["orig_ts"] - a["orig_ts"])})
        rate, method = None, None
        if len(seg_rates) == 1:
            rate, method = seg_rates[0]["rate"], "AB"
        elif len(seg_rates) > 1:
            rate, method = float(np.median([s["rate"] for s in seg_rates])), "AB-multi"
        elif len(pairs) >= 2:
            o = np.array([p["orig_ts"] for p in pairs], dtype=float)
            t = np.array([p["track_ts"] for p in pairs], dtype=float)
            if o.max() - o.min() > 1e-3:
                rate, method = float(np.polyfit(o, t, 1)[0]), "fit"
        gt[num] = {"pairs": pairs, "n": len(pairs), "rate": rate,
                   "rate_method": method, "segment_rates": seg_rates,
                   "files": sorted(by_file)}
    return gt
