"""Track-mix → AUTO GENERATED labels, gated by a by-ear confirm (F3 / G2 → labels).

`track_mix.py` (#16) *computes* each original↔mix rate/offset but nothing turns a
confirmed alignment into labels. This closes that gap with the same confirm/reject loop
as F1 (skip clips):

1. **Review clips** — `generate_review_clips()` overlays each track's original onto its
   mix region at the recovered rate/offset and writes one clip per track (coherent ⇒ the
   alignment is right; doubling/smear ⇒ wrong), into the clip player's dir + a
   `track-mix-candidates.json` sidecar.
2. **Confirm/reject** — `confirm_track()` / `reject_track()` record a per-track decision
   in `labels/track-mix-decisions.tsv` (last-wins; a real TSV, not label rows, so it never
   pollutes the sync ground truth that `track_mix.parse_sync_points` reads).
3. **Emit** — `emit_track_labels()` writes, for **confirmed** tracks only, AUTO GENERATED
   `origNNN/trackNNN sync: A|B` rows + a human-readable `orig-map` annotation into
   `<cap>.trackmix.auto.labels.tsv` in an **output dir** (never `labels/`). The A/B pair is
   built in the capture frame so `track_mix.track_sync_groundtruth` recovers exactly the
   confirmed `rate`; the annotation preserves the true `rate` + original offset.

Provisional (unconfirmed / rejected) alignments stay in the report, never the labels —
precision-first, every emitted line ends with " AUTO GENERATED", and `labels/` (hand
ground truth) is never written here.
"""

import json
import os

import numpy as np

from . import audio as _audio
from . import clips as _clips
from . import groundtruth as _gt
from . import track_mix as _track_mix

SUFFIX = " AUTO GENERATED"
DECISIONS_NAME = "track-mix-decisions.tsv"
CANDIDATES_NAME = "track-mix-candidates.json"
_DEC_HEADER = ("# track\tdecision\trate\toffset_orig_s\tcapture\tnote   "
               "(track-mix by-ear decisions; last-wins per track)\n")


# --------------------------------------------------------------------------- #
# Decision store: labels/track-mix-decisions.tsv (last-wins per track)
# --------------------------------------------------------------------------- #

def _decisions_path(labels_dir=None):
    return os.path.join(labels_dir or _gt.LABELS_DIR, DECISIONS_NAME)


def load_decisions(labels_dir=None):
    """{track_num: {decision, rate, offset_orig_s, capture, note}} (last row wins)."""
    path = _decisions_path(labels_dir)
    out = {}
    if not os.path.isfile(path):
        return out
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            p = line.rstrip("\n").split("\t")
            if len(p) < 2:
                continue
            try:
                track = int(p[0])
            except ValueError:
                continue
            out[track] = {
                "decision": p[1],
                "rate": float(p[2]) if len(p) > 2 and p[2] else None,
                "offset_orig_s": float(p[3]) if len(p) > 3 and p[3] else None,
                "capture": p[4] if len(p) > 4 else "",
                "note": p[5] if len(p) > 5 else "",
            }
    return out


def decision_for(track, labels_dir=None, decisions=None):
    """"confirm" / "reject" / None for a track."""
    if decisions is None:
        decisions = load_decisions(labels_dir)
    d = decisions.get(int(track))
    return d["decision"] if d else None


def _write_decision(track, decision, rate, offset_orig_s, capture, note, labels_dir):
    decisions = load_decisions(labels_dir)
    decisions[int(track)] = {"decision": decision, "rate": rate,
                             "offset_orig_s": offset_orig_s, "capture": capture,
                             "note": note}
    path = _decisions_path(labels_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(_DEC_HEADER)
        for tn in sorted(decisions):
            d = decisions[tn]
            handle.write("%d\t%s\t%s\t%s\t%s\t%s\n" % (
                tn, d["decision"],
                ("%.6f" % d["rate"]) if d["rate"] is not None else "",
                ("%.6f" % d["offset_orig_s"]) if d["offset_orig_s"] is not None else "",
                d["capture"], (d["note"] or "").replace("\t", " ")))
    os.replace(tmp, path)
    return decisions[int(track)]


def confirm_track(track, rate=None, offset_orig_s=None, capture="", note="",
                  labels_dir=None):
    """Record a track-mix alignment as confirmed (eligible for emission)."""
    return _write_decision(track, "confirm", rate, offset_orig_s, capture, note, labels_dir)


def reject_track(track, rate=None, offset_orig_s=None, capture="", note="",
                 labels_dir=None):
    """Record a track-mix alignment as rejected (never emitted)."""
    return _write_decision(track, "reject", rate, offset_orig_s, capture, note, labels_dir)


# --------------------------------------------------------------------------- #
# Emit: confirmed alignments → <cap>.trackmix.auto.labels.tsv (in an OUTPUT dir)
# --------------------------------------------------------------------------- #

def sync_rows_for_track(track, cap_local_begin, cap_local_end, rate, offset_orig_s):
    """AUTO GENERATED rows (start, end, text) encoding one track's original↔mix map.

    Built in the capture frame so `track_sync_groundtruth` recovers exactly `rate`:
    track A/B at the mix-region ends (cap-local), orig A/B spaced by span/rate (so
    (trackB−trackA)/(origB−origA) == rate). A final `orig-map` annotation preserves the
    true rate + original offset (which the A/B capture-frame encoding abstracts away).
    """
    rb, re = float(cap_local_begin), float(cap_local_end)
    span = re - rb
    dorig = span / rate if rate else span
    return [
        (rb, rb, "orig%03d sync: A%s" % (track, SUFFIX)),
        (rb, rb, "track%03d sync: A%s" % (track, SUFFIX)),
        (rb + dorig, rb + dorig, "orig%03d sync: B%s" % (track, SUFFIX)),
        (re, re, "track%03d sync: B%s" % (track, SUFFIX)),
        (rb, re, "track %d orig-map: rate=%.5f orig_offset=%.3fs%s"
         % (track, rate, offset_orig_s, SUFFIX)),
    ]


def _append_rows(path, rows):
    prior = ""
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            prior = handle.read()
        if prior and not prior.endswith("\n"):
            prior += "\n"
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(prior)
        for a, b, text in rows:
            handle.write("%.6f\t%.6f\t%s\n" % (a, b, text))
    os.replace(tmp, path)


def emit_track_labels(results, out_dir, meta, labels_dir=None, only_confirmed=True):
    """Emit AUTO GENERATED track↔mix sync rows for confirmed alignments into out_dir.

    `results` = `track_mix.batch_align(...)["results"]`; `meta` = the track-metadata
    tracks dict (for each track's master_begin/end). Writes `<cap>.trackmix.auto.labels.tsv`
    per capture (grouped; multiple tracks append). `out_dir` MUST NOT be `labels/` — these
    are auto rows and would otherwise be read back as hand ground truth. Returns
    {track: capture} for the tracks emitted.
    """
    if os.path.realpath(out_dir) == os.path.realpath(labels_dir or _gt.LABELS_DIR):
        raise ValueError("refusing to emit AUTO GENERATED rows into the labels/ ground-"
                         "truth dir; choose a separate out_dir")
    os.makedirs(out_dir, exist_ok=True)
    decisions = load_decisions(labels_dir)
    starts = _gt.resolve_starts(labels_dir)
    by_cap = {}
    emitted = {}
    for r in results:
        if "error" in r:
            continue
        track = int(r["track"])
        if only_confirmed and decision_for(track, decisions=decisions) != "confirm":
            continue
        cap = r.get("capture")
        e = meta.get(str(track)) or {}
        mb, me = e.get("master_begin_seconds"), e.get("master_end_seconds")
        if cap is None or cap not in starts or mb is None or me is None:
            continue
        cstart = starts[cap]
        rows = sync_rows_for_track(track, mb - cstart, me - cstart,
                                   r["rate"], r.get("offset_orig_s", 0.0))
        by_cap.setdefault(cap, []).extend(rows)
        emitted[track] = cap
    for cap, rows in by_cap.items():
        _append_rows(os.path.join(out_dir, cap + ".trackmix.auto.labels.tsv"), rows)
    return emitted


# --------------------------------------------------------------------------- #
# By-ear review clips: overlay original on mix at the recovered rate/offset
# --------------------------------------------------------------------------- #

def make_align_clip(orig, mix_region, rate, offset_orig_s, sr=_audio.SR,
                    start_local_s=0.0, dur_s=20.0):
    """Overlay the original (mapped into mix time) on a mix-region excerpt.

    `mix_region` is the capture slice for the track (mix-local 0 = region start);
    `orig` the original track. For each mix-local time t in the excerpt, the original
    sample is taken at `offset_orig_s + t / rate` (linear interp). Aligned ⇒ the two
    reinforce coherently; a wrong rate/offset ⇒ audible doubling/smear. Returns the mono
    clip (original and mix averaged), or None if the excerpt is empty.
    """
    i0 = int(start_local_s * sr)
    i1 = min(len(mix_region), int((start_local_s + dur_s) * sr))
    if i1 <= i0:
        return None
    mseg = np.asarray(mix_region[i0:i1], dtype=np.float64)
    n = len(mseg)
    mix_local_t = (np.arange(n) + i0) / float(sr)
    orig_t = offset_orig_s + mix_local_t / (rate or 1.0)
    orig_idx = orig_t * sr
    oseg = np.interp(orig_idx, np.arange(len(orig)), np.asarray(orig, dtype=np.float64),
                     left=0.0, right=0.0)
    clip = (mseg + oseg) * 0.5
    peak = float(np.max(np.abs(clip))) if len(clip) else 0.0
    return (clip / peak * 0.95) if peak > 1e-9 else clip


def generate_review_clips(results, sources_dir, meta, out_dir, labels_dir=None,
                          dur_s=20.0, sr=_audio.SR):
    """Write one original-vs-mix overlay clip per aligned track + manifest + sidecar.

    Skips tracks with errors or missing audio/placement. Reuses clips.write_clip /
    _append_manifest; the sidecar (`track-mix-candidates.json`) records each clip's
    track/capture/rate/offset for the confirm/reject step. Returns the manifest entries.
    """
    os.makedirs(out_dir, exist_ok=True)
    starts = _gt.resolve_starts(labels_dir)
    entries = []
    sidecar = _load_sidecar(out_dir)
    for r in results:
        if "error" in r:
            continue
        track = int(r["track"])
        cap = r.get("capture")
        e = meta.get(str(track)) or {}
        mb, me = e.get("master_begin_seconds"), e.get("master_end_seconds")
        if cap is None or cap not in starts or mb is None or me is None:
            continue
        orig_path = _track_mix.find_original(track, sources_dir)
        if not orig_path or not _audio.find_audio_file(cap):
            continue
        cstart = starts[cap]
        cap_audio = _audio.load_audio(cap)
        mix_region = cap_audio[int((mb - cstart) * sr):int((me - cstart) * sr)]
        orig = _audio.load_audio(orig_path)
        clip = make_align_clip(orig, mix_region, r["rate"], r.get("offset_orig_s", 0.0),
                               sr=sr, dur_s=dur_s)
        if clip is None:
            continue
        cid = "trackmix_%03d_%s" % (track, cap)
        fn = cid + ".mp3"
        _clips.write_clip(clip, os.path.join(out_dir, fn), sr=sr)
        entries.append({
            "id": cid, "audio": fn,
            "title": "track %d ↔ %s: orig/mix align (rate %.4f)" % (track, cap, r["rate"]),
            "description": ("Original overlaid on the mix at the recovered rate/offset. "
                            "Coherent ⇒ confirm; doubling/smear ⇒ reject."),
            "duration": len(clip) / sr,
            "annotations": [{"t": 0.0, "label": "orig+mix (rate %.4f, offset %.2fs)"
                             % (r["rate"], r.get("offset_orig_s", 0.0))}],
        })
        sidecar[cid] = {"id": cid, "track": track, "capture": cap, "rate": r["rate"],
                        "offset_orig_s": r.get("offset_orig_s", 0.0)}
    _clips._append_manifest(out_dir, entries)
    _save_sidecar(out_dir, sidecar)
    return entries


def _sidecar_path(out_dir):
    return os.path.join(out_dir, CANDIDATES_NAME)


def _load_sidecar(out_dir):
    path = _sidecar_path(out_dir)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def _save_sidecar(out_dir, sidecar):
    path = _sidecar_path(out_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(sidecar, handle, indent=2)
    os.replace(tmp, path)


def decide_clip(clip_id, decision, out_dir, labels_dir=None, note=""):
    """Confirm/reject a track-mix clip by id, using the sidecar. Returns (decision_rec).

    Raises KeyError for an unknown id, ValueError for a bad decision.
    """
    cand = _load_sidecar(out_dir)[clip_id]
    fn = {"confirm": confirm_track, "reject": reject_track}.get(decision)
    if fn is None:
        raise ValueError("decision must be 'confirm' or 'reject', got %r" % decision)
    return fn(cand["track"], rate=cand.get("rate"), offset_orig_s=cand.get("offset_orig_s"),
             capture=cand.get("capture", ""), note=note, labels_dir=labels_dir)
