"""Emit AUTO GENERATED labels.tsv from the engine's placements (G1c).

Turns a solve's `{stem: master_start}` (plus file durations) into Audacity-style
label files, using the grammar from `data/sheet/analysis notes.csv` (`file start
sync`, `file end`, and skip `file note`s). Two hard rules:

  * **Every emitted label line ends with " AUTO GENERATED"** so Tim can tell mine
    from his hand labels and correct them.
  * **Never overwrite a hand-made labels.tsv.** Everything is written to a separate
    `out_dir` Tim reviews; within it, a recording Tim hasn't hand-labelled gets the
    canonical `<stem>.labels.tsv`, one he has gets `<stem>.auto.labels.tsv`
    (a supplement). The hand-labels dir is only ever read, never written.

This is solve-agnostic (takes positions in), so feed it the best available solve
(skip-aware once that lands). Round-trips: `groundtruth.resolve_starts(out_dir)`
recovers the emitted master-starts.
"""

import os

from . import audio as _audio

SUFFIX = " AUTO GENERATED"


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


def emit_labels(positions, out_dir, hand_labels_dir, durations=None, skip_maps=None):
    """Write an AUTO GENERATED labels.tsv per placed file into `out_dir`.

    Un-hand-labelled stems -> `<stem>.labels.tsv`; hand-labelled stems ->
    `<stem>.auto.labels.tsv`. `durations`/`skip_maps` are {stem: ...} (optional).
    Returns {stem: written_path}. Never writes into `hand_labels_dir`.
    """
    os.makedirs(out_dir, exist_ok=True)
    durations = durations or {}
    skip_maps = skip_maps or {}
    written = {}
    for stem, master_start in positions.items():
        has_hand = os.path.isfile(os.path.join(hand_labels_dir, stem + ".labels.tsv"))
        fn = (stem + ".auto.labels.tsv") if has_hand else (stem + ".labels.tsv")
        rows = _rows_for_file(stem, master_start, durations.get(stem),
                              skip_maps.get(stem))
        path = os.path.join(out_dir, fn)
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
