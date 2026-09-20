# Scripts — what each one is for, and when to reach for it

> **What this is:** the index of every script in `scripts/` and `labels/` — purpose, when to run
> it, and what it costs. Every script also carries a full docstring; this is the map, not the
> manual.
> **Fits in:** [README](../README.md) · [PROCESS](../PROCESS.md) (the labelling loop) ·
> [HOWTO](../HOWTO.md) (which tool *now*) · [FINDING_MYSTERY_TRACKS](../FINDING_MYSTERY_TRACKS.md)
> (identifying the unnamed tracks).

One Python: **`.venv`**, built with uv by `make venv` from both requirements files (`make dep` installs into an existing one; `make venv-rebuild` deletes and recreates it — stop the harvester and the align server first) —
the label tooling (numpy, pydub, pyaudacity) and the alignment engine + harvester
(**librosa**, soundfile). It prefers python3.13, the interpreter the harvester runs under.
(Until 2026-09 there were two venvs: a general `.env` and the librosa `.venv`. `.env` is now
the variables *file*, below; an old `.env/` directory must go — `make` says so.)

Most scripts want the venv active and the machine paths from `.env`. Activating puts `scripts/` on
the path (`netradio-scripts.pth`, written by `make venv` and `make dep`), so no `PYTHONPATH`:

```bash
set -a && . ./.env && set +a
. .venv/bin/activate
```

**Running the tests:**

```bash
.venv/bin/python3 -m unittest discover -s tests    # the full suite (or: make test)
```

The suite is hermetic on a fresh clone: it parses only committed evidence (the 2017 notes
resolve capture stems against the committed `labels/` files when no audio is on disk), and
the tests that decode/encode real audio **skip** cleanly when `librosa`/`soundfile` are
absent. One deliberate exception: the undefined-name guard (`tests/test_harvest_runtime.py`)
**fails, never skips**, when `pyflakes` is missing — a skip there is how a NameError ships.
`pip install pyflakes` (it is in both requirements files) and it runs anywhere; the
audio-dependent tests need `.venv` (`make venv`).

---

## Labelling a capture (the core loop — see [PROCESS](../PROCESS.md))

| Script | What | When |
|---|---|---|
| `labels/sort_tsv.py` | the **only** txt→tsv tool: sort, scope, validate the grammar | every time you export labels from Audacity |
| `labels/publish.py` | hard-gated publish: validate → sort → push → refresh the sheet | when a capture is finished |
| `streamalign hints <stem>` | the engine's opinion as a **separate** label track — proposed anchors, skips, sync-anchor pairs, and questions | before labelling a capture |
| `streamalign match-hints <stem> <NNN>`/`--all` | seat an original inside a capture: paired sync-point proposals (Pass 1), verified in the companion player project's `/align` inspector (Pass 2). MATCH runs headless — no Sonic Visualiser step | when a capture's tracks have originals on disk (PROCESS step 9) |
| `streamalign starter <owner>` | carry a finished file's labels forward to seed the next | after finishing a capture |
| `streamalign align/validate/groundtruth` | measure an offset; grade the engine against your hand work | placing a file; checking the engine |
| `streamalign skip-clips / skip-confirm / skip-reject` | find and rule on skips | after placing, before chaining onward |
| `scripts/build_track_metadata.py` | labels → `track-metadata.json`. **The only writer of that file.** | after changing labels |
| `scripts/render_tracklist.py` | `track-metadata.json` → `TRACKLIST.md` (with per-track `#tNN` anchors) | after the build; needs network for artwork |

## Identifying the Mystery Tracks (see [FINDING_MYSTERY_TRACKS](../FINDING_MYSTERY_TRACKS.md))

| Script | What | When |
|---|---|---|
| `scripts/mkmysteryvideo.sh` | build the "Unknown Track N" video for a track-ID post, locally | when you have a clip to publish. **The highest-yield method — humans have solved 2 of 3.** |
| `scripts/identify_by_chroma.py` | chroma-match a clip against a pool of candidate records | when you have candidate audio |
| `scripts/identify_by_api.py` | ask the commercial catalogues (ACRCloud + AudD) to name a clip — searches ~150-160M tracks you don't own, unlike the local chroma pool | when a mystery may be a catalogued release. **Acoustic fingerprinting may be defeated by the 1998 codec/EQ like AcoustID is — it's an experiment; every hit is a lead to confirm by ear.** Needs `ACRCLOUD_*` / `AUDD_API_TOKEN` in `.env` |
| `scripts/match_queue.py` | chroma-match the mysteries against the listen queue's **downloaded, unlistened** tracks | one-off sweep of what's already on disk |
| `scripts/harvest.py` | the long-runner: sign the audio that lands in the harvest directories → chroma signature → **drop the audio** → score | continuously. See [the harvester](#the-harvester) |
| `scripts/discogs_leads.py` | read the labels this DJ actually played, ask Discogs what else they released 1994–99. A lead is tested by adding a stream of it to the listen queue through the queue's add box | when the pool needs new leads |
| `scripts/acoustid_check.py` | verify the **originals** against AcoustID; catch mislabelled source files | occasionally. **Does not work on stream audio** — see `Archive/LESSON_acoustid_stream.md` |

## Measuring the matcher

| Script | What | When |
|---|---|---|
| `scripts/extract_tracks.py` | cut every well-defined track **out of the mix**, reassembling across captures. Refuses anything it cannot place precisely. | once; **re-run whenever a capture gains precise timing or a track's span changes** |
| `scripts/calibrate.py` | score every known mix track against every known original → `docs/CALIBRATION.md` | **whenever the matcher changes.** It is the regression test for the whole matching stack |
| `scripts/selftest.py` | the **canary**: re-score the canary's stored signature (named by `NETRADIO_CANARY_KEY`) against the canary's mix and the current mysteries, demanding cost, rank **and** margin; `--offline` re-runs one calibration case from local files | continuously, by the harvester (every pass). Surfaced on the harvest page. See [below](#the-canary-does-the-matcher-still-work) |

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
| `scripts/cache_budget.py` | the one cache policy: a registry of size-bounded local caches, `reserve` before every write, an eviction run, one disk floor — a library the cache-holding scripts register with (dark until `NETRADIO_CACHE_ROOT` is set). A byte-identical copy of a module shared with the companion checkout; the decoded-array cache in `scripts/streamalign/audio.py` registers with it |

## Retired

`scripts/splitexport.py`, `alignfinder.py`, `pipeclient.py` — the Audacity-era tools. See
[`Archive/`](../Archive/).

---

## The harvester

```bash
make harvest-run                                                      # runs for weeks

set -a && . ./.env && set +a
. .venv/bin/activate && python scripts/harvest.py --status
.venv/bin/python scripts/harvest.py --pause         # / --resume
.venv/bin/python scripts/harvest.py --purge-audio   # throw every retained excerpt away
.venv/bin/python scripts/harvest.py --forget 7      # drop MT7's leads + pairings
.venv/bin/python scripts/harvest.py --rescan        # score every held signature against
                                                     # every mystery it has not met yet (no
                                                     # network)
.venv/bin/python scripts/harvest.py --sign-one u1f0e8d2ba9c4d6f8a1b2
                                                     # sign one file, by key, through the
                                                     # same path the loop uses
```

**The harvester knows directories, and nothing else.** `NETRADIO_HARVEST_DIRS` (in `.env`)
names one or more absolute directories, `:`-separated; the loop reads the **top level** of
each and writes nothing into any of them. An audio file is `<key>.<ext>` with a `<key>.json`
sidecar beside it — no sidecar, no signature — and the ledger `.harvest/ledger.json` is the
mark: one row per key, `signed` or `delayed` with a reason. The full contract, written for
whatever fills the directories (the key encoding, the sidecar schema, the completeness rule,
the ledger and what a feed reads from it), is
[docs/HARVEST_FEED.md](HARVEST_FEED.md). The harvester never reads a queue or an index to
decide what to work on, and nothing that runs in this repo fetches from the web: what arrives
in the directories is signed; what never arrives is not missed.

**The harvester's caches live on the machine's cache policy.** The signature working cache and
the excerpt board (`chroma` and `candidates`) are no longer fixed paths: each lives under
`NETRADIO_CACHE_ROOT` (`NETRADIO_CHROMA_CACHE_DIR` / `NETRADIO_CANDIDATES_CACHE_DIR` to override),
bounded by the policy — a 14-day age on signatures (the bucket is their long-term home), a
250 MB cap on the board that gives up the worst excerpt of a mystery first, and the policy's
shared disk floor. With `NETRADIO_CACHE_ROOT` unset there is no cache directory at all and the
harvester **refuses to start**, naming the setting: a signature it cannot keep is decode cost
paid for nothing. The on-demand `--rescan` refuses the same way: it reads the same cache, and
scoring nothing would stamp a completion that never happened. Set the root in `.env` (see
`.env.example`). The tracks cut by
`extract_tracks.py` are the policy's `stream_tracks` cache, cut as **FLAC** now: each cut is
written whole under a temporary name the policy holds, and renamed into its final `.flac`
name once ffmpeg has it.

**Clip formats: `.wav`, `.wv`, `.flac`, `.m4a`, `.mp3`** — lossless preferred, in that order
(everything decodes through ffmpeg, which reads WavPack natively). `.wv` earned its place the
hard way: Mystery Track 4's clip was wavpack-compacted and silently **left the query set** —
the harvester ran for days with the page saying "working" while searching for everything except
the one thing missing a clip. With no searchable clip at all, the harvester now stamps a
first-class **"nothing to search for"** state (`state["no_queries"]`) and keeps signing, instead
of leaving a stale "working" phase behind; and each pass it publishes the **bucket's**
signature count (`state["pool"]`, the pool's real size post-migration) and the current query
key per mystery (`state["query_keys"]`) so the harvest page can show a live "compared: N of
pool" per mystery.

**A new mystery sees the WHOLE corpus.** A chroma signature is not tied to the question you asked
of it: the same 12×N matrix answers MT4 today and MT8 next month, for free. So the harvester
remembers which **(signature, mystery)** pairs it has scored, and any unpaired combination is work
to do — a chunk each pass, riding along between signs. The day a new
`Mystery Track N` clip lands, every signature the pool holds is scored against it **without a
single new decode**: ~0.06 s each, ~3 minutes for the lot. `--rescan` does it all at once, for
when you want it finished now. `--rescan` also needs the ledger to exist (start the harvester
once and it is seeded from the bucket) and the rulings file to be readable, for the same reason
every scoring path does.

**The ledger replaces the working queue, and the recovery goes with it.** The old
`queue.json` lists (`pending`, `done`, `retry_later`) are gone; at its first start under this
contract the harvester seeds one `signed` row per key the bucket already holds (the bucket's
listing was the only record of what is signed), and on every start it reconciles the rows
against that listing: a `signed` row whose object is gone loses its `uploaded_etag`, so the
feeder feeds that key again. Past a safety cap (`NETRADIO_RECONCILE_DROP_CAP`, default 10% of
the signed corpus) the reconciliation **reports** — a standing `sig_alert` in the state, one
that stands down by itself on the first start that finds the loss gone — and touches
nothing: a mass drop means the store broke, not the rows, and would put the whole pool
back on the feeder's list over a configuration fault. **One writer, enforced:** both writer
paths (`--run`, `--sign-one`) hold the same flock (`harvest.WRITER_LOCK`, under its historic
`collector.lock` name) for their lifetime — a second writer, including the hand tool under a
running daemon, refuses loudly instead of interleaving.

The rescan also fills in **where** each old match hit (`at_s`), which is why the harvest page
can cue a lead to the moment it matched even for leads found before that field existed. The
position was never lost: it is recomputable from the signature we already hold.

**A clip too short to distinguish records is refused** (`MIN_QUERY_S`, 60s). MT7's clip is **23
seconds**, and it produced five *confident* false positives all within **0.0007** of each other: a
short query drives every cost down until the matcher can no longer tell records apart, and a
degenerate ranking looks exactly like a real one. Better to search for nothing than to search for
everything. The mystery re-enters the search **by itself** once a longer clip is cut.

**A re-cut clip asks the whole corpus again.** `state["scored"]` is keyed on the mystery number
**plus a fingerprint of the clip's contents** — so a better clip voids every pairing made against
the old one, and every held signature is scored against the new question. Keyed on the number
alone, a better MT7 clip would have silently inherited the 23-second clip's verdicts and never
actually been asked. Use `--forget N` to drop the stale *leads* as well: the pairings go by
themselves, but the leads are the part that misleads a human into ruling on evidence gathered with
a broken instrument.

**A ruled-out record stays ruled out.** `not a match` is deliberately **global**: it
means "not any Mystery Track", including the ones whose clips do not exist yet. So a rescan skips
it. Without that, the day MT8 lands, every record you have already rejected comes straight back at
you. (It does **not** mean "heard" — you can rule a record out as a match and still want to listen
to it. The two verdicts are kept apart.) The retired set is a **rulings file**,
`.harvest/rulings.json`: one key per entry that has been ruled on (heard, discarded, ignored,
duplicate, not-a-match) or that is the owner's own upload, each with its reason. The rulings'
writer writes it whole, atomically, at its start and after every ruling; the harvester only reads
it — the keys alone, never the reasons — re-reading it every pass so a ruling takes effect within
one loop iteration. **The harvester refuses to run without it** (`--run` and `--rescan` both
refuse, naming the file), because a search that has forgotten every ruling hands back records
already rejected. An empty file is fine — that is nothing ruled on yet; only a missing or
unreadable file is a refusal.

**What it does.** For each audio file in the configured directories with a complete sidecar and
no ledger row — or with a row whose size or modification time no longer matches, or a
`no_space` delay: decode it with ffmpeg in a child process, compute the **chroma signature**
(12×N float16, ~55 KB against ~8 MB), upload the signature and the sidecar beside it to the
bucket, write the ledger row, score the signature against every unsolved Mystery Track — **but
only the mysteries it holds a clip of** (see
[PROCESS §8b](../PROCESS.md#8b-giving-the-harvester-a-new-or-better-mystery-track-clip)) — and
keep an excerpt if the match is near. Then sleep and repeat.

A file comes back **delayed** instead of `signed` with one of five reasons: `length_mismatch`
(the decoded length disagrees with the sidecar's `duration_s` by more than `max(10 s, 2 %)` — a
hand-over that disagrees with its own label is not signed), `too_long` (over four hours —
refused, never truncated; splitting long audio is the feeder's job, each part a key of its
own), `decode_failed`, `no_space` (transient — retried on later passes until the policy makes
room), or `missing_sidecar` (the signature is in the bucket but its companion sidecar is
not — re-feed the key, and the row's empty size and mtime make the scan propose it for a
fresh sign that re-uploads both). A file that vanishes mid-sign gets **no row at all**, so it
stays on the feeder's list and is signed again when it comes back.

**It proposes; you dispose.** It never marks a mystery solved. It keeps the best **leads** (best 12
per mystery, evicting the worst when a better one lands) and you rule on them on the harvest
page — see
[PROCESS: *Ruling on what the harvester finds*](../PROCESS.md#ruling-on-what-the-harvester-finds-harvest).

**A lead is a URL, not audio.** `--purge-audio` threw away the retained excerpts, and nothing is
hoarded now: what survives is the key, the cost, the mystery, and *where* in the candidate it
matched. The page reviews each candidate by **embed at its source**. This is both the better
review and the only defensible copyright posture — the retained audio had grown to 2.2 GB and
included a 108-minute DJ mix kept whole, which broke the one claim the posture rested on. There is
now a hard cap in `write_excerpt`, and a test that feeds it that mix and demands 30 seconds back.

**Why signatures.** The matcher can only find what's in the pool, and the pool we want is far
bigger than this disk. 100,000 tracks is ~5 GB of signatures and 0 GB of audio.

**Watch it** on the harvest page: pause/resume, the self-test, the mysteries it is *not*
searching for, and the ruling buttons. The peer repo **supervises** it — it adopts a
hand-started harvester rather than spawning a second, and a watchdog revives it if it dies.

### How much memory it uses, and why

Two `harvest.py` processes exist while a file is being signed: the long-running parent
(`--run`) and a **decode child** (`--sign-job DIR`), plus the child's `ffmpeg`. The child does
the whole decode — read the file, decode, chroma, cache, upload — and exits. The parent, which
holds the state, the ledger and the matching board, never touches a file's audio, so its
footprint stays flat across files rather than climbing to a high-water mark and staying
there.

Three things put the memory back:

| What | Why it was needed |
|---|---|
| The tuning estimate runs 300 seconds at a time (`chroma_recipe.estimate_tuning_blockwise`) | `chroma_cqt` estimated the recording's distance from concert pitch over the **whole file** first, holding several copies of a 1025-row spectrogram with one column per 512 samples. Profiled on the development Mac, that one call accounted for about 9.8 GB of a 10.2 GB peak on a 117-minute candidate; the CQT itself cost about a tenth of it. The estimate returns the same float either way, so **every signature is byte-identical** and `RECIPE_VERSION` stays 1 — `tests/test_chroma_tuning.py` compares both against librosa's own whole-file path with `==` and `np.array_equal`. |
| `MallocLargeCache=0` | macOS libmalloc keeps freed large blocks inside the process instead of returning them to the kernel. Python frees everything and the footprint does not move; under pressure those dirty pages get compressed and swapped. Measured on macOS 26.5.2 by allocating and freeing 800 MB of float32: 764 MB still held afterwards by default, 0 MB with the variable set. The variable is **undocumented**, which is why the harvester re-measures it on every start. |
| ffmpeg writes the decoded PCM to a **file** in the job directory | The old path piped it through `communicate()`, which builds a chunk list and then joins it — two full copies of the audio at the moment of the join (1,034 MB held for 451 MB of PCM, measured). The parent reads the spool back as a memory map, and the file is unlinked as soon as it is mapped. Disk cost is 64 KB per second of audio, for as long as the file is being scored. |

The child's command line is `harvest.py --sign-job <dir>` and the audio path is **not on it** —
it is handed over in the job directory. The peer repo's supervisor finds a live harvester by
matching `--run` as a substring of the whole `ps` line, so a path on the argv would be a way for
a process that lives for one file to be adopted as the harvester. The job file (`sign.json`)
carries the path **and** the sidecar's declared length for it, so the child's length check weighs
the same facts the scan weighed; describing half the job there and half on a command line is how
the two would drift apart. To sign one file by hand, `--sign-one KEY` uses the same path (it
takes the writer lock and reconciles the ledger first).

`NETRADIO_HARVEST_CHILD=0` runs the decode in the harvester's own process instead of a child. It
is for diagnosing a venv or environment problem in the child; it brings the memory back with it,
so it is not for normal use.

`make harvest-run` sets `MallocLargeCache=0` for you; the peer repo's supervisor sets it too. It
is read at process start, so setting it from inside a running harvester does nothing. On every
start the harvester allocates and frees 800 MB and prints what the allocator kept — if that
number goes back up after an OS upgrade, the variable has stopped working and an `issues` row
says so.

Each signed file leaves a row in `state["mem"]` (and the last 50 in `state["mem_log"]`): the
parent's footprint, the parent's lifetime peak, and the decode child's peak and final footprint.
Set `NETRADIO_HARVEST_MEM_CEILING_MB` to have the **parent** stand down when it goes over that
number — the supervisor's watchdog then restarts it. It is **off by default**, because the right
number depends on what a long file actually costs and that measurement has not been taken yet. A
child over the ceiling only earns an `issues` row: its memory left with it.

**Stopping it.** `Ctrl-C`, or `SIGTERM` to the parent's pid, stops cleanly: the state is saved, the
file that was being signed gets **no ledger row**, and it is signed again on a later pass. No
signature is ever written unless ffmpeg exited 0, the decode is long enough, the file is not
over-long, the length matches the sidecar's claim, and no stop was asked for. The supervisor's
`stop()` signals the whole process group and still works.

### The canary: does the matcher still WORK?

`scripts/selftest.py`.

| Mode | What it proves |
|---|---|
| **offline** | Re-identifies a track we already know (Jamie Myerson, *Sky Blue*) out of a small pool, from local files. The matcher still **works** — not merely that the process is alive. No network. |
| **live** | Re-scores the canary's **stored** signature, named by `NETRADIO_CANARY_KEY`, against the canary's mix and the current mysteries. The canary is an ordinary entry: its file arrived through the harvest directories, was signed once, and its signature lives in the bucket under its key. Every pass the harvester pulls it back (the working cache first, the bucket if it is not local) and scores it — a re-score, never a re-sign. Needs `NETRADIO_CANARY_KEY` set in `.env` (see `.env.example`); `make_canary.py --key <url>` prints it. |

Both demand **cost, rank *and* a margin**. Requiring only "cost in range, rank 1" is not enough: a
degenerate matcher scores everything identically, ties sort by track number, and the subject — the
lowest-numbered case — ranks first. The canary then vouches for a completely broken matcher. *A tie
is not a win.*

The live check's three outcomes, all reported on the harvest page as distinct states — *a skip is
not a pass*:

- **PASS** — the canary's stored signature, scored against the canary's mix and the current
  mysteries, came back in the true-match range, first among the rivals, and by a real margin. The
  matcher is working; a standing canary alert (a previous failure) is cleared.
- **FAIL** — a known record's stored signature did not come back a match. The matcher or the
  signature is broken; the harvester raises `sig_alert` (the same alarm the ledger's reconcile
  raises, kept apart by a `kind` field) and every "no match" it reports from there is meaningless
  until it is fixed.
- **not checked** — `NETRADIO_CANARY_KEY` is unset (the canary is not configured), the canary's
  signature is not in the cache or the bucket, or there are no calibration cases to build the
  canary's mix from. None of those is a verdict on the matcher, so it is not a failure: the run
  carries on, the same way it does when the searched hit is refused.

**How the canary is named.** Under the new contract the canary is an ordinary entry: its file
arrives through the harvest directories like any other, is signed once, and is named by its
**key** (`NETRADIO_CANARY_KEY` in `.env`) — the pool's own rule, `u` + the first 20 hex of the
SHA-1 of the canary's source URL. `scripts/make_canary.py --key <url>` prints it, or the one-line
recipe in `.env.example` computes it. The harvester re-scores the canary's stored signature every
pass; `--live` runs the same re-score by hand, loading the signature through `sigstore`.

**Which track the canary is.** The canary's mix is built from the **first calibration case** (the
same track `--offline` uses), so the re-score works end-to-end on a fresh machine once the key is
set and the canary's signature is in the bucket — no `canary.json` step is needed. Feed the first
calibration case's source URL through the queue as the canary. (A `canary.json` written by the
by-hand `establish_canary` step overrides the default, naming a different calibration case as the
canary.)

**Establishing a canary by hand** (optional, for naming a non-default track as the canary):
`establish_canary` in `selftest.py` searches for a stream of a solved track, fetches it, and
scores what it fetched against the original held on disk. If the stream is not the record it is
rejected, not enshrined — a canary that cries wolf is worse than no canary. This is the one place
a fetch still lives; the per-pass re-score itself never fetches.
