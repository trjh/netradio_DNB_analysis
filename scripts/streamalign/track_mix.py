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

_ORIG_RE = re.compile(r"\borig(\d+)\s+sync:\s*(\S+)", re.I)
_TRACK_RE = re.compile(r"\btrack\s+sync:\s*(\S+)", re.I)
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
        m = _ORIG_RE.search(r["text"])
        if m:
            origs.append({"num": int(m.group(1)), "label": m.group(2),
                          "t": r["t"], "file": r["file"]})
            continue
        m = _TRACK_RE.search(r["text"])
        if m:
            tracks.append({"label": m.group(1), "t": r["t"], "file": r["file"]})
    out = {}
    for o in origs:
        cands = [tk for tk in tracks
                 if tk["file"] == o["file"] and tk["label"] == o["label"]]
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
    """{track_num: {pairs, n, rate, files}} for tracks with >=1 sync pair.

    `rate` = least-squares slope of track_ts vs orig_ts (mix seconds per original
    second; ~1.0 means same speed, <1 slowed, >1 sped up). None with <2 pairs.
    """
    points = parse_sync_points(labels_dir)
    gt = {}
    for num, pairs in points.items():
        pairs = sorted(pairs, key=lambda p: p["orig_ts"])
        rate = None
        if len(pairs) >= 2:
            o = np.array([p["orig_ts"] for p in pairs], dtype=float)
            t = np.array([p["track_ts"] for p in pairs], dtype=float)
            if o.max() - o.min() > 1e-3:
                rate = float(np.polyfit(o, t, 1)[0])
        gt[num] = {"pairs": pairs, "n": len(pairs), "rate": rate,
                   "files": sorted({p["file"] for p in pairs})}
    return gt
