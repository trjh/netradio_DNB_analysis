"""Batch skip-check clip runner + confirm/reject write-back (F1).

Two pieces the rest of G1 leaves open:

1. **Batch runner** — `generate_clips()` enumerates the detected skips across the
   placed / hand-verified overlap pairs and drives `clips.py` to render one
   skip-check clip per skip into the clip review player's dir (`player/public/clips/`
   by default), plus a `skip-candidates.json` sidecar so each clip's
   skipper/reference/position/magnitude survive for the write-back step. Today
   `clips.generate_skip_clips` is per-pair and keeps no candidate record; this drives
   the whole graph in one pass and persists what each clip *is*.

2. **Confirm/reject write-back** (the loop Tim asked for), keyed by clip id:
   * **CONFIRM** (the clip sounds coherent ⇒ the skip is right) → append the skip to
     the skipper's **hand** `<stem>.labels.tsv` as
     `file note: skip <dir> <mag>s verified <ref>` — the existing hand grammar, so it
     becomes ground truth. Hand files are **append-only** here and never rewritten;
     an equivalent row already present is left as-is (idempotent).
   * **REJECT** (doubling/dissonance ⇒ the skip is wrong) → record it in a durable
     **`labels/skip-rejections.tsv`** (a real TSV, NOT an inline `ERROR` row that a
     regen would clobber).

Consumption by the solve/emit path:
  * `apply_decisions(skip_maps, labels_dir)` drops rejected skips before emit, so a
    wrong auto-detected skip never reaches `<stem>.auto.labels.tsv`.
  * Confirmed skips live in the hand `<stem>.labels.tsv` and are already read as
    ground truth by `groundtruth.resolve_*` (hand labels take precedence over auto).
  * Anything auto-detected but neither confirmed nor rejected stays **provisional** —
    `emit_labels` marks it `AUTO GENERATED`.

Audio is required only by `enumerate_candidates`/`generate_clips`; the decision store
and `apply_decisions` are pure I/O and run anywhere (so they're fully unit-tested
without the captures on disk).
"""

import json
import os

from . import audio as _audio
from . import clips as _clips
from . import groundtruth as _gt
from . import skips as _skips
from . import solve as _solve

REJECTIONS_NAME = "skip-rejections.tsv"
CANDIDATES_NAME = "skip-candidates.json"
_REJECT_HEADER = "# stem\tat_s\tdelta_s\treference\tnote   (skip-check rejections; do not edit by hand carelessly)\n"
# How close (seconds) two skips must be to count as "the same skip" when matching a
# candidate against a stored confirmation/rejection. Skips within one capture are far
# enough apart that ~1.5 s is unambiguous and tolerates detector jitter.
MATCH_TOL_S = 1.5


def _direction(delta_s):
    """(word, magnitude) for a skip's offset step. Mirrors emit_labels' convention:
    a negative offset step means the skipper jumped AHEAD; positive means BACK."""
    return ("ahead" if delta_s < 0 else "back"), abs(delta_s)


def _same_skip(a_at, a_delta, b_at, b_delta, tol_s=MATCH_TOL_S):
    """True if two skips (position + direction) are the same physical event."""
    same_dir = (a_delta < 0) == (b_delta < 0)
    return same_dir and abs(a_at - b_at) <= tol_s


# --------------------------------------------------------------------------- #
# Rejection store: labels/skip-rejections.tsv
# --------------------------------------------------------------------------- #

def _rejections_path(labels_dir=None):
    return os.path.join(labels_dir or _gt.LABELS_DIR, REJECTIONS_NAME)


def load_rejections(labels_dir=None):
    """Return [{stem, at_s, delta_s, reference, note}] from skip-rejections.tsv."""
    path = _rejections_path(labels_dir)
    out = []
    if not os.path.isfile(path):
        return out
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 3:
                continue
            try:
                at_s, delta_s = float(parts[1]), float(parts[2])
            except ValueError:
                continue
            out.append({
                "stem": parts[0],
                "at_s": at_s,
                "delta_s": delta_s,
                "reference": parts[3] if len(parts) > 3 else "",
                "note": parts[4] if len(parts) > 4 else "",
            })
    return out


def is_rejected(stem, at_s, delta_s, rejections=None, labels_dir=None, tol_s=MATCH_TOL_S):
    """Has this skip (stem + position + direction) been rejected?"""
    if rejections is None:
        rejections = load_rejections(labels_dir)
    for r in rejections:
        if r["stem"] == stem and _same_skip(at_s, delta_s, r["at_s"], r["delta_s"], tol_s):
            return True
    return False


def reject_skip(stem, at_s, delta_s, reference="", note="", labels_dir=None):
    """Record a rejected skip in labels/skip-rejections.tsv (idempotent, atomic).

    Returns "added" or "already" (already present for this stem+position+direction).
    """
    path = _rejections_path(labels_dir)
    existing = load_rejections(labels_dir)
    if is_rejected(stem, at_s, delta_s, existing):
        return "already"
    row = "%s\t%.6f\t%.6f\t%s\t%s\n" % (stem, at_s, delta_s, reference, note.replace("\t", " "))
    header = "" if os.path.isfile(path) else _REJECT_HEADER
    tmp = path + ".tmp"
    prior = ""
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            prior = handle.read()
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(header + prior + row)
    os.replace(tmp, path)
    return "added"


# --------------------------------------------------------------------------- #
# Confirm: promote a skip into the skipper's HAND <stem>.labels.tsv
# --------------------------------------------------------------------------- #

def _hand_labels_path(stem, labels_dir=None):
    return os.path.join(labels_dir or _gt.LABELS_DIR, stem + ".labels.tsv")


def _skip_row_text(delta_s, reference):
    word, mag = _direction(delta_s)
    ref = (" verified %s" % reference) if reference else ""
    return "file note: skip %s %.3fs%s" % (word, mag, ref)


def confirm_skip(stem, at_s, delta_s, reference="", before_s=None, after_s=None,
                 labels_dir=None):
    """Append a confirmed skip to the skipper's HAND <stem>.labels.tsv (idempotent).

    Writes `file note: skip <dir> <mag>s verified <ref>` in the existing hand grammar,
    as a region [before_s, after_s] (point at `at_s` if those aren't given). The hand
    file is only ever appended to — existing rows are preserved byte-for-byte — and an
    equivalent skip row already present (auto or hand) is not duplicated.

    Returns "added" or "already".
    """
    path = _hand_labels_path(stem, labels_dir)
    lo = at_s if before_s is None else before_s
    hi = at_s if after_s is None else after_s
    text = _skip_row_text(delta_s, reference)
    word, mag = _direction(delta_s)

    prior = ""
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            prior = handle.read()
        # already confirmed? match a same-direction skip row near this position.
        for line in prior.splitlines():
            cols = line.split("\t")
            if len(cols) < 3:
                continue
            low = cols[2].lower()
            if "skip" not in low or ("skip %s" % word) not in low:
                continue
            try:
                row_at = float(cols[0])
            except ValueError:
                continue
            if abs(row_at - lo) <= MATCH_TOL_S:
                return "already"

    row = "%.6f\t%.6f\t%s\n" % (lo, hi, text)
    if prior and not prior.endswith("\n"):
        prior += "\n"
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(prior + row)
    os.replace(tmp, path)
    return "added"


# --------------------------------------------------------------------------- #
# Consumption: filter rejected skips out of a skip map before emit
# --------------------------------------------------------------------------- #

def apply_decisions(skip_maps, labels_dir=None, rejections=None):
    """Drop rejected skips from a {stem: [skip, ...]} map (for the emit path).

    Each skip is a dict with at_s/delta_s. Returns a new map; stems with all skips
    rejected map to an empty list (kept so the caller still emits the file, just
    without the bad skip). Confirmed skips already live in the hand labels, so they
    are not re-injected here.
    """
    if rejections is None:
        rejections = load_rejections(labels_dir)
    out = {}
    for stem, sk_list in skip_maps.items():
        out[stem] = [s for s in sk_list
                     if not is_rejected(stem, s.get("at_s", 0.0), s.get("delta_s", 0.0),
                                        rejections)]
    return out


# --------------------------------------------------------------------------- #
# Candidate enumeration + batch clip generation
# --------------------------------------------------------------------------- #

def _audio_len_s(stem):
    return len(_audio.load_audio(stem)) / float(_audio.SR)


def _overlap_window(skipper, reference, seed_offset_s):
    """A-local [start, end] over which skipper overlaps reference (skipper time)."""
    la, lb = _audio_len_s(skipper), _audio_len_s(reference)
    lo = max(0.0, seed_offset_s)
    hi = min(la, lb + seed_offset_s)
    return lo, hi


def enumerate_candidates(labels_dir=None, pairs=None, conf_min=0.7,
                         skip_rejected=True):
    """Detect skips over the hand-verified overlap pairs → candidate dicts.

    For each pair with audio present, measure the edge offset (skip-aware), walk the
    overlap, and detect skips. The better-anchored capture (nearer the master anchor
    by filename) is the **reference**; the other is the **skipper** the skip is
    attributed to. Returns
      [{skipper, reference, at_s, delta_s, before_s, after_s, seed_offset_s, conf}].
    Rejected skips are omitted when `skip_rejected` (so a re-run doesn't resurface
    them). Requires audio; raises nothing for missing files — those pairs are skipped.
    """
    pairs = pairs if pairs is not None else _solve._dedupe(_gt.alignment_edges(labels_dir))
    rejections = load_rejections(labels_dir) if skip_rejected else []
    candidates = []
    for a, b in pairs:
        if not (_audio.find_audio_file(a) and _audio.find_audio_file(b)):
            continue
        try:
            edge = _solve.measure_edge_skipaware(a, b)
        except (FileNotFoundError, RuntimeError):
            continue
        if edge is None or edge["conf"] < conf_min:
            continue
        seed = edge["offset_s"]
        # reference = the capture nearer the anchor (smaller filename start); skipper
        # = the other. a[i] ~ b[i - seed], so if we flip the skipper we flip the seed.
        skipper, reference, seed_off = _orient(a, b, seed)
        lo, hi = _overlap_window(skipper, reference, seed_off)
        if hi - lo < 2.0:
            continue
        char = _skips.characterise_overlap(skipper, reference, lo, hi, seed_off)
        for sk in char["skips"]:
            if skip_rejected and is_rejected(skipper, sk["at_s"], sk["delta_s"], rejections):
                continue
            # local skipper→reference offsets bracketing the skip, so the write-back can
            # convert into the reference's timeline if Tim reattributes (--owner).
            off_before = _clips._offset_at(char["walk"], sk["before_s"])
            off_after = _clips._offset_at(char["walk"], sk["after_s"])
            candidates.append({
                "skipper": skipper, "reference": reference,
                "at_s": sk["at_s"], "delta_s": sk["delta_s"],
                "before_s": sk["before_s"], "after_s": sk["after_s"],
                "off_before_s": off_before, "off_after_s": off_after,
                "seed_offset_s": seed_off, "conf": edge["conf"],
            })
    return candidates


def _orient(a, b, seed_a_to_b):
    """Pick (skipper, reference, seed) so reference is the better-anchored capture.

    Reference = the one with the smaller filename-range start (closer to master 0,
    so better anchored); the skip is attributed to the other (the skipper). Flipping
    which file is A negates the offset (a~b[i-seed] ⇒ b~a[i+seed])."""
    ra = _filename_start(a)
    rb = _filename_start(b)
    if rb <= ra:                       # b is the (better-anchored) reference
        return a, b, seed_a_to_b
    return b, a, -seed_a_to_b          # a is the reference; b becomes the skipper


def _filename_start(stem):
    """Leading file number of a capture stem (d356-375 → 356); large if unparseable."""
    import re
    m = re.match(r"d-?(\d+)", stem)
    return int(m.group(1)) if m else 10 ** 9


def clip_id(cand, index):
    """Stable id for a candidate clip (matches clips.py's <a>_<b>_skipN style)."""
    return "%s_%s_skip%d" % (cand["skipper"], cand["reference"], index + 1)


def generate_clips(candidates, out_dir, sr=_audio.SR):
    """Render one skip-check clip per candidate into out_dir + manifest + sidecar.

    Reuses clips.make_skip_clip / write_clip / _append_manifest, then persists the
    candidate metadata to `skip-candidates.json` keyed by clip id, so confirm/reject
    can act on a clip id later without re-detecting. Returns the manifest entries.
    """
    os.makedirs(out_dir, exist_ok=True)
    entries = []
    sidecar = load_candidates(out_dir)
    # number skips per (skipper,reference) pair so ids match clips.generate_skip_clips
    seen_pair = {}
    for cand in candidates:
        a = _audio.load_audio(cand["skipper"])
        b = _audio.load_audio(cand["reference"])
        walk = _skips.walk_overlap(a, b, cand["before_s"] - _clips.PAD_S - 2.0,
                                   cand["after_s"] + _clips.PAD_S + 2.0,
                                   cand["seed_offset_s"])
        made = _clips.make_skip_clip(a, b, cand, walk, sr=sr)
        if made is None:
            continue
        clip, ann = made
        pair = (cand["skipper"], cand["reference"])
        idx = seen_pair.get(pair, 0)
        seen_pair[pair] = idx + 1
        cid = clip_id(cand, idx)
        fn = cid + ".mp3"
        _clips.write_clip(clip, os.path.join(out_dir, fn), sr=sr)
        word, mag = _direction(cand["delta_s"])
        entries.append({
            "id": cid, "audio": fn,
            "title": "%s ↔ %s: skip %s %.3fs @ %s %.1fs" % (
                cand["skipper"], cand["reference"], word, mag, cand["skipper"], cand["at_s"]),
            "description": ("A=%s (skipper — skip attributed here), B=%s (reference). "
                            "Coherent ⇒ confirm; doubling ⇒ reject." % (
                                cand["skipper"], cand["reference"])),
            "duration": len(clip) / sr,
            "annotations": ann,
        })
        sidecar[cid] = dict(cand, id=cid)
    _clips._append_manifest(out_dir, entries)
    save_candidates(out_dir, sidecar)
    return entries


def candidates_path(out_dir):
    return os.path.join(out_dir, CANDIDATES_NAME)


def load_candidates(out_dir):
    path = candidates_path(out_dir)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def save_candidates(out_dir, sidecar):
    path = candidates_path(out_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(sidecar, handle, indent=2)
    os.replace(tmp, path)


def reattribute(cand, owner):
    """Resolve a candidate to (stem, at_s, delta_s, before_s, after_s, reference) for the
    chosen `owner`, transforming coordinates when the owner is the reference.

    Default (owner None or the skipper) keeps the candidate's skipper-local values. To
    attribute the skip to the **reference** instead, convert into the reference's local
    timeline — `reference_t = skipper_t − offset` using the per-point offsets bracketing
    the skip — and **invert the offset-step direction** (a skipper "ahead" is a reference
    "back"); the `verified` ref then points back at the original skipper. `owner` must be
    one of the pair's two members.
    """
    skipper, ref = cand["skipper"], cand["reference"]
    if owner in (None, skipper):
        lo, hi = cand.get("before_s"), cand.get("after_s")
        at = cand["at_s"] if lo is None or hi is None else 0.5 * (lo + hi)
        return skipper, at, cand["delta_s"], lo, hi, ref
    if owner != ref:
        raise ValueError("--owner must be the skipper (%s) or reference (%s); got %r"
                         % (skipper, ref, owner))
    obf, oaf = cand.get("off_before_s"), cand.get("off_after_s")
    if obf is None or oaf is None:
        raise ValueError("cannot reattribute to %s: per-point offsets missing from the "
                         "candidate (re-run skip-clips to record them)" % ref)
    rb, ra = cand["before_s"] - obf, cand["after_s"] - oaf   # → reference-local times
    lo, hi = sorted((rb, ra))
    return ref, 0.5 * (lo + hi), -cand["delta_s"], lo, hi, skipper


def decide(clip_id_, decision, out_dir, labels_dir=None, owner=None, note=""):
    """Apply a confirm/reject to a clip by id, using the candidates sidecar.

    `decision` is "confirm" or "reject". `owner` (optional) reattributes the skip to the
    pair's reference instead of the skipper — coordinates + direction are transformed into
    that file's timeline (see `reattribute`). Returns (status, candidate). Raises KeyError
    if the clip id isn't in the sidecar, ValueError for a bad owner.
    """
    sidecar = load_candidates(out_dir)
    cand = sidecar[clip_id_]
    stem, at, delta, before_s, after_s, ref = reattribute(cand, owner)
    if decision == "confirm":
        status = confirm_skip(stem, at, delta, reference=ref,
                              before_s=before_s, after_s=after_s, labels_dir=labels_dir)
    elif decision == "reject":
        status = reject_skip(stem, at, delta, reference=ref, note=note, labels_dir=labels_dir)
    else:
        raise ValueError("decision must be 'confirm' or 'reject', got %r" % decision)
    return status, cand
