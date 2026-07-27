# PROCESS — taking one capture from raw audio to published

> **What this is:** the single home for *how the analysis is actually done* — the manual
> Audacity pass, the by-ear technique that makes it work, and exactly where the engine helps.
> **Fits in:** [README](./README.md) (what this repo is) · [HOWTO](./HOWTO.md) (which tool to run
> right now) · [`scripts/streamalign/`](./scripts/streamalign/) (the engine).
> **Contains:** the per-recording loop, and the full **[label grammar](#label-grammar)** the loop writes.

The manual process is the backbone. The engine measures, does arithmetic, and cross-checks;
it **asks** when it cannot tell. Ear and judgement stay yours.

Format note: each step is **commands first**, then **Warnings** (things that bite), then
**Need-to-know** (context you may skip until something surprises you).

---

## The shape of the work

Place every capture on one **master timeline**; identify every track along it. Progress lives
on the sheet's **File List** tab, driven straight from the labels:

| Status | Means | Set by |
|---|---|---|
| **complete** | listened + labelled to the end | `file end: <stem>.wav … COMPLETE` |
| **verified** | position cross-checked against an *overlapping* capture | `verified <other>` on a `file [start] sync:` line |

Two kinds of neighbour — decides what the engine can do:

- **Overlapping** = shared audio → the engine can *measure* offsets, *find* skips, cross-check
  placements. Most of the stream.
- **Exactly-joined** = no shared audio → nothing to correlate. Position can only be **carried
  forward** from the previous file's `file_<next>:` hand link; anything the engine says is
  derived, not measured — and it says so.

---

## The per-recording loop

Steps 8–9 only apply to files containing unidentified or original-bearing tracks.

### 0. Pick the next file

Work outward from already-placed neighbours (anchor: `d000-018 = 0`). Use the File List
complete/verified columns + [`tracklist-2017.txt`](./tracklist-2017.txt).

Need-to-know:
- Finishing a file (step 6) guesses this file for you from its `file_<next>:` link and offers
  to prep it — step 0 is usually just confirming that guess.

### 1. Open it, carrying the previous file's work forward

1. Open the previous capture's `.aup3` (the one whose labels link to your new file).
2. **Save As** the new name (e.g. `d356-375.aup3`).
3. Remove the old master track and its labels.
4. **Select All** (`⌘A`) → **Tracks ▸ Keep Tracks Synchronized** (Sync-Lock) **on**.
5. Click at the start of the new base track; drag the selection to zero.
6. **Select ▸ Track ▸ In All Tracks** (`⇧⌘K`) → **Delete** (`⌘K`).
7. **Select All**; Sync-Lock back **off**.

Also open an already-placed **overlapping** neighbour, if there is one.

### 2. Ask the engine what it thinks (hints)

```bash
make align-env        # ONCE per machine: the librosa venv (python3.13)

set -a && . ./.env_vars && set +a
PYTHONPATH=scripts .venv/bin/python -m streamalign hints <stem>
# -> labels/<stem>.hints.tsv    Audacity: File ▸ Import ▸ Labels
```

Warnings:
- Plain `python3` works but yields everything *except* the sync anchors (chroma needs the
  venv). `make align-check` says whether you're set up.
- This is a deterministic script, not an AI session — same inputs, same hints.

Need-to-know:
- The hints file proposes: `file start sync:` + `file end:`; the **measured offset to each
  overlapping neighbour** (+ skip candidates) — or a plain question if the file is
  exactly-joined; what the [1998/2017 notes](./tracklist-2017.txt) say plays here; **sync-anchor
  candidates** (moments an original plays *alone* — a ready-made `track sync:`/`origNNN sync:`
  pair, see step 9); `note QUESTION:` wherever it cannot corroborate.
- Every row carries confidence (`confidence 9.8/10`) and is marked `HINT`.
- **Hints never touch your labels.** `<stem>.hints.tsv` is gitignored scratch, invisible to
  solve/build/sheet. Import, copy what you accept, delete the rest.
- Three sources, three trust levels: **audio cross-correlation** (measured — strongest);
  **your own previous labels** (as good as that label); **the 1998/2017 notes** (approximate —
  always confirm). Hints for file N+1 get better once you finish file N; just re-run.

### 3. Place the file

```bash
PYTHONPATH=scripts python3 -m streamalign align <placed-neighbour> <this-file>
# master_start(this) = master_start(neighbour) + offset
```

Warnings:
- **Verify by ear.** The number is a measurement, not a verdict.

Need-to-know:
- Prints offset (s) + 0–1 confidence.
- A second overlapping placed neighbour → measure that too; redundant edges cross-check the
  placement (`validate` grades them all in step 10).
- Exactly-joined neighbour: nothing to measure — the placement is the previous file's
  `file_<next>:` link, and is only as good as that hand label.

### 4. Certify the skips

```bash
PYTHONPATH=scripts python3 -m streamalign skip-clips          # detect + render review clips
PYTHONPATH=scripts python3 -m streamalign skip-confirm <id>   # writes into your hand labels
PYTHONPATH=scripts python3 -m streamalign skip-reject  <id>   # engine stops re-proposing it
```

Warnings:
- Skips are **load-bearing**: `master_end = local_length + master_start + Σ(skips)`. A missed
  skip shifts everything placed downstream — find them before chaining onward.
- Skip detection walks an **overlap** — on an exactly-joined file it has nothing to work with;
  those skips are yours to find by ear.

### 5. Hand-label in Audacity

Lay the label track (full spec: [label grammar](#label-grammar)):

- `file start sync: <stem>.wav <master_start> verified <neighbour>` at local **0.0**
- `startNNN: ID: <Artist> - <Title>` at each track boundary
- `file note: skip ahead|back <N>s` at each confirmed skip
- `track sync: A`/`B` + `origNNN sync: A`/`B` where an original lines up (technique below)
- `file end: <stem>.wav COMPLETE` at the end
- `LABELTRACK <name>` as the first label of a track, if exporting several tracks at once

### 6. Export, sort, sanity-check

**File ▸ Export ▸ Export Labels** (`⇧⌘;`) → `labels/<stem>.labels.txt`, then:

```bash
python3 labels/sort_tsv.py labels/<stem>.labels.txt          # rename .txt→.tsv, sort, validate
python3 labels/sort_tsv.py labels/<stem>.labels.tsv --test   # dry run: report notices only
python3 labels/sort_tsv.py labels/<stem>.labels.tsv --live   # diff on-disk vs Audacity's live labels
python3 labels/sort_tsv.py labels/<stem>.labels.tsv --adjust # rebase timestamps to the first file start
```

Warnings:
- **Name the export `<stem>.labels.txt`, not `<stem>.txt`** — a bare `<stem>.tsv` is written
  fine but invisible to solve, build, publish and the sheet (`sort_tsv.py` warns).

Need-to-know:
- This **is** the txt→tsv tool — there is no other. Sorts, splits `file_OTHER:` rows onto the
  neighbour's file, validates grammar, flags a sync missing `verified` or a `file end:`
  missing `COMPLETE`.
- **One command = steps 6 + 7 + the offer of step 0/2 for the next file**: it seeds the
  neighbours' starters, guesses the successor (from `file_<next>:`, else the notes, else the
  filename), and offers to run `hints` on it. `--nextfile <stem>` overrides the guess;
  `--no-next` skips; `--next` runs without asking. The prompt only appears at a real terminal.

### 7. Seed the next file — automatic

Done by `sort_tsv.py` in step 6 (writes `<next>.starter.labels.tsv`, gitignored, for every
linked neighbour). To re-seed by hand after editing links:

```bash
PYTHONPATH=scripts python3 -m streamalign starter <this-stem>
```

### 8. Identify the tracks

```bash
PYTHONPATH=scripts .venv/bin/python scripts/identify_by_chroma.py --all-mystery
PYTHONPATH=scripts .venv/bin/python scripts/identify_by_chroma.py --pool ~/dnb-candidates
PYTHONPATH=scripts .venv/bin/python scripts/acoustid_check.py --mismatch   # verify the ORIGINALS
```

Warnings:
- **AcoustID cannot identify stream audio — don't try** (stream-vs-own-original fingerprint
  similarity 0.511 ≈ random; clean control 0.99; 65/89 originals ARE in AcoustID — the stream
  is the problem, see [`Archive/LESSON_acoustid_stream.md`](./Archive/LESSON_acoustid_stream.md)).
  Chroma survives what fingerprints don't — identification is *matching*, not *lookup*.
- **The matcher can only find what is IN THE POOL.** Mysteries are by definition absent from
  the known originals — the remaining work is **acquiring candidates** (era-appropriate
  1997/98 D&B, this DJ's labels/artists, `tracklist-2017.txt` leads), then `--pool` them.
- A mislabelled original poisons everything downstream — hence `acoustid_check.py` (clean
  files fingerprint fine; it caught two on its first run).

Need-to-know:
- Unidentified span → match by ear or chroma; label `ID: <Artist> - <Title>`. Placed but
  unnamed = `Mystery Track N`.
- Calibration: the true record ranks #1 at cost 0.0337 with runner-up 0.1017 (validated on
  *Urban Style*); the gate refuses anything not both good and decisively ahead.

#### 8b. Giving the harvester a new (or better) Mystery Track clip

```bash
cp "my-extract.wav" "$NETRADIO_SOURCES_DIR/Mystery Track 8.wav"
# then restart the harvester from /harvest (off → on)
```

Warnings:
- **No clip = not searched.** A mystery with no clip is not in the query set at all — the
  harvester can run a month without ever looking for it. `/harvest` lists what it cannot
  search and why.
- **Full-length extract, not a snippet.** A short query drives every cost down until
  unrelated records tie (MT7's 23 s clip → five "confident" false positives within 0.0007).
- The number is the **mystery's** number (4, 6, 7, 8…), *not* the master track number.

Need-to-know:
- The filename is the interface: `Mystery Track <N>.<wav|mp3|flac|m4a>`, case-insensitive,
  any digit count; `.wav` wins over a lossy duplicate.
- **The clip does not define mystery-ness** — the *title in `track-metadata.json`* does.
  Identify the track upstream and it leaves the search on the next metadata rebuild, even
  with the clip still on disk (MT5 today). This stops solved tracks being re-searched.
- Pickup is on harvester **start** (query set built once, in `run()`); restart resumes from
  disk — nothing is re-fetched or re-analysed.

### 9. Align the originals

Seat the A/B anchors (accept/confirm the `solo_anchors` from step 2's hints — each gives the
mix instant *and* the original instant; seat one early, one late; confirm by ear, technique
below). Then grade:

```bash
PYTHONPATH=scripts .venv/bin/python -m streamalign track-mix --tracks <n> [<n> …]
```

Warnings:
- `PYTHONPATH=scripts` is required — without it: `No module named streamalign`.
- `--sources` is a **flag** (default `sources_local`); `NETRADIO_SOURCES_DIR` is not read by
  this subcommand.
- **Don't chase the track's start/end** — records are blended; there is no objective "begins"
  frame. Anchor on moments the record plays **alone**.

Need-to-know:
- Defaults: `--meta track-metadata.json`, `--sources sources_local`. `--tracks` limits to
  this file's tracks; omit = re-grade all synced originals.
- **`sort_tsv.py` does not do this step** (it covers 6–7 + the next-file offer). 8–9 are
  manual and conditional; 9 only where you have the original.
- `track-mix` recovers mix/original rate + offset (chroma + DTW) and reports whether it's
  trustworthy. Free sanity check: a DJ pitches by a few percent — anchors implying a rate far
  from 1.0 are not both on the record (the engine gates on this).

### 10. Build + validate

```bash
python3 scripts/build_track_metadata.py --seed track-metadata.json     # labels + remainder.tsv → JSON
PYTHONPATH=scripts .venv/bin/python -m streamalign validate            # audio vs hand labels
```

Warnings:
- `validate` needs the venv python — plain `python3` fails with `No module named 'numpy'`.
- **`--seed` is not optional in practice**: curated fields (artwork, links, manual
  title/artist) reach the output *only* through the seed, and the drop is silent. The build
  **refuses** a rebuild that would destroy an identification (`--allow-identification-loss`
  to override; adding/filling is always waved through).

Need-to-know:
- `build_track_metadata.py` is the **only** writer of `track-metadata.json`; it also reads
  `labels/remainder.tsv`, so changes there (or in `tracklist-2017.txt`) land via this build.
- `validate` grades **overlapping capture pairs only** (confirmed/suspect/adjacent). Pairs
  come from `verified <capture-stem>` annotations — `verified d336-355` makes an edge;
  `verified by 067 Wave Forms` (a track) does not.
- **A tail file placed via originals may not appear at all — expected, not a failure.**
  End-to-end captures share no audio (< 5 s = "adjacent", none = nothing); such a file's
  placement is carried by its step-9 track anchors.
- What you're checking: your new file's overlap pairs (if any) are **confirmed**, and no
  previously-confirmed pair went suspect.

Then mirror to the player:

```bash
make sync            # 3-way, PR-based; reads NETRADIO_PLAYER_REPO from .env_vars
make tracklist-check # do the two copies agree?
```

### 11. Publish — open a PR with your labels, merge it, refresh the sheet

```bash
python3 labels/publish.py <stem>           # validate → sort → branch → commit → push → open PR
python3 labels/publish.py <stem> --check   # gate only, no branch/commit/push/PR
python3 labels/publish.py <stem> --dry-run # show the whole plan, touch nothing
```

`main` is PR-only (branch protection: a direct push is rejected with GH013), so publish no
longer pushes `main`. It hard-gates and sorts, then proposes the sorted labels on a fresh
branch and opens a PR for a human to merge:

1. **Hard gate** (`sort_tsv.py --test`: unverified sync / missing `COMPLETE` / bad grammar →
   refuse). All-or-nothing across the files you give it — one failure and **nothing** is
   committed or pushed.
2. **Sort** each file **in place** in your checkout (so your local copy matches what the PR
   proposes, exactly as before).
3. **Branch → commit → push → PR:** the commit/push/PR happen in a **throwaway `git worktree`
   under `.worktree/`** cut from `origin/main` (the same pattern `make sync` uses). Your
   invoking checkout — its branch, index, HEAD — is left **untouched**, so publish is safe to
   run even from the live `main` checkout the harvester runs out of. The branch is
   `labels/publish-YYYYMMDD-HHMMSS` (UTC); the PR is opened with `gh pr create`.
4. **Merge** the PR yourself. Publish **never pushes to main and never merges.** If `gh` is
   missing or the PR can't be opened, the branch is still pushed and publish prints the exact
   `compare/main...<branch>` URL to open the PR by hand.
5. **Refresh the sheet — only after the merge.** The sheet imports from `origin/main`, so the
   labels aren't there until the PR is merged; publish therefore **defers** the refresh and
   prints a reminder. Once merged:

   ```bash
   python3 labels/publish.py --refresh-only   # POSTs NETRADIO_SHEET_WEBHOOK (GithubImport)
   ```

Need-to-know:
- **Do not `git push` by hand first — publish does it** (onto its own branch, not main).
- The sheet imports from **origin `main`** — labels reach it only once the PR is merged.
- `NETRADIO_SHEET_WEBHOOK` unset → `--refresh-only` prints the **Reload Data** reminder and
  you click it yourself.
- Then **loop to step 0.**

---

## Aligning an original to the mix (by ear + by eye)

How to *find* the seat for the paired sync points of steps 5/9. The mix and the original are
rarely at the same clock rate (the DJ beatmatches) — the A/B pair *captures* that drift, you
don't fight it.

**Pick the cue:**
- A sharp **transient**, never a sustained sound: consonant plosive, word onset, snare/rim,
  vinyl click, one-shot FX.
- Prefer **aperiodic** — a purely rhythmic match can be a whole bar out.
- Vocals: switch to **Spectrogram** — consonants are clean vertical bursts.

**Hear the seat:**
- Hard-pan mix left / original right. Off by a hair = a **flam** leaning to one ear; at the
  seat it collapses to one centred hit; overshoot flams the other way.
- Sharper: **phase null** — `Effect ▸ Invert` the original, listen to the sum; shared content
  thins to a null at the seat (won't reach silence; the mix has other layers). Un-invert after.

**See the seat:** at sample zoom, match the **timing** of peaks/zero-crossings, not their
heights (the mix copy is EQ'd/compressed). Coarse-align on the phrase → transient → samples.

**Nudge precisely:**
- **Sync-Lock Tracks** moves the original + its `origNNN` labels as a unit. **Snap off**;
  Selection Toolbar in **samples**.
- Time-Shift drag at sample zoom = sub-sample per pixel.
- No numeric nudge exists. Exact amounts: edit the **head of the original only** —
  `Generate ▸ Silence` (typed duration) pushes later; delete `0`→`N` at the head to pull
  earlier. Drop Sync-Lock for that one edit or the mix ripples too.

**Two anchors, not one.** A early + B late; an outro a few ms out after a perfect intro *is*
the rate difference, solved downstream by `speed = (trackB − trackA) / (origB − origA)`
([`sheetscript/Code.js`](./sheetscript/Code.js)).

---

## The harvester, and the bot wall

`harvest.py` streams candidates, reduces each to a chroma signature, discards the audio,
scores against every unsolved mystery, for weeks. Watch it — and rule — at **`/harvest`**.

Give it a YouTube session — set **one** of these in `.env_vars` (gitignored), then restart:

```
NETRADIO_YTDLP_COOKIES=/path/to/cookies.txt        # PREFERRED: Netscape-format export
NETRADIO_YTDLP_COOKIES_FROM_BROWSER=firefox        # 2nd choice; chromium browsers last resort
```

Warnings:
- **Do not use `chrome` on macOS.** yt-dlp decrypts Chrome's cookie DB via the login
  Keychain **once per process** — a restarting (e.g. crash-looping) harvester prompts
  forever, and no number of correct passwords stops it (2026-07-13: four prompts; the real
  fault was a `KeyError` restart loop). Firefox's `cookies.sqlite` is unencrypted — no
  prompt; a `cookies.txt` file has no Keychain involvement at all and works headless.
- **The cookie is your logged-in session — a credential.** It lives in `.env_vars`, outside
  the repo (which is public). Close the browser before profile reads (locked DB).
- The bot-wall error (`Sign in to confirm you're not a bot`) carries **no 403/429**, so it
  bypasses host-backoff — the harvester **halts** on it by design. Waiting never fixes it;
  only a session does.

Need-to-know:
- Values pass straight to yt-dlp (`--cookies-from-browser <value>` / `--cookies <path>`), so
  anything yt-dlp accepts works, incl. `chrome:Profile 2`.
- Both are off by default — the harvester never reads a browser profile unbidden.

### Exporting a `cookies.txt`

1. **Private/incognito window** → log in to YouTube.
2. Export with a Netscape-format extension (*Get cookies.txt LOCALLY* — Chrome/Brave/Edge;
   *cookies.txt* — Firefox).
3. Store **outside the repo**, locked down:

       mkdir -p ~/.config/netradio && chmod 700 ~/.config/netradio
       mv ~/Downloads/youtube.com_cookies.txt ~/.config/netradio/youtube-cookies.txt
       chmod 600 ~/.config/netradio/youtube-cookies.txt

4. `.env_vars`: `NETRADIO_YTDLP_COOKIES=/Users/<you>/.config/netradio/youtube-cookies.txt`,
   then restart the harvester from `/harvest`.
5. **Close the private window without logging out** — logout rotates the session and kills
   the cookie you just exported.

If it halts again with cookies configured, the session **expired** — re-export. `/harvest`
(and the player's `run_player.sh status`) says which situation you're in.

## Ruling on what the harvester finds (`/harvest`)

The harvester **proposes**; you **dispose**. It never marks a mystery solved (costs are
distances; the true/non-match populations overlap; the matcher has been caught vouching for
itself). Three rulings, one click each, recorded against the lead:

- **match** — it's the record. Favourites + marks heard in the listen queue. Then finish the
  job: fill the identification (step 8) and **name the mystery** so it leaves the query set —
  a solved track still titled `Mystery Track N` keeps being searched for.
- **near** — related, not it (shared break / sample / remix). Kept as a lead.
- **no** — dismissed, won't return.

Reading a candidate — in order of how much each should move you:

| | |
|---|---|
| **Rank + margin** | The one that matters: a true match beats the field, not ties it. |
| **Cost** | True match ≈ 0.004–0.03; unrelated ≈ 0.095. Populations **overlap** — cost alone is not a verdict. |
| **Key** | All 12 transpositions are tried; an odd key is not suspicious. |

([`docs/CALIBRATION.md`](./docs/CALIBRATION.md) for provenance.)

Warnings:
- **A tight cluster is a degenerate match, not five near-misses** (MT7: five "confident"
  hits within 0.0007 — cause: a 23 s query). Fix the clip (§8b); don't rule on noise.

Need-to-know:
- Nothing is kept as audio (`--purge-audio`): a lead = URL + cost + mystery + key + where it
  matched. `/harvest` plays candidates from a source embed — better review, defensible
  copyright posture.
- Rulings are durable and shared with the listen queue (chroma-sig badge there; queue flags
  here) — a track you've already heard and discarded is not re-offered as a discovery.
- `/harvest` also names mysteries it **cannot** search (no clip) — otherwise invisible.
- Self-test states: **PASS / FAIL / not checked** — a skip is never a pass. *offline* proves
  the matcher works (re-IDs a known track); *live* proves it end-to-end from a real stream,
  and refuses to establish a canary on a wrong upload (hand it a known-good URL if it never
  settles).

## What the engine cannot do

- **No overlap, no measurement.** Cross-correlation is its only evidence; on an
  exactly-joined file it cannot propose a sync or find a skip — it asks instead.
- **No blind search for an original.** `locate_original` on a whole capture: 2 hits in 8,
  most-confident answer wrong by 25 min (beatmatching drifts a fixed-lag correlation; the
  broadcast repeats material, so the answer isn't unique). Deliberately not offered as a
  hint — see [`Archive/LESSON_locate_original.md`](./Archive/LESSON_locate_original.md).
  **With a prior it works** — that's `solo_anchors`.
- **No track start/end.** Records are blended; start/end is a judgement call and stays
  yours. It offers the moments a record plays **alone** instead.
- **It does not decide.** Everything produces labels, clips, scores or questions for review.
  Only `build_track_metadata.py` writes the authoritative JSON.

## Label grammar

Each Audacity label is `start_time ⇥ end_time ⇥ text`; `sort_tsv.py` and `Code.js` parse the
text with the same patterns. Timestamps are **local**; adding the file's `file start sync`
offset yields **master time**.

**Label-track scoping (`LABELTRACK`)**

- `LABELTRACK <name>` — first label of an Audacity label track *names* it, so one export can
  carry several tracks. Scopes every following label until the next `LABELTRACK`; stripped
  from the emitted `.tsv`. `<name>` resolves: **primary stem** → verbatim; **another capture
  stem** → re-homed via `file_<name>:`; **anything else** (e.g. `070.labels`) →
  prefix-expanded (`sync: 0` → `orig070 sync: 0`). A file using `LABELTRACK` is validated —
  every block must carry a marker or `sort_tsv.py` fails before writing. Legacy exports
  without markers sort unchanged. `--stem <name>` sets the primary when reading stdin.
- **The track name is not the qualifier** — `069-dig.labels` and `069.vinyl` both hold labels
  *about* `orig069`: the qualifier is the **3-digit original in the name**.

**Free text vs. a mistyped keyword** — decided by **shape**, in order:

1. Already names its subject (`orig070 sync: A`, `file end: …`, `069s0`) → emitted as written.
2. `<qualifier> ` + label parses → qualified (`sync: 0` → `orig070 sync: 0`).
3. **Keyword-shaped but parses under no rule → ERROR, nothing written.** Keyword-shaped =
   leads with an entity (`orig…`, `track…`, `file…`, `mix…`), a verb + colon (`sync:`,
   `start:`, `end:`, `note:`, `ID:`), or compact `NNNs`/`NNNe`. So `orig070 start` (no
   colon), `orig069: start` (misplaced), `s71e1` (transposed) are all caught.
4. Free text → auto-noted (`start overlap` → `note: start overlap`), silently; the run prints
   a summary of everything auto-noted.

**Scope check:** inside `LABELTRACK 071`, a label whose **head** names a different original
(`orig017 sync: A`) is an **ERROR** — the one typo class no grammar can see. Note *bodies* may
mention any original; put genuine cross-references in the primary track.

**File-level markers (the connection backbone)**

- `file start: FILE.wav` — physical start / pre-roll. **Not** authoritative.
- `file start sync: FILE.wav OFFSET [verified OTHERFILE]` — **authoritative master anchor**;
  OFFSET + local time = master time.
- `file sync: FILE.wav OFFSET verified OTHERFILE` — sync tying this file to another; often
  authoritative for local 0.
- `file end: FILE.wav … COMPLETE` (or `COMPLETED`) — endpoint; **drives "complete"**.
- `verified OTHERFILE` / `verified by …` / `MARK verified by …` / `NOT VERIFIED` — the
  connection record; **drives "verified"**.
- `file_OTHER: <label>` — re-home `<label>` onto a different wav's track.

**Track identity**

- `startNNN: ID: Artist - Title` — track start (`NNN` = track number; split on ` - `).
- `ID: Artist - Title` — an ID without starting a new track.

**Sync points (mix ↔ original; drive the speed calc)**

- `track sync: X` / `trackNNN sync: X` — mix-side; `origNNN sync: X` — original-side.
- `X`: **`A`/`B` are the paired speed anchors** (`(trackB−trackA)/(origB−origA)`); numeric
  `0,1,2,…` are rough/secondary.

**Original-track spans**

- `origNNN start:|end:|note: …` — colon **required**, argument optional (`orig070 start:`
  alone = "begins here"). No colon / misplaced colon errors out (see shape rules).
- Shorthand `NNNs…`/`NNNe…` (3 digits first: `069s0`, `067eB`, `071e1` — never `s71e1`).

**Generic notes & skips**

- `mix start|end|note: …`, `note[ TAG]: …`.
- Skips are notes: `file note: SKIP ahead 1.248s` / `note: SKIP back 2.279s (multiple files)`
  — stream discontinuities ([STREAM_PROVENANCE.md](./STREAM_PROVENANCE.md)).

---

## File-naming: who owns what

| File | Written by | Committed? | Authority |
|---|---|---|---|
| `<stem>.labels.tsv` | **you**, by hand | **yes** | authoritative; nothing may overwrite it. Reaches the sheet. |
| `<stem>.auto.labels.tsv` | engine (`tail-solve --emit`) | yes | regenerable; consumed by solve/build. Reaches the sheet. |
| `<stem>.starter.labels.tsv` | `sort_tsv.py` / `starter` | gitignored | seed only; excluded everywhere |
| `<stem>.hints.tsv` | `streamalign hints` | gitignored | suggestions + questions; invisible to solve/build/sheet |

The sheet importer (`sheetscript/Code.js`) reads **only** `*.labels.tsv` (incl. `.auto.`) +
the hand-kept `remainder.tsv` — mirroring `groundtruth.is_pipeline_label_file`. A bare
`<stem>.tsv`, starters and hints are all excluded.

---

## Addendum — does the record's speed *drift* while it plays?

**No: constant within a track, different per track.** Measured (chroma/DTW warp path fitted
linearly, 7 tracks with originals):

| track | R² (linear fit) | residual | rate | curvature |
|---|---|---|---|---|
| 7 | 1.00000 | 0.06 s | 0.9961 | 0.0001 |
| 11 | 1.00000 | 0.05 s | 1.0022 | 0.0016 |
| 12 | 1.00000 | 0.08 s | 1.0008 | 0.0009 |
| 16 | 1.00000 | 0.06 s | 1.0113 | 0.0002 |
| 13 | 0.99999 | 0.36 s | 1.0030 | 0.0003 |
| 10 | 0.99994 | 1.13 s | 1.0075 | 0.0150 |
| **6** | 0.99942 | 0.41 s | 1.0123 | **0.1247** |

Six of seven are dead straight — a pitch fader set once (what beatmatching requires); rates
*differ between* tracks (0.9961…1.0123): the DJ picks a pitch per record.

Consequences:
- **Two anchors per track is the right model**, not an approximation — chasing a per-moment
  rate is chasing noise.
- **A rate belongs to a track, never a file.** Don't carry one across a boundary.
- **All measured rates sit within ~1.3% of 1.0** — a pair implying otherwise hasn't found the
  record (the engine's `RATE_PLAUSIBLE` gate).

Caveat: track 6 shows real curvature *and* the weakest fit — a DJ riding the fader, or more
likely a poor alignment. One outlier with the worst R² is not evidence of drift. If it
matters, listen to it.
