"""CLI for the stream alignment engine.

  python3 -m streamalign groundtruth
  python3 -m streamalign align d000-018 d001-026b
  python3 -m streamalign validate            # align every hand-verified pair, score

Run from the repo's scripts/ dir or with scripts/ on PYTHONPATH.
"""

import argparse
import sys

from . import align as _align
from . import audio as _audio
from . import groundtruth as _gt
from . import score as _score


def _cmd_groundtruth(args):
    g = _gt.resolve_starts(args.labels)
    for stem in sorted(g, key=lambda k: g[k]):
        have = "audio" if _audio.find_audio_file(stem) else "NO-AUDIO"
        print("%-20s %14.6f  %s" % (stem, g[stem], have))
    print("# %d files" % len(g))


def _cmd_align(args):
    r = _align.align_pair(args.a, args.b, decim=args.decim)
    print("%s -> %s" % (r["a"], r["b"]))
    print("  offset: %.4f s  (%.1f samples)" % (r["offset_seconds"], r["offset_samples"]))
    print("  confidence: %.4f" % r["confidence"])
    g = _gt.resolve_starts(args.labels)
    if r["a"] in g and r["b"] in g:
        exp = g[r["b"]] - g[r["a"]]
        print("  ground truth: %.4f s  (error %.2f ms)"
              % (exp, (r["offset_seconds"] - exp) * 1000.0))


def _unique_edges(edges):
    seen = set()
    out = []
    for a, b in edges:
        key = tuple(sorted((a, b)))
        if key not in seen:
            seen.add(key)
            out.append((a, b))
    return out


def _cmd_validate(args):
    g = _gt.resolve_starts(args.labels)
    edges = _unique_edges(_gt.alignment_edges(args.labels))
    results = []
    skipped = []
    for a, b in edges:
        if a not in g or b not in g:
            continue
        if not _audio.find_audio_file(a) or not _audio.find_audio_file(b):
            skipped.append((a, b, "no-audio"))
            continue
        try:
            results.append(_align.align_pair(a, b, decim=args.decim))
        except Exception as exc:  # noqa: BLE001 - report and continue the sweep
            skipped.append((a, b, str(exc)[:60]))
    rows, summary = _score.score_pairwise(results, g)
    rows.sort(key=lambda r: -abs(r["error_samples"]))
    print("%-13s %-13s %10s %9s %7s" % ("a", "b", "err_ms", "err_samp", "conf"))
    for r in rows:
        flag = "  <-- check" if abs(r["error_samples"]) > args.tol else ""
        print("%-13s %-13s %10.2f %9.1f %7.3f%s"
              % (r["a"], r["b"], r["error_ms"], r["error_samples"],
                 r["confidence"] or 0.0, flag))
    print("\nsummary: n=%d  median=%.2f samp  max=%.1f samp (%.2f ms)"
          % (summary.get("n", 0), summary.get("median_samp", 0),
             summary.get("max_samp", 0), summary.get("max_ms", 0)))
    within = sum(1 for r in rows if abs(r["error_samples"]) <= args.tol)
    print("within %d samples (%.2f ms): %d/%d"
          % (args.tol, args.tol / _audio.SR * 1000.0, within, len(rows)))
    if skipped:
        print("skipped %d pairs (%s ...)"
              % (len(skipped), ", ".join("%s/%s:%s" % s for s in skipped[:4])))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="streamalign", description=__doc__)
    parser.add_argument("--labels", default=None, help="labels dir (default: repo labels/)")
    parser.add_argument("--decim", type=int, default=8, help="coarse decimation factor")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("groundtruth", help="dump resolved hand master-starts")
    pa = sub.add_parser("align", help="align two captures")
    pa.add_argument("a")
    pa.add_argument("b")
    pv = sub.add_parser("validate", help="align every hand-verified pair and score")
    pv.add_argument("--tol", type=int, default=16, help="pass tolerance in samples (16=1ms)")
    args = parser.parse_args(argv)
    {"groundtruth": _cmd_groundtruth, "align": _cmd_align,
     "validate": _cmd_validate}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
