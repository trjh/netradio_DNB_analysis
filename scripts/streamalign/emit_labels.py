"""Emit AUTO GENERATED labels.tsv from the engine's placements (G1c).

Turns a solve's `{stem: master_start}` (plus file durations) into Audacity-style
label files, using the grammar from `data/sheet/analysis notes.csv` (`file start
sync`, `file end`, and skip `file note`s). Two hard rules:

  * **Every emitted label line ends with " AUTO GENERATED"** so Tim can tell mine
    from his hand labels and correct them.
  * **Programmatic output is ALWAYS `<stem>.auto.labels.tsv`.** The plain
    `<stem>.labels.tsv` name is reserved for hand-generated/confirmed labels and is
    never written here — so auto files can safely sit alongside hand labels and a
    hand-made `<stem>.labels.tsv` is never overwritten. Consumers (analysis, playback,
    media generation) read BOTH; where labels conflict, the hand `<stem>.labels.tsv`
    takes precedence. (That read-side precedence lives in the consumers, not here.)

This is solve-agnostic (takes positions in), so feed it the best available solve
(skip-aware once that lands). Round-trips: `groundtruth.resolve_starts(out_dir)`
recovers the emitted master-starts (it reads `*.auto.labels.tsv` too).
"""

import os
import re

from . import audio as _audio
from . import groundtruth as _gt

SUFFIX = " AUTO GENERATED"
STARTER_SUFFIX = " STARTER"

# An owner row that homes a neighbour file: `file_<other>: <inner label>` at the
# neighbour's local start time inside the owner (Proposal B's carry-forward link).
_LINK_RE = re.compile(r"^file_([^:]+):\s*(.+)", re.IGNORECASE)


def _rows_for_file(stem, master_start, duration=None, skips=None):
    """Label rows (start_s, end_s, text) for one placed file, all AUTO GENERATED.

    Timestamps are the file's own local timeline (the start sync sits at local 0.0,
    the file end at its duration). `skips` (optional) is a list of {at_s, delta_s}
    in the file's local time; only emitted when supplied (direction by sign).
    """
    rows = [(0.0, 0.0, "file start sync: %s.wav %.6f%s" % (stem, master_start, SUFFIX))]
    if duration is not None:
        rows.append((duration, duration, "file end: %s.wav%s" % (stem, SUFFIX)))
    for sk in (skips or []):
        t = sk["at_s"]
        direction = "ahead" if sk.get("delta_s", 0) < 0 else "back"
        rows.append((t, t, "file note: skip %s %.3fs%s"
                     % (direction, abs(sk.get("delta_s", 0)), SUFFIX)))
    return rows


def write_labels_tsv(rows, path):
    """Write rows to a `.labels.tsv` (atomic)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        for a, b, text in rows:
            handle.write("%.6f\t%.6f\t%s\n" % (a, b, text))
    os.replace(tmp, path)


def emit_labels(positions, out_dir, durations=None, skip_maps=None,
                exclude_rejected=True, labels_dir=None):
    """Write an AUTO GENERATED `<stem>.auto.labels.tsv` per placed file into `out_dir`.

    Programmatic output is ALWAYS `<stem>.auto.labels.tsv` (never the plain
    `<stem>.labels.tsv`, reserved for hand-generated/confirmed labels), so it can sit
    alongside hand labels without ever overwriting them. `durations`/`skip_maps` are
    {stem: ...} (optional). Returns {stem: written_path}.

    When `exclude_rejected` (default), skips Tim has by-ear rejected
    (`labels/skip-rejections.tsv`, read from `labels_dir`) are dropped before emit, so
    a wrong auto-detected skip never reaches the auto labels (F1's "solve consumes
    rejections"). Confirmed skips already live in the hand labels and take precedence
    there, so they are not re-emitted here.
    """
    os.makedirs(out_dir, exist_ok=True)
    durations = durations or {}
    skip_maps = skip_maps or {}
    if exclude_rejected and skip_maps:
        from . import skip_review as _skip_review
        skip_maps = _skip_review.apply_decisions(skip_maps, labels_dir=labels_dir)
    written = {}
    for stem, master_start in positions.items():
        rows = _rows_for_file(stem, master_start, durations.get(stem),
                              skip_maps.get(stem))
        path = os.path.join(out_dir, stem + ".auto.labels.tsv")
        write_labels_tsv(rows, path)
        written[stem] = path
    return written


def durations_for(stems, audio_dir=None):
    """{stem: duration_seconds} for stems whose audio is present (others omitted)."""
    out = {}
    for stem in stems:
        if _audio.find_audio_file(stem, audio_dir):
            out[stem] = _audio.duration_seconds(stem, audio_dir=audio_dir)
    return out


def _read_label_lines(path):
    """Read a `.labels.tsv` as a list of (start_s, end_s, text); skips malformed lines."""
    rows = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                parts = line.rstrip("\n").split("\t", 2)
                if len(parts) < 3:
                    continue
                try:
                    a, b = float(parts[0]), float(parts[1])
                except ValueError:
                    continue
                rows.append((a, b, parts[2].strip()))
    except OSError:
        pass
    return rows


def _links_in(rows):
    """{other_stem: link_local_time} for each neighbour the owner rows home.

    The link time is the neighbour's `file [start] sync` row when present (its true
    start inside the owner), else the earliest `file_<other>:` reference.
    """
    cands = {}
    for a, _b, text in rows:
        m = _LINK_RE.match(text)
        if not m:
            continue
        other = _audio.stem_of(m.group(1))
        cands.setdefault(other, []).append((a, "start sync" in m.group(2).lower()))
    links = {}
    for other, seen in cands.items():
        starts = [a for a, is_start in seen if is_start]
        links[other] = min(starts) if starts else min(a for a, _ in seen)
    return links


def emit_starter(owner_stem, labels_dir=None, out_dir=None):
    """Write `<other>.starter.labels.tsv` seeds for every neighbour the owner links.

    For each `file_<other>:` link in `<owner>.labels.tsv`, carry ALL of the owner's
    labels at/after the link's local time, shifted onto `<other>`'s local timeline
    (maximal seed; the labeller prunes), each suffixed `" STARTER"`. The anchor
    `file start sync: <other>.wav <master> STARTER` at local 0.0 is **derived** from
    `groundtruth.resolve_starts` (the `file start sync` labels) rather than hand-copied
    from the sheet — closing the `--adjust` loop.

    Starter files are SEED-ONLY: `<other>.starter.labels.tsv` is excluded from the sheet
    import (`Code.js`) and from solve/build (`groundtruth`, `track_mix`,
    `build_track_metadata`), so it never reaches analysis. Returns {other_stem: path}.
    """
    labels_dir = labels_dir or _gt.LABELS_DIR
    out_dir = out_dir or labels_dir
    owner_stem = _audio.stem_of(owner_stem)
    rows = _read_label_lines(os.path.join(labels_dir, owner_stem + ".labels.tsv"))
    if not rows:
        return {}

    starts = _gt.resolve_starts(labels_dir)
    owner_master = starts.get(owner_stem)

    written = {}
    for other, link_t in _links_in(rows).items():
        # neighbour master start: prefer the resolved value, else owner + link offset
        if other in starts:
            other_master = starts[other]
        elif owner_master is not None:
            other_master = owner_master + link_t
        else:
            other_master = link_t  # owner has no anchor; emit a relative seed
        out_rows = [(0.0, 0.0, "file start sync: %s.wav %.6f%s"
                     % (other, other_master, STARTER_SUFFIX))]
        for a, b, text in rows:
            if a < link_t - 1e-9:
                continue  # before the neighbour begins -> not its label
            lm = _LINK_RE.match(text)
            if lm:
                if _audio.stem_of(lm.group(1)) != other:
                    continue  # homed onto a DIFFERENT neighbour -> not this one's seed
                inner = lm.group(2).strip()
                if "start sync" in inner.lower():
                    continue  # the originating link row -> replaced by the anchor above
                # Any OTHER `file_<other>:` row is a label the labeller wrote about this
                # neighbour while working in the owner (a whole `LABELTRACK <other>` track of
                # them, typically). It IS the seed. Carry it, prefix stripped -- on the
                # neighbour's own timeline it needs no re-homing. Skipping every prefixed row
                # (the old rule) threw away exactly the labels the starter exists to carry.
                text = inner
            out_rows.append((a - link_t, b - link_t, text + STARTER_SUFFIX))
        out_rows.sort(key=lambda r: (r[0], r[1]))
        path = os.path.join(out_dir, other + ".starter.labels.tsv")
        write_labels_tsv(out_rows, path)
        written[other] = path
    return written
