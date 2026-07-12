# TASKLIST

> **What this is:** the mutable list of what's outstanding, and — just as importantly — the
> **known gaps in what already exists**. A gap that isn't written down is a gap that gets
> rediscovered by accident.
> **Fits in:** [README](./README.md) · [PROCESS](./PROCESS.md) ·
> [FINDING_MYSTERY_TRACKS](./FINDING_MYSTERY_TRACKS.md) · [docs/SCRIPTS](./docs/SCRIPTS.md).

---

## Known gaps in what's already built

These are shipped and working, but architecturally wrong. Recorded so they're a decision, not a
surprise.

### 1. Two queues that should be one

| | |
|---|---|
| listen queue | `player/metadata/listen_queue.json` — ~1,000 items, heard/favourite flags |
| harvester queue | `analysis/.harvest/queue.json` — its own, separate |

**The harvester does not read the listen queue.** Subscriptions feed the *listen* queue; the
harvester never sees them unless separately seeded. So the same track can be queued twice, and
nothing you mark as *heard* is skipped by the harvester.

**Should be:** one queue. The harvester processes any item **under 10 minutes** that is **not yet
heard**. Marking something heard or favourite in the player then means something to both.

### 2. Nothing starts the harvester

It's launched by hand (`nohup … harvest.py --run`). No cron, no launchd, nothing in the player.
**It dies on reboot and does not come back**, and there is no way to tell it to stop other than
finding the process.

**Should be:** the player supervises it — starts it on launch if enabled, kills it on shutdown,
restarts it if it dies, with an on/off control on `/harvest`. (The player is deliberately
stdlib-only; the harvester needs librosa/numba. So: a supervised child process, *not* an
in-process integration, or the player inherits a heavy dependency tree it has been careful to
avoid.)

### 3. Source-finding is scattered across four places

`discogs_leads.py` (which records) → `seed_leads.py` (their URLs) → `harvest.py` (the work), plus
**subscriptions** in the player feeding a different queue entirely. Four moving parts for one
idea. Folding the queues together (gap 1) collapses most of this.

### 4. `mkvideo.sh` renders at 30 fps

The reference upload is 60. Visually identical for a bar spectrum, and it halves the encode
(~9 min for a 5-minute track). `FPS=60` if it ever matters.

---

## Outstanding

### Harvester — next iteration

- [ ] **One queue.** Harvester reads the listen queue; processes items **< 10 min** that are
      **not heard**; skips heard items entirely.
- [ ] **Player supervises the harvester** — start/stop with the player, on/off from `/harvest`.
- [ ] **Mark a candidate as MATCH or DISCARD** from `/harvest`, so a rejected candidate is never
      re-proposed. (Open question: is a discard per-mystery or global?)
- [ ] **Rate display** on `/harvest`: links/hour, next long pause, or "paused, resumes in X";
      tracks processed in the last 24 h.
- [ ] **Explain the process** in a blurb at the foot of `/harvest`: where URLs come from, what
      review means, where the review data lives, what the results are.

### Player UI

- [ ] **`/harvest` as a primary page** alongside player / admin / queue.
- [ ] **Distinguish primary-page buttons from secondary-function buttons.** Primary: player,
      admin, queue, harvest. Secondary: reload-json, refresh-from-reddit, archive-favorites,
      downloads, subscriptions, pause-harvest.

### Mystery Tracks

- [ ] **MT7's clip is 23 s** — its capture audio isn't on this box. *Blocked: needs Tim.* This is
      the single biggest limiter on MT7 being identifiable at all; a 23-second query drives every
      cost down and produces false positives.
- [ ] **MT8, MT9** — no clips (capture audio missing). *Blocked: needs Tim.*
- [ ] **MT11** — no master span; it needs labelling before it can be cut at all.
- [ ] **Publish `Unknown Track 6.mp4`** (built, ready) and MT7/MT10 once their audio exists.
      *Humans have solved 2 of the 3 published mysteries. This remains the highest-yield move by
      a wide margin.*

### Data

- [ ] **Track 14** (Me'Shell NdegéOcello — *Stay*) ranks 6th against its own original. The one
      genuine matcher failure in 41. Worth understanding.
- [ ] **Seven tracks refused by `extract_tracks.py`** for a hole in coverage — they can't be
      placed precisely. Needs capture audio or labelling.
- [ ] **`d-` captures have no precise timing.** Everything derived from them is approximate, and
      the tools now exclude them. Fixing their timing would unlock those tracks.

### Done, kept for reference

- Source-finding: **enough for now** (Tim). The harvester will examine anything added to the
  queue manually or by subscription.
