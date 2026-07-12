#!/usr/bin/env python3
"""Chroma-match the Mystery Tracks against the listen queue's DOWNLOADED, UNLISTENED tracks.

    PYTHONPATH=scripts .venv/bin/python scripts/match_queue.py --out /tmp/queue-match.txt

The listen queue is a second candidate pool that already exists on disk. Only the tracks Tim has
NOT listened to are worth checking: if he had heard it and it were the mystery, it would not be
a mystery any more.

Chroma, not fingerprints -- see Archive/LESSON_acoustid_stream.md. Signatures are cached to
`.chroma-cache/` (a 12xN float16 matrix, ~50KB a track) so a re-run is instant and, more to the
point, so that the SIGNATURE can outlive the audio: the cache is the thing worth keeping, not
the file. That is the basis for scaling this to material we cannot afford to store.
"""

import argparse
import hashlib
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from streamalign import audio as _audio          # noqa: E402

HOP = 2048
CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     ".chroma-cache")
AUDIO_EXTS = (".m4a", ".opus", ".mp3", ".webm", ".flac", ".wav", ".ogg", ".wv", ".aac")


def chroma_of(path, min_seconds=45.0):
    """Cached chroma signature. The cache key is content-addressed (size+mtime+name).

    `min_seconds` guards CANDIDATES (a 10s clip is not a record). It must NOT be applied to a
    QUERY: Mystery Track 7 is only 23s of audio, and silently dropping it meant the one mystery
    most in need of help was quietly excluded from its own search.
    """
    st = os.stat(path)
    key = hashlib.sha1(("%s|%d|%d" % (os.path.basename(path), st.st_size,
                                      int(st.st_mtime))).encode()).hexdigest()[:20]
    cached = os.path.join(CACHE, key + ".npy")
    if os.path.exists(cached):
        return np.load(cached).astype("float32")
    import librosa
    try:
        y = _audio.load_audio(path)
    except Exception:
        return None
    if len(y) < min_seconds * _audio.SR:
        return None
    c = librosa.feature.chroma_cqt(y=np.asarray(y, dtype="float32"),
                                   sr=_audio.SR, hop_length=HOP) + 1e-6
    c = librosa.util.normalize(c, norm=2, axis=0)
    os.makedirs(CACHE, exist_ok=True)
    np.save(cached, c.astype("float16"))          # float16: half the disk, no loss that matters
    return c


def cost(q, c):
    """Best cost over TRANSPOSITIONS -> (cost, semitones).

    Chroma is invariant to timbre, NOT to pitch: a semitone shift rotates all twelve bins, so a
    naive comparison holds C major against C# and calls a true match noise. That is exactly how
    Mystery Track 5 was missed (see streamalign/chroma_match.py). Uploads get pitch-nudged
    routinely, so this is the common case, not an edge case.
    """
    from streamalign import chroma_match as _cm
    if c is None or c.shape[1] < q.shape[1]:
        return None, None
    return _cm.match(q, c)[:2]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", default=None, help="listen_queue.json")
    ap.add_argument("--root", default=None, help="NETRADIO_DOWNLOAD_ROOT")
    ap.add_argument("--out", default="-")
    ap.add_argument("--include-listened", action="store_true")
    args = ap.parse_args()

    root = args.root or os.path.expanduser(os.environ.get("NETRADIO_DOWNLOAD_ROOT", ""))
    queue = args.queue or os.path.expanduser(
        "~/Downloads/Netradio/player/metadata/listen_queue.json")
    src = os.environ.get("NETRADIO_SOURCES_DIR")
    out = sys.stdout if args.out == "-" else open(args.out, "w", buffering=1)

    data = json.load(open(queue))
    items = data.get("items", data)
    # Only what he has NOT heard: if he'd heard the mystery, it would not be a mystery.
    unheard = {i.get("id"): i for i in items
               if args.include_listened or not i.get("listened")}
    print("# %d queue items, %d unlistened" % (len(items), len(unheard)), file=out)

    files = []
    for base, _dirs, names in os.walk(root):
        for n in names:
            if n.lower().endswith(AUDIO_EXTS):
                files.append(os.path.join(base, n))
    print("# %d downloaded audio file(s) under %s" % (len(files), root), file=out)

    # Map a downloaded file to its queue item where we can (the id/video-id appears in the
    # filename); if we cannot, still check it -- an unmatched file is not evidence of anything.
    def is_unheard(path):
        name = os.path.basename(path)
        for qid, item in unheard.items():
            for token in (str(qid), item.get("canonical") or "", item.get("url") or ""):
                tok = token.rsplit("/", 1)[-1].rsplit("=", 1)[-1]
                if tok and len(tok) > 6 and tok in name:
                    return True, item
        return (not unheard), None

    todo = []
    for f in files:
        ok, item = is_unheard(f)
        if ok:
            todo.append((f, item))
    print("# %d of them are UNLISTENED -> checking those\n" % len(todo), file=out)

    # The query list comes from track-metadata.json, NEVER from a filename glob: sources/ still
    # holds clips of mysteries that have since been SOLVED (2 and 3), and querying those wastes
    # the run and invites a spurious "match" against an answered question.
    from streamalign import mystery as _mystery
    queries = []
    for entry in _mystery.searchable(src):
        c = chroma_of(entry["clip"], min_seconds=15.0)   # queries may be short (MT7 is 23s)
        if c is not None:
            queries.append(("Mystery Track %d" % entry["number"],
                            c[:, :int(120 * _audio.SR / HOP)]))
    print("# queries (UNSOLVED only, per track-metadata.json): %s\n"
          % ", ".join(q[0] for q in queries), file=out)

    results = {name: [] for name, _ in queries}
    for i, (path, item) in enumerate(todo, 1):
        c = chroma_of(path)
        if c is None:
            continue
        for name, q in queries:
            v, shift = cost(q, c)
            if v is not None:
                results[name].append((v, os.path.basename(path),
                                      (item or {}).get("title") or "", shift))
        if i % 20 == 0:
            print("# ... %d/%d" % (i, len(todo)), file=out)

    from streamalign import chroma_match as _cm
    print("\n# RESULTS  (a TRUE match scores ~0.034; the non-match floor is ~0.10)", file=out)
    for name, _ in queries:
        ranked = sorted(results[name])
        print("\n%s:" % name, file=out)
        if not ranked:
            print("   (nothing scored)", file=out)
            continue
        best = ranked[0][0]
        runner = ranked[1][0] if len(ranked) > 1 else 1.0
        # A match must be BOTH absolutely good AND decisively ahead of the runner-up. The second
        # half is not optional: a SHORT query drives every cost down, so five candidates tie near
        # the threshold and all five look like matches. That is not five identifications, it is
        # zero -- a winner that cannot beat its rivals has not identified anything.
        decisive = best <= 0.050 and best <= 0.60 * runner
        for v, f, title, shift in ranked[:5]:
            key = ("  [%s]" % _cm.describe_shift(shift)) if shift else ""
            flag = ""
            if decisive and v == best:
                flag = "   <== MATCH"
            elif v < 0.07:
                flag = "   <-- worth an ear"
            print("   %.4f  %-44s %s%s%s" % (v, f[:44], title[:26], key, flag), file=out)
        if best <= 0.050 and not decisive:
            print("   ^^ NOT a match: the top %d are within %.4f of each other. A short query "
                  "drives all costs down;" % (min(5, len(ranked)), runner - best), file=out)
            print("      a winner that cannot beat its rivals has identified nothing.", file=out)
    print("\n# done", file=out)


if __name__ == "__main__":
    main()
