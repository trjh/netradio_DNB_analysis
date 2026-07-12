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


def chroma_of(path):
    """Cached chroma signature. The cache key is content-addressed (size+mtime+name)."""
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
    if len(y) < 45 * _audio.SR:
        return None
    c = librosa.feature.chroma_cqt(y=np.asarray(y, dtype="float32"),
                                   sr=_audio.SR, hop_length=HOP) + 1e-6
    c = librosa.util.normalize(c, norm=2, axis=0)
    os.makedirs(CACHE, exist_ok=True)
    np.save(cached, c.astype("float16"))          # float16: half the disk, no loss that matters
    return c


def cost(q, c):
    import librosa
    if c is None or c.shape[1] < q.shape[1]:
        return None
    d, wp = librosa.sequence.dtw(X=q, Y=c, subseq=True, metric="cosine")
    return float(d[-1, wp[0][1]]) / len(wp)


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

    queries = []
    for n in sorted(os.listdir(src or ".")):
        if n.lower().startswith("mystery") and n.lower().endswith((".wav", ".mp3")):
            stem = os.path.splitext(n)[0]
            if stem not in [q[0] for q in queries]:
                c = chroma_of(os.path.join(src, n))
                if c is not None:
                    queries.append((stem, c[:, :int(120 * _audio.SR / HOP)]))
    print("# queries: %s\n" % ", ".join(q[0] for q in queries), file=out)

    results = {name: [] for name, _ in queries}
    for i, (path, item) in enumerate(todo, 1):
        c = chroma_of(path)
        if c is None:
            continue
        for name, q in queries:
            v = cost(q, c)
            if v is not None:
                results[name].append((v, os.path.basename(path),
                                      (item or {}).get("title") or ""))
        if i % 20 == 0:
            print("# ... %d/%d" % (i, len(todo)), file=out)

    print("\n# RESULTS  (a TRUE match scores ~0.034; the non-match floor is ~0.10)", file=out)
    for name, _ in queries:
        hits = sorted(results[name])[:5]
        print("\n%s:" % name, file=out)
        for v, f, title in hits:
            flag = "   <== MATCH" if v <= 0.050 else ("   <-- worth an ear" if v < 0.07 else "")
            print("   %.4f  %-48s %s%s" % (v, f[:48], title[:30], flag), file=out)
    print("\n# done", file=out)


if __name__ == "__main__":
    main()
