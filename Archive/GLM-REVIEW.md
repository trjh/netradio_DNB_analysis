# GLM-REVIEW — netradio_DNB_analysis

**Reviewer:** GLM-5.2 (OpenClaw)
**Date:** 2026-07-01
**Scope:** Full repo review — code, structure, tests, documentation

---

## Disposition (2026-07-04)

Reviewed and archived. Suggestion-by-suggestion outcome:

| # | Suggestion | Outcome |
|---|---|---|
| 1 | Split `sort_tsv.py` into a package | Deferred — "it works and is well-tested"; low priority. |
| 2 | `track_mix.py --dry-run-emit` | Deferred to the player-repo ROADMAP (F3 emission path). |
| 3 | `backup_sheet.py` xlsx fragility | Won't-fix — acceptable for a personal project, noted. |
| 4 | Archive `sheetscript/*draft.js` | **Done** — moved to `Archive/sheetscript-drafts/`. |
| 5 | Document the two requirements files | **Done** — added a header comment to `requirements.txt`. |
| 6 | `track-metadata.json` schema validation | Deferred — revisit if the file grows materially. |
| 7 | Add CI (`pytest` on push) | **Blocked** — `test_build_track_metadata.LiveTailTests::test_tail_present_and_tagged` fails on `main` today (build yields 90 tracks, test expects ≥91). Wire CI only after that drift is resolved, so it doesn't land red. |
| 8 | `make` target to refresh `data/sheet/` CSVs | Deferred — needs Google creds; the manual `backup_sheet.py` path is documented. |

---

## Overview

This is the public analysis repo for a 1998 netradio.com Drum & Bass ISDN stream. It reverse-engineers where each capture file sits on a master timeline, identifies tracks, aligns originals to the mix, and sources missing audio. The repo is mature, well-documented, and has a clear data-authority model.

**Strengths:** excellent documentation (README, HOWTO, STREAM_PROVENANCE, ROADMAP), a proven stream-alignment engine, a rigorous label grammar with tooling, and a clean separation between hand-authored data (labels) and computed output (auto labels, track-metadata.json).

---

## Structure

```
labels/*.labels.tsv          — hand-authored Audacity label exports (the timeline authority)
labels/sort_tsv.py           — label grammar parser/sorter/validator (A1)
labels/publish.py             — hard-gated publish wrapper (A4)
scripts/streamalign/         — the alignment engine (14 modules)
scripts/build_track_metadata.py — generates track-metadata.json from labels
scripts/g4_missing_sources.py — missing-originals inventory
scripts/render_tracklist.py  — renders TRACKLIST.md
scripts/backup_sheet.py      — Google Sheet backup
sheetscript/Code.js          — Google Apps Script (sheet view from repo data)
tests/                       — 10 test files, well-structured
track-metadata.json          — the authoritative metadata (46KB)
tracklist-2017.txt           — hand notes (the original tracklist)
data/sheet/                  — CSV snapshots of the Google Sheet
```

## Code Quality

### streamalign engine
- **align.py** (162 lines): Clean FFT cross-correlation + GCC-PHAT with parabolic interpolation. Good docstrings explaining offset convention. The two-stage approach (coarse decimated → fine full-rate) is efficient.
- **skips.py** (132 lines): Windowed local-offset tracking for skip detection. The algorithm is well-documented and validated against a known 4-skip overlap.
- **solve.py** (222 lines): BFS-based global solve from hand-verified edges. Correctly chains pairwise offsets. Redundant edges cross-check via `score.consistency_report`.
- **groundtruth.py** (161 lines): Faithful port of the player's parser. Good — keeps the engine independent. The test cross-checks against the player's parser.
- **score.py** (97 lines): Simple, clear scoring. Stats in both samples and ms.
- **clips.py** (160 lines): Skip-check clip generator. Follows Tim's spec (A+B combined, skip-ahead/back handling). Clean numpy + ffmpeg.
- **emit_labels.py** (187 lines): AUTO GENERATED label emitter. Correctly enforces the two hard rules (AUTO GENERATED suffix, separate .auto. file).
- **track_mix.py**: Chroma+DTW aligner. Uses Theil-Sen for rate estimation (robust choice). Good reliability gate.

### Label tooling
- **sort_tsv.py**: Comprehensive parser with LABELTRACK support (A1). Handles secondary files, grammar validation, `--live` Audacity diff, `--adjust` rebasing. 700+ lines — could benefit from splitting into a module, but it works.
- **publish.py**: Clean hard-gated publish wrapper. Reuses `sort_tsv.py --test` instead of re-implementing grammar. Good design.
- **build_track_metadata.py** (612 lines): Well-structured. The `--seed` carry-forward for curated fields is smart. Good label-row parsing.

### Other scripts
- **g4_missing_sources.py** (194 lines): Clean inventory with smart classification (have/placeholder/missing). Good name-match guard against the prefix-collision trap.
- **render_tracklist.py** (199 lines): Pure renderer, no network. Album-first artwork resolution. Good.
- **backup_sheet.py** (142 lines): Reads xlsx via stdlib (no openpyxl dep). Good mandatory-skip-tabs guard for credentials.

## Tests

10 test files covering: backup_sheet, build_track_metadata, emit_starter, find_streaming_links, g4_missing_sources, merge_track_sources, publish, skip_review, sort_tsv, streamalign. Audio-dependent tests skip gracefully when captures aren't present. The test_streamalign.py (485 lines) is thorough — ground-truth parsing, alignment accuracy, skip detection, solve validation.

**Suggestion:** Consider a test for `render_tracklist.py` — it's a pure renderer and easily testable.

## Documentation

Excellent. README covers the full manual workflow + process. HOWTO.md, STREAM_PROVENANCE.md, and the streamalign docs (README, WALKTHROUGH, TAIL_SOLVE, RATE_AXIS_SPIKE) are thorough. The `docs/` directory (in the player repo) holds the ROADMAP, plans, and WORM log.

## Suggestions

1. **sort_tsv.py size:** At 700+ lines with multiple responsibilities (parsing, sorting, validation, secondary-file handling, LABELTRACK scoping, `--live` diff), it could be split into a small package (`labels/` module). Low priority — it works and is well-tested.

2. **track_mix.py emission gap:** The ROADMAP notes "#16's results never reach the labels" — `track_mix.py` reports but doesn't emit. The F3 emission path exists (PR #22) but requires by-ear confirmation. Consider adding a `--dry-run-emit` that shows what WOULD be emitted, to make the ear-check workflow faster.

3. **backup_sheet.py xlsx parsing:** The stdlib XML parsing is impressive but fragile to xlsx format changes. Acceptable for a personal project — just note it.

4. **sheetscript/Code.js:** Contains `first draft.js` and `second draft.js` — these could be archived or removed to reduce confusion about which is current.

5. **requirements-streamalign.txt vs requirements.txt:** Two requirements files with overlapping but different deps. Consider documenting which to use when (the Makefile handles this, but a comment would help).

6. **track-metadata.json is 46KB of JSON:** At this size it's fine, but if it grows significantly, consider a schema validation step (jsonschema or a hand-written validator) to catch corruption before it propagates to the player.

7. **No CI:** The repo has no GitHub Actions. A simple workflow running `python -m pytest tests/` on push would catch regressions. The publish gate (A4) has a webhook trigger design but it's not wired to CI.

8. **data/sheet/ CSVs are committed snapshots:** Consider adding a `make` target to refresh them (or document the manual `backup_sheet.py` command in the README process section, if not already).

## Summary

This is a well-engineered personal research repo. The alignment engine is the standout — precision-first, validated against hand ground truth, with clean separation of concerns. The label grammar and tooling (sort_tsv, publish, emit_labels) form a solid pipeline. The main gap is CI automation, but for a personal project the test suite + manual workflow is sufficient.
