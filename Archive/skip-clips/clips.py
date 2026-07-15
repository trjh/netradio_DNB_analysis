"""Skip-check review clips (G1b).

For a detected skip between the **skipper** A (the recording that skips) and a
**reference** B (in sync over that segment), build one listenable clip that presents
the broadcast continuously across the skip, so Tim can hear whether the skip's
position and magnitude are right: in sync ⇒ coherent; wrong ⇒ doubling/dissonance.

Per Tim's spec: A+B combined until A skips; on a skip-AHEAD, B alone fills the
content A jumped over, then A+B resume; on a skip-BACK, B rewinds to where A
returned and A+B resume. 15 s before the skip + 15 s after.

Convention: the walk's offset(t) means A[t] ~ B[t − offset], so B-local for A-local
t is `t − offset`. Clips + annotations are appended to a manifest the clip review
player (`player/public/clips/`) reads. ffmpeg writes the audio; numpy builds it.
"""

import json
import os
import subprocess

import numpy as np

from . import audio as _audio
from . import skips as _skips

PAD_S = 15.0  # seconds of A+B on each side of the skip


def _seg(arr, t0, t1, sr):
    i0 = max(0, int(round(t0 * sr)))
    i1 = min(len(arr), int(round(t1 * sr)))
    return np.asarray(arr[i0:i1], dtype=np.float64) if i1 > i0 else np.zeros(0)


def _mix(x, y):
    """Average two segments over their common length (average, not sum, so a B-alone
    bridge matches the A+B loudness; misalignment still doubles audibly)."""
    n = min(len(x), len(y))
    if n == 0:
        return x if len(x) else y
    return (x[:n] + y[:n]) * 0.5


def _norm(a):
    peak = float(np.max(np.abs(a))) if len(a) else 0.0
    return (a / peak * 0.95) if peak > 1e-9 else a


def _offset_at(walk, t, max_dist_s=5.0):
    """Offset of the confident walk point nearest time t (None if none close)."""
    best = None
    for wt, wo, wc in walk:
        if wc < 0.8:
            continue
        d = abs(wt - t)
        if best is None or d < best[0]:
            best = (d, wo)
    return best[1] if best and best[0] <= max_dist_s else None


def make_skip_clip(skipper, reference, skip, walk, sr=_audio.SR, pad_s=PAD_S):
    """(clip_array, annotations) for one skip, or None if offsets can't be resolved.

    `skipper`/`reference` are mono float arrays; `walk`/`skip` from
    characterise_overlap/detect_skips on (skipper, reference).
    """
    p = skip["at_s"]
    off1 = _offset_at(walk, skip["before_s"])
    off2 = _offset_at(walk, skip["after_s"])
    if off1 is None or off2 is None:
        return None
    a, b = skipper, reference
    # Part 1: A+B for pad_s up to the skip.
    part1 = _mix(_seg(a, p - pad_s, p, sr), _seg(b, (p - pad_s) - off1, p - off1, sr))
    parts = [part1]
    ann = [{"t": 0.0, "label": "A+B in sync (offset %.3fs)" % off1}]
    t = len(part1) / sr
    gap_lo, gap_hi = p - off1, p - off2   # B-local: where pre-skip ends, post-skip resumes
    if gap_hi > gap_lo + 0.01:            # forward gap -> skip-ahead: B alone fills it
        bridge = _seg(b, gap_lo, gap_hi, sr)
        parts.append(bridge)
        ann.append({"t": pad_s, "label": "A skips AHEAD %.3fs - B fills the gap" % (gap_hi - gap_lo)})
        t += len(bridge) / sr
        ann.append({"t": t, "label": "A resumes; A+B in sync (offset %.3fs)" % off2})
    else:                                 # rewind -> skip-back: B replays, A+B resume
        ann.append({"t": pad_s, "label": "A skips BACK %.3fs - B rewinds" % (gap_lo - gap_hi)})
        ann.append({"t": pad_s, "label": "A+B resume (offset %.3fs)" % off2})
    # Part 3: A+B for pad_s after the skip.
    part3 = _mix(_seg(a, p, p + pad_s, sr), _seg(b, p - off2, p - off2 + pad_s, sr))
    parts.append(part3)
    clip = _norm(np.concatenate(parts))
    ann.append({"t": len(clip) / sr, "label": "clip end"})
    return clip, ann


def write_clip(arr, path, sr=_audio.SR):
    """Write a mono float array to an mp3 via ffmpeg (f32le on stdin)."""
    pcm = np.clip(arr, -1.0, 1.0).astype("<f4").tobytes()
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "f32le", "-ar", str(sr),
           "-ac", "1", "-i", "-", "-b:a", "96k", path]
    proc = subprocess.run(cmd, input=pcm, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg clip write failed: "
                           + proc.stderr.decode("utf-8", "replace")[-300:])


def generate_skip_clips(skipper_name, reference_name, a_start_s, a_end_s,
                        seed_offset_s, out_dir, sr=_audio.SR):
    """Characterise the overlap and write one skip-check clip per detected skip.

    `skipper_name` is the recording with the skip(s); `reference_name` the in-sync
    reference. Coordinates (a_start/a_end/seed) are in the SKIPPER's local time, with
    `seed_offset_s` the skipper→reference offset (skipper[t] ~ reference[t − offset]).
    Writes mp3s + appends entries to `out_dir/manifest.json`. Returns the entries.
    """
    a = _audio.load_audio(skipper_name)
    b = _audio.load_audio(reference_name)
    walk = _skips.walk_overlap(a, b, a_start_s, a_end_s, seed_offset_s)
    found = _skips.detect_skips(walk)
    os.makedirs(out_dir, exist_ok=True)
    sa, sb = _audio.stem_of(skipper_name), _audio.stem_of(reference_name)
    entries = []
    for i, skip in enumerate(found):
        made = make_skip_clip(a, b, skip, walk, sr=sr)
        if made is None:
            continue
        clip, ann = made
        cid = "%s_%s_skip%d" % (sa, sb, i + 1)
        fn = cid + ".mp3"
        write_clip(clip, os.path.join(out_dir, fn), sr=sr)
        entries.append({
            "id": cid, "audio": fn,
            "title": "%s ↔ %s: skip %d (%.3fs @ %s %.1fs)" % (
                sa, sb, i + 1, abs(skip["delta_s"]), sa, skip["at_s"]),
            "description": ("A=%s (skipper), B=%s (reference). A+B until the skip; "
                            "B bridges; A+B resume. Coherent = correct." % (sa, sb)),
            "duration": len(clip) / sr,
            "annotations": ann,
        })
    _append_manifest(out_dir, entries)
    return entries


def _append_manifest(out_dir, entries):
    path = os.path.join(out_dir, "manifest.json")
    data = {"clips": []}
    if os.path.isfile(path):
        try:
            with open(path) as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            data = {"clips": []}
    by_id = {c.get("id"): c for c in data.get("clips", []) if c.get("id")}
    for e in entries:
        by_id[e["id"]] = e   # replace same-id entries, keep others
    data["clips"] = list(by_id.values())
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(data, handle, indent=2)
    os.replace(tmp, path)
