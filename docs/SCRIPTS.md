# Scripts — what each one is for, and when to reach for it

> **What this is:** the index of every script in `scripts/` and `labels/` — purpose, when to run
> it, and what it costs. Every script also carries a full docstring; this is the map, not the
> manual.
> **Fits in:** [README](../README.md) · [PROCESS](../PROCESS.md) (the labelling loop) ·
> [HOWTO](../HOWTO.md) (which tool *now*) · [FINDING_MYSTERY_TRACKS](../FINDING_MYSTERY_TRACKS.md)
> (identifying the unnamed tracks).

Two Pythons, and it matters:

| | |
|---|---|
| **`.env/bin/python`** | the general venv. numpy, pydub, ffmpeg. Everything except the chroma work. |
| **`.venv/bin/python`** | the alignment venv — **librosa**, pinned to python 3.13 (numba has no 3.14 wheels). Anything doing chroma. `make align-env` builds it. |

Most scripts want `PYTHONPATH=scripts` and the machine paths from `.env_vars`:

```bash
set -a && . ./.env_vars && set +a
```

---

## Labelling a capture (the core loop — see [PROCESS](../PROCESS.md))

| Script | What | When |
|---|---|---|
| `labels/sort_tsv.py` | the **only** txt→tsv tool: sort, scope, validate the grammar | every time you export labels from Audacity |
| `labels/publish.py` | hard-gated publish: validate → sort → push → refresh the sheet | when a capture is finished |
| `streamalign hints <stem>` | the engine's opinion as a **separate** label track — proposed anchors, skips, sync-anchor pairs, and questions | before labelling a capture |
| `streamalign starter <owner>` | carry a finished file's labels forward to seed the next | after finishing a capture |
| `streamalign align/validate/groundtruth` | measure an offset; grade the engine against your hand work | placing a file; checking the engine |
| `streamalign skip-clips / skip-confirm / skip-reject` | find and rule on skips | after placing, before chaining onward |
| `scripts/build_track_metadata.py` | labels → `track-metadata.json`. **The only writer of that file.** | after changing labels |
| `scripts/render_tracklist.py` | `track-metadata.json` → `TRACKLIST.md` (with per-track `#tNN` anchors) | after the build; needs network for artwork |

## Identifying the Mystery Tracks (see [FINDING_MYSTERY_TRACKS](../FINDING_MYSTERY_TRACKS.md))

| Script | What | When |
|---|---|---|
| `scripts/mkvideo.sh` | build the "Unknown Track N" video for a track-ID post, locally | when you have a clip to publish. **The highest-yield method — humans have solved 2 of 3.** |
| `scripts/identify_by_chroma.py` | chroma-match a clip against a pool of candidate records | when you have candidate audio |
| `scripts/match_queue.py` | chroma-match the mysteries against the listen queue's **downloaded, unlistened** tracks | one-off sweep of what's already on disk |
| `scripts/harvest.py` | the long-runner: stream candidates → chroma signature → **drop the audio** → score | continuously. See [the harvester](#the-harvester) |
| `scripts/discogs_leads.py` | read the labels this DJ actually played, ask Discogs what else they released 1994–99 | when the pool needs new leads |
| `scripts/seed_leads.py` | turn those leads into URLs and queue them for the harvester | after `discogs_leads.py` |
| `scripts/acoustid_check.py` | verify the **originals** against AcoustID; catch mislabelled source files | occasionally. **Does not work on stream audio** — see `Archive/LESSON_acoustid_stream.md` |

## Measuring the matcher

| Script | What | When |
|---|---|---|
| `scripts/extract_tracks.py` | cut every well-defined track **out of the mix**, reassembling across captures. Refuses anything it cannot place precisely. | once; **re-run whenever a capture gains precise timing or a track's span changes** |
| `scripts/calibrate.py` | score every known mix track against every known original → `docs/CALIBRATION.md` | **whenever the matcher changes.** It is the regression test for the whole matching stack |
| `scripts/selftest.py` | the **canary**: re-identify a track we already know and demand cost, rank **and** margin — offline (small pool) and live (real stream) | continuously, by the harvester. Surfaced at `/harvest`. See [below](#the-canary-does-the-matcher-still-work) |

`calibrate.py` is not a one-off. It is how we know that the true-match and non-match populations
**overlap** — and therefore that *rank*, not cost, is the reliable signal. Any change to
`chroma_match.py` should be followed by a run: if 40-of-41 tracks stop ranking #1 against their
own original, the change is wrong.

## The library / sheet plumbing

| Script | What |
|---|---|
| `scripts/enrich_musicbrainz.py`, `enrich_mb_links.py`, `enrich_album_covers.py`, `enrich_covers_links.py` | fill artwork/links on `track-metadata.json` (network) |
| `scripts/merge_track_sources.py`, `g4_missing_sources.py`, `find_streaming_links.py` | source inventory: what we have, what's missing, where to get it |
| `scripts/backup_sheet.py` | back up the Google Sheet |
| `scripts/tracklist_sync.sh`, `check_tracklist_sync.sh` | cross-repo sync of `track-metadata.json` (PR-based) |

## Retired

`scripts/splitexport.py`, `alignfinder.py`, `pipeclient.py` — the Audacity-era tools. See
[`Archive/`](../Archive/).

---

## The harvester

```bash
set -a && . ./.env_vars && set +a
PYTHONPATH=scripts .venv/bin/python scripts/harvest.py --status
PYTHONPATH=scripts .venv/bin/python scripts/harvest.py --run          # runs for weeks
PYTHONPATH=scripts .venv/bin/python scripts/harvest.py --pause        # / --resume
PYTHONPATH=scripts .venv/bin/python scripts/harvest.py --purge-audio  # throw every retained excerpt away
```

**What it does.** Takes its candidates from the player's **listen queue** (skipping anything
already heard, discarded, ignored or duplicate) and keeps its own working queue in `.harvest/`.
For each candidate: streams the audio (never to disk), reduces it to a **chroma signature** (12×N
float16, ~55 KB against ~8 MB), throws the audio away, and scores the signature against every
unsolved Mystery Track — **but only the mysteries it holds a clip of** (see
[PROCESS §8b](../PROCESS.md#8b-giving-the-harvester-a-new-or-better-mystery-track-clip)).

**It proposes; you dispose.** It never marks a mystery solved. It keeps the best **leads** (best 12
per mystery, evicting the worst when a better one lands) and you rule on them at **`/harvest`** —
see [PROCESS: *Ruling on what the harvester finds*](../PROCESS.md#ruling-on-what-the-harvester-finds-harvest).

**A lead is a URL, not audio.** `--purge-audio` threw away the retained excerpts, and nothing is
hoarded now: what survives is the url, the cost, the mystery, the key, and *where* in the candidate
it matched. `/harvest` reviews each candidate by **embed at its source**. This is both the better
review and the only defensible copyright posture — the retained audio had grown to 2.2 GB and
included a 108-minute DJ mix kept whole, which broke the one claim the posture rested on. There is
now a hard cap in `write_excerpt`, and a test that feeds it that mix and demands 30 seconds back.

**Why signatures.** The matcher can only find what's in the pool, and the pool we want is far
bigger than this disk. 100,000 tracks is ~5 GB of signatures and 0 GB of audio.

**How it stays polite.** Rotates hosts between tracks (no site sees a burst), per-host token
buckets, jittered delays (never a fixed cadence), 4–5 h sessions then 40–120 min idle,
exponential backoff on 429/403, and a hard stop after 5 refusals from one host. Every track is
fetched **once, ever** — the signature cache guarantees it.

**The bot wall.** *"Sign in to confirm you're not a bot"* carries no 403 and no 429, so it slips
past the backoff logic entirely. The harvester **halts** on it instead of failing forever. Feed it
a cookie — see [PROCESS: *The harvester, and the bot wall*](../PROCESS.md#the-harvester-and-the-bot-wall).
On macOS, prefer a `cookies.txt` file or `firefox`; `chrome` raises a **Keychain prompt per
process**, so a restarting harvester will ask for your password endlessly.

**Watch it** at **`/harvest`** on the player: pause/resume, the self-test, the mysteries it is
*not* searching for, and the ruling buttons. The player **supervises** it — it adopts a
hand-started harvester rather than spawning a second, and a watchdog revives it if it dies.
`scripts/run_player.sh status` in the player repo reports it too.

### The canary: does the matcher still WORK?

`scripts/selftest.py`.

| Mode | What it proves |
|---|---|
| **offline** | Re-identifies a track we already know (Jamie Myerson, *Sky Blue*) out of a small pool. The matcher still **works** — not merely that the process is alive. |
| **live** | The same, end to end, against a real stream fetched from the internet. |

Both demand **cost, rank *and* a margin**. Requiring only "cost in range, rank 1" is not enough: a
degenerate matcher scores everything identically, ties sort by track number, and the subject — the
lowest-numbered case — ranks first. The canary then vouches for a completely broken matcher. *A tie
is not a win.*

The live check **refuses to establish a canary** if the stream it finds is not the record (it scores
the candidate against our own copy first). A canary that cries wolf gets ignored, which is worse
than no canary — so it retries rather than enshrining a wrong upload. `/harvest` reports **PASS**,
**FAIL** and **not checked** as three distinct states: *a skip is not a pass.*
