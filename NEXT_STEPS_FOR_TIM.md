# Next steps for Tim — follow-up on the 2026-06-11/12 overnight work

What got built, what needs your hand, and where we are on the tasklist.

## TL;DR

Overnight I built the **Stream Alignment Engine** (`scripts/streamalign/`) that
reconstructs the master timeline from the capture audio, validated each phase
against your hand alignments, and kept it all review-clean. Everything is in
**two open PRs** waiting on you to merge, plus some uncommitted player tasklist
edits. The engine reaches the point where it can place *overlapping* captures
precisely; placing the *unlabelled tail* is blocked on a real question only you can
answer (were the tail captures recorded contiguously?).

## 1. PRs to merge (in order)

| repo | PR | what | state |
|---|---|---|---|
| analysis | **#10** | `STREAM_PROVENANCE.md` — the data's chain of custody + what "master time" is | review-clean |
| analysis | **#11** | the Stream Alignment Engine (P0–P4) + walkthrough | passed local-review (6 iterations) |

Player repo: **no open PRs** (the 91-track migration #22 merged). But
`player/TASKLIST.md` has **uncommitted working-tree edits** (the headline-project
section + P0–P4 progress checkmarks) — review and commit/keep them as you like;
it's the mutable running tasklist. (`HANDOFF.md`/`next_steps.txt` there are
untracked older files, ignore.)

Merge order doesn't matter much; #10 is pure docs, #11 is the engine. **I never
merge — these are yours.**

## 2. How to run / poke the engine

From the analysis repo (`scripts/` on the path), with the captures on disk:

```bash
PYTHONPATH=scripts python3 -m streamalign groundtruth         # the hand answer key
PYTHONPATH=scripts python3 -m streamalign align d000-018 d001-026b   # one pair + error
PYTHONPATH=scripts python3 -m streamalign --labels <dir> validate    # align every hand pair, score
PYTHONPATH=scripts python3 -m unittest discover -s tests      # 85 tests
```

Read order to understand it: `scripts/streamalign/README.md` (status) →
`WALKTHROUGH.md` (how the functions compose) → `STREAM_PROVENANCE.md` (the data).

## 3. Where we are on the tasklist (the "Stream Alignment Engine" headline)

| phase | status |
|---|---|
| **P0** ground-truth harness | ✅ done — resolves your `file start sync` values, matches the trusted player parser exactly (55 files, 0 mismatches) |
| **P1** pairwise aligner | ✅ done for clean overlaps — reproduces your hand alignments to **±1 sample** (median 0.18 samp) |
| **P2** skip detection | ✅ done — recovers the documented `d084-103b`/`d065-087` skips with **exact magnitudes**. *Refinements pending:* narrow skip positions (~2 s now); auto-seed; adaptive radius for large skips |
| **P4** discovery + global solve | ⚠️ mechanism done & validated (11/19 placed files to ≤1 ms), but **edge measurement is the weak link**: loop/pre-roll multi-match and skip-in-overlap inject errors. *Next:* skip-aware edge measurement + consistency-based outlier rejection |
| **P3** verification renderer | ⬜ not started (summed proof clips + inverted-null Audacity label file) |
| **P5** emit tail labels | ⬜ blocked on the tail-anchoring decision below |

## 4. Decisions only you can make

1. **Were the tail captures recorded contiguously (back-to-back, no gaps)?**
   This is the blocker for the end goal. Discovery shows the tail captures
   (`d356-375`, `d376-395`, `d396-415`, then the `d416-435…` and `d465-484…`
   clusters) **do not overlap the master-anchored region** — they abut. So audio
   alignment can place files *within* an overlapping cluster but **cannot anchor a
   non-overlapping tail file to master time**. If you know they were recorded
   contiguously with no gaps, we can chain them by that assumption (each file's
   master start = previous file's skip-resolved master end), accepting per-boundary
   uncertainty. If there might be gaps, we need another anchor.
   - ⚠️ Caveat I corrected mid-session: I first concluded "the tail is disconnected
     islands" as fact — that was an **overstatement**. My blind overlap detector
     false-negatives on small/skip-heavy overlaps, so an "isolated" file might have
     an overlap I missed. **True tail connectivity is still unknown** until either a
     robust detector exists or you confirm the contiguity.

2. **How much to invest in robust blind small-overlap discovery?** It's an open
   problem (I tried decimated x-corr, energy-envelope, full-file PHAT — all fail
   because a small overlap's true peak doesn't dominate an unbounded search). The
   promising path is a **bounded** search around the filename-range prior (±~5 min).
   If contiguity (decision 1) is solid, we may not need this at all for the tail.

## 5. What I'm doing next (you asked me to start this)

**Original-source-track ↔ mix alignment** — automating what you did by hand with
the `orig###`/`track###` sync points. You have **dozens of tracks** (003–047+) with
hand sync points and 56 source files in `sources_local/`, which is excellent ground
truth. Unlike stream-vs-stream, this case has the **speed/pitch changes and
polarity** the DJ introduced, so it's the general alignment problem. Plan +
first experiment in `scripts/streamalign/TRACK_MIX_PLAN.md` (being written).
