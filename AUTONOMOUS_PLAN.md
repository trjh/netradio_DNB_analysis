# Autonomous loop plan — netradio analysis

**Status: decisions settled with Tim 2026-06-14 (see bottom). Awaiting Tim's review
of the goals/loop prompt before the loop starts — nothing runs autonomously yet.**

The goal of this doc is a self-contained **loop prompt** I can run under `/loop`
(dynamic pacing) to make maximal unattended progress toward three goals, plus the
operating rules and success criteria that let me *keep going* instead of stopping
at every decision (which is what limited the first overnight run).

---

## The loop prompt (paste into `/loop` once approved)

> Work the netradio analysis goals below in order, autonomously, for as long as you
> can. **Don't wait on Tim for decisions** — make a reasonable, documented call and
> keep going; he reviews via the clip player and PRs. Checkpoint every coherent
> chunk as a commit + `local-review`; fix BLOCKING findings before moving on. Use
> `.venv/bin/python` for anything needing librosa. Put build narrative in PR
> descriptions, not READMEs. Generate review clips for anything that needs Tim's
> ear/eye and keep going — don't block on his verification. **Stop only when:** a
> goal's success criteria are met (move to the next), you hit a true technical wall
> (document it, switch to another goal), or an action is destructive/irreversible
> and needs Tim. Prefer breadth: if one goal stalls, advance another. Re-run this
> prompt each wake-up.
>
> Goals, in order: **G0** clip review player → **G1** finish master-timeline
> mapping → **G2** originals→mix mapping (1st then 2nd pass) → **G3** identify
> missing tracks. Details in `AUTONOMOUS_PLAN.md`.

---

## Operating rules (how to keep going)

1. **Don't stop at decisions.** The first run stalled waiting on Tim. Default to
   acting: pick the reasonable option, write down the assumption, continue. Only
   genuinely irreversible/destructive things (force-push, deleting data, anything
   outward-facing) need him.
2. **Checkpoint discipline.** Each coherent chunk → commit + `local-review`. A
   BLOCKING finding must be fixed/refuted before proceeding; NON_BLOCKING gets a
   documented decision. Never merge PRs (Tim merges).
3. **Verification is async.** When Tim's ear/eye is needed, generate a **review
   clip** (see G0) and keep working — his check happens later, not as a blocker.
4. **Tools/deps.** librosa etc. live in `.venv` (python3.13); document new deps in
   `requirements-streamalign.txt`. Core engine stays numpy+ffmpeg.
5. **Breadth over depth at a wall.** If a goal hits a real wall, log it and move to
   the next goal rather than halting the loop.
6. **Use a Workflow for fan-out.** Once a per-item method works (e.g. align one
   track), run the many-item version (all ~40 tracks) as a Workflow, not serially.

---

## G0 — Clip review player (build first; it unblocks everything)

Tim verifies far faster with a tiny player than by opening Audacity. Build a
lightweight web page that plays **clips I generate**, each with:
- a **rolling ~2 s timestamp window** with my **annotations** (e.g. "file
  transition", "skip ahead 1.248 s", "A solo → A+B → B solo"),
- a **playback-position indicator** moving through that window,
- the clip list with a one-line description each.

Think "a tiny slice of what Audacity shows, without launching it." It indexes a
folder of generated clips + per-clip annotation sidecars (JSON: `[{t, label}]`).
A small **standalone** page served by the player repo's stdlib http.server
(settled — separate from the main player).
**Done when:** Tim can open it, pick a clip, and hear it with rolling annotations.

## G1 — Finish mapping files to the master timeline

1. **Skip-aware edge measurement** — replace the single-offset `align_pair` in the
   solve with `characterise_overlap` so a skip in the overlap doesn't bias the edge
   (fixes the ~1 s `d026-045`/`d041-064` errors).
2. **Loop-wrap + multi-match** — handle the `d-<neg>-N` loop-wrap files (two-offset
   matches) and reject inconsistent edges (consistency-based outlier rejection),
   fixing the `d-25-005b` gross error.
3. **Coverage** — raise the solve from 19 toward all 55 labelled files; validate vs
   ground truth (target: all corroborated placements within a few samples).
4. **Tail** — try bounded-search discovery (filename-range prior) to bridge the
   tail; if still unbridged, place by contiguity (flag each boundary) and generate
   **skip-check clips** for Tim. *(Tim is checking tail contiguity manually.)*
5. **Skip-check clip spec** (per Tim, for G0/G1): A = a recording with a skip,
   B = one without a skip over that segment. The clip is **A+B combined until A
   skips**, then:
   - *A skipped ahead* → split A at the skip; **B continues** for the length of the
     split; then A resumes at the far side of the split where it re-matches B.
   - *A skipped back* → split B at the skip and **rewind B** to the point A skipped
     back to; A and B continue together.
   15 s before the skip + 15 s after.
6. **Emit** candidate `.labels.tsv` rows for the unlabelled tail (review diff).
**Done when:** every reachable file placed with a skip map, validated where ground
truth exists, with clips for the unverifiable boundaries.

## G2 — Map originals → mix (1st pass, then 2nd pass)

Ground truth: `sources_local/NNN-*.{mp3,flac,…}` (56 files) + the `orig###`/`track###`
sync points (dozens of tracks). Waveform correlation already proven NOT to work
(T1) → use **chroma + DTW** (librosa).
- **T0** parse the sync points → ground-truth offset/rate per synced track.
- **1st pass:** chroma+DTW align each original (with sync points) to its mix region
  (region known from `track-metadata.json`); recover offset + rate + polarity;
  validate vs the sync points; emit clips. Fan-out over all synced tracks via a
  Workflow.
- **2nd pass:** refine — piecewise DJ-edit segmentation, tighter rate, polarity;
  hit a tolerance of **±50 ms** at sync points (settled); then produce
  candidate mappings + clips for tracks **without** sync points.
**Done when:** every synced track maps within tolerance; candidate maps + clips for
the rest.

## G3 — Identify missing / unidentified tracks

The 9 "Mystery Track N" segments + any unidentified spans. Per missing track,
build a **dossier**: a clean extracted clip, BPM + musical key, an **AcoustID**
acoustic-fingerprint lookup (settled OK), Whisper transcript of any vocal/spoken
hook → search queries, and a chroma self-match against the source library as a
backup. Output a dossier + clip per track for Tim.
**Done when:** each missing track has a dossier with the best available leads.
**AcoustID key:** provided and stored in the analysis repo's **gitignored `.env`**
as `ACOUSTID_API_KEY` (read from env at runtime, never committed). Tim wants to
**rotate it later** — replace the value in `.env` when he does. I'll install
`fpcalc`/chromaprint myself.

---

## Decisions (settled with Tim 2026-06-14)

1. **Clip player:** standalone page in the **player** repo.
2. **Autonomy:** push through **all non-destructive decisions**; record each choice
   + assumption in `LOOP_DECISIONS.md` (append-only) for Tim to review at the end.
   Only destructive/outward-facing actions pause for him.
3. **G2 tolerance:** ±50 ms at sync points.
4. **G3 fingerprinting:** AcoustID approved (needs an API key from Tim; see G3).
5. **Order:** my call → **G0 → G1 → G2 → G3**, but breadth-first within reason: G0
   first because it's how Tim verifies; G1 next because it feeds clips to G0 and the
   master timeline underpins G2; then G2, then G3. Switch goals at any hard wall.
6. **Cadence:** dynamic-pacing `/loop`, Workflows for fan-out.

**Separately handled (not loop work):** the iPhone lock-screen MediaSession player
feature — done in player PR #24.

## How the loop stays unstuck (re Tim's question)

The `/loop` wakes me on a timer I set; it doesn't auto-detect "stuck". What keeps it
moving is the design:
- **Each wake-up re-states the goals** (the loop prompt), so I re-orient even after a
  context summarisation — that's the "restart" effect.
- **At a wall I switch goals** (operating rule 5) instead of halting, and log the
  wall in `LOOP_DECISIONS.md`.
- **I only fully stop** when every goal is met or blocked on Tim, or an action is
  destructive — and then I send one notification rather than spin.
- A standing **"stuck rule":** if a sub-task makes no progress for ~2 consecutive
  ticks, I drop it to a logged "blocked/needs-Tim" item and move on, rather than
  burning ticks on it.

So: the loop keeps me going and re-focused; *I* manage stuck-ness by switching and
logging, and surface a blocker only when it's truly Tim's to resolve.
