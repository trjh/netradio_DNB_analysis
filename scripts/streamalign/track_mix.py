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

from . import audio as _audio
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
    for fn in sorted(n for n in os.listdir(labels_dir) if _gt.is_pipeline_label_file(n)):
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


# --- T1: chroma + DTW original→mix aligner ------------------------------------
# Waveform correlation does not lock original-to-mix (DJ EQ + lossy 16 kHz mix vs a
# clean source); chroma is robust to timbre/EQ and subsequence-DTW finds where the
# played excerpt sits in the original, recovering the rate (warp-path slope). librosa
# is imported lazily so the rest of the module loads under the core (no-librosa)
# python; run callers with .venv/bin/python.

# Reliability gate (precision-first): a recovered rate is only trusted when the warp
# path is BOTH straight (high R²) AND a good chroma match (low per-frame DTW cost).
# Validated on tracks 8/10/13/16/23 against the sync ground truth (norm_cost measured
# at the selected subsequence endpoint): the two tracks within target (13: err 0.0019,
# 16: err 6e-5) clear conf≥0.999 & cost≤0.03; the three wrong/degenerate ones each
# fail a gate — track 23 (wrong-match) on both (conf 0.77, cost 0.050), track 10
# (degenerate slope=1.0) on confidence (0.99846 < 0.999), track 8 (empty mix → NaN
# slope) on the finite check. Both gates are load-bearing: confidence rejects the
# degenerate flat fit (which still scores low cost), the cost gate rejects the
# wrong-match. Thresholds are heuristic (5 graded tracks) — widen the validation set
# before tightening them (track 10's 0.99846-vs-0.999 margin is the tightest).
_MIN_CONFIDENCE = 0.999
_MAX_NORM_COST = 0.03


def is_reliable(confidence, norm_cost, slope):
    """The precision-first gate: trust a recovered rate only when the warp path is
    both straight (R² ≥ _MIN_CONFIDENCE) and a close chroma match (mean per-frame
    cost ≤ _MAX_NORM_COST), and the slope is finite. See the calibration note above."""
    return bool(np.isfinite(slope) and confidence >= _MIN_CONFIDENCE
                and norm_cost <= _MAX_NORM_COST)


def chroma_dtw_rate(orig, mix, sr=_audio.SR, hop=2048):
    """Recover the mix/orig rate by chroma + subsequence DTW.

    `orig` (full original track) and `mix` (the mix region where it plays) are mono
    float arrays. Returns {rate, offset_orig_s, confidence, norm_cost, n_path,
    reliable}: rate = d(mix_time)/d(orig_time) (the warp-path slope; ~1.0 same
    speed), offset_orig_s = where in the original the mix excerpt begins,
    `confidence` = warp-path R², `norm_cost` = mean per-frame DTW cost, `reliable` =
    the precision-first gate (both confidence and cost good enough to trust `rate`).
    """
    import librosa  # lazy: only T1 needs it (.venv)
    # Floor the chroma so silent frames aren't all-zero columns (cosine distance is
    # NaN on a zero-norm vector — e.g. track 8's quiet intro).
    co = librosa.feature.chroma_cqt(y=np.asarray(orig, dtype="float32"),
                                    sr=sr, hop_length=hop) + 1e-6
    cm = librosa.feature.chroma_cqt(y=np.asarray(mix, dtype="float32"),
                                    sr=sr, hop_length=hop) + 1e-6
    # Subsequence DTW finds the mix (the query X) WITHIN the original (the database Y),
    # which requires the mix to be no longer than the original. If the mix region is
    # longer (e.g. a master span that exceeds the source length — track 40), the
    # premise is violated: librosa reorients X/Y so `wp` no longer indexes `dist`
    # consistently, and any recovered rate would be meaningless. Flag and bail rather
    # than crash or emit a bogus rate.
    if cm.shape[1] > co.shape[1]:
        return {"rate": float("nan"), "offset_orig_s": 0.0, "confidence": 0.0,
                "norm_cost": float("nan"), "n_path": 0, "reliable": False,
                "note": "mix region longer than original (subsequence premise violated)"}
    # subsequence DTW: locate the mix (X) within the original (Y).
    dist, wp = librosa.sequence.dtw(X=cm, Y=co, subseq=True, metric="cosine")
    wp = wp[::-1].astype(float)          # ascending; columns: (mix_frame, orig_frame)
    n = len(wp)
    # Trim the subsequence-DTW boundary flats (the path often runs flat/vertical at
    # the ends while it "searches"), then take a robust (Theil-Sen) slope so a few
    # erratic segments don't swing the rate.
    lo, hi = int(0.1 * n), int(0.9 * n)
    seg = wp[lo:hi] if hi - lo > 10 else wp
    x, y = seg[:, 1], seg[:, 0]          # orig frames, mix frames
    from scipy import stats
    slope, intercept = stats.theilslopes(y, x)[:2]
    pred = slope * x + intercept
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - float(np.sum((y - pred) ** 2)) / ss_tot if ss_tot > 0 else 0.0
    # Cost at the SELECTED subsequence endpoint, not dist[-1, -1]: subseq DTW lets the
    # best match end before the original's last frame, so dist[-1, -1] scores aligning
    # the excerpt to the END of the original — a different alignment than `wp` — which
    # inflates the cost (and falsely flags) whenever the excerpt isn't end-aligned.
    end_i, end_j = int(wp[-1, 0]), int(wp[-1, 1])
    norm_cost = float(dist[end_i, end_j]) / max(1, n)
    return {"rate": float(slope), "offset_orig_s": float(wp[0, 1] * hop / sr),
            "confidence": r2, "norm_cost": norm_cost, "n_path": int(n),
            "reliable": is_reliable(r2, norm_cost, slope)}


def find_original(track_num, sources_dir):
    """Path to the original source file for a track number, or None.

    Looks for `<NNN>-*.{mp3,flac,m4a,opus,wav,aif,aiff}` in `sources_dir`.
    """
    prefix = "%03d-" % int(track_num)
    if not os.path.isdir(sources_dir):
        return None
    for fn in sorted(os.listdir(sources_dir)):
        if fn.startswith(prefix) and fn.rsplit(".", 1)[-1].lower() in (
                "mp3", "flac", "m4a", "opus", "wav", "aif", "aiff"):
            return os.path.join(sources_dir, fn)
    return None


def _select_capture(srcs, mb, me, starts, audio_dir=None):
    """Pick the source capture that actually CONTAINS the master span [mb, me].

    `source_files` is ordered by overlap, not by which capture holds the track, so
    `srcs[0]` often starts after (or ends before) the track's region — slicing it then
    yields an empty mix (the track 8/12/20/27/38 nan cases). Walk the candidates and
    take the first placed, audio-present capture whose [start, end] fully covers
    [mb, me]; fall back to the one with the largest overlap. Returns (cap, cstart) or
    (None, reason)."""
    sr = _audio.SR
    best, best_overlap, reason = None, 0.0, "no placed capture with audio"
    for s in srcs:
        cap = os.path.splitext(os.path.basename(s))[0]
        if cap not in starts:
            continue
        if not _audio.find_audio_file(cap, audio_dir):
            continue
        cstart = starts[cap]
        cend = cstart + len(_audio.load_audio(cap, audio_dir=audio_dir)) / sr
        if cstart <= mb and cend >= me:
            return cap, cstart                      # fully contains the span
        overlap = max(0.0, min(cend, me) - max(cstart, mb))
        if overlap > best_overlap:
            best, best_overlap, reason = (cap, cstart), overlap, "partial overlap only"
    if best is not None and best_overlap > 0:
        return best                                  # best partial (caller may flag)
    return None, reason


def align_track(track_num, track_metadata, sources_dir, labels_dir=None,
                audio_dir=None, hop=2048):
    """Chroma+DTW-align track NNN's original to its mix region; grade vs sync rate.

    `track_metadata` is the loaded track-metadata.json tracks dict. The mix region is
    the track's [master_begin, master_end] inside the `source_files` capture that
    actually contains that span (capture master start from the resolved hand
    placements; see `_select_capture`). Returns a dict with the recovered `rate`,
    `offset_orig_s`, the ground-truth `gt_rate`/`rate_method`, and `rate_err`; or
    {"error": ...} if inputs are missing.
    """
    e = track_metadata.get(str(track_num)) or {}
    mb, me = e.get("master_begin_seconds"), e.get("master_end_seconds")
    srcs = e.get("source_files") or []
    if mb is None or me is None or not srcs:
        return {"error": "no master span / source_files"}
    orig_path = find_original(track_num, sources_dir)
    if not orig_path:
        return {"error": "no original audio"}
    starts = _gt.resolve_starts(labels_dir)
    cap, cstart = _select_capture(srcs, mb, me, starts, audio_dir=audio_dir)
    if cap is None:
        return {"error": "no source capture contains span (%s)" % cstart}
    cap_audio = _audio.load_audio(cap, audio_dir=audio_dir)
    sr = _audio.SR
    mix = cap_audio[int((mb - cstart) * sr):int((me - cstart) * sr)]
    orig = _audio.load_audio(orig_path, audio_dir=audio_dir)
    r = chroma_dtw_rate(orig, mix, sr=sr, hop=hop)
    g = track_sync_groundtruth(labels_dir).get(int(track_num), {})
    r.update({"track": int(track_num), "capture": cap, "gt_rate": g.get("rate"),
              "rate_method": g.get("rate_method"),
              "rate_err": (abs(r["rate"] - g["rate"]) if g.get("rate") else None)})
    return r


def batch_align(track_metadata, sources_dir, labels_dir=None, audio_dir=None,
                hop=2048, tracks=None, rate_tol=0.005):
    """G2 1st pass at scale: align every synced track that has an original + capture.

    `tracks` limits to a subset (default: every track with a T0 sync `rate`). Each
    result is `align_track`'s dict plus `within_tol` (reliable AND |rate_err| ≤
    `rate_tol`). Returns {results, reliable, within_tol, flagged, no_original,
    errored} where the lists hold track numbers — `no_original` is the G4 signal
    (synced track, no source file)."""
    gt = track_sync_groundtruth(labels_dir)
    nums = tracks if tracks is not None else sorted(
        k for k, v in gt.items() if v.get("rate") is not None)
    results, reliable, within, flagged, no_orig, errored = [], [], [], [], [], []
    for tn in nums:
        if find_original(tn, sources_dir) is None:
            no_orig.append(tn)
            continue
        r = align_track(tn, track_metadata, sources_dir, labels_dir=labels_dir,
                        audio_dir=audio_dir, hop=hop)
        if "error" in r:
            errored.append(tn)
            results.append({"track": tn, "error": r["error"]})
            continue
        err = r.get("rate_err")
        r["within_tol"] = bool(r.get("reliable") and err is not None and err <= rate_tol)
        results.append(r)
        (reliable if r.get("reliable") else flagged).append(tn)
        if r["within_tol"]:
            within.append(tn)
    return {"results": results, "reliable": reliable, "within_tol": within,
            "flagged": flagged, "no_original": no_orig, "errored": errored}
