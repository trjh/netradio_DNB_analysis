"""List the hand-placed original<->capture sync pairs as JSON (the `placed` op).

The whole-song review tool needs the pairs that were seated BY HAND in Audacity --
they live in the committed labels (`origNNN sync:` / `track sync:` marker pairs), not
in any `*.match.hints.tsv` file. This op turns that bookkeeping into the same
point-set shape the hints files provide, so a hand-placed pair and a hints pair are
interchangeable rows in a pair picker:

  .venv/bin/python -m streamalign placed            # every capture
  .venv/bin/python -m streamalign placed d376-395   # one capture stem

Per (capture stem x original number): the fitted rate (original-native seconds per
stream second -- the reciprocal of the sheet's `(trackB-trackA)/(origB-origA)` speed,
i.e. the same convention as the match-hints summary rate and `inspect-slice --rate`),
and per sync point: the marker key, the capture-local stream instant, the
original-NATIVE instant where the `origNNN start:` clip bookkeeping derives one
(`orig_ts - start_t`, exactly `sync-audit`'s seat reconstruction), a `derivable`
flag with the reason when it is false, the AP-04 `verified` token, and -- where a
saved `sync-audit --json` report is on disk -- that point's audited verdict and
seat confidence.

READ-ONLY and DSP-free: pure label parsing, no audio is opened, no numpy beyond what
the label parsers already use -- fast enough to sit behind a page load. Refresh the
optional grades with:

  .venv/bin/python -m streamalign sync-audit --json sync-audit.json

(the repo-root `sync-audit.json` is the default report location; it is gitignored --
machine-local, regenerable).
"""

import json
import os

from . import groundtruth as _gt
from . import sync_audit as _sa
from . import track_mix as _tm

# Mirror of the inspector boundary's MAX_OVERVIEW_POINTS: no pair answers with an
# unbounded point list (hand labels hold a handful of points per pair; hitting this
# cap means something is wrong with the labels, not the cap).
MAX_POINTS_PER_PAIR = 64

# The default saved sync-audit report (repo root, gitignored). `--audit-json`
# overrides; absent simply means every grade is null.
DEFAULT_AUDIT_JSON = os.path.join(_gt.REPO_ROOT, "sync-audit.json")


def default_audit_json():
    """The repo-root report path when one is on disk, else None. The CLI and the
    inspect-worker resolve the default HERE, server-side -- a caller never names a
    report path across the boundary."""
    return DEFAULT_AUDIT_JSON if os.path.isfile(DEFAULT_AUDIT_JSON) else None


def load_audit_grades(audit_json):
    """{(track, stem, label): {"grade", "seat_conf"}} from a saved `sync-audit --json`
    report, or {} when the path is falsy/absent/unreadable. Grades are advisory --
    a broken report must never break the listing."""
    if not audit_json or not os.path.isfile(audit_json):
        return {}
    try:
        with open(audit_json, "r", encoding="utf-8") as handle:
            report = json.load(handle)
        out = {}
        for p in report.get("points") or []:
            key = (int(p["track"]), str(p["stem"]), str(p["label"]))
            conf = p.get("seat_conf")
            out[key] = {"grade": str(p["verdict"]),
                        "seat_conf": float(conf) if conf is not None else None}
        return out
    except (OSError, ValueError, TypeError, KeyError):
        return {}


def _point(num, pr, stem, starts, grades):
    """One sync point record: seat reconstruction exactly as sync_audit.audit does it
    (nearest-matching `origNNN start:` row; orig_ts - start_t = original-native)."""
    start_t = _sa.start_for(starts, stem, num, pr["label"], pr["orig_ts"])
    orig_s, why = None, None
    if start_t is None:
        why = "no origNNN start: row"
    else:
        b_native = pr["orig_ts"] - start_t
        if b_native < 0:
            why = "sync before clip head"
        else:
            orig_s = float(b_native)
    g = grades.get((num, stem, pr["label"]), {})
    return {"k": pr["label"],
            "stream_s": float(pr["track_ts"]),
            "orig_s": orig_s,
            "derivable": orig_s is not None,
            "why": why,
            "verified": bool(pr.get("verified")),
            "grade": g.get("grade"),
            "seat_conf": g.get("seat_conf")}


def list_placed(stem=None, labels_dir=None, audit_json=None):
    """The op: {"pairs": [...]} for every capture (stem=None) or one capture stem.

    Each pair entry: {stem, orig, rate, rate_method, points, [rate_note],
    [truncated]} -- rate is
    original-native seconds per stream second (1 / the sheet speed), None when the
    labels give no rate. With complete A/B pairs in several captures the FIRST
    pair's rate is used (never a blend) and rate_method reads "AB-first";
    rate_note tells the operator about the multiplicity, and about any duplicate
    designated A/B letters inside one capture (owner rules, 2026-08-18). Points
    ride in stream order. An unknown stem answers
    {"error": ...} (the stem namespace is the committed `<stem>.labels.tsv` files).
    """
    labels_dir = labels_dir or _gt.LABELS_DIR
    if stem is not None:
        stem = str(stem)
        if os.path.basename(stem) != stem or not os.path.isfile(
                os.path.join(labels_dir, stem + ".labels.tsv")):
            return {"error": "no labels for capture %s" % stem}
    grades = load_audit_grades(audit_json)
    gt = _tm.track_sync_groundtruth(labels_dir)
    starts = _sa.parse_orig_starts(labels_dir)
    by_pair = {}
    for num in sorted(gt):
        info = gt[num]
        notes = []
        sheet_rate = info["rate"]
        rate_method = info["rate_method"]
        # Owner rule (2026-08-18): with complete A/B pairs in SEVERAL captures,
        # do not blend them -- use the FIRST pair's rate (first in the original's
        # own time order, which is how groundtruth builds segment_rates) and say
        # so in a note, so the operator sees the multiplicity instead of a
        # silent median.
        if rate_method == "AB-multi" and info["segment_rates"]:
            first = info["segment_rates"][0]
            sheet_rate = first["rate"]
            rate_method = "AB-first"
            allfiles = ", ".join(s2["file"].replace(".labels.tsv", "")
                                 for s2 in info["segment_rates"])
            notes.append("A/B pairs in %d captures (%s); using the first (%s)"
                         % (len(info["segment_rates"]), allfiles,
                            first["file"].replace(".labels.tsv", "")))
        # Owner rule (2026-08-18): duplicate designated letters inside ONE
        # capture are a data problem to surface, not silently last-wins.
        dup_counts = {}
        for pr in info["pairs"]:
            if pr["label"] in ("A", "B"):
                key = (pr["file"], pr["label"])
                dup_counts[key] = dup_counts.get(key, 0) + 1
        for (fn, lab), n in sorted(dup_counts.items()):
            if n > 1:
                notes.append("%dx %s rows in %s (last by original time wins)"
                             % (n, lab, fn.replace(".labels.tsv", "")))
        rate = (1.0 / float(sheet_rate)) if sheet_rate and sheet_rate > 0 else None
        for pr in info["pairs"]:
            pr_stem = pr["file"].replace(".labels.tsv", "")
            if stem is not None and pr_stem != stem:
                continue
            entry = by_pair.setdefault((pr_stem, num), {
                "stem": pr_stem, "orig": num, "rate": rate,
                "rate_method": rate_method, "points": []})
            if notes and "rate_note" not in entry:
                entry["rate_note"] = "; ".join(notes)
            entry["points"].append(_point(num, pr, pr_stem, starts, grades))
    pairs = []
    for key in sorted(by_pair):
        entry = by_pair[key]
        entry["points"].sort(key=lambda p: p["stream_s"])
        if len(entry["points"]) > MAX_POINTS_PER_PAIR:
            entry["points"] = entry["points"][:MAX_POINTS_PER_PAIR]
            entry["truncated"] = True
        pairs.append(entry)
    return {"pairs": pairs}
