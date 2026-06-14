"""CLI for the stream alignment engine.

  python3 -m streamalign groundtruth
      Prints each file's resolved hand master-start (seconds) and whether its audio
      is present, then the file count. This is the answer key the engine is graded
      against.

  python3 -m streamalign align d000-018 d001-026b
      Prints the measured offset (seconds + samples) and confidence for the pair;
      if both are in the ground truth, also the expected offset and error in ms.

  python3 -m streamalign validate
      Aligns every hand-verified pair and prints a per-pair error table (error in
      ms/samples, confidence), worst first, then a summary: median/max error, how
      many fall within the pass tolerance, and pairs skipped for missing audio.
      The headline "does the engine match Tim's hand work" check.

  python3 -m streamalign track-mix --meta track-metadata.json --sources sources_local
      G2 1st pass: chroma+DTW-align every synced original to its mix region, grade
      the recovered rate against the sync ground truth, and print a per-track table
      + summary (reliable / within-tolerance / flagged / no-original). Needs librosa
      (run with .venv/bin/python). --json writes the full results to a file.

Run from the repo's scripts/ dir or with scripts/ on PYTHONPATH.
"""

import argparse
import json
import sys

from . import align as _align
from . import audio as _audio
from . import groundtruth as _gt
from . import score as _score
from . import track_mix as _track_mix


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


def _cmd_track_mix(args):
    with open(args.meta, "r", encoding="utf-8") as handle:
        meta = json.load(handle)
    tracks = meta.get("tracks", meta)
    only = [int(t) for t in args.tracks] if args.tracks else None
    out = _track_mix.batch_align(tracks, args.sources, labels_dir=args.labels,
                                 hop=args.hop, tracks=only, rate_tol=args.rate_tol)
    print("%4s %9s %9s %9s %7s %7s %5s %5s"
          % ("trk", "rate", "gt_rate", "rate_err", "conf", "cost", "rel", "tol"))
    for r in out["results"]:
        if "error" in r:
            print("%4s  %s" % (r["track"], r["error"]))
            continue
        err = r.get("rate_err")
        print("%4d %9.5f %9.5f %9s %7.4f %7.4f %5s %5s"
              % (r["track"], r["rate"], (r["gt_rate"] or float("nan")),
                 ("%.5f" % err if err is not None else "-"),
                 r["confidence"], (r["norm_cost"] if r["norm_cost"] == r["norm_cost"]
                                   else float("nan")),
                 "Y" if r.get("reliable") else "-",
                 "Y" if r.get("within_tol") else "-"))
    print("\nreliable: %d %s" % (len(out["reliable"]), out["reliable"]))
    print("within rate tol (<=%.3f): %d %s"
          % (args.rate_tol, len(out["within_tol"]), out["within_tol"]))
    print("flagged unreliable: %d %s" % (len(out["flagged"]), out["flagged"]))
    if out["errored"]:
        print("errored: %d %s" % (len(out["errored"]), out["errored"]))
    print("no original (G4 missing-source signal): %d %s"
          % (len(out["no_original"]), out["no_original"]))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(out, handle, indent=2)
        print("\nwrote %s" % args.json)


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
    pt = sub.add_parser("track-mix", help="G2 1st pass: align synced originals to the mix")
    pt.add_argument("--meta", default="track-metadata.json", help="track-metadata.json path")
    pt.add_argument("--sources", default="sources_local", help="originals dir (NNN-*.ext)")
    pt.add_argument("--hop", type=int, default=2048, help="chroma hop length")
    pt.add_argument("--rate-tol", type=float, default=0.005, help="rate err pass tolerance")
    pt.add_argument("--tracks", nargs="*", help="limit to these track numbers")
    pt.add_argument("--json", default=None, help="write full results JSON here")
    args = parser.parse_args(argv)
    {"groundtruth": _cmd_groundtruth, "align": _cmd_align,
     "validate": _cmd_validate, "track-mix": _cmd_track_mix}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
