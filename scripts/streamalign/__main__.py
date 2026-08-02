"""CLI for the stream alignment engine.

  python3 -m streamalign groundtruth
      Prints each file's resolved hand master-start (seconds) and whether its audio
      is present, then the file count. This is the answer key the engine is graded
      against.

  python3 -m streamalign align d000-018 d001-026b
      Prints the measured offset (seconds + samples) and confidence for the pair;
      if both are in the ground truth, also the expected offset and error in ms.

  python3 -m streamalign validate
      Verifies every hand-verified pair by comparing ONLY its overlapping audio
      (equal-length slices tiled across the labeled overlap): confirmed (residual
      ~0, high confidence), suspect (real overlap but the audio doesn't match at the
      labeled offset — worst first, resid_ms = how far off), or adjacent (labels
      place the pair end-to-end, no overlap — listed apart, not an error). Ends with
      a graded / confirmed / suspect / adjacent / skipped summary. The headline
      "does the audio confirm Tim's hand labels" check.

  python3 -m streamalign track-mix --meta track-metadata.json --sources sources_local
      G2 1st pass: chroma+DTW-align every synced original to its mix region, grade
      the recovered rate against the sync ground truth, and print a per-track table
      + summary (reliable / within-tolerance / flagged / no-original). Needs librosa
      (run with .venv/bin/python). --json writes the full results to a file.

  .venv/bin/python -m streamalign match-hints d376-395 72
      Align-tool Pass 1: seat original 072 inside capture d376-395 (MATCH coarse
      map via sonic-annotator, then rate-swept + rate-corrected GCC-PHAT anchors,
      loop-shift disambiguation by whole-overlap anchor mass) and emit the paired
      *.match.hints.tsv files for Audacity import. The stream is trimmed to the
      original's expected neighbourhood before MATCH runs (--around, else derived
      from the track's master span; AP-02); the MATCH path then referees each PHAT
      anchor (per-anchor delta, QUESTION on disagreement; AP-03); solo-anchor
      moments seed the rate sweep where librosa is available (AP-13). `--all` (or
      an explicit list of track numbers) batches every overlapping track (AP-16).
      Needs numpy + ffmpeg (venv); needs sonic-annotator + the match-vamp plugin
      unless --csv provides a pre-exported match:a_b path CSV.

  .venv/bin/python -m streamalign inspect-slice d376-395 72 --stream-t 196 --orig-t 228.4
      Align-tool Pass 2: JSON slices (or --refine snap-to-best, --refine --engine
      match for the MATCH-path snap target, --context SECONDS for the zoomed-out
      context strip) for the player's /align inspector; all DSP happens here.

  .venv/bin/python -m streamalign inspect-worker --sources sources_local
      AP-08 keep-warm worker: JSON-lines requests on stdin (op: slice|refine|
      context, same fields/guards as inspect-slice), one JSON response line each;
      holds the current (stem, orig, rate) pair's decoded audio between requests.

Run from the repo's scripts/ dir or with scripts/ on PYTHONPATH.
"""

import argparse
import json
import sys

import os

from . import align as _align
from . import audio as _audio
from . import emit_labels as _emit
from . import groundtruth as _gt
from . import skip_review as _skip_review
from . import tail as _tail
from . import track_mix as _track_mix


def _cmd_groundtruth(args):
    g = _gt.resolve_starts(args.labels)
    for stem in sorted(g, key=lambda k: g[k]):
        have = "audio" if _audio.find_audio_file(stem) else "NO-AUDIO"
        print("%-20s %14.6f  %s" % (stem, g[stem], have))
    print("# %d files" % len(g))


def _cmd_starter(args):
    written = _emit.emit_starter(args.owner, labels_dir=args.labels, out_dir=args.out)
    if not written:
        print("no file_<other>: links found in %s.labels.tsv" % _audio.stem_of(args.owner))
        return
    for other in sorted(written):
        print("%-20s -> %s" % (other, written[other]))
    print("# %d starter file(s) (seed-only; excluded from import/solve/build)" % len(written))


def _match_hints_one(stem, stream, orig_num, args, meta, master):
    """One capture+original run: trim -> MATCH -> convert -> referee -> emit."""
    import tempfile

    from . import hints as _hints
    from . import matchconv as _mc
    from . import track_mix as _tm

    orig_path = _tm.find_original(orig_num, args.sources)
    if not orig_path:
        print("no original %03d-* under %s" % (int(orig_num), args.sources))
        return
    orig = _audio.load_audio(orig_path, use_cache=False)
    stream_len_s = len(stream) / _audio.SR
    if args.csv:
        pairs = _mc.parse_ab_csv(args.csv)
    else:
        # AP-02: make MATCH's forced files-start-together assumption approximately true
        # by handing it only the slice of the capture where the original is expected.
        around = args.around
        if around is None:
            around = _mc.derive_around(orig_num, meta, master)
            if around is not None:
                print("orig %03d: expected near capture-local %.1fs (master span - "
                      "capture start)" % (int(orig_num), around))
        if around is None:
            lo, hi = 0.0, stream_len_s
            print("orig %03d: no expected position (no --around, no metadata span, or "
                  "unplaced capture) -- MATCH runs on the whole capture" % int(orig_num))
        else:
            lo, hi = _mc.trim_window(around, len(orig) / _audio.SR, stream_len_s)
            print("orig %03d: MATCH runs on the trimmed %.1f-%.1fs slice"
                  % (int(orig_num), lo, hi))
        with tempfile.TemporaryDirectory(prefix="matchconv-") as tmp:
            s_wav = _mc.write_wav16(os.path.join(tmp, stem + ".wav"),
                                    stream[int(lo * _audio.SR):int(hi * _audio.SR)])
            o_wav = _mc.write_wav16(os.path.join(tmp, "orig%03d.wav" % int(orig_num)), orig)
            pairs = _mc.apply_trim_offset(_mc.parse_ab_csv(_mc.run_match(s_wav, o_wav, tmp)),
                                          lo)
    result = _mc.convert(stream, orig, pairs, anchor_count=args.anchors, solo_probes=True)
    rate, anchors = result["rate"], result["anchors"]
    print("rate %.5f (original runs %+.2f%% vs stream)   polarity %s   sweep score %.2f"
          % (rate, (rate - 1.0) * 100.0,
             "INVERTED" if result["inverted"] else "normal", result["sweep_conf"]))
    kept = sum(1 for m in result["grid"] if not m[4] and m[2] >= _mc.MIN_CONF)
    print("# grid: %d probe(s), %d usable, %d selected; coverage %d%% of possible overlap"
          "%s" % (len(result["grid"]), kept, len(anchors),
                  round(result["coverage"] * 100),
                  " (LOW -- possible loop-shift, verify by ear)"
                  if result["coverage"] < 0.5 else ""))
    if result["solo_probes"]:
        print("# solo-anchor probes seeded the sweep at: %s"
              % "  ".join("%.1fs" % p for p in result["solo_probes"]))
    if result["ambiguous"]:
        print("!! a rival seat scored nearly as high -- likely whole-bar loop shift; "
              "verify a structurally unique moment by ear")
    deltas = result["match_deltas"]
    for k, (a, off, conf, _inv, _out) in enumerate(anchors, start=1):
        d = deltas[k - 1] if k <= len(deltas) else None
        print("  sync %d  stream %9.3fs  <->  orig %9.3fs  confidence %.1f/10  MATCH %s"
              % (k, a, (a - off) * rate, 10.0 * conf,
                 ("%+.3fs" % d) if d is not None else "n/a"))
    if not anchors:
        print("no anchors above confidence %.2f -- nothing to emit" % _mc.MIN_CONF)
        return
    stream_rows, orig_rows = _mc.build_rows(
        orig_num, anchors, rate, result["inverted"],
        orig_native_len_s=len(orig) / _audio.SR, stream_len_s=stream_len_s,
        coverage=result["coverage"], ambiguous=result["ambiguous"],
        match_deltas=deltas)
    if args.dry_run:
        for a, b, text in sorted(stream_rows + orig_rows, key=lambda r: (r[0], r[1])):
            print("%10.3f %10.3f  %s" % (a, b, text))
        return
    out_dir = args.out or (args.labels or _gt.LABELS_DIR)
    os.makedirs(out_dir, exist_ok=True)
    s_name, o_name = _mc.hint_filenames(stem, orig_num)
    for rows, name in ((stream_rows, s_name), (orig_rows, o_name)):
        print("wrote %s" % _hints.write_hints(rows, os.path.join(out_dir, name)))
    print("Import each in Audacity: File > Import > Labels -- they land as their OWN "
          "tracks, beside your labels. Nothing you have is touched.")


def _cmd_match_hints(args):
    from . import hints as _hints
    from . import matchconv as _mc
    from . import track_mix as _tm

    stem = _audio.stem_of(args.stem)
    if not _audio.find_audio_file(stem):
        print("no audio for capture %s" % stem)
        return
    stream = _audio.load_audio(stem)
    meta = _hints._load_track_metadata()
    starts = _gt.resolve_starts(args.labels)
    master = starts.get(stem)

    if args.orig:                      # an explicit list always wins (with or without --all)
        nums = [int(n) for n in args.orig]
    elif args.all:                     # AP-16: every overlapping track with an original
        if master is None:
            print("capture %s has no resolved master start -- cannot derive its overlap "
                  "worklist; give explicit track numbers instead" % stem)
            return
        overlapping = _mc.tracks_overlapping(master, len(stream) / _audio.SR, meta)
        if not overlapping:
            print("no track's master span overlaps capture %s" % stem)
            return
        nums, skipped = [], []
        for num, _mb, _me in overlapping:
            (nums if _tm.find_original(num, args.sources) else skipped).append(num)
        if skipped:
            print("skipping %d overlapping track(s) with no original under %s: %s"
                  % (len(skipped), args.sources, " ".join("%03d" % n for n in skipped)))
        if not nums:
            print("no overlapping track has an original under %s" % args.sources)
            return
        print("capture %s: running %d overlapping track(s): %s"
              % (stem, len(nums), " ".join("%03d" % n for n in nums)))
    else:
        print("give a track number (or a list), or --all for every overlapping track")
        return
    if args.around is not None and len(nums) > 1:
        print("--around describes ONE original's position; drop it for batch runs")
        return
    if args.csv and len(nums) > 1:
        print("--csv is a single pre-exported pair run; drop it for batch runs")
        return
    for num in nums:
        if len(nums) > 1:
            print("\n== %s x orig %03d ==" % (stem, num))
        _match_hints_one(stem, stream, num, args, meta, master)


def _cmd_sync_audit(args):
    from . import sync_audit as _sa
    result = _sa.audit(labels_dir=args.labels, sources_dir=args.sources,
                       tracks=args.tracks, background=args.background,
                       search_s=args.search)
    if args.only_unchecked:
        # AP-04: the hand points the system has never touched (no `verified` token)
        result = dict(result, points=[p for p in result["points"] if not p.get("verified")])
    print(_sa.render(result))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(result, handle)
        print("wrote %s" % args.json)


def _cmd_sync_sweep(args):
    from . import sync_audit as _sa
    result = _sa.sweep_point(args.track, args.label, labels_dir=args.labels,
                             sources_dir=args.sources, wins=tuple(args.wins),
                             search_s=args.search)
    print("track %03d %s in %s  seat conf %.2f  (stream %.0f s)"
          % (result["track"], result["label"], result["stem"], result["seat_conf"],
             result["stream_len_s"]))
    for win, w in result["windows"].items():
        if w.get("error"):
            print("  win %ss: %s" % (win, w["error"]))
            continue
        if w.get("front_clipped_s") or w.get("window_clipped_to_s"):
            print("  win %ss: clipped by the original's edge (front %.2fs, length %s)"
                  % (win, w.get("front_clipped_s") or 0.0,
                     "%.2fs" % w["window_clipped_to_s"] if w.get("window_clipped_to_s")
                     else "full"))
        print("  win %ss: %d positions  min %.1f%% at %.3fs (true seat %.3fs%s)  "
              "true-seat residual %.1f%%"
              % (win, w["n_positions"], w["min_residual"], w["min_at_s"],
                 w["true_at_s"], " = MIN" if w["true_is_min"] else "",
                 w["true_residual"] if w["true_residual"] is not None else float("nan")))
        print("  win %ss: positions below thresholds: %s"
              % (win, "  ".join("<%s%%: %d" % kv for kv in sorted(
                  w["below"].items(), key=lambda kv: int(kv[0])))))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(result, handle)
        print("wrote %s" % args.json)


def _cmd_inspect_slice(args):
    from . import inspect_slice as _isl
    from . import track_mix as _tm

    stem = _audio.stem_of(args.stem)
    orig_path = _tm.find_original(args.orig, args.sources)
    out = None
    if args.refine and args.context is not None:
        out = {"error": "--refine and --context are mutually exclusive"}
    elif not _audio.find_audio_file(stem):
        out = {"error": "no audio for capture %s" % stem}
    elif not orig_path:
        out = {"error": "no original %03d-* under %s" % (int(args.orig), args.sources)}
    if out is None:
        if args.refine:
            fn, kwargs = _isl.refine_seat, {"radius_s": args.radius,
                                            "engine": args.engine}
        elif args.context is not None:
            fn, kwargs = _isl.build_context, {"context_s": args.context}
        else:
            fn, kwargs = _isl.build_slices, {}
        try:
            out = fn(stem, orig_path, args.stream_t, args.orig_t, args.rate,
                     args.invert, args.win, **kwargs)
        except Exception as exc:   # stdout must stay JSON: the player relays it
            out = {"error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}
    print(json.dumps(out))
    if "error" in out:
        raise SystemExit(1)


def _cmd_inspect_worker(args):
    from . import inspect_worker as _iw
    _iw.serve(args.sources)


def _cmd_hints(args):
    from . import hints as _hints
    rows, diag = _hints.build_hints(args.stem, labels_dir=args.labels, decim=args.decim)
    if args.dry_run:
        for a, b, text in sorted(rows, key=lambda r: (r[0], r[1])):
            print("%10.3f %10.3f  %s" % (a, b, text))
    else:
        out_dir = args.out or (args.labels or _gt.LABELS_DIR)
        os.makedirs(out_dir, exist_ok=True)
        path = _hints.write_hints(
            rows, os.path.join(out_dir, _audio.stem_of(args.stem) + ".hints.tsv"))
        print("wrote %s" % path)
        print("Import it in Audacity: File > Import > Labels -- it lands as its OWN track, "
              "beside your labels. Nothing you have is touched.")
    n_q = diag["questions"]
    print("# %d row(s): %d question(s) for you, %d overlapping neighbour(s), %d skip candidate(s)"
          % (len(rows), n_q, len(diag["neighbours"]), diag["skips"]))


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
    graded = []
    adjacent = []
    skipped = []
    for a, b in edges:
        if a not in g or b not in g:
            continue
        if not _audio.find_audio_file(a) or not _audio.find_audio_file(b):
            skipped.append((a, b, "no-audio"))
            continue
        try:
            r = _align.slice_check(a, b, g[a], g[b],
                                   min_overlap_s=args.min_overlap)
        except Exception as exc:  # noqa: BLE001 - report and continue the sweep
            skipped.append((a, b, str(exc)[:60]))
            continue
        (adjacent if r["status"] == "adjacent" else graded).append(r)

    def confirmed(r):
        return (r["confidence"] or 0.0) >= args.conf_ok and abs(r["residual_samples"]) <= args.tol

    # Each hand-verified pair is graded by comparing ONLY its overlapping audio,
    # positioned at the labels' offset. This asks "does the audio confirm the labels?"
    # robustly, without a full-signal search that misdecodes large lags on very
    # different-length pairs. Three outcomes: confirmed (audio matches), suspect
    # (real overlap but doesn't match), adjacent (no overlap — not gradeable).
    ok = [r for r in graded if confirmed(r)]
    suspect = [r for r in graded if not confirmed(r)]
    suspect.sort(key=lambda r: ((r["confidence"] or 0.0), -abs(r["residual_samples"])))
    ok.sort(key=lambda r: -abs(r["residual_samples"]))

    print("%-13s %-13s %10s %9s %7s %8s" % ("a", "b", "resid_ms", "resid_sm", "conf", "overlap"))
    for r in suspect + ok:
        flag = "  <-- SUSPECT" if not confirmed(r) else ""
        print("%-13s %-13s %10.2f %9.1f %7.3f %7.0fs%s"
              % (r["a"], r["b"], r["residual_ms"], r["residual_samples"],
                 r["confidence"] or 0.0, r["overlap_seconds"], flag))
    if adjacent:
        print("\nadjacent (overlap < %.1fs — labels place them end-to-end, nothing to compare):"
              % args.min_overlap)
        for r in sorted(adjacent, key=lambda r: r["a"]):
            print("  %-13s %-13s  overlap=%.2fs" % (r["a"], r["b"], r["overlap_seconds"]))
    print("\nsummary: %d graded (%d confirmed, %d suspect), %d adjacent, %d skipped"
          % (len(graded), len(ok), len(suspect), len(adjacent), len(skipped)))
    print("confirmed = conf>=%.2f and |residual|<=%d samp (%.2f ms)"
          % (args.conf_ok, args.tol, args.tol / _audio.SR * 1000.0))
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


def _cmd_tail_solve(args):
    res = _tail.solve_tail(args.labels)
    diag = res["diagnostics"]
    print("== Session B absolute master placements (loop-wrap anchor, d000-018=0) ==")
    for stem in sorted(res["absolute"], key=lambda k: res["absolute"][k]):
        d = diag[stem]
        print("  %-12s master_start=%10.3f  edges=%d  max_resid=%.3f  %s"
              % (stem, res["absolute"][stem], d["edges"], d["max_residual_s"] or 0.0,
                 "CORROB" if d["corroborated"] else "single"))
    print("\nloop-wrap anchor S*=%.3f  (spread over %d clean edges: %.3f s)"
          % (res["s_star"], len(res["anchor_estimates"]), res["anchor_spread_s"]))
    for e in res["anchor_estimates"]:
        print("  %-12s -> %-11s off=%9.3f conf=%.3f  S*=%.3f"
              % (e["a"], e["b"], e["offset_s"], e["conf"], e["s_star"]))
    b = res["bridge"]
    print("\ncandidate (NOT emitted): %s placed off %s -> master_start=%.3f (conf %.3f, partial overlap)"
          % (b["b"], b["a"], b["master_start"], b["conf"]))
    print("orphan    (NOT emitted): %s — butt-jointed both sides, no audio anchor" % res["orphan"])
    if args.emit:
        written = _tail.emit(res, out_dir=args.out, labels_dir=args.labels)
        print("\nemitted %d AUTO GENERATED label file(s) to %s"
              % (len(written), args.out or _gt.LABELS_DIR))
        for stem in sorted(written):
            print("  %s" % written[stem])
    else:
        print("\n(report only; pass --emit to write <stem>.auto.labels.tsv for the CORROB files)")


def _cmd_skip_confirm(args):
    out_dir = args.out or args.labels
    status, cand = _skip_review.decide(args.id, "confirm", out_dir, labels_dir=args.labels,
                                       owner=args.owner)
    stem, _at, delta, _b, _a, ref = _skip_review.reattribute(cand, args.owner)
    word, mag = _skip_review._direction(delta)
    print("%s: confirmed skip %s %.3fs into %s.labels.tsv (verified %s)"
          % (status, word, mag, stem, ref))


def _cmd_skip_reject(args):
    out_dir = args.out or args.labels
    status, cand = _skip_review.decide(args.id, "reject", out_dir, labels_dir=args.labels,
                                       owner=args.owner, note=args.note or "")
    stem, _at, delta, _b, _a, _ref = _skip_review.reattribute(cand, args.owner)
    word, mag = _skip_review._direction(delta)
    print("%s: rejected skip %s %.3fs for %s → %s"
          % (status, word, mag, stem, _skip_review.REJECTIONS_NAME))


def _cmd_skip_rejections(args):
    rej = _skip_review.load_rejections(args.labels)
    for r in rej:
        word, mag = _skip_review._direction(r["delta_s"])
        print("%-13s skip %-5s %.3fs @ %.1fs  ref=%s  %s"
              % (r["stem"], word, mag, r["at_s"], r["reference"], r["note"]))
    print("# %d rejection(s)" % len(rej))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="streamalign", description=__doc__)
    parser.add_argument("--labels", default=None, help="labels dir (default: repo labels/)")
    parser.add_argument("--decim", type=int, default=8, help="coarse decimation factor")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("groundtruth", help="dump resolved hand master-starts")
    pa = sub.add_parser("align", help="align two captures")
    pa.add_argument("a")
    pa.add_argument("b")
    pv = sub.add_parser("validate",
                        help="verify every hand-verified pair by comparing its overlapping audio")
    pv.add_argument("--tol", type=int, default=16, help="confirm tolerance in samples (16=1ms)")
    pv.add_argument("--min-overlap", type=float, default=5.0,
                    help="seconds of overlap required to grade a pair (else 'adjacent')")
    pv.add_argument("--conf-ok", type=float, default=0.5,
                    help="min normalized-correlation confidence to confirm a pair")
    pt = sub.add_parser("track-mix", help="G2 1st pass: align synced originals to the mix")
    pt.add_argument("--meta", default="track-metadata.json", help="track-metadata.json path")
    pt.add_argument("--sources", default="sources_local", help="originals dir (NNN-*.ext)")
    pt.add_argument("--hop", type=int, default=2048, help="chroma hop length")
    pt.add_argument("--rate-tol", type=float, default=0.005, help="rate err pass tolerance")
    pt.add_argument("--tracks", nargs="*", help="limit to these track numbers")
    pt.add_argument("--json", default=None, help="write full results JSON here")

    ptail = sub.add_parser("tail-solve",
                           help="P5: place the tail captures via the dense overlap + loop-wrap anchor")
    ptail.add_argument("--emit", action="store_true",
                       help="write <stem>.auto.labels.tsv for the corroborated Session-B placements")
    ptail.add_argument("--out", default=None, help="output dir for emitted labels (default: labels/)")

    # Skips are detected and surfaced by `streamalign hints` (as `note QUESTION: … [id …]`
    # rows in <stem>.hints.tsv, backed by skip-candidates.json). These act on a skip by that id.
    pc = sub.add_parser("skip-confirm",
                        help="F1: confirm a skip → skipper's hand <stem>.labels.tsv")
    pc.add_argument("id", help="skip id from the hints row / skip-candidates.json")
    pc.add_argument("--out", default=None, help="dir holding skip-candidates.json (default: labels dir)")
    pc.add_argument("--owner", default=None, help="attribute the skip to this stem instead")
    pr = sub.add_parser("skip-reject",
                        help="F1: reject a skip → labels/skip-rejections.tsv (engine stops re-proposing it)")
    pr.add_argument("id", help="skip id from the hints row / skip-candidates.json")
    pr.add_argument("--out", default=None, help="dir holding skip-candidates.json (default: labels dir)")
    pr.add_argument("--owner", default=None, help="attribute the skip to this stem instead")
    pr.add_argument("--note", default=None, help="optional note recorded with the rejection")
    sub.add_parser("skip-rejections", help="F1: list recorded skip rejections")
    pst = sub.add_parser("starter",
                         help="A2: emit <other>.starter.labels.tsv seeds from file_<other>: links")
    pst.add_argument("owner", help="owner capture stem whose file_<other>: links seed neighbours")
    pst.add_argument("--out", default=None, help="output dir (default: labels dir)")

    pm = sub.add_parser("match-hints",
                        help="align-tool Pass 1: MATCH-seed + PHAT-refine an original inside "
                             "a capture; emit paired <stem>.origNNN.match.hints.tsv / "
                             "origNNN.<stem>.match.hints.tsv (never labels)")
    pm.add_argument("stem", help="capture stem the original plays inside, e.g. d376-395")
    pm.add_argument("orig", type=int, nargs="*",
                    help="original track number(s) (NNN, per sources_local); an explicit "
                         "list overrides --all")
    pm.add_argument("--all", action="store_true",
                    help="AP-16: run every track whose master span overlaps the capture "
                         "(tracks with no original on disk are listed and skipped)")
    pm.add_argument("--around", type=float, default=None,
                    help="AP-02: rough capture-local seconds of the original's start; the "
                         "stream is trimmed to that neighbourhood before MATCH runs "
                         "(auto-derived from the track's master span when omitted)")
    pm.add_argument("--csv", default=None,
                    help="pre-exported match:a_b CSV (else sonic-annotator is run)")
    pm.add_argument("--sources", default="sources_local", help="originals dir (NNN-*.ext)")
    pm.add_argument("--anchors", type=int, default=8, help="sync points to emit")
    pm.add_argument("--out", default=None, help="output dir (default: labels dir)")
    pm.add_argument("--dry-run", action="store_true",
                    help="print the hint rows instead of writing files")

    psa = sub.add_parser("sync-audit",
                        help="grade every hand origNNN sync point against the audio "
                             "(seat confidence, hand error, inspector residuals); "
                             "--background N adds mismatch samples for calibration")
    psa.add_argument("--sources", default="sources_local", help="originals dir (NNN-*.ext)")
    psa.add_argument("--tracks", nargs="*", type=int, help="limit to these track numbers")
    psa.add_argument("--background", type=int, default=0,
                     help="wrong-place samples per audited point (default 0)")
    psa.add_argument("--search", type=float, default=1.5,
                     help="seat search radius seconds around the reconstructed seat")
    psa.add_argument("--only-unchecked", action="store_true",
                     help="AP-04: list only the hand points that do NOT carry the "
                          "`verified` token (never touched by the system)")
    psa.add_argument("--json", default=None, help="write the full results JSON here")

    psw = sub.add_parser("sync-sweep",
                        help="sweep ONE hand sync point's original window against EVERY "
                             "sample position of its capture; histogram + minima (the "
                             "align-tool color-calibration curve)")
    psw.add_argument("track", type=int, help="track number with hand sync ground truth")
    psw.add_argument("label", help="sync point label (A, B, 1, ...)")
    psw.add_argument("--wins", nargs="*", type=float, default=[6.0, 0.6],
                     help="window sizes seconds (default: 6.0 0.6)")
    psw.add_argument("--sources", default="sources_local", help="originals dir (NNN-*.ext)")
    psw.add_argument("--search", type=float, default=1.5, help="seat search radius seconds")
    psw.add_argument("--json", default=None, help="write the full results JSON here")

    pi = sub.add_parser("inspect-slice",
                        help="align-tool Pass 2: emit JSON slices (or --refine snap-to-best) "
                             "for the player's /align inspector; all DSP happens here")
    pi.add_argument("stem", help="capture stem, e.g. d376-395")
    pi.add_argument("orig", type=int, help="original track number (NNN)")
    pi.add_argument("--stream-t", type=float, required=True, help="capture-local seconds")
    pi.add_argument("--orig-t", type=float, required=True, help="original-native seconds")
    pi.add_argument("--rate", type=float, default=1.0, help="original-seconds per stream-second")
    pi.add_argument("--invert", action="store_true", help="apply the polarity flip (M2)")
    pi.add_argument("--win", type=float, default=6.0, help="comparison window seconds (cap 30)")
    pi.add_argument("--sources", default="sources_local", help="originals dir (NNN-*.ext)")
    pi.add_argument("--refine", action="store_true", help="snap-to-best instead of slices")
    pi.add_argument("--radius", type=float, default=0.05, help="refine radius seconds (cap 2)")
    pi.add_argument("--engine", choices=("phat", "match"), default="phat",
                    help="AP-14: --refine's snap engine -- phat (GCC-PHAT residual, the "
                         "default) or match (the trimmed MATCH path's implied instant; "
                         "needs sonic-annotator)")
    pi.add_argument("--context", type=float, default=None,
                    help="AP-05: emit the zoomed-out context strip instead -- decimated "
                         "min/max columns over +/-SECONDS around the point (default "
                         "45 when the player asks, cap 60); not full audio")

    pw = sub.add_parser("inspect-worker",
                        help="AP-08: keep-warm DSP worker -- JSON-lines requests on stdin "
                             "(op: slice|refine|context, same fields as inspect-slice), one "
                             "JSON response line each; caches the current (stem, orig, "
                             "rate) pair's decoded audio between requests")
    pw.add_argument("--sources", default="sources_local", help="originals dir (NNN-*.ext)")

    ph = sub.add_parser("hints",
                        help="emit <stem>.hints.tsv: suggested sync/start/end/skips + questions "
                             "to import alongside your hand labels (never overwrites them)")
    ph.add_argument("stem", help="capture stem to hint, e.g. d356-375")
    ph.add_argument("--out", default=None, help="output dir (default: labels dir)")
    ph.add_argument("--dry-run", action="store_true",
                    help="print the hint rows instead of writing the file")

    args = parser.parse_args(argv)
    {"groundtruth": _cmd_groundtruth, "align": _cmd_align,
     "validate": _cmd_validate, "track-mix": _cmd_track_mix, "tail-solve": _cmd_tail_solve,
     "skip-confirm": _cmd_skip_confirm,
     "skip-reject": _cmd_skip_reject, "skip-rejections": _cmd_skip_rejections,
     "starter": _cmd_starter, "hints": _cmd_hints,
     "match-hints": _cmd_match_hints,
     "inspect-slice": _cmd_inspect_slice,
     "inspect-worker": _cmd_inspect_worker,
     "sync-audit": _cmd_sync_audit,
     "sync-sweep": _cmd_sync_sweep}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
