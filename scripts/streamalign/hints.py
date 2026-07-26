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
exactly joins its neighbours (they follow one another directly, sharing no audio -- e.g.
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


def _notes_drift_baseline(stem, labels_dir, master=None):
    """The labels-minus-notes drift the 1998/2017 notes ALREADY carry at this point in the
    chain: the delta of the nearest placed capture that starts before `stem`'s (else the
    nearest after; else 0.0). The notes chain the recordings' physical durations as if
    nothing is missing, so every stretch of missing time the labels discover shifts all
    later deltas permanently -- a new file inheriting its neighbours' drift is
    agreement, not disagreement."""
    from . import tracklist2017 as _tl
    starts = _gt.resolve_starts(labels_dir)
    own = starts.get(stem)
    if own is None:
        own = master          # carried-anchor case: not yet in the resolved starts
    try:
        notes = _tl.parse()
    except Exception:
        return 0.0
    deltas = []
    for other, info in notes.items():
        other = re.sub(r"^dnb(?=\d)", "d", other)   # the clean-tree parse variant (known
        ms = (info or {}).get("master_start_s")      # non-hermeticity) must still match
        if other == stem or ms is None or other not in starts:
            continue
        deltas.append((starts[other], starts[other] - ms))
    if not deltas:
        return 0.0
    deltas.sort()
    if own is None:
        return deltas[-1][1]
    before = [d for s, d in deltas if s <= own]
    return before[-1] if before else deltas[0][1]


def _carry_forward_anchor(stem, labels_dir):
    """(owner, link_local_t, owner_duration) for the hand `file_<stem>:` link that anchors
    `stem`, or None. This is how an exactly-joined capture gets its master start: a neighbour's
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

        # skips are detected once for the whole file, after the neighbour loop (below) --
        # via skip_review.scan_for_hints, which also persists the id so skip-reject can act.

    # --- skips over every verified overlap, with an id you can rule on ------------------
    # Detection lives in skips.py (unchanged); scan_for_hints orients each skip, persists the
    # `skip-candidates.json` sidecar, and hands back the skips ATTRIBUTED TO THIS FILE. You
    # audition each in Audacity against the real capture (the old averaged-overlap review clip
    # never gave an audible signal); if real it needs a `file note: SKIP <dir> <mag>s`, and if
    # spurious `streamalign skip-reject <id>` stops the engine re-proposing it.
    try:
        from . import skip_review as _skip_review
        for cid, at_s, direction, mag, other in _skip_review.scan_for_hints(stem, labels_dir):
            rows.append(_question(
                at_s, at_s,
                "possible SKIP %s %.3fs here (found while walking the overlap with %s) "
                "[id %s]. If real: `file note: SKIP %s %.3fs`. If not: `streamalign "
                "skip-reject %s`." % (direction, mag, other, cid, direction, mag, cid)))
            diag["questions"] += 1
            diag["skips"] += 1
    except Exception:                                              # pragma: no cover
        pass

    # --- what was playing at the join (carry-forward content) --------------------------
    # An exactly-joined file inherits whatever the owner had mid-flight at the join, so the
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

    # --- the 1998/2017 notes ------------------------------------------------------------
    rows.extend(_tracklist_rows(stem, duration, master, diag, audio_dir, labels_dir=labels_dir))

    # --- sync anchors: where a single original plays alone --------------------------------
    rows.extend(_anchor_rows(stem, duration, diag, audio_dir))

    # --- original-track spans (not offered -- see below) ---------------------------------
    rows.extend(_orig_span_rows(stem, diag))

    return rows, diag


def _tracklist_rows(stem, duration, master, diag, audio_dir=None, labels_dir=None):
    """Hints from `tracklist-2017.txt` -- the oldest evidence, and often the ONLY evidence.

    For a capture with no overlapping neighbour these notes are worth more than the whole
    alignment engine: they say which tracks play, roughly where, whether the file follows its
    predecessor directly, and which track carries over the join. Everything here is a HINT --
    hand-typed in 1998/2017 and approximate -- so it is offered for confirmation, never as an
    anchor.
    """
    from . import tracklist2017 as _tl
    note = _tl.for_stem(stem, audio_dir=audio_dir)
    if not note:
        return []
    rows = []
    diag["tracklist"] = True

    if note.get("transition_from"):
        rows.append(_hint(0.0, 0.0,
                          "the 1998/2017 notes say this file is a DIRECT TRANSITION from %s "
                          "(no overlap, so nothing to correlate -- its position rests on that "
                          "file's link, not on measurement)." % note["transition_from"]))
    if note.get("continuation"):
        rows.append(_hint(0.0, 0.0,
                          "the 1998/2017 notes say this file opens mid-track, continuing: %s"
                          % note["continuation"]))

    # Cross-check the anchor against the notes -- a THIRD independent source. The notes
    # timeline chains the recordings' physical durations as if nothing is missing, so it
    # CANNOT see broadcast time the captures never recorded; every such stretch the
    # labels discover (e.g. the two skips inside d336-355, exposed by an original-anchored
    # placement -- see STREAM_PROVENANCE.md, "Three timelines") shifts all later files'
    # deltas by that much, permanently. So the baseline is the NEIGHBOURS' delta, not
    # zero: question only a NEW step, never the inherited, already-resolved drift
    # (the +3.754s at d356-375/d376-395 would otherwise re-fire on every tail file).
    ms = note.get("master_start_s")
    if ms is not None and master is not None:
        delta = master - ms
        baseline = _notes_drift_baseline(stem, labels_dir, master=master)
        step = delta - baseline
        if abs(step) > 2.5:
            rows.append(_question(
                0.0, 0.0,
                "the 1998/2017 notes put this file's start at master %.3f, but the hand labels "
                "place it at %.3f -- a %+.3fs disagreement, of which %+.3fs is the drift the "
                "notes already carry from earlier discovered missing time (see STREAM_PROVENANCE.md). "
                "The NEW step of %+.3fs is the outlier worth resolving: either this placement "
                "is wrong, or the recordings are missing ~that much audio near this file's "
                "start. Which is it?" % (ms, master, delta, baseline, step)))
            diag["questions"] += 1

    for track in note.get("tracks", []):
        local = track["local_s"]
        if local < 0 or local > duration + 5:
            continue
        rows.append(_hint(local, local,
                          "the 1998/2017 notes put a track start here: %s  -- confirm by ear, "
                          "then label it `startNNN: ID: <Artist> - <Title>`" % track["name"]))
        for cue in track.get("cues", []):
            if "sync" in cue["text"].lower():
                rows.append(_hint(local, local,
                                  "...and mark a sync point INSIDE that original at its %s "
                                  "(%s) -- the 1998/2017 notes flagged it as a sync point."
                                  % (_hms(cue["at_s"]), cue["text"])))
    diag["tracklist_tracks"] = len(note.get("tracks", []))
    return rows


def _hms(seconds):
    return "%d:%05.2f" % (int(seconds // 60), seconds % 60)


_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text):
    """Comparable word set. Drops the noise words that differ between the two sources."""
    stop = {"the", "a", "an", "original", "mix", "remix", "feat", "ft", "vs", "and"}
    return {w for w in _WORD.findall((text or "").lower()) if w not in stop} - {""}


def resolve_track_number(name, master_s, tracks_meta, tolerance_s=90.0):
    """Which track number is the 1998/2017 notes' `name` at `master_s`? -> int or None.

    Two independent keys, and BOTH must agree:

      * **master time** -- `track-metadata.json` carries each track's `master_begin_seconds`,
        and the notes carry their own master time. On the tracks checked these agree to within
        a second or two (two of them exactly), so proximity alone almost settles it.
      * **the name** -- as a check, because proximity alone would happily pick the neighbour of
        a track that is merely missing.

    The name check is order-agnostic on purpose: the notes are hand-typed and inconsistent --
    "Hypnotising / PFM" is Title / Artist, while "Fokus / On Line (Original Mix)" is
    Artist / Title. Comparing word SETS against artist+title together sidesteps the ambiguity
    instead of guessing a rule that the data does not actually follow.

    Returns None rather than a guess when the two keys disagree: a wrong track number would
    point the anchor search at the wrong record, and a confident anchor on the wrong record is
    exactly the failure this whole feature is trying not to repeat.
    """
    want = _tokens(name)
    best, best_score = None, 0.0
    for num, entry in (tracks_meta or {}).items():
        if not str(num).isdigit():
            continue
        begin = entry.get("master_begin_seconds")
        if begin is None or abs(begin - master_s) > tolerance_s:
            continue
        have = _tokens(entry.get("artist")) | _tokens(entry.get("title"))
        if not want or not have:
            continue
        overlap = len(want & have) / float(len(want))
        if overlap > best_score:
            best, best_score = int(num), overlap
    # Half the notes' words must appear in the metadata's artist+title. "Mystery Track 4"
    # matches its metadata twin; a mere neighbour in time does not.
    return best if best_score >= 0.5 else None


def _anchor_rows(stem, duration, diag, audio_dir=None):
    """Candidate sync anchors: instants where one original plays ALONE in the mix.

    This is what the labeller actually needs, and it is deliberately NOT a track start/end: in
    a DJ mix records are blended, so there is no frame at which one "begins" and picking one is
    subjective. A solo instant is objective, and it is what an A/B anchor is made of.

    Each candidate names BOTH times -- the moment in the mix and the matching moment inside the
    original -- so it can be written straight down as a `track sync:` / `origNNN sync:` pair.

    The search is bounded by the 1998/2017 notes (which say roughly where each track sits).
    That bound is the whole trick: blind, the search is ambiguous and wrong more often than
    right (see Archive/LESSON_locate_original.md).
    """
    from . import tracklist2017 as _tl
    note = _tl.for_stem(stem, audio_dir=audio_dir)
    if not note or not note.get("tracks"):
        return []

    missing = []
    try:
        import librosa  # noqa: F401
    except ImportError:
        missing.append("librosa is not installed (run `make align-env`)")
    sources = os.environ.get("NETRADIO_SOURCES_DIR")
    if not sources or not os.path.isdir(sources):
        missing.append("the originals are not reachable (set NETRADIO_SOURCES_DIR in .env_vars)")
    if missing:
        diag["anchors_blocked"] = missing
        return [_hint(0.0, 0.0,
                      "no sync-anchor candidates: %s. With those in place the engine can "
                      "propose the instants where each original plays ALONE in the mix -- the "
                      "A/B anchor points -- for the tracks the 1998/2017 notes place here."
                      % "; and ".join(missing))]

    from . import track_mix as _tm
    meta = _load_track_metadata()
    capture = _audio.load_audio(stem, audio_dir=audio_dir)

    entries = sorted(note["tracks"], key=lambda t: t["local_s"])
    rows = []
    for index, track in enumerate(entries):
        num = resolve_track_number(track["name"], track["master_s"], meta)
        if num is None:
            rows.append(_question(
                track["local_s"], track["local_s"],
                "could not match '%s' to a track number in track-metadata.json, so no anchor "
                "search was run for it. Which track is it?" % track["name"]))
            diag["questions"] += 1
            continue
        orig_path = _tm.find_original(num, sources)
        if not orig_path:
            rows.append(_hint(
                track["local_s"], track["local_s"],
                "no original on disk for track %03d (%s), so no anchor search. If you have the "
                "record, drop it in the originals dir as `%03d-...` and re-run."
                % (num, track["name"], num)))
            continue

        # Bound the search: from this track's start to the next one's (plus a little slack),
        # clamped to the file. This is the prior that makes the search work at all.
        window_start = max(0.0, track["local_s"] - 15.0)
        window_end = (entries[index + 1]["local_s"] + 15.0
                      if index + 1 < len(entries) else duration)
        window_end = min(duration, window_end)

        orig = _audio.load_audio(orig_path, audio_dir=audio_dir)
        anchors = _tm.solo_anchors(orig, capture, window_start, window_end, top=3)
        if not anchors:
            rows.append(_question(
                track["local_s"], track["local_s"],
                "searched %s-%s for a solo passage of track %03d (%s) and found none clear "
                "enough to offer. Is it really playing here?"
                % (_hms(window_start), _hms(window_end), num, track["name"])))
            diag["questions"] += 1
            continue

        # The free sanity check: a DJ pitches a record a few percent, not tens of percent. An
        # anchor pair implying a rate far from 1.0 has matched noise, and says so by its own
        # absurdity. This is a constraint INDEPENDENT of the machinery that produced the
        # answer -- unlike a confidence score, which cannot catch a confidently wrong match.
        rate = _tm.implied_rate(anchors)
        lo, hi = _tm.RATE_PLAUSIBLE
        if rate is not None and not (lo <= rate <= hi):
            rows.append(_question(
                track["local_s"], track["local_s"],
                "found candidate anchors for track %03d (%s) but they imply a mix/original "
                "rate of %.2f -- a DJ pitches a record by a few percent, not by that, so these "
                "have matched noise rather than the record. DISCARDED; find them by ear."
                % (num, track["name"], rate)))
            diag["questions"] += 1
            diag["anchors_gated"] = diag.get("anchors_gated", 0) + 1
            continue

        # A is the EARLY anchor and B the late one -- that ordering is not cosmetic: the speed
        # calc is (trackB - trackA) / (origB - origA), so swapping them inverts the sense of
        # the rate. `solo_anchors` returns them best-scoring first, so sort by time here.
        pair = sorted(anchors[:2], key=lambda a: a["mix_s"])
        for letter, anchor in zip("AB", pair):
            rows.append(_hint(
                anchor["mix_s"], anchor["mix_s"],
                "SYNC ANCHOR %s for track %03d (%s): this instant in the mix is %s inside the "
                "original (%.0fs solo passage, %s). Label `track sync: %s` here and "
                "`orig%03d sync: %s` at %s in the original."
                % (letter, num, track["name"], _hms(anchor["orig_s"]), anchor["run_s"],
                   _conf(anchor["score"]), letter, num, letter, _hms(anchor["orig_s"]))))
        diag["anchors"] = diag.get("anchors", 0) + min(2, len(anchors))
        if rate is not None and len(anchors) >= 2:
            rows.append(_hint(
                anchors[0]["mix_s"], anchors[0]["mix_s"],
                "...those two anchors imply a mix/original rate of %.4f (the sheet's "
                "`(trackB-trackA)/(origB-origA)`). Plausible for a DJ pitch; confirm by ear."
                % rate))
    return rows


def _load_track_metadata(path=None):
    """The tracks dict from track-metadata.json, or {}."""
    import json
    path = path or os.path.join(_gt.REPO_ROOT, "track-metadata.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data.get("tracks", data)


def _orig_span_rows(stem, diag):
    """origNNN spans are NOT offered. Deliberately. Here is why.

    The appeal is obvious: locating an original inside the mix needs no overlapping capture,
    so it is the one hint that would work on an exactly-joined file like d356-375. The
    machinery exists (`track_mix.locate_original`). It is not trustworthy enough to hint from.

    Measured against the 8 tracks whose position is known from `track-metadata.json` (and whose
    capture is picked the way the trusted `align_track` picks it), the search lands within 5s
    of the truth **2 times out of 8** -- and, worse, its single most confident answer (track 2,
    margin 0.69, the highest in the set) is wrong by *25 minutes*. There is therefore no
    confidence or margin threshold that separates its right answers from its wrong ones, so it
    cannot be gated into safety.

    Two things defeat it, and neither is a tuning problem:
      * the DJ beatmatches, so the record plays at a rate the mix chose -- a fixed-lag
        correlation drifts out of alignment within a minute (subsequence DTW, which warps,
        scored no better here);
      * the broadcast repeats material, so the same record genuinely occurs in several places
        and "the" answer is not unique.

    `locate_original` stays in the tree because the maths is now right (see its docstring: the
    non-negative-chroma pedestal is removed, so a wrong answer at least *reports* itself as
    low-confidence) and it is the obvious starting point for solving this properly. But a wrong
    origNNN span is worse than no origNNN span: it would send the labeller to the wrong minute
    of a 20-minute file with a number next to it implying it had been checked. So: silence, and
    an honest note.
    """
    diag["orig_offered"] = False
    return [_hint(
        0.0, 0.0,
        "no origNNN spans offered: the engine CAN search for an original inside a capture, but "
        "on this material it is right about 2 times in 8, and its most confident answer in "
        "testing was wrong by 25 minutes -- the DJ beatmatches (so the record's speed is not "
        "its own) and the broadcast repeats material (so the answer is not unique). Locating "
        "the originals is still a by-ear job. See track_mix.locate_original if you want to "
        "attack it.")]
