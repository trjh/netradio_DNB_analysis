# PROCESS — taking one capture from raw audio to published

> **What this is:** the single home for *how the analysis is actually done* — the manual
> Audacity pass, the by-ear technique that makes it work, and exactly where the engine helps.
> **Fits in:** [README](./README.md) (what this repo is) · [HOWTO](./HOWTO.md) (which tool to run
> right now) · [`scripts/streamalign/`](./scripts/streamalign/) (the engine).
> **Contains:** the per-recording loop, and the full **[label grammar](#label-grammar)** the loop writes.

The manual process is the backbone and is **not** being replaced. The engine's job is to do
the measuring, the arithmetic and the cross-checking — the things a human is slow and
unreliable at — and to **ask** rather than assert when it cannot tell. Ear and judgement stay
where they are.

---

## The shape of the work

Every capture file has to be placed on one **master timeline** for the whole broadcast, and
every track identified along it. Progress is tracked per capture on the spreadsheet's
**File List** tab, with two statuses that come straight from the labels:

| Status | What it means | The label that sets it |
|---|---|---|
| **complete** | you listened to and labelled the file to its end | `file end: <stem>.wav … COMPLETE` |
| **verified** | its position was cross-checked against an *overlapping* capture | `verified <other>` on a `file [start] sync:` line |

**Verified is the record of how the captures connect to each other**, and it is only possible
where two captures share audio.

### Two kinds of neighbour — this decides what the engine can do for you

- **Overlapping** captures share audio. The engine can cross-correlate them, so it can
  *measure* the offset, *find* skips, and cross-check a placement. Most of the stream is
  like this.
- **Exactly-joined** captures follow one another directly and share **no** audio. There is
  nothing to correlate: the engine cannot measure an offset or detect a skip, and the next
  file's position can only be **carried forward** from a hand link in the previous one
  (`file_<next>:`). Anything it says about such a file is derived, not measured — and it will
  tell you so.

---

## The per-recording loop

Steps 8–9 only apply to files containing unidentified or original-bearing tracks.

### 0. Pick the next file

Work **outward from already-placed neighbours** — a file is ready when it overlaps (or joins)
one already on the master clock. The chain starts from the anchor `d000-018 = 0`. Use the
**File List** complete/verified columns plus [`tracklist-2017.txt`](./tracklist-2017.txt).

In practice you rarely start here cold: finishing a file (step 6) **guesses this file for you**
from its `file_<next>:` link and offers to prep it, so step 0 is usually just confirming that
guess.

### 1. Open it, carrying the previous file's work forward

Rather than starting from a blank project, reuse the previous capture's Audacity project so
its labels come with you:

1. Open the previous capture's `.aup3` — the one whose labels link to your new file.
2. **Save As** the new name (e.g. `d356-375.aup3`).
3. Remove the old master track and its labels.
4. **Select All** (`⌘A`), then **Tracks ▸ Keep Tracks Synchronized** (Sync-Lock) **on**.
5. Click at the start of the new base track and drag the selection to zero.
6. **Select ▸ Track ▸ In All Tracks** (`⇧⌘K`), then **Delete** (`⌘K`).
7. **Select All** again and turn Sync-Lock back **off**.

The carried labels now sit on the new file's timeline. Open an already-placed **overlapping
neighbour** too, if there is one.

### 2. Ask the engine what it thinks (hints)

> **This is a script, not an AI session.** You run it yourself, once per capture, and it is
> deterministic — same inputs, same hints. Nothing in this loop needs an LLM. (One was used to
> *write* the engine; none is needed to *run* it.)

Before labelling, get the engine's opinion as a **separate label track** you can accept,
ignore, or argue with:

```bash
make align-env        # ONCE per machine: the librosa venv + NETRADIO_SOURCES_DIR (see below)

set -a && . ./.env_vars && set +a
PYTHONPATH=scripts .venv/bin/python -m streamalign hints <stem>
# -> labels/<stem>.hints.tsv    Audacity: File ▸ Import ▸ Labels
```

You get, in one file:

- a proposed **`file start sync:`** and **`file end:`**;
- the **measured offset to each overlapping neighbour** (and skip candidates found by walking
  that overlap) — or, if the file is *exactly joined* and there is no overlap, a question
  saying so plainly rather than a guess;
- **what the [1998/2017 notes](./tracklist-2017.txt) say plays here** — each track's start, in
  this file's local time, by name;
- **sync-anchor candidates**: instants where one original plays *alone* in the mix, giving both
  the moment in the mix **and** the matching moment inside the record — a ready-made
  `track sync: A` / `origNNN sync: A` pair (see step 9);
- and a **`note QUESTION:`** wherever it cannot corroborate something, explaining *why*.

Every row carries its confidence spelled out (`confidence 9.8/10`) and is marked `HINT`.

**Hints never touch your labels.** They are written to `<stem>.hints.tsv`, which is not a
`.labels.tsv` and so is invisible to the solve and the build; it is **gitignored** (regenerable
scratch), and the sheet importer skips it by name — so the engine's guesses can never reach the
sheet as if they were facts. They only ever *add*: import the track, copy across what you
accept, delete the rest.

**Plain `python3` works too**, but without the librosa venv you get everything *except* the
sync anchors (they need chroma). `make align-check` tells you whether you're set up for them.

#### Where the hints come from — and so, when they get better

The engine has exactly three sources, and knowing which is which tells you how far to trust a
row:

| Source | Gives you | Trust |
|---|---|---|
| **The audio** (cross-correlation vs an *overlapping* capture) | offsets, skips | **measured** — the strongest thing here |
| **Your own hand labels** (the previous file's `file_<next>:` link, its last open track) | the anchor, what's still playing at local 0.0 | only as good as that label |
| **The 1998/2017 notes** | which tracks play, roughly where; the prior that makes the anchor search possible | hand-typed, approximate — always confirm |

So: **the hints for file N+1 get better once you finish file N**, because two of those three
sources are *your own work*. That's the loop — no re-analysis needed, just re-run the script.

### 3. Place the file

For an **overlapping** neighbour, measure the offset:

```bash
PYTHONPATH=scripts python3 -m streamalign align <placed-neighbour> <this-file>
```

It prints the offset in seconds and a 0–1 confidence, and `master_start(this) =
master_start(neighbour) + offset`. **Verify by ear.** If a second placed neighbour also
overlaps, measure that too — redundant edges cross-check the placement (`… validate` grades
them all later).

For an **exactly-joined** neighbour there is nothing to measure: the placement comes from the
previous file's `file_<next>:` link, and is only as good as that hand label.

### 4. Certify the skips

Skips are **load-bearing**: `master_end = local_length + master_start + Σ(skips inside)`. A
missed skip shifts everything placed downstream, so find them before chaining onward.

```bash
PYTHONPATH=scripts python3 -m streamalign skip-clips     # detect + render review clips
PYTHONPATH=scripts python3 -m streamalign skip-confirm <id>   # -> writes into your hand labels
PYTHONPATH=scripts python3 -m streamalign skip-reject  <id>   # -> engine stops re-proposing it
```

Listen to each clip in the clip-review player and rule on it. Skip detection walks an
**overlap**, so on an exactly-joined file it has nothing to work with — those skips are yours
to find by ear.

### 5. Hand-label in Audacity

With the master start and the skips known, lay the label track (full spec: the
[label grammar](#label-grammar)):

- `file start sync: <stem>.wav <master_start> verified <neighbour>` at local **0.0**
- `startNNN: ID: <Artist> - <Title>` at each track boundary
- `file note: skip ahead|back <N>s` at each confirmed skip
- `track sync: A`/`B` + `origNNN sync: A`/`B` where an original lines up (see below)
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

This **is** the txt→tsv tool — there is no other. It sorts, splits `file_OTHER:` rows onto the
neighbour's file, validates the grammar, and flags a sync line missing `verified` or a
`file end:` missing `COMPLETE`.

**Name the export `<stem>.labels.txt`, not `<stem>.txt`.** The pipeline and the sheet read
`<stem>.labels.tsv`; a bare `<stem>.tsv` is written fine but invisible to solve, build, publish
and the sheet — so `sort_tsv.py` warns you when it writes one.

**One command does steps 6 + 7, and offers step 0 for the next file.** On a successful sort it
also **seeds the neighbours** (step 7, below — no separate command) and then **offers to prep
the next file**: it guesses the successor from your `file_<next>:` link (falling back to the
1998/2017 notes, then the filename range) and, if you agree, runs `streamalign hints` on it so
its `<next>.hints.tsv` is waiting. `--nextfile <stem>` overrides the guess; `--no-next` skips
it; `--next` runs without asking. The prompt appears only at a real terminal, so `publish.py`
(which calls `sort_tsv.py`) never blocks.

### 7. Seed the next file — automatic

If you captured where the **next** file begins (a `file_<next>:` link), its labels are carried
forward so the next file doesn't start blank. **`sort_tsv.py` does this for you in step 6** —
you don't run a separate command. It writes `<next>.starter.labels.tsv` (gitignored,
regenerable) for every neighbour this file links.

To re-seed by hand (or after editing the links), the underlying command still exists:

```bash
PYTHONPATH=scripts python3 -m streamalign starter <this-stem>   # sort_tsv already ran this
```

### 8. Identify the tracks

For an unidentified span: AcoustID-fingerprint it, or match it against your originals by ear.
Add `ID: <Artist> - <Title>` to the label. An unnamed but placed track is `Mystery Track N`.

**The Mystery Tracks.** Identification by AcoustID **does not work on this material, and
cannot** — see [`Archive/LESSON_acoustid_stream.md`](./Archive/LESSON_acoustid_stream.md). The
same record taken from the 1998 broadcast has a bitwise fingerprint similarity of **0.511** to
its own clean original, and 0.50 is random noise: the ISDN/RealAudio compression and the DJ's EQ
destroy exactly the detail a fingerprint keys on. (Measured with controls — the *clean* file of
that record matches at 0.99, and 65 of 89 originals are in AcoustID. The database is fine; the
stream audio is not.)

**Chroma, however, survives what fingerprints do not** — which is why the alignment engine works
at all. So identification is a *matching* problem, not a *lookup* problem:

```bash
PYTHONPATH=scripts .venv/bin/python scripts/identify_by_chroma.py --all-mystery
PYTHONPATH=scripts .venv/bin/python scripts/identify_by_chroma.py --pool ~/dnb-candidates
```

Validated: given 90 s of the **stream** where Dead Calm's *Urban Style* plays, matched against
78 originals, the true record ranks **#1 (cost 0.0337)** with the runner-up at **0.1017**. The
gate refuses anything that isn't both absolutely good and decisively ahead, because a bland file
that matches everything is matching nothing.

> **The catch is the whole game: it can only find what is IN THE POOL.** The Mystery Tracks are
> by definition records nobody recognised, so they are not among the known originals — run it
> today and every one scores at the non-match floor. **The remaining work is not matching, it is
> ACQUIRING CANDIDATES**: era-appropriate 1997/98 D&B, the labels and artists this DJ was
> playing, leads from `tracklist-2017.txt`. Point `--pool` at them and the matcher will tell you,
> reliably, whether the mystery is among them.

And **verify the originals themselves** — a mislabelled source poisons every alignment and ID
downstream. This one *does* use AcoustID, because clean files fingerprint fine:

```bash
PYTHONPATH=scripts .env/bin/python scripts/acoustid_check.py --mismatch
```

It caught two on its first run: `013-DJ Addiction - Senses.mp3` is really Blame's *J-Walkin'*
(the same record as `021`), and `022-Castillo - Junkle I.flac` is by *Callisto*.

#### 8b. Giving the harvester a new (or better) Mystery Track clip

The harvester can only search for a mystery **it holds a clip of**. A mystery with no clip is not
in the query set at all — the search can run for a month and never find it, *because it is not
looking*. This is the single most consequential thing you can do for the search, and it is one
file copy.

**The whole procedure:**

```bash
cp "my-extract.wav" "$NETRADIO_SOURCES_DIR/Mystery Track 8.wav"
```

That is it. Nothing to run, nothing to register.

**The filename is the interface.** `streamalign/mystery.py` matches, case-insensitively:

       Mystery Track <N>.<wav|mp3|flac|m4a>

- `<N>` is **any number of digits** — `Mystery Track 11.wav` works exactly like `Mystery Track 8.wav`.
- `.wav` **wins** over a lossy re-encode of the same clip, so you can leave both in place.
- The number is the **mystery's** number (4, 6, 7, 8…), *not* the master track number (68, 76, 84…).

**A clip does not make a mystery, and a filename never did.** A track is a mystery if, and only if,
its **title in `track-metadata.json`** still says `Mystery Track N`. Identify it upstream and it
leaves the search the moment the metadata is rebuilt — *even though its clip is still sitting in
`sources/`*. That is deliberate: the tools used to glob `sources/Mystery Track *` and spent **~40%
of their work re-answering questions that were already solved**, and a spurious hit against a
solved track reads exactly like a real lead. Mystery Track 5's clip is still on disk today, and it
is correctly no longer searched for.

**When does a running harvester pick it up?** On its **next start** — the query set is built once,
in `run()`. So drop the file in and restart it from `/harvest` (**off**, then **on**). It resumes
where it left off: the queue, the `done` list and every chroma signature are on disk, so nothing is
re-fetched or re-analysed. `/harvest` lists what it is searching for, and names the mysteries it
**cannot** search for and why.

**Make it a full-length extract, not a snippet.** A short query drives *every* cost down until
unrelated records tie for first. Mystery Track 7's clip is **23 seconds** and it produced five
confident false positives, all within 0.0007 of each other. Prefer the whole span the record plays
for. This is the same lesson as the canary's margin check — winning by nothing is not winning.

### 9. Align the originals

**Don't chase the track's start and end** — in a DJ mix records are *blended*, so there is no
frame at which one "begins", and picking one is subjective. What you actually need is the
opposite: moments where the record plays **alone**, which are objective and are exactly what
an A/B anchor is made of.

The engine will propose those (`solo_anchors`, surfaced through `streamalign hints`, and
bounded by the [2017 notes](./tracklist-2017.txt)). Each candidate gives **both** times — an
instant in the mix *and* the matching instant in the original — i.e. a ready-made
`track sync:` / `origNNN sync:` pair. Seat one early (`A`) and one late (`B`); confirm both by
ear using the technique below.

Then grade the result:

```bash
make align-env      # once: the librosa venv (python3.13) + NETRADIO_SOURCES_DIR in .env_vars
make align-check
.venv/bin/python -m streamalign track-mix --meta track-metadata.json --sources <originals-dir>
```

`track-mix` recovers the mix/original rate + offset per track (chroma + DTW) and reports
whether it is reliable enough to trust.

> **Sanity check that costs nothing:** a DJ pitches a record by a *few percent*. If your two
> anchors imply a rate far from 1.0, they are not both on the record — the engine gates its own
> candidates on exactly this, and it is how it catches its own bad matches.

### 10. Build + validate

```bash
python3 scripts/build_track_metadata.py --seed track-metadata.json   # labels + remainder.tsv → JSON
PYTHONPATH=scripts python3 -m streamalign validate                   # engine vs hand: error table
```

`build_track_metadata.py` is the **only** writer of `track-metadata.json`. Nothing in the
notating steps above writes it. It reads the hand labels **and** `labels/remainder.tsv` (the
first-pass tail, tracks ~67–91), so this is also how a change to `remainder.tsv` or
`tracklist-2017.txt` reaches the JSON.

**`--seed` is not optional in practice.** Curated fields — artwork, links, and any manual
title/artist — reach the output *only* through the seed. Without it they are dropped, and the drop
is silent: a rebuild that destroys an identification looks exactly like a rebuild that worked.

So it now **refuses**:

       REFUSING TO WRITE -- this rebuild would DESTROY 2 identification(s):
         track 74: artist  "Jacob's Optical Stairway" -> ''
         track 90: artist  'Depeche Mode' -> ''

The guard is one-directional: *adding* a title, or filling in a missing artist, is waved straight
through — it only ever objects to going backwards. `--allow-identification-loss` overrides it, if
you genuinely mean to.

Then mirror it to the player, which keeps a copy for its own tracklist:

```bash
make sync            # 3-way, PR-based; reads NETRADIO_PLAYER_REPO from .env_vars
make tracklist-check # report whether the two copies agree
```

### 11. Publish — push your labels to the sheet

```bash
python3 labels/publish.py <stem>           # update the Google Sheet from your labels
python3 labels/publish.py <stem> --check   # check only, push nothing
```

That first line is the whole job: it takes your finished `<stem>.labels.tsv` and gets it into
the sheet. Under the hood it **hard-gates** every file first — re-running `sort_tsv.py --test`
to refuse an unverified sync, a `file end` missing `COMPLETE`, or bad grammar — and only if all
pass does it sort → `git commit` → `git push` → POST the sheet's refresh webhook. All-or-nothing
across the files you give it. Then **loop to step 0.**

---

## Aligning an original to the mix (by ear + by eye)

Steps 5 and 9 say to overlay the original and drop paired sync points. This is how you
actually *find* the seat.

Up front: **the mix and the original are rarely at the same clock rate** — the DJ beatmatches
— so a point that seats perfectly at one end drifts by the other. That drift is *captured* by
the **A/B anchor pair**, not fought.

**Pick the cue.**

- Anchor on a sharp **transient**, never a sustained sound: a consonant plosive, a word onset,
  a snare or rim, a vinyl click, a one-shot FX. The phrase you remember only tells you *which*
  transient to seat on.
- Prefer an **aperiodic** cue. The break loops, so a purely rhythmic match can be a whole bar
  out; a non-repeating event pins the *absolute* position.
- For vocals, switch the track to **Spectrogram** — consonants show as clean vertical energy
  bursts that line up by eye where the waveform is an ambiguous blob.

**Hear the seat.**

- Hard-pan the mix left and the original right on headphones. Off by a hair you hear a
  **flam** leaning to one ear; as you close in it collapses to a single centred hit, and flams
  the *other* way if you overshoot. Where the flam vanishes is good to a few samples.
- Sharper still, a **phase null**: `Effect ▸ Invert` the original and listen to the sum. What
  the two share (bass, break) audibly thins into a null at the seat — a *deepening
  cancellation* is easier to hear than a tightening echo. It won't null to silence (the mix
  has other layers). Un-invert when done.

**See the seat.** At sample zoom Audacity draws individual sample dots. Match the **timing**
of peaks and zero-crossings, **not** their heights — the mix copy is EQ'd and compressed
differently, so amplitudes won't agree. Coarse-align on the phrase → zoom to the transient →
zoom to samples.

**Nudge precisely.**

- **Sync-Lock Tracks** moves the original's audio and its `origNNN` labels as a unit. Turn
  **Snap off** or every edit quantizes. Set the **Selection Toolbar** to **samples**.
- Drag with Time-Shift zoomed to samples — one screen pixel is then a fraction of a sample.
- Audacity has no numeric "nudge by N samples". For an exact amount, edit the **head of the
  original only**: `Generate ▸ Silence` (typed duration) pushes it later; select `0`→`N` at
  the head and delete to pull it earlier. If the mix is in the Sync-Lock group it ripples too
  — drop Sync-Lock for that one edit.

**Capture two anchors, not one.** Seat a cue near the start (`track sync: A` on the mix,
`origNNN sync: A` on the original) and another near the end (`… B`). If the intro seats
perfectly but the outro is a few ms out, that *is* the clock-rate difference — and the A/B
pair is exactly what lets the speed calculation solve it:

```
speed = (trackB − trackA) / (origB − origA)
```

computed downstream in [`sheetscript/Code.js`](./sheetscript/Code.js), instead of you chasing
a moving target by hand.

---

## The harvester, and the bot wall

The harvester (`harvest.py`) streams candidate records, reduces each to a chroma signature, throws
the audio away, and scores the signature against every unsolved mystery. It runs for weeks. You
watch it, and rule on what it finds, at **`/harvest`** in the player.

**YouTube now refuses anonymous downloads**, with:

       ERROR: [youtube] <id>: Sign in to confirm you're not a bot.
       Use --cookies-from-browser or --cookies for the authentication.

Note what that error is **not**: it carries no `403` and no `429`, so it slipped straight past the
host-backoff logic, and the harvester ground through the queue failing identically on every item
while the dashboard reported a cheerful yellow *"waiting on youtube.com"*. It now **halts** on it.
Waiting does not fix this; only a signed-in session does.

**Give it a session.** Set **one** of these in `.env_vars` (gitignored — it never reaches the
public remote), then start the harvester again:

       NETRADIO_YTDLP_COOKIES_FROM_BROWSER=chrome     # brave | chrome | chromium | edge | firefox
                                                      # | opera | safari | vivaldi | whale
       NETRADIO_YTDLP_COOKIES=/path/to/cookies.txt    # a Netscape-format cookies.txt export

The value is passed straight to `yt-dlp` as `--cookies-from-browser <value>` (or `--cookies
<path>`), so **anything yt-dlp accepts works** — including a profile, e.g.
`chrome:Profile 2`. Both are **off by default**: the harvester never touches a browser profile, or
reads a credential, unless explicitly told to.

**Do not use `chrome` on macOS.** yt-dlp reads Chrome's cookie DB off disk and decrypts it with a
key held in the **login Keychain** — so macOS raises

       "security" wants to use your confidential information stored in
       "Chrome Safe Storage" in your keychain.

**once per yt-dlp process, not once ever.** Entering your password authorises *that* process and
nothing more; the next fetch is a new process and asks again. A harvester that restarts — say,
because it is crash-looping — will ask you forever, and no number of correct passwords will stop
it. (This is exactly what happened on 2026-07-13: four password prompts, and the real fault was a
`KeyError` restart loop underneath.) Clicking **Always Allow** would persist the grant, but the
prompt is easy to mistake for a failure, and on a headless box there is nobody to click it.

Prefer, in order:

1. **A `cookies.txt` file** (`NETRADIO_YTDLP_COOKIES`) — a plain file read. No Keychain, no
   prompt, works headless, survives browser updates. See below.
2. **`firefox`** (`NETRADIO_YTDLP_COOKIES_FROM_BROWSER=firefox`) — Firefox keeps cookies in an
   unencrypted `cookies.sqlite`, so it needs no Keychain and never prompts.
3. Chromium-based browsers (`chrome`, `brave`, `edge`, …) — only if you have no other option.

Either way: **the cookie is your logged-in YouTube session — treat it as a credential.** That is
why it lives in `.env_vars` and not in the repo, and why the harvester will not go looking for one
on its own. Close the browser before reading its profile, or the read can fail on a locked DB.

### Exporting a `cookies.txt`

Use a **private/incognito window**, and **close it without logging out** — YouTube rotates the
session when you log out, which invalidates the cookie you just exported.

1. Open a private window and log in to YouTube.
2. Export with a Netscape-format extension — *Get cookies.txt LOCALLY* (Chrome/Brave/Edge) or
   *cookies.txt* (Firefox). Both write the `# Netscape HTTP Cookie File` format yt-dlp wants.
3. Save it **outside the repo** — this is a credential and the repo is public. Lock it down:

       mkdir -p ~/.config/netradio && chmod 700 ~/.config/netradio
       mv ~/Downloads/youtube.com_cookies.txt ~/.config/netradio/youtube-cookies.txt
       chmod 600 ~/.config/netradio/youtube-cookies.txt

4. Point `.env_vars` at it, then restart the harvester from `/harvest`:

       NETRADIO_YTDLP_COOKIES=/Users/<you>/.config/netradio/youtube-cookies.txt

5. Close the private window (do **not** log out).

If the harvester halts again with cookies already configured, the **session expired** — re-export.
`/harvest` says which of the two situations you are in, and so does `scripts/run_player.sh status`
in the player repo.

## Ruling on what the harvester finds (`/harvest`)

The harvester **proposes**; you **dispose**. It never marks a mystery solved on its own — a cost is
a distance, the true-match and non-match populations overlap, and the matcher has already been
caught vouching for itself (see *A tie is not a win*, below). So every candidate ends in a **human
ruling**, and the review loop is the point of the whole thing.

**Where the leads live.** Nothing is kept as audio. `--purge-audio` threw away the retained
excerpts and the harvester no longer hoards them: what survives is the **lead** — the URL, the
cost, which mystery it matched, the key it matched in, and *where in the candidate* it matched.
`/harvest` plays the candidate from an **embed at its source**, which is both a better review (it
is the record, not our copy of it) and the only defensible copyright posture.

**Reading a candidate.** Three numbers, in order of how much they should move you:

| | |
|---|---|
| **Rank + margin** | The one that matters. A true match should beat the field, not tie it. |
| **Cost** | A true match runs **0.004–0.03**; an unrelated record sits near **0.095**. The populations **overlap** — a cost alone is not a verdict. |
| **Key** | The transposition it matched in. All twelve are tried, so a match in an odd key is not suspicious by itself. |

See [`docs/CALIBRATION.md`](./docs/CALIBRATION.md) for where those numbers come from.

**Be suspicious of a tight cluster.** MT7 produced *five* confident false positives all within
0.0007 of each other. When everything scores the same, nothing has been distinguished — that is a
degenerate match, not five near-misses. The usual cause is a **short query**: MT7's clip is 23
seconds, and a short query drives every cost down. Fix the clip (§8b), don't rule on the noise.

**The three rulings.** Each is one click on `/harvest`, and each is recorded against the lead:

- **match** — this is it. Favourites the candidate and marks it heard in the listen queue. Then do
  the real work: fill in the identification (§8), and give the mystery a **name** so it leaves the
  query set. A mystery that is solved but still named `Mystery Track N` keeps getting searched for.
- **near** — related but not the record. A shared break, a sample, a remix of the same tune. Worth
  keeping as a lead; it is often the thread that leads to the right record.
- **no** — not it. Dismissed, and it will not come back.

The rulings are **durable** and shared with the listen queue: the queue page shows a **chroma-sig
badge** on anything the harvester has analysed, and `/harvest` shows the queue's flags on each
candidate. Two views, one queue — so a track you have already heard and discarded is not offered
back to you as a discovery.

**What it is NOT searching for.** `/harvest` names the mysteries with **no clip**, because that is
invisible otherwise: a mystery with no clip is not "searched for and not found", it is *not in the
query set at all*, and the harvester can run for a month without ever looking for it. If a mystery
is listed there, the fix is §8b — one file copy.

**The self-test, in three states.** `/harvest` reports **PASS**, **FAIL**, and **not checked** as
three distinct things, and never lets the third look like the first — *a skip is not a pass*.

- **offline** — re-identifies a track we already know (Jamie Myerson, *Sky Blue*) out of a small
  pool. Proves the matcher still *works*, not merely that the process is alive.
- **live** — the same, end to end, against a real stream fetched from the internet. It refuses to
  establish a canary if the stream it finds is **not the record** (it checks the candidate against
  our own copy first). A canary that cries wolf gets ignored, which is worse than no canary — so
  it retries rather than enshrining a wrong upload. If it never settles, hand it a known-good URL.

## What the engine cannot do

Worth knowing, so you don't wait for help that isn't coming:

- **No overlap, no measurement.** Cross-correlation is the engine's only evidence. On an
  exactly-joined file it cannot propose a sync point or detect a skip *at all* — it will say
  so in a question rather than pretend.
- **It cannot search for an original *blind*.** Given a whole capture and no idea where a
  record sits, `locate_original` is wrong more often than right here (measured: 2 hits in 8,
  and its *most confident* answer was wrong by 25 minutes). Two reasons, neither fixable by
  tuning: the DJ beatmatches, so a fixed-lag correlation drifts out of alignment within a
  minute; and the broadcast repeats material, so the answer isn't even unique. It is
  deliberately **not** offered as a hint. See
  [`Archive/LESSON_locate_original.md`](./Archive/LESSON_locate_original.md).
  **With a prior it works** — see `solo_anchors` above. Bounding the search is the whole
  difference.
- **It cannot tell you where a track starts or ends.** Nor can anyone: in a DJ mix records are
  *blended*, so there is no frame at which one "begins". Start/end is a judgement call and
  stays yours. What the engine can offer instead is the moments where a record plays **alone**
  — which is what an anchor actually needs.
- **It does not decide.** Everything in the notating steps produces labels, clips, scores or
  questions **for you to review**. Only `build_track_metadata.py` writes the authoritative
  JSON.

## Label grammar

Each Audacity label is `start_time ⇥ end_time ⇥ text`. `sort_tsv.py` and `Code.js` parse the
`text` with the same small set of patterns. Timestamps are **local** to the file; adding the
file's `file start sync` offset yields **master time**.

**Label-track scoping (`LABELTRACK`)**

- `LABELTRACK <name>` — the first (earliest) label of an Audacity label track *names* that
       track, so one export can carry several tracks unambiguously. `sort_tsv.py` reads it before
       sorting and scopes every following label to `<name>` until the next `LABELTRACK`; the marker
       itself is **stripped** from the emitted `.tsv`. `<name>` resolves three ways:
       the **primary stem** (`== <stem>`) → labels pass through verbatim; **another capture stem**
       (e.g. `d356-375`) → re-homed onto that file via `file_<name>:` (the secondary mechanism);
       **anything else** (e.g. `070.labels`) → prefix-expanded (`sync: 0` → `orig070 sync: 0`).
       A file that uses `LABELTRACK` is **validated**: every label-track block (the file start, and
       each backwards-timestamp boundary) must carry a marker, or `sort_tsv.py` fails before writing.
       Legacy exports with no `LABELTRACK` markers are sorted unchanged. Use `--stem <name>` to set
       the primary when reading from stdin.
- **The track name is not the qualifier.** A track called `070.labels`, `069-dig.labels` or
       `069.vinyl` holds labels *about* `orig070` / `orig069` — so the qualifier is the **3-digit
       original in the name**, not the name itself. `069-dig` and `069.vinyl` are two tracks about
       the same original and both expand to `orig069`.

**Free text vs. a mistyped keyword**

You should not have to type `note:` in front of every passing thought, and a fat-fingered
keyword should not slip through silently. `sort_tsv.py` tells them apart by **shape**, not by
whether the label happens to parse. In order:

1. **Already names its subject** (`orig070 sync: A`, `file end: …`, `069s0`) → emitted as written.
2. **`<qualifier> ` + label parses** → qualified: `sync: 0` → `orig070 sync: 0`.
3. **Keyword-shaped but parses under no rule** → **ERROR**, and nothing is written. A label is
   keyword-shaped if it leads with an entity (`orig…`, `track…`, `file…`, `mix…`), a verb *and its
   colon* (`sync:`, `start:`, `end:`, `note:`, `ID:`), or the compact `NNNs`/`NNNe` form. So
   `orig070 start` (no colon), `orig069: start` (colon misplaced) and `s71e1` (transposed `071e1`)
   are all caught.
4. **Free text** → auto-noted: `start overlap` → `note: start overlap` (or `orig070 note: …` inside
   a numbered track). No warning; the run prints a **summary** of everything it auto-noted, so you
   can still eyeball the list.

Free text is anything without that shape — `peak`, `drum starts`, `close next sync`,
`vocals: oh-ohh`, `sync2.1-spectro` all sail through untouched.

**Scope check (numbered label tracks).** Inside `LABELTRACK 071`, every label is *about* 071, so a
label whose head names a different original — `orig017 sync: A` — is an **ERROR**. This is the one
class of typo no grammar check can see: `orig017 sync: A` parses perfectly. The check reads the
label's **head** only, so a note's body may mention any original freely
(`orig071 note: 069s0 is the digital sync` is fine). For a genuine cross-reference, write it in the
primary track.

**File-level markers (the connection backbone)**

- `file start: FILE.wav` — *physical* file start / pre-roll marker. **Not** authoritative;
       must never override a sync marker.
- `file start sync: FILE.wav OFFSET [verified OTHERFILE]` — **authoritative master anchor.**
       `OFFSET` is added to every local timestamp to get master time. e.g.
       `file start sync: d336-355.wav 19637.763068 verified d328-342`.
- `file sync: FILE.wav OFFSET verified OTHERFILE` — a sync tying this file to another; often
       authoritative for local offset 0.
- `file end: FILE.wav … COMPLETE` (or `COMPLETED`) — file endpoint; **drives "complete."**
- `verified OTHERFILE` / `verified by OTHERFILE [double-checked]` / `MARK verified by …` /
       `NOT VERIFIED` — **the connection record:** which overlapping capture confirmed this file's
       position. **Drives "verified."**
- `file_OTHER: <label>` — **secondary-file re-homing:** places `<label>` onto a *different*
       wav's track (used where one file's labels also document where a neighbour starts/ends).

**Track identity**

- `startNNN: ID: Artist - Title` — **track start** (`NNN` = track number). Title is split on
       ` - ` into artist / name.
- `ID: Artist - Title` (no `start`) — a track **ID** without starting a new track.

**Sync points (mix ↔ original; drive the speed calc)**

- `track sync: X …` / `trackNNN sync: X …` — mix-side alignment point.
- `origNNN sync: X …` — original-side alignment point.
- `X` is a single token: **`A` and `B` are the paired anchors** used for speed
       `(trackB−trackA)/(origB−origA)`; numeric `0,1,2,…` are rough/secondary syncs.

**Original-track spans**

- `origNNN start: …` / `origNNN end: …` / `origNNN note: …` — where the original starts/ends/notes.
       **The colon is required**; the argument after it is **optional**. `orig070 start: A` anchors
       the start to sync point `A`; a bare **`orig070 start:`** simply says the original begins
       here — the timestamp is the data and there is no sync point to name. What does *not* parse
       is dropping the colon (`orig070 start`) or misplacing it (`orig069: start`) — both are
       keyword-shaped and error out, see *Free text vs. a mistyped keyword* above.
- Shorthand `NNNs…` / `NNNe…` (3 digits + `s`/`e`) — compact orig start/end (e.g. `069s0`,
       `065e10`, `067eB`). Note the digits come **first**: `071e1`, never `s71e1`.

**Generic notes & skips**

- `mix start|end|note: …`, `note[ TAG]: …` — mix markers and free/tagged notes.
- Skips are notes: `note: SKIP back 2.279s (multiple files)`, `file note: SKIP ahead 1.248s` —
       they mark stream discontinuities in the RealAudio capture (see [STREAM_PROVENANCE.md](./STREAM_PROVENANCE.md)).

---

---

## File-naming: who owns what

| File | Who writes it | Committed? | Authority |
|---|---|---|---|
| `<stem>.labels.tsv` | **you**, by hand | **yes** | authoritative; nothing else may overwrite it. Reaches the sheet. |
| `<stem>.auto.labels.tsv` | the engine (`tail-solve --emit`) | yes | regenerable; consumed by solve/build. Reaches the sheet. |
| `<stem>.starter.labels.tsv` | `sort_tsv.py` (auto) / `streamalign starter` | **gitignored** | seed only; excluded from import/solve/build |
| `<stem>.hints.tsv` | `streamalign hints` | **gitignored** | **suggestions + questions**; invisible to solve/build **and the sheet**, yours to accept or delete |

The sheet importer (`sheetscript/Code.js`) reads **only** `*.labels.tsv` (including
`*.auto.labels.tsv`) plus the one hand-kept `remainder.tsv` — mirroring
`groundtruth.is_pipeline_label_file`, so the sheet and the engine agree on what is real. A bare
`<stem>.tsv`, a `*.starter.labels.tsv`, and a `*.hints.tsv` are all excluded.

---

## Addendum — does the record's speed *drift* while it plays?

Short answer: **no. The rate is constant within a track, and different for every track.**

This matters because the whole A/B anchor scheme assumes it. If the record's speed wandered
while it played, two anchors and a straight line between them would be a fiction, and you would
have to chase the rate continuously.

**Measured, not assumed.** The chroma/DTW warp path *is* instantaneous mix-time vs
original-time, so if the rate drifted the path would visibly curve. Fitting a straight line to
it, across seven tracks that have originals on disk:

| track | R² (linear fit) | residual | rate | curvature |
|---|---|---|---|---|
| 7 | 1.00000 | 0.06 s | 0.9961 | 0.0001 |
| 11 | 1.00000 | 0.05 s | 1.0022 | 0.0016 |
| 12 | 1.00000 | 0.08 s | 1.0008 | 0.0009 |
| 16 | 1.00000 | 0.06 s | 1.0113 | 0.0002 |
| 13 | 0.99999 | 0.36 s | 1.0030 | 0.0003 |
| 10 | 0.99994 | 1.13 s | 1.0075 | 0.0150 |
| **6** | 0.99942 | 0.41 s | 1.0123 | **0.1247** |

Six of the seven are dead straight — a residual of 50–360 ms across an entire track, and
essentially zero curvature. That is a **pitch fader set once and left alone**, which is exactly
what beatmatching a record into a mix requires.

But the rates *differ between* tracks (0.9961 … 1.0123). The DJ picks a pitch **per record**.

**Consequences:**

- **Two anchors per track is the right model, not an approximation.** Seat A early and B late,
  let `(trackB − trackA) / (origB − origA)` do the rest. Chasing a per-moment rate would be
  chasing noise.
- **A rate belongs to a track, never to a file.** Do not carry one across a track boundary.
- **Every measured rate sits within ~1.3% of 1.0.** So a pair implying anything far from unity
  has not found the record — which is why the engine gates its own candidates on exactly that
  (see `RATE_PLAUSIBLE`).

**The one caveat:** track 6 shows real curvature (0.12) *and* the weakest fit of the set. That
is either a DJ riding the fader, or — more likely — a poor alignment. One outlier with the
worst R² is not evidence of drift. If it matters, listen to it.
