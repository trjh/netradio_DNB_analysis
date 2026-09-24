"""Generate the canary set: `chroma/_canary/` — known audio + the signature it must produce.

The second bucket-bootstrap script (with `make_recipe.py`). A worker runs the canary before
taking real work: it downloads each `audio-<id>.flac`, recomputes the signature with the recipe,
and refuses to work unless it matches `expected-<id>.npy` within tolerance. So the canary needs
only INTERNAL consistency (audio ↔ expected), which means it is fully regenerable from any audio
source — a rebuild does not have to reproduce the old bytes, only a fresh consistent set.

Excerpts are cut from OUR OWN corpus (no rights issue, no external fetch). Source audio is a
private path, so it is passed in — this script names none:

    . .venv/bin/activate && python scripts/make_canary.py \\
        --source-dir "$NETRADIO_CANARY_SOURCE_DIR" --out ./_canary
    # then upload (private bucket/endpoint — see the player runbook):
    aws --endpoint-url <ep> s3 sync ./_canary s3://<bucket>/chroma/_canary/

Deterministic: given the same source dir it picks the same files (sorted) and offsets, so a
rebuild is repeatable. `ffmpeg` comes from --ffmpeg, then $NETRADIO_FFMPEG, then imageio-ffmpeg,
then PATH.

The harvester's self-test re-signs one known track's file whenever the feeder puts it back and
compares the result with the signature stored under its key (`NETRADIO_CANARY_KEY`). `--key <url>` prints that key for a URL — the pool's one rule
(`u` + sha1(url)[:20]), the same stem every signature in the bucket is filed under:

    python scripts/make_canary.py --key '<the canary URL>'

(Single quotes: inside double quotes the shell would run any $(...) or backticks in the URL.)
"""

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np                                   # noqa: E402

import chroma_recipe                                 # noqa: E402


def find_ffmpeg(explicit=None):
    if explicit:
        return explicit
    env = os.environ.get("NETRADIO_FFMPEG")
    if env:
        return env
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def decode(ffmpeg, src, offset, seconds):
    """src -> mono float32 @ recipe SR, `seconds` from `offset`."""
    p = subprocess.run([ffmpeg, "-v", "error", "-ss", str(offset), "-t", str(seconds),
                        "-i", src, "-ac", "1", "-ar", str(chroma_recipe.SR), "-f", "f32le",
                        "pipe:1"], capture_output=True, timeout=300)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg: %s" % p.stderr.decode()[:200])
    return np.frombuffer(p.stdout, dtype="float32")


def pack(chroma):
    """chroma (float32) -> (expected .npy bytes, sha256). The stored form is float16 — the same
    cast the pool uses — so a worker recomputing on the same audio gets the same bytes. The
    digest is of the serialised .npy FILE, numpy header included: what `expected-<id>.npy` on
    disk hashes to, NOT the raw sample bytes. Anything that checks a canary against
    `sha256_expected` hashes through here, so the test and the script cannot drift apart."""
    c = np.asarray(chroma).astype(chroma_recipe.STORE_DTYPE)
    buf = io.BytesIO()
    np.save(buf, c)
    return buf.getvalue(), hashlib.sha256(buf.getvalue()).hexdigest()


def sign(y):
    """samples -> (expected .npy bytes, sha256, shape), via `pack`."""
    c = chroma_recipe.compute_chroma(y)
    npy, sha = pack(c)
    return npy, sha, list(c.shape)


def build(source_dir, out_dir, ffmpeg, count, seconds, offset, exts=(".mp3", ".flac", ".wav",
                                                                      ".m4a", ".au")):
    srcs = sorted(f for f in os.listdir(source_dir)
                  if os.path.splitext(f)[1].lower() in exts)[:count]
    if not srcs:
        raise SystemExit("no audio in %s" % source_dir)
    os.makedirs(out_dir, exist_ok=True)
    items = []
    for i, name in enumerate(srcs, 1):
        cid = "canary-%02d" % i
        # Encode the excerpt to FLAC FIRST — that .flac is the artifact a worker downloads. FLAC
        # is an integer codec, so encoding float PCM quantises it; therefore we must sign the
        # audio AS DECODED BACK FROM THE FLAC (the worker's exact path), or audio and expected
        # would not be a matched pair. (A live round-trip check enforces this — see the tests /
        # the recovery proof.)
        flac = os.path.join(out_dir, "audio-%s.flac" % cid)
        enc = subprocess.run([ffmpeg, "-y", "-v", "error", "-ss", str(offset), "-t", str(seconds),
                              "-i", os.path.join(source_dir, name), "-ac", "1",
                              "-ar", str(chroma_recipe.SR), "-c:a", "flac", flac],
                             capture_output=True, timeout=120)
        if enc.returncode != 0:
            raise RuntimeError("flac encode: %s" % enc.stderr.decode()[:200])
        y = decode(ffmpeg, flac, 0, seconds)         # decode the FLAC, exactly as a worker will
        if len(y) < chroma_recipe.MIN_SECONDS * chroma_recipe.SR:
            os.remove(flac)
            continue                                 # too short after the cut; skip quietly
        npy, sha, shape = sign(y)
        with open(os.path.join(out_dir, "expected-%s.npy" % cid), "wb") as fh:
            fh.write(npy)
        items.append({"id": cid, "audio": "audio-%s.flac" % cid,
                      "expected": "expected-%s.npy" % cid, "sha256_expected": sha,
                      "shape": shape, "source": name})
    manifest = {"recipe_version": chroma_recipe.RECIPE_VERSION,
                "tolerance": chroma_recipe.TOLERANCE,
                "comparison": "worker recomputes each audio and compares to expected within "
                              "tolerance (float32 view)", "items": items}
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1)
    return manifest


def sig_key_for(url):
    """The signature file name a URL is filed under: the pool's one key rule
    (docs/HARVEST_FEED.md) -- `u` + the first 20 hex of the SHA-1 of the URL as given,
    fragment included. The stem of every signature in the bucket is this rule, and it
    never changes."""
    return "u" + hashlib.sha1(url.encode()).hexdigest()[:20]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source-dir", default=os.environ.get("NETRADIO_CANARY_SOURCE_DIR")
                    or os.environ.get("NETRADIO_MP3_DIR"),
                    help="corpus audio to cut excerpts from (or $NETRADIO_CANARY_SOURCE_DIR / "
                         "$NETRADIO_MP3_DIR)")
    ap.add_argument("--out", help="output dir for the canary set")
    ap.add_argument("--ffmpeg")
    ap.add_argument("--count", type=int, default=3)
    ap.add_argument("--seconds", type=float, default=75.0)
    ap.add_argument("--offset", type=float, default=120.0)
    ap.add_argument("--key", metavar="URL",
                    help="print the pool's key for URL (u + sha1(url)[:20]) and exit; the same "
                         "rule every signature in the bucket is filed under. Set "
                         "NETRADIO_CANARY_KEY in .env to the result to name the canary the "
                         "harvester re-signs and compares.")
    args = ap.parse_args()

    if args.key is not None:
        print(sig_key_for(args.key))
        return

    if not args.out:
        raise SystemExit("--out is required (or use --key <url> to print the canary's key)")
    if not args.source_dir:
        raise SystemExit("--source-dir (or $NETRADIO_CANARY_SOURCE_DIR / $NETRADIO_MP3_DIR) "
                         "is required")
    ffmpeg = find_ffmpeg(args.ffmpeg)
    if not ffmpeg:
        raise SystemExit("no ffmpeg (set --ffmpeg or $NETRADIO_FFMPEG, or pip install "
                         "imageio-ffmpeg)")
    m = build(args.source_dir, args.out, ffmpeg, args.count, args.seconds, args.offset)
    print("wrote %d canary item(s) to %s" % (len(m["items"]), args.out), file=sys.stderr)


if __name__ == "__main__":
    main()
