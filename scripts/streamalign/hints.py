"""Hint labels: what the engine *thinks*, offered to the human labeller as suggestions.

The manual Audacity process is good and is not being replaced. This produces an extra
label track to import alongside your own, carrying the engine's best guesses -- proposed
sync points and file start/end, skip candidates, original-track spans -- and, where it
cannot decide, an explicit **question for you to answer**.

Three hard rules
----------------
1. **Hints never override hand labels.** Output is ALWAYS ``<stem>.hints.tsv``. That name
   does not end in ``.labels.tsv``, so it is invisible to ``groundtruth.is_pipeline_label_file``,
   to ``build_track_metadata``, to the solve and to the sheet import: a hint can never be
   mistaken for a fact or leak into the player's metadata. ``write_hints`` refuses to write
   any ``*.labels.tsv`` path at all, so this cannot be undone by a stray ``--out``.
   Hints only ever *add*: import them as their own track and copy across what you accept.
2. **Every row is marked.** Each line ends in ``HINT`` (mirroring the engine's existing
   ``AUTO GENERATED`` convention), so a hint is obvious in Audacity and stays obvious if a
   row is ever pasted into a hand file by accident.
3. **Every claim carries its confidence, spelled out** -- ``confidence 9.8/10`` -- so you can
   see at a glance how much to trust a row. A hint the engine cannot corroborate does not
   quietly vanish: it degrades into a ``note QUESTION:`` row saying *why* it could not.

Grammar
-------
Rows use the existing grammar (``labels/sort_tsv.py`` ``keyword_patterns``) -- chiefly the
tagged-note form ``note <TAG>: <text>`` -- so nothing new has to be taught to the validator.
Questions are ``note QUESTION: ...``; suggestions the engine is merely offering are
``note HINT: ...``. Proposed anchors keep their native ``file start sync:`` / ``file end:``
shape (with a trailing ``?``) so they read naturally next to the real thing.

What it can and cannot know
---------------------------
The engine's evidence is cross-correlation against **overlapping** audio. Where a capture
butt-joints its neighbours (they follow one another directly, sharing no audio -- e.g.
``d356-375``), there is nothing to correlate: no sync point and no skip detection are
possible, and the anchor can only be *carried forward* from the neighbour's hand link.
Rather than emit an empty file, this says so, in a question.
"""

import os
import re

from . import audio as _audio
from . import groundtruth as _gt

SUFFIX = " HINT"

# Grammar-anchored, deliberately NOT substring matches: `note: ...starting here?` is prose,
# not a track start, and treating it as one produces a confidently wrong hint.
_TRACK_START_RE = re.compile(r"^\s*start(\d+)\s*:", re.IGNORECASE)
_ORIG_START_RE = re.compile(r"^\s*orig(\d+)\s+start\s*:", re.IGNORECASE)

# A hint is only worth surfacing if it is either confident or interesting. Below this the
# engine is guessing, and a guess presented as a hint is worse than silence -- it becomes a
# question instead.
CONF_TRUST = 0.80

# Two placed captures must share more than this many seconds of audio before it is worth
# trying to correlate them. Below it, an "overlap" is an artefact of rounding.
MIN_OVERLAP_S = 5.0


def _conf(confidence):
    """Render a 0-1 confidence the way the labeller reads it: `confidence 9.8/10`."""
    return "confidence %.1f/10" % (10.0 * max(0.0, min(1.0, float(confidence))))


def _row(start, end, text):
    return (float(start), float(end), text + SUFFIX)


def _question(start, end, text):
    return _row(start, end, "note QUESTION: " + text)


def _hint(start, end, text):
    return _row(start, end, "note HINT: " + text)


def write_hints(rows, path):
    """Write hint rows to `path` (atomic). REFUSES to write any hand/pipeline label file.

    The guard is the point: `<stem>.labels.tsv` is hand-only and `*.auto.labels.tsv` is the
    solve's, and neither may be written from here even by explicit request. Hints add; they
    never overwrite.
    """
    if path.endswith(".labels.tsv"):
        raise ValueError(
            "refusing to write hints to %r: *.labels.tsv is hand/pipeline territory. "
            "Hints go to <stem>.hints.tsv and never overwrite labels." % path)
    rows = sorted(rows, key=lambda r: (r[0], r[1]))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        for a, b, text in rows:
            handle.write("%.6f\t%.6f\t%s\n" % (a, b, text))
    os.replace(tmp, path)
    return path


def overlapping_neighbours(stem, starts, audio_dir=None, min_overlap_s=MIN_OVERLAP_S):
    """Placed captures that share real audio with `stem`: [(other, overlap_s, seed_offset_s)].

    `seed_offset_s` follows the engine's convention `master_start(b) - master_start(a)`,
    i.e. where `other` sits inside `stem`'s local timeline. Captures that merely *abut*
    `stem` (the common case in the tail of the stream) share no audio and are excluded --
    correlating them would return noise, not an offset.
    """
    if stem not in starts or not _audio.find_audio_file(stem, audio_dir):
        return []
    a0 = starts[stem]
    a1 = a0 + _audio.duration_seconds(stem, audio_dir=audio_dir)
    out = []
    for other, b0 in starts.items():
        if other == stem or not _audio.find_audio_file(other, audio_dir):
            continue
        b1 = b0 + _audio.duration_seconds(other, audio_dir=audio_dir)
        overlap = min(a1, b1) - max(a0, b0)
        if overlap > min_overlap_s:
            out.append((other, overlap, b0 - a0))
    return sorted(out, key=lambda t: -t[1])


def _carry_forward_anchor(stem, labels_dir):
    """(owner, link_local_t, owner_duration) for the hand `file_<stem>:` link that anchors
    `stem`, or None. This is how a butt-jointed capture gets its master start: a neighbour's
    hand label says "the next file starts here".
    """
    from . import emit_labels as _emit
    if not os.path.isdir(labels_dir):
        return None
    for fn in sorted(os.listdir(labels_dir)):
        if not fn.endswith(".labels.tsv") or fn.endswith(".starter.labels.tsv"):
            continue
        owner = _gt._stem(fn).replace(".labels", "")
        rows = _emit._read_label_lines(os.path.join(labels_dir, fn))
        link_t = _emit._links_in(rows).get(stem)
        if link_t is not None:
            return owner, link_t, rows
    return None


def build_hints(stem, labels_dir=None, audio_dir=None, decim=8):
    """Build the hint rows for one capture. Returns (rows, diagnostics).

    Emits, in the file's own local time:
      * a proposed `file start sync:` anchor, with how it was arrived at and how far it can
        be trusted;
      * a proposed `file end:`;
      * per overlapping neighbour: the correlated sync offset + confidence, and any skips
        detected while walking that overlap;
      * questions wherever the engine cannot corroborate something, or where the hand labels
        disagree with the audio.
    """
    stem = _audio.stem_of(stem)
    labels_dir = labels_dir or _gt.LABELS_DIR
    rows, diag = [], {"stem": stem, "questions": 0, "neighbours": [], "skips": 0}

    audio_path = _audio.find_audio_file(stem, audio_dir)
    if not audio_path:
        raise SystemExit("no audio found for %s (looked for %s.wav/.au/.mp3)" % (stem, stem))
    duration = _audio.duration_seconds(stem, audio_dir=audio_dir)
    diag["duration"] = duration

    starts = _gt.resolve_starts(labels_dir)
    master = starts.get(stem)

    # --- the anchor -------------------------------------------------------------------
    carried = _carry_forward_anchor(stem, labels_dir)
    if master is not None:
        how = "already anchored in the hand labels"
        rows.append(_row(0.0, 0.0, "file start sync?: %s.wav %.6f  (%s)"
                         % (stem, master, how)))
    elif carried:
        owner, link_t, _ = carried
        owner_master = starts.get(owner)
        if owner_master is not None:
            master = owner_master + link_t
            rows.append(_row(0.0, 0.0,
                             "file start sync?: %s.wav %.6f  (carried forward from %s's link "
                             "at its local %.3fs -- not corroborated by audio)"
                             % (stem, master, owner, link_t)))
    if master is None:
        rows.append(_question(0.0, 0.0,
                              "no master anchor for %s: it is not anchored in the hand labels "
                              "and no other file's `file_%s:` link points at it. Where does it "
                              "start?" % (stem, stem)))
        diag["questions"] += 1
    diag["master"] = master

    # The anchor is derived from a point inside the OWNER's audio. If the owner's link sits
    # past the end of the owner's own audio, the anchor inherits that error exactly.
    if carried:
        owner, link_t, _ = carried
        if _audio.find_audio_file(owner, audio_dir):
            owner_dur = _audio.duration_seconds(owner, audio_dir=audio_dir)
            slack = link_t - owner_dur
            if slack > 0.05:
                rows.append(_question(
                    0.0, 0.0,
                    "%s's link that anchors this file sits at its local %.3fs, but %s's audio "
                    "is only %.3fs long -- the anchor is derived from a point %.3fs PAST the "
                    "end of the audio it was measured in. If the two files really do follow "
                    "one another directly, %s should start %.3fs earlier (at master %.6f). "
                    "Is there a real %.3fs gap in the recording, or is the anchor late?"
                    % (owner, link_t, owner, owner_dur, slack, stem, slack,
                       (starts.get(owner) or 0.0) + owner_dur, slack)))
                diag["questions"] += 1
                diag["anchor_slack_s"] = slack

    # --- the end ----------------------------------------------------------------------
    rows.append(_row(duration, duration,
                     "file end?: %s.wav  (audio is %.3fs long)" % (stem, duration)))

    # --- neighbours: sync points + skips ----------------------------------------------
    neighbours = overlapping_neighbours(stem, starts, audio_dir) if master is not None else []
    if not neighbours:
        rows.append(_question(
            0.0, min(duration, 1.0),
            "no placed capture OVERLAPS this file, so the engine has no audio to correlate "
            "against: it cannot propose a sync point, and it cannot detect skips (skip "
            "detection walks an overlap). Everything above the file-end marker is carried "
            "forward or hand-derived, NOT measured. Any sync point here has to be found by "
            "ear."))
        diag["questions"] += 1
    for other, overlap, seed in neighbours:
        diag["neighbours"].append({"other": other, "overlap_s": overlap})
        try:
            from . import align as _align
            res = _align.align_pair(stem, other, decim=decim)
        except Exception as exc:                                   # pragma: no cover
            rows.append(_question(0.0, 0.0, "could not align against %s: %s" % (other, exc)))
            diag["questions"] += 1
            continue
        conf, off = res["confidence"], res["offset_seconds"]
        lo = max(0.0, seed if seed > 0 else 0.0)
        hi = min(duration, lo + overlap)
        drift = off - seed
        if conf >= CONF_TRUST:
            rows.append(_row(lo, hi,
                             "file note: in sync with %s over this range "
                             "(measured offset %.3fs, %s)" % (other, off, _conf(conf))))
            if abs(drift) > 0.05:
                rows.append(_question(
                    lo, hi,
                    "the audio says %s sits at offset %.3fs, but the hand labels place it at "
                    "%.3fs -- a %.3fs disagreement (%s). Which is right?"
                    % (other, off, seed, drift, _conf(conf))))
                diag["questions"] += 1
        else:
            rows.append(_question(
                lo, hi,
                "tried to correlate against %s (%.0fs of overlap) but only reached %s -- too "
                "weak to trust. Is this really an overlap, or is one of the two anchors wrong?"
                % (other, overlap, _conf(conf))))
            diag["questions"] += 1
            continue

        # skips, walked over the same overlap
        try:
            from . import skips as _skips
            char = _skips.characterise_overlap(stem, other, lo, hi, off)
            for sk in char.get("skips", []):
                t = sk["at_s"]
                direction = "ahead" if sk.get("delta_s", 0) < 0 else "back"
                rows.append(_question(
                    t, t,
                    "possible SKIP %s %.3fs here (found while walking the overlap with %s). "
                    "Confirm or reject -- if it is real it needs a `file note: SKIP %s %.3fs`."
                    % (direction, abs(sk["delta_s"]), other, direction, abs(sk["delta_s"]))))
                diag["questions"] += 1
                diag["skips"] += 1
        except Exception:                                          # pragma: no cover
            pass

    # --- what was playing at the join (carry-forward content) --------------------------
    # A butt-jointed file inherits whatever the owner had mid-flight at the join, so the
    # most useful thing we can say about local 0.0 is "this track was already playing".
    # Match the GRAMMAR (`start067:`, `orig067 start:`), never a substring: a plain
    # `note: ...starting here?` is prose, not a track start.
    if carried:
        owner, link_t, owner_rows = carried
        opened = [(a, t) for a, _b, t in owner_rows
                  if a < link_t and (_TRACK_START_RE.match(t) or _ORIG_START_RE.match(t))]
        if opened:
            last_t = max(opened, key=lambda r: r[0])[1]
            rows.append(_hint(
                0.0, 0.0,
                "%s runs straight into this file, and the last track it opened before the "
                "join was: %s -- so that is most likely still playing at local 0.0 here."
                % (owner, last_t)))

    # --- original-track spans ----------------------------------------------------------
    rows.extend(_orig_span_rows(stem, diag))

    return rows, diag


def _orig_span_rows(stem, diag):
    """origNNN start/end spans from track-mix, or a question explaining why not.

    This is the one hint that does NOT need an overlapping capture -- it correlates the
    *original* recordings against the mix -- so it is the most valuable hint for a
    butt-jointed file. It needs librosa (chroma/DTW) and the originals on disk; when either
    is missing we say so rather than emitting nothing.
    """
    missing = []
    try:
        import librosa  # noqa: F401
    except ImportError:
        missing.append("librosa is not installed (`pip install librosa`)")
    sources = os.path.join(_gt.REPO_ROOT, "sources")
    if not os.path.isdir(sources):
        missing.append("the originals are not reachable (`sources/` does not resolve -- "
                       "is the media volume mounted?)")
    if missing:
        diag["questions"] += 1
        diag["orig_blocked"] = missing
        return [_question(
            0.0, 0.0,
            "no origNNN spans: %s. This is the hint that does not need an overlapping "
            "capture, so it is the one most worth unblocking for a file like this."
            % "; and ".join(missing))]
    return []                                                      # pragma: no cover
