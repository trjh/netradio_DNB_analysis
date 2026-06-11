"""Scoring the engine against Tim's hand alignments (P0).

Master time is self-defined (reconstructed from the windows), so "correct" means:
  1. matches Tim's hand `file start sync` values (this module), and
  2. redundant overlaps are self-consistent (consistency_report).
The unit that matters is samples at 16 kHz (Audacity's 0.001 s == 16 samples).
"""

from . import audio as _audio


def _stats(errors_samples):
    if not errors_samples:
        return {"n": 0}
    a = sorted(abs(e) for e in errors_samples)
    n = len(a)
    return {
        "n": n,
        "max_samp": a[-1],
        "median_samp": a[n // 2],
        "mean_samp": sum(a) / n,
        "max_ms": a[-1] / _audio.SR * 1000.0,
        "median_ms": a[n // 2] / _audio.SR * 1000.0,
    }


def score_pairwise(results, ground_truth, sr=_audio.SR):
    """Grade pairwise offset estimates against ground-truth start differences.

    `results`: iterable of dicts with keys a, b, offset_seconds, confidence.
    `ground_truth`: {stem: master_start_seconds}. Each pair's expected offset is
    gt[b] - gt[a]. Returns (rows, summary). Pairs with a/b missing from gt skip.
    """
    rows = []
    errs = []
    for r in results:
        a, b = r["a"], r["b"]
        if a not in ground_truth or b not in ground_truth:
            continue
        expected = ground_truth[b] - ground_truth[a]
        err_s = r["offset_seconds"] - expected
        err_samp = err_s * sr
        rows.append({
            "a": a, "b": b,
            "expected_seconds": expected,
            "estimated_seconds": r["offset_seconds"],
            "error_ms": err_s * 1000.0,
            "error_samples": err_samp,
            "confidence": r.get("confidence"),
        })
        errs.append(err_samp)
    return rows, _stats(errs)


def score_absolute(estimates, ground_truth, anchor=None, sr=_audio.SR):
    """Grade absolute master-start estimates against ground truth.

    Both are {stem: master_start_seconds}. If `anchor` is given, both are shifted
    so that file's start is 0 before comparing (master time is only defined up to
    the anchor). Returns (rows, summary) over files present in both.
    """
    est = dict(estimates)
    gt = dict(ground_truth)
    if anchor and anchor in est and anchor in gt:
        eoff = est[anchor]
        goff = gt[anchor]
        est = {k: v - eoff for k, v in est.items()}
        gt = {k: v - goff for k, v in gt.items()}
    rows = []
    errs = []
    for k in sorted(set(est) & set(gt)):
        err_s = est[k] - gt[k]
        rows.append({"stem": k, "estimated": est[k], "ground_truth": gt[k],
                     "error_ms": err_s * 1000.0, "error_samples": err_s * sr})
        errs.append(err_s * sr)
    return rows, _stats(errs)


def consistency_report(placements, edges, sr=_audio.SR):
    """Self-consistency of redundant overlaps (P4 validation, no ground truth).

    `placements`: {stem: master_start_seconds} from the global solve.
    `edges`: iterable of (a, b, measured_offset_seconds) — each independently
    measured overlap. For each, residual = measured - (place[b] - place[a]); large
    residuals flag a missed skip or a bad lock. Returns (rows, summary).
    """
    rows = []
    errs = []
    for a, b, measured in edges:
        if a not in placements or b not in placements:
            continue
        implied = placements[b] - placements[a]
        resid = measured - implied
        rows.append({"a": a, "b": b, "residual_ms": resid * 1000.0,
                     "residual_samples": resid * sr})
        errs.append(resid * sr)
    return rows, _stats(errs)
