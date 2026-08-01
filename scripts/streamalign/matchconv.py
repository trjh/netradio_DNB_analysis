"""MATCH path -> PHAT-refined align points -> paired hints files (align-tool Pass 1).

The Sonic Visualiser + MATCH flow gives the labeller a rate-aware *coarse map* of where an
original sits inside a capture, but (gate 0, 2026-07-31) the raw MATCH path is NOT an anchor
source: its online DTW is forced to start the two files together, so on a mix that contains
a record somewhere in its middle the path lands seconds off and reports the wrong rate. What
does work, verified against a hand-seated pair to ~10 ms: use the path only to seed a coarse
(offset, rate) guess, then

  1. sweep the rate around the coarse guess, scoring each candidate by GCC-PHAT confidence
     (an un-corrected 2% rate error smears the correlation to conf ~= 0, so the confidence
     peak IS the rate detector);
  2. walk an anchor grid across the overlap: short rate-corrected windows, `refine_offset`
     at both polarities (originals can be, and are, polarity-inverted against the stream);
  3. re-fit the rate from the high-confidence anchors and re-walk once if it moved;
  4. reject anchors that sit off the locally-smooth offset curve (drum&bass self-similarity
     lets a window lock onto the wrong loop repetition with respectable confidence);
  5. keep the best spread-out survivors and emit them as paired hint rows.

Converter-trust additions (2026-08, AP-02/03/04/13/16):

  * **Trim before MATCH (AP-02).** MATCH's forced files-start-together assumption is made
    approximately true by trimming the stream WAV to the expected overlap (`trim_window`)
    before sonic-annotator runs; `apply_trim_offset` shifts the path back onto the full
    capture's clock. The expected position auto-derives from the track's master span and
    the capture's resolved master start (`derive_around`), or comes from `--around`.
  * **MATCH as referee (AP-03).** PHAT remains the ONLY anchor source; the (trimmed) MATCH
    path's implied original position is evaluated at each selected anchor
    (`referee_deltas`) and the delta reported per anchor -- a disagreement beyond
    REFEREE_TOL_S becomes a `note QUESTION:` row.
  * **`verified` token (AP-04).** Emitted sync rows read `track sync: 1 verified
    confidence 5.9/10 HINT` -- the token, immediately after the marker, is what the sheet
    and `sync-audit` recognise as "machine-checked". Distinct from (and never to be
    conflated with) the file-sync `verified <neighbour>` keyword.
  * **Solo-anchor probe seeding (AP-13).** Where librosa is available the rate sweep also
    probes the record-playing-alone moments (`track_mix.solo_anchors`) -- the instants
    PHAT locks best -- instead of only blind fractions of the overlap.
  * **Batch worklist (AP-16).** `tracks_overlapping` lists every track whose master span
    overlaps a capture, for `match-hints <stem> --all`.

Output is two `.hints.tsv` files in the existing grammar (never `.labels.tsv` -- written
through `hints.write_hints`, which enforces that): `track sync: <k>` rows at capture-local
times for the stream, `orig<NNN> sync: <k>` rows at original-local times (native rate) for
the original, plus proposed `orig<NNN> start:`/`end:` rows and a summary note carrying the
recovered rate and polarity. The human folds accepted rows into the hand labels in Audacity;
nothing here writes a label file.
"""

import os
import shutil
import subprocess
import wave

import numpy as np

from . import audio as _audio
from . import align as _align
from . import hints as _hints

SR = _audio.SR                 # all analysis at the stream's native 16 kHz
DEFAULT_WIN_S = 6.0            # PHAT window per anchor (gate 0: locks well, drift-tolerant)
DEFAULT_STEP_S = 10.0          # anchor grid spacing
GRID_RADIUS_S = 0.35           # per-anchor search radius once seeded (covers drift + reseed slack)
SWEEP_RADIUS_S = 45.0          # rate-sweep search radius: MATCH's absolute placement can be
                               # tens of seconds off on a full mix (gate 0 measured ~36 s)
RATE_SPAN = 0.05               # sweep rates within +/-5% of the coarse guess (DJ pitch range)
RATE_STEP = 0.005              # coarse sweep step; the anchor refit recovers the rest
RATE_PRIOR = (0.90, 1.10)      # a DJ pitches by a few percent; slopes outside this are noise
MIN_CONF = 0.15                # an anchor below this is a guess, not a hint
OUTLIER_TOL_S = 0.25           # offset residual vs the local median that marks a loop-skip
TRIM_MARGIN_S = 60.0           # AP-02: slack either side of the expected overlap when trimming
REFEREE_TOL_S = 0.25           # AP-03: MATCH-vs-PHAT disagreement that earns a QUESTION row
REFEREE_GROSS_S = 5.0          # AP-03: a MEDIAN delta beyond this is a globally-off MATCH
                               # path (its forced-start failure), not 8 separate disputes


def parse_ab_csv(path):
    """[(t_stream, t_orig)] from a sonic-annotator match:a_b CSV (seconds, 20 ms hops).

    Units note (gate 0): `-m` multiplex resamples both files to the FIRST file's rate, so
    with the stream given first the times on both columns are seconds on the stream's own
    clock -- no sample-rate conversion is owed here.
    """
    pairs = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            a, b = line.split(",")[:2]
            pairs.append((float(a), float(b)))
    return pairs


def trim_window(around_s, orig_len_s, stream_len_s, rate_guess=1.0, margin_s=TRIM_MARGIN_S):
    """(lo_s, hi_s) of the capture worth handing to MATCH, given a rough start (AP-02).

    MATCH's headless online DTW is forced to start the two files together, so on a full
    20-minute mix its path lands seconds-to-tens-of-seconds off. Trimming the stream to
    where the original is *expected* makes that forced assumption approximately true:
    `around_s` is the rough capture-local position of the original's START, and the kept
    window runs from `margin_s` before it to the original's expected end (its length on
    the stream clock, `orig_len_s / rate_guess`) plus `margin_s` after. The margins absorb
    both the roughness of `around_s` (a track's metadata position is where it becomes
    audible, not where the record's local 0 sits) and a few percent of DJ pitch.
    """
    lo = max(0.0, float(around_s) - margin_s)
    hi = min(float(stream_len_s), float(around_s) + float(orig_len_s) / rate_guess + margin_s)
    return lo, hi


def apply_trim_offset(pairs, lo_s):
    """Shift a trimmed run's a_b path back onto the FULL capture's clock (AP-02).

    MATCH saw only stream[lo:hi], so its `a` column is seconds into the trimmed WAV;
    adding `lo_s` restores capture-local time and everything downstream (coarse map,
    sweep, grid, emission) is unchanged.
    """
    lo_s = float(lo_s)
    return [(a + lo_s, b) for a, b in pairs]


def derive_around(orig_num, tracks_meta, capture_master_start):
    """Auto-derive `--around`: the track's expected capture-local position, or None (AP-02).

    Expected local position = the track's master-timeline position (build_track_metadata's
    `master_begin_seconds`, itself resolved from the hand `start<NNN>:` label rows) minus
    the capture's resolved master start (`groundtruth.resolve_starts`). None when either
    side is unknown -- the caller then runs untrimmed, exactly as before.
    """
    if capture_master_start is None:
        return None
    entry = (tracks_meta or {}).get(str(int(orig_num))) or {}
    begin = entry.get("master_begin_seconds")
    if begin is None:
        return None
    return float(begin) - float(capture_master_start)


def tracks_overlapping(capture_master_start, capture_len_s, tracks_meta):
    """[(num, master_begin_s, master_end_s)] for every track overlapping the capture (AP-16).

    Overlap is judged on the master timeline: the track's [master_begin, master_end] span
    (build_track_metadata output) against [capture_start, capture_start + length]. This is
    the batch-mode worklist -- the caller still drops tracks with no original on disk.
    """
    if capture_master_start is None:
        return []
    lo = float(capture_master_start)
    hi = lo + float(capture_len_s)
    out = []
    for num, entry in (tracks_meta or {}).items():
        if not str(num).isdigit():
            continue
        mb = entry.get("master_begin_seconds")
        me = entry.get("master_end_seconds")
        if mb is None or me is None:
            continue
        if float(me) > lo and float(mb) < hi:
            out.append((int(num), float(mb), float(me)))
    return sorted(out)


def match_predict(pairs, a_s):
    """The (trimmed) MATCH path's own implied original-native seconds at stream `a_s`.

    Linear interpolation over the a_b rows; None outside the path's span. This is the
    quantity AP-03 referees against the PHAT anchors -- MATCH stays a coarse map, never
    an anchor source, but where the two disagree the labeller should hear about it.
    """
    if not pairs:
        return None
    xs = np.array([p[0] for p in pairs], dtype=float)
    ys = np.array([p[1] for p in pairs], dtype=float)
    if a_s < xs[0] or a_s > xs[-1]:
        return None
    return float(np.interp(float(a_s), xs, ys))


def referee_deltas(pairs, anchors, rate):
    """Per selected anchor: MATCH-implied minus PHAT-implied original position (AP-03).

    PHAT's implied original-native position at an anchor is `(a - off) * rate`; MATCH's
    is `match_predict(a)`. Returns one delta (seconds, or None where the MATCH path does
    not cover the anchor) per anchor, anchor order preserved.
    """
    out = []
    for a, off, _conf, _inv, _out in anchors:
        m = match_predict(pairs, a)
        out.append(None if m is None else float(m - (a - off) * rate))
    return out


def coarse_map(pairs, orig_len_s=None):
    """Robust (offset_s, rate) seed from a MATCH a_b path.

    A real full-mix path is mostly garbage: the head is forced through (0,0), and past
    the original's end the a_b output simply tracks the diagonal (b keeps growing to the
    stream's own length). So: keep only rows whose b lies inside the original's span,
    measure local slopes over ~2 s spans, and trust only the rows whose slope is
    DJ-plausible (RATE_PRIOR) -- the "locked" stretch of the path. The result only has to
    land within the sweep's capture range (rate +/-RATE_SPAN, offset +/-SWEEP_RADIUS_S).
    """
    if len(pairs) < 10:
        raise ValueError("MATCH path too short to seed a coarse map (%d rows)" % len(pairs))
    rows = [(a, b) for a, b in pairs if b > 1.0
            and (orig_len_s is None or b < orig_len_s - 1.0)]
    if len(rows) < 10:
        rows = list(pairs)
    hop = max(1e-3, (rows[-1][0] - rows[0][0]) / max(1, len(rows) - 1))
    span = max(1, int(round(2.0 / hop)))            # local-slope baseline ~2 s of stream
    locked = []
    slopes = []
    for i in range(len(rows) - span):
        a0, b0 = rows[i]
        a1, b1 = rows[i + span]
        da = a1 - a0
        if da <= 0.5:
            continue
        slope = (b1 - b0) / da
        if RATE_PRIOR[0] <= slope <= RATE_PRIOR[1]:
            locked.append(b0 - a0)
            slopes.append(slope)
    if locked:
        offset = float(np.median(locked))
        rate = float(np.median(slopes))
    else:                                           # nothing locked: neutral-rate fallback
        offset = float(np.median([b - a for a, b in rows]))
        rate = 1.0
    return offset, min(max(rate, RATE_PRIOR[0]), RATE_PRIOR[1])


def resample_by_rate(orig, rate):
    """orig played at `rate` original-seconds per stream-second (linear interp).

    After this, one sample of the result spans the same wall-clock as one stream sample,
    so a constant offset models the whole (locally rate-stable) overlap.
    """
    idx = np.arange(0, len(orig), rate)
    idx = idx[idx < len(orig) - 1]
    return np.interp(idx, np.arange(len(orig)), orig).astype(np.float32)


def _refine_peaks(stream, orig2, a_s, b2_s, win_s, radius_s, n_peaks=1):
    """Windowed refine at both signs. Returns [(offset_s, conf>=0, inverted)], best first.

    `refine_offset`'s confidence is the signed normalised correlation at the peak, so a
    polarity-inverted match shows up as a strong NEGATIVE conf on the + sign (and vice
    versa); we try both signs and read inversion off which combination won. With
    n_peaks > 1 the runner-up peaks come back too -- on loop-based material the true seat
    is not always the tallest peak in a wide search window.
    """
    a0 = int((a_s - win_s / 2 - 1) * SR)
    a1 = int((a_s + win_s / 2 + 1) * SR)
    if a0 < 0 or a1 > len(stream):
        return []
    # the b slice is clamped, not rejected: a wide search radius (the sweep uses tens of
    # seconds) legitimately overruns a short original's ends, and refine_offset's offset
    # math is derived from the actual slice starts either way
    b0 = max(0, int((b2_s - win_s / 2 - 1 - radius_s) * SR))
    b1 = min(len(orig2), int((b2_s + win_s / 2 + 1 + radius_s) * SR))
    if b1 - b0 < int((win_s + 2) * SR):
        return []
    around = int(round((a_s - b2_s) * SR))
    found = []
    for sign, seg in ((1, orig2[b0:b1]), (-1, -orig2[b0:b1])):
        for off, conf in _align.refine_offset_multi(
                stream[a0:a1], seg, around - (a0 - b0),
                radius=int(radius_s * SR), win=int(win_s * SR), n_peaks=n_peaks):
            inverted = (sign < 0) == (conf > 0)
            found.append(((off + (a0 - b0)) / SR, abs(float(conf)), inverted))
    found.sort(key=lambda p: -p[1])
    return found


def _refine_both_polarities(stream, orig2, a_s, b2_s, win_s, radius_s):
    """Best single seat at this window: (offset_s, conf>=0, inverted) or None."""
    peaks = _refine_peaks(stream, orig2, a_s, b2_s, win_s, radius_s, n_peaks=1)
    return peaks[0] if peaks else None


def solo_probe_positions(stream, orig, offset0, top=4):
    """Capture-local instants where the original plays ALONE, to seed the sweep (AP-13).

    The rate sweep's default probes are blind fractions of the coarse overlap; a probe
    that lands on heavily layered audio wastes its window. `track_mix.solo_anchors`
    (chroma+DTW) finds the record-playing-alone moments -- exactly where GCC-PHAT locks
    best -- so those become additional, preferred probe positions. Best-effort: needs
    librosa (the .venv), and any failure just returns [] (the blind fractions still run).
    The search window is the coarse overlap widened by the sweep radius, because the
    coarse map can be tens of seconds off.
    """
    orig_native_len_s = len(orig) / SR
    lo = max(0.0, -offset0 - SWEEP_RADIUS_S)
    hi = min(len(stream) / SR, orig_native_len_s - offset0 + SWEEP_RADIUS_S)
    if hi - lo < DEFAULT_WIN_S * 3:
        return []
    try:
        from . import track_mix as _tm
        anchors = _tm.solo_anchors(orig, stream, lo, hi, top=top)
    except Exception:
        return []
    return sorted(float(a["mix_s"]) for a in anchors)


def sweep_rate(stream, orig, offset0, rate0, span=RATE_SPAN, step=RATE_STEP,
               win_s=DEFAULT_WIN_S, radius_s=SWEEP_RADIUS_S, probe_positions=None):
    """Find the rate by confidence peak: try candidates, PHAT-probe mid-overlap, keep the best.

    `offset0` is the MATCH-convention delta (median of b - a: original-native seconds ahead
    of the stream clock). Returns up to three candidate seats as
    [(rate, offset_s, score, inverted)], best first, offsets in this module's PHAT
    convention -- off = a - b2, b2 being rate-corrected original time.

    Two defences live here. Rate: an un-corrected 2% error smears the whole window, so
    only near-true rate candidates correlate at all. Position: loop-based material (all of
    drum & bass) can correlate strongly at a WRONG, loop-shifted position, so a candidate's
    score is the summed confidence of probes that AGREE on an offset -- and the runner-up
    seats are returned too, for the caller to disambiguate by grid coverage.

    `probe_positions` (AP-13): extra capture-local probe instants -- typically the
    solo-anchor moments from `solo_probe_positions` -- tried IN ADDITION to the blind
    fractions of the overlap (positions outside the coarse overlap are dropped).
    """
    orig_native_len_s = len(orig) / SR
    # stream instants covered by the original under the coarse map: b = a + offset0
    lo = max(0.0, -offset0)
    hi = min(len(stream) / SR, orig_native_len_s - offset0)
    if hi - lo < win_s * 3:
        raise ValueError("rate sweep found no usable probe window (overlap too small?)")
    positions = [lo + (hi - lo) * frac for frac in (0.25, 0.4, 0.55, 0.7)]
    positions += [float(p) for p in (probe_positions or []) if lo <= p <= hi]
    probes = []
    for rate in np.arange(rate0 - span, rate0 + span + step / 2, step):
        if rate <= 0.5:
            continue
        orig2 = resample_by_rate(orig, rate)
        for a_s in positions:
            b2_s = (a_s + offset0) / rate       # coarse original position, rate-corrected
            for off_s, conf, inverted in _refine_peaks(
                    stream, orig2, a_s, b2_s, win_s, radius_s, n_peaks=4):
                if conf > 0.02:
                    probes.append((float(rate), off_s, conf, inverted))
    if not probes:
        raise ValueError("rate sweep found no usable probe window (overlap too small?)")
    # cluster by offset agreement, anchored to each cluster's best member -- NOT by
    # nearest-neighbour chaining, which (with multi-peak probes every bar or two apart)
    # merges every distinct seat into one mega-cluster and erases the true one
    probes.sort(key=lambda p: -p[2])
    clusters = []
    for p in probes:
        for cl in clusters:
            if abs(p[1] - cl[0][1]) <= 2.5:
                cl.append(p)
                break
        else:
            clusters.append([p])
    out = []
    for cl in clusters:
        score = sum(p[2] for p in cl)
        top = cl[0]
        inv = sum(1 for p in cl if p[3]) > len(cl) / 2
        out.append((top[0], top[1], score, inv))
    out.sort(key=lambda c: -c[2])
    # every cluster is a candidate seat: on loop-based material the true seat's probes can
    # score BELOW a loop-shifted seat's (a 4-bar shift correlates almost everywhere on a
    # minimal arrangement), so ranking here is only provisional -- the caller walks each
    # candidate and lets whole-overlap anchor mass decide. Cap only to bound the walks.
    return out[:6]


def anchor_grid(stream, orig2, offset_seed, step_s=DEFAULT_STEP_S, win_s=DEFAULT_WIN_S,
                radius_s=GRID_RADIUS_S):
    """[(a_s, offset_s, conf, inverted)] every `step_s` across the overlap.

    Each anchor is seeded from the previous accepted offset, so the search follows the
    DJ's slow pitch-riding (the offset drifts smoothly) instead of assuming one constant.
    """
    rows = []
    offset = offset_seed
    lo = max(win_s, offset + win_s)
    hi = min(len(stream) / SR, len(orig2) / SR + offset) - win_s
    a_s = lo
    while a_s <= hi:
        got = _refine_both_polarities(stream, orig2, a_s, a_s - offset, win_s, radius_s)
        if got is not None:
            off_s, conf, inverted = got
            rows.append((float(a_s), off_s, conf, inverted))
            if conf >= MIN_CONF:
                offset = off_s          # track the drift
        a_s += step_s
    return rows


def refit_rate(marked, rate):
    """Confidence-weighted least-squares slope of orig-native-time vs stream-time.

    The sweep's grid is RATE_STEP-coarse; the anchors themselves measure the true average
    rate far more precisely. b_native(a) = (a - off(a)) * rate, so the fitted slope of
    b_native against a IS the corrected rate. Fits only the in-curve anchors (an outlier
    would drag the slope), thresholded *relative* to the run's best confidence -- absolute
    confidence varies wildly with material (0.7 on a clean record, far less on heavily
    layered audio) while the peak positions stay solid.
    """
    if not marked:
        return rate
    top = max(m[2] for m in marked)
    floor = max(MIN_CONF * 0.2, 0.5 * top)
    good = [(a, (a - off) * rate, conf)
            for a, off, conf, _inv, out in marked if not out and conf >= floor]
    if len(good) < 3:
        return rate
    xs = np.array([g[0] for g in good])
    ys = np.array([g[1] for g in good])
    ws = np.array([g[2] for g in good])
    xm = np.average(xs, weights=ws)
    ym = np.average(ys, weights=ws)
    slope = float(np.sum(ws * (xs - xm) * (ys - ym)) / np.sum(ws * (xs - xm) ** 2))
    return slope


def mark_outliers(anchors, tol_s=OUTLIER_TOL_S, k=5):
    """[(a, off, conf, inverted, is_outlier)] -- outlier = off the local median curve.

    Confidence alone cannot be trusted: a window can lock onto the wrong repetition of a
    loop with conf ~0.3 while sitting ~0.6 s off the true (smooth) offset curve. The local
    median of the neighbours is the curve; excursions beyond `tol_s` are marked.
    """
    out = []
    offs = [a[1] for a in anchors]
    for i, (a, off, conf, inv) in enumerate(anchors):
        lo = max(0, i - k // 2)
        window = offs[lo:lo + k]
        med = float(np.median(window))
        out.append((a, off, conf, inv, abs(off - med) > tol_s))
    return out


def select_anchors(marked, count):
    """Best `count` spread-out, in-curve, confident anchors (ascending stream time).

    Spread matters more than raw confidence: the labeller wants anchors early AND late
    (the pair encodes the rate), so selection is per-bucket best rather than global top-K.
    """
    ok = [m for m in marked if not m[4] and m[2] >= MIN_CONF]
    if not ok or count <= 0:
        return []
    count = min(count, len(ok))
    lo, hi = ok[0][0], ok[-1][0]
    width = (hi - lo) / count if hi > lo else 1.0
    picked = []
    for b in range(count):
        b_lo = lo + b * width
        b_hi = b_lo + width if b < count - 1 else hi + 1
        bucket = [m for m in ok if b_lo <= m[0] < b_hi]
        if bucket:
            picked.append(max(bucket, key=lambda m: m[2]))
    return sorted(picked, key=lambda m: m[0])


def build_rows(orig_num, anchors, rate, inverted, orig_native_len_s, stream_len_s,
               coverage=None, ambiguous=False, match_deltas=None):
    """(stream_rows, orig_rows) hint rows for the two files, existing grammar only.

    Stream rows are capture-local seconds; orig rows are original-local seconds at the
    original's native rate (b_native = (a - off) * rate -- rate-corrected coordinates map
    back to native seconds by construction, whatever the native sample rate is).

    Sync rows carry the ` verified` token immediately after the marker (AP-04) -- the
    machine-checked mark the sheet/audit plumbing keys on; it is free text to the sync-row
    grammar, and deliberately NOT the file-sync `verified <neighbour>` keyword (a different,
    load-bearing thing). `match_deltas` (AP-03, from `referee_deltas`) appends each anchor's
    MATCH-vs-PHAT delta to its row text; a disagreement beyond REFEREE_TOL_S earns a
    `note QUESTION:` row at that anchor -- unless the MEDIAN delta is itself beyond
    REFEREE_GROSS_S, in which case the MATCH path is globally off (its forced-start
    failure mode, measured at -40 s on the d376-395/072 gate pair) and ONE question about
    the whole path replaces a per-anchor pile-up that would all say the same thing.
    """
    tag = "orig%03d" % int(orig_num)
    stream_rows, orig_rows = [], []
    finite = [d for d in (match_deltas or []) if d is not None]
    med_delta = float(np.median(finite)) if finite else 0.0
    grossly_off = abs(med_delta) > REFEREE_GROSS_S
    for k, (a, off, conf, _inv, _out) in enumerate(anchors, start=1):
        b_native = (a - off) * rate
        delta = match_deltas[k - 1] if match_deltas and k <= len(match_deltas) else None
        extra = "" if delta is None else " MATCH %+.3fs" % delta
        stream_rows.append(_hints._row(
            a, a, "track sync: %d verified %s%s" % (k, _hints._conf(conf), extra)))
        orig_rows.append(_hints._row(
            b_native, b_native,
            "%s sync: %d verified %s%s" % (tag, k, _hints._conf(conf), extra)))
        if delta is not None and abs(delta) > REFEREE_TOL_S and not grossly_off:
            stream_rows.append(_hints._question(
                a, a,
                "MATCH and PHAT disagree at sync %d: the MATCH path puts %s %+.3f s away "
                "from the PHAT anchor's position. PHAT stays primary; check this anchor "
                "by ear before trusting either." % (k, tag, delta)))
    if grossly_off and anchors:
        stream_rows.append(_hints._question(
            anchors[0][0], anchors[-1][0],
            "the MATCH path disagrees with the PHAT anchors by a median %+.1f s across "
            "the whole overlap -- MATCH's forced files-start-together failure mode, not "
            "%d separate disputes. PHAT stays primary; the per-anchor MATCH deltas are "
            "relative to that globally-off path." % (med_delta, len(anchors))))
    # proposed head/tail of the original on the capture's timeline, from the nearest anchor
    # (b2 = a - off, so original local 0 sits at a = off; its end at a = off + len/rate)
    if anchors:
        start_s = anchors[0][1]
        end_s = anchors[-1][1] + orig_native_len_s / rate
        if start_s >= 0:
            stream_rows.append(_hints._row(start_s, start_s, "%s start: ? %s" % (
                tag, _hints._conf(anchors[0][2]))))
        else:
            stream_rows.append(_hints._question(
                0.0, 0.0, "%s starts %.3f s BEFORE this capture's first sample" % (
                    tag, -start_s)))
        if end_s <= stream_len_s:
            stream_rows.append(_hints._row(end_s, end_s, "%s end: ? %s" % (
                tag, _hints._conf(anchors[-1][2]))))
        else:
            stream_rows.append(_hints._question(
                stream_len_s, stream_len_s,
                "%s ends %.3f s AFTER this capture's last sample" % (tag, end_s - stream_len_s)))
    pol = "INVERTED (flip the original before a null test)" if inverted else "not inverted"
    summary = ("%s via MATCH+PHAT: rate %.5f (original runs %+.2f%% vs stream), polarity %s, "
               "%d anchors" % (tag, rate, (rate - 1.0) * 100.0, pol, len(anchors)))
    stream_rows.append(_hints._hint(0.0, 0.0, summary))
    orig_rows.append(_hints._hint(0.0, 0.0, summary + "; times are original-local seconds"))
    low_cov = coverage is not None and coverage < 0.5
    if (low_cov or ambiguous) and anchors:
        # the loop trap's smoke: thin coverage, or a rival seat nearly as strong, can mean
        # the whole alignment locked onto the wrong repetition of a loop
        why = ("cover only %d%% of the possible overlap" % round(coverage * 100)
               if low_cov else "have a rival whole-bar-shifted seat almost as strong")
        stream_rows.append(_hints._question(
            anchors[0][0], anchors[-1][0],
            "%s anchors %s -- on loop-heavy material this can be a loop-shifted "
            "(wrong-repetition) alignment; verify one anchor at a structurally unique "
            "moment (intro/breakdown) by ear" % (tag, why)))
    return stream_rows, orig_rows


def write_wav16(path, arr, sr=SR):
    """Mono 16-bit WAV at `sr` from a float array -- the decode sonic-annotator can't do.

    The stream captures are mp3 and many originals are WavPack/opus; sonic-annotator's own
    decoders don't cover those, so the export runs on ffmpeg-decoded (via `load_audio`)
    16 kHz mono WAVs. That is also exactly the signal PHAT analyses -- one decode, one truth
    (the field guide's "align against the same decoded files the converter will read").
    """
    x = np.asarray(arr, dtype=np.float32)
    peak = float(np.max(np.abs(x))) if len(x) else 0.0
    if peak > 1.0:
        x = x / peak
    pcm = (x * 32767.0).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return path


def hint_filenames(stem, orig_num):
    """(stream_side, orig_side) output names for one capture+original run.

    BOTH carry the capture stem: hint writes atomically replace, so an
    original-side name without the stem would be silently clobbered the next
    time the same original is aligned inside a different capture.
    """
    return ("%s.orig%03d.match.hints.tsv" % (stem, int(orig_num)),
            "orig%03d.%s.match.hints.tsv" % (int(orig_num), stem))


def sonic_annotator_argv(stream_wav, orig_wav, out_dir, exe="sonic-annotator"):
    """The fixed argv for the a_b export. One list, no shell, nothing interpolated."""
    return [exe, "-m", "-d", "vamp:match-vamp-plugin:match:a_b",
            stream_wav, orig_wav, "-w", "csv", "--csv-basedir", out_dir, "--csv-force"]


def run_match(stream_wav, orig_wav, out_dir, exe=None):
    """Run the headless MATCH export; returns the a_b CSV path. Needs sonic-annotator."""
    exe = exe or shutil.which("sonic-annotator")
    if not exe:
        raise FileNotFoundError(
            "sonic-annotator not on PATH; pass --csv with a pre-exported a_b CSV instead")
    subprocess.run(sonic_annotator_argv(stream_wav, orig_wav, out_dir, exe),
                   check=True, capture_output=True)
    base = os.path.splitext(os.path.basename(stream_wav))[0]
    path = os.path.join(out_dir, base + "_vamp_match-vamp-plugin_match_a_b.csv")
    if not os.path.exists(path):
        raise FileNotFoundError("MATCH ran but %s was not produced" % path)
    return path


def convert(stream, orig, csv_pairs, anchor_count=8,
            step_s=DEFAULT_STEP_S, win_s=DEFAULT_WIN_S, solo_probes=False):
    """Core pipeline on decoded 16 kHz mono arrays. Returns a result dict (no I/O).

    `csv_pairs` is the parsed MATCH a_b path (already shifted back onto the full
    capture's clock if the run was trimmed -- see `apply_trim_offset`); `stream`/`orig`
    are float arrays at SR. `solo_probes=True` (AP-13) seeds the rate sweep with the
    solo-anchor moments (needs librosa; silently skipped without it). The result carries
    `match_deltas` (AP-03): the MATCH path's own disagreement with each selected anchor.
    """
    offset0, rate0 = coarse_map(csv_pairs, orig_len_s=len(orig) / SR)
    probe_positions = solo_probe_positions(stream, orig, offset0) if solo_probes else []
    candidates = sweep_rate(stream, orig, offset0, rate0, win_s=win_s,
                            probe_positions=probe_positions)

    def _walk(rate, seed):
        marked = []
        for _pass in range(3):
            # walk the grid at `rate`; the anchors' offsets live in THIS rate's
            # coordinates, so the emitted mapping b_native = (a - off) * rate stays
            # self-consistent and each pass's refit only decides whether another walk
            # is worth it.
            orig2 = resample_by_rate(orig, rate)
            grid = anchor_grid(stream, orig2, offset_seed=seed, step_s=step_s, win_s=win_s)
            marked = mark_outliers(grid)
            rate2 = refit_rate(marked, rate)
            if not marked or abs(rate2 - rate) / rate <= 1e-4:
                break
            # transform the best anchor's offset into the new rate's coordinates so the
            # next walk's seed stays inside the small search radius:
            # b2' = b_native / rate' = (a - off) * rate / rate'  =>  off' = a - b2'
            ref = max((m for m in marked if not m[4]), key=lambda m: m[2], default=None)
            if ref is not None:
                seed = ref[0] - (ref[0] - ref[1]) * rate / rate2
            rate = rate2
        usable = [m for m in marked if not m[4] and m[2] >= MIN_CONF]
        # anchor-mass, the loop-trap disambiguator: a loop-shifted seat correlates only
        # inside the looped section, the true seat everywhere the record plays -- so the
        # seat whose anchors cover more of the overlap (weighted by confidence) wins.
        mass = sum(m[2] for m in usable) * (1.0 + (usable[-1][0] - usable[0][0]) / 100.0
                                            if len(usable) > 1 else 1.0)
        return rate, marked, mass

    walked = []
    for rate_c, off_c, score_c, _inv_c in candidates:
        rate_w, marked_w, mass_w = _walk(rate_c, off_c)
        walked.append((mass_w, rate_w, marked_w, score_c))
    walked.sort(key=lambda w: -w[0])
    mass, rate, marked, sweep_score = walked[0]
    # a runner-up seat nearly as massive as the winner is the loop trap saying hello:
    # the two seats differ by whole bars and only a structurally unique moment separates
    # them -- surface it rather than silently picking one.
    ambiguous = len(walked) > 1 and walked[1][0] > 0.8 * mass
    picked = select_anchors(marked, anchor_count)
    inverted_votes = [m[3] for m in picked] or [m[3] for m in marked]
    inverted = sum(inverted_votes) > len(inverted_votes) / 2
    usable = [m for m in marked if not m[4] and m[2] >= MIN_CONF]
    theoretical = min(len(stream) / SR, len(orig) / SR / rate)
    coverage = ((usable[-1][0] - usable[0][0]) / theoretical) if len(usable) > 1 else 0.0
    return {
        "rate": float(rate),
        "sweep_conf": float(sweep_score),
        "inverted": bool(inverted),
        "grid": marked,
        "anchors": picked,
        "coverage": float(coverage),
        "runner_up_seats": len(walked) - 1,
        "ambiguous": bool(ambiguous),
        "solo_probes": probe_positions,
        "match_deltas": referee_deltas(csv_pairs, picked, rate),
    }
