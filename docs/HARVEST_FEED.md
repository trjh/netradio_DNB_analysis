# Feeding the harvester

This document is the whole contract between whatever lands audio files in the harvester's
directories and the harvester that signs them. It is written for a third party with audio to
offer: nothing outside this page is needed to feed it, and nothing about who fills the
directories is assumed. The harvester itself knows only what is written here.

## What the harvester is

The harvester is a signer and a scorer. It loops over one or more directories of audio named
in its `.env` (`NETRADIO_HARVEST_DIRS`, absolute paths, `:`-separated); it signs any audio it
has not already signed, and only audio with a complete sidecar; it marks each file in its
ledger, `signed` or `delayed` with a reason; and it uploads the signature, with the sidecar
beside it, to the signature bucket. It is not responsible for moving audio into or out of the
directories. It never deletes, renames, or writes anything in them.

**The top level of each directory only.** A subdirectory is never read. A listed directory
that does not exist is skipped with a row in the harvester's issues list, and picked up again
on a later pass once it exists — so a directory that arrives late is not a configuration
problem.

A pass signs at most one file, then re-reads its settings. A directory holding nothing
new is swept in about twenty seconds; a new file is signed on the next pass after its sidecar
lands, so a feed of twenty files is signed over twenty passes.

## The key and the file name

An audio file is keyed by its source URL:

```python
"u" + hashlib.sha1(url.encode()).hexdigest()[:20]
```

The first 20 hex characters of the SHA-1 of the URL **as given** — query string included,
`#t=` media fragments included, exactly the string the feed's own record carries. The key of
every object already in the signature bucket is this rule, and it never changes.

* **The file is `<key>.<ext>`.** The harvester takes the stem as the key. It never computes a
  key from anything and never parses a fragment; a `url` in the sidecar is data it carries,
  never something it reads for meaning.
* The key's shape is checked: `u` followed by 20 hex characters. A file whose stem is not a
  key's shape is refused, because a signature filed under any other name is invisible to the
  pool's own listing.
* A name ending in `.part` is skipped, whatever its stem, with no row: it is the usual mark
  of a download still in progress — feeder state, not a feeder bug.
* One key belongs in one directory. When the same key sits in more than one, copies that
  agree in size and modification time are one file. Copies that differ are signed once,
  oldest first, and then the key is held: it is not signed again, whichever copy changes,
  until only one copy remains. An issues row names each copy's path, size and modification
  time.

## The sidecar

Beside every audio file, write `<key>.json`, after the audio's final rename, whole and
atomically (write a temporary file and rename it, or the harvester may read a torn copy and
take the file for unfinished).

| Field | Required | Meaning |
|---|---|---|
| `key` | yes | `u<sha1(url)[:20]>`, equal to the audio file's stem |
| `url` | no | the source URL, fragment included; carried to the ledger and the bucket, never parsed |
| `title` | no | as the source names it |
| `artist` | no | the uploader or channel |
| `duration_s` | no | the file's length in seconds, for the length check |
| `fed_at` | yes | when the sidecar was written; a sidecar without it is refused, with a row in the issues list |

**No sidecar, no signature.** A file without a sidecar is not complete, and the harvester
does not read it, sign it, or record it — the sidecar is how the harvester knows the feed is
finished. Two sidecar errors are refused outright, so a feeder bug is visible rather than
silent — no ledger row, one row in the issues list: one whose `key` differs from the audio
file's stem, and one missing its required `fed_at`. A torn or unreadable sidecar is neither —
it is the mid-write state again, retried on the next pass.

## What the harvester does with a file

1. It decodes the file with ffmpeg (mono float32 at 16 kHz) in a child process — the audio
   never persists; the decoded samples are dropped as soon as the signature and any excerpt
   are cut.
2. It computes the chroma signature — the pool's one recipe, published as `chroma/_recipe.json`.
3. It uploads `<key>.npy` to the signature bucket, verified, and the sidecar beside it as
   `<key>.json`.
4. It writes the ledger row.

Both objects must land before the row is written: a `signed` row is the promise that the
signature and its sidecar are in the bucket together, and the harvester treats such a row as
the file being done with. When the bucket is configured and either upload fails, the file gets
**no row at all** and is signed again on a later pass, so a bucket that is briefly refusing
writes costs one re-decode, never a signature with no sidecar beside it.

A file can come back **delayed** instead of signed, with one of five reasons: three verdicts
on the file as fed, and two automatic reasons the harvester retries on its own:

| Reason | Meaning | What the feed does |
|---|---|---|
| `length_mismatch` | the decoded length differs from the sidecar's `duration_s` by more than `max(10 s, 2 %)` — a hand-over that disagrees with its own label is not signed | a verdict on the file as fed |
| `too_long` | the file's length (the sidecar's claim, or the decode's own measure when the sidecar declares none) is over four hours — nothing is ever truncated | a verdict on the file as fed |
| `decode_failed` | ffmpeg could not decode it, or it is under 45 s (a signature shorter than that is not trusted) | a verdict on the file as fed |
| `no_space` | the cache policy had no room for the signature | automatic: the file is retried on later passes until room is made, even when it is unchanged |
| `missing_sidecar` | the signature is in the bucket but its companion sidecar is not — a legacy object from before the sidecar was mandatory, a half-landed sign the bucket held onto, or a sidecar that has since left the bucket | automatic, like `no_space`: the scan proposes any file under the key, changed or not, for a fresh sign that re-uploads both; a key with no file in the directories is fed like a key with no row |

A sidecar with no `duration_s` makes no length claim, and no claim is never a mismatch; only
the four-hour backstop applies to it.

## The signature bucket

The harvester uploads every signature, with its sidecar beside it, to one S3-compatible
signature bucket. The bucket is what makes a signature durable: the local cache is bounded
and evicts cold entries, and the bucket is the record the ledger is reconciled against. The
upload, the HEAD, and the listing are all through the AWS CLI (`aws`), so the bucket is
**configured, not assumed** — the harvester does not run a signer that promises an upload it
cannot make.

These settings live in `.env` (gitignored, machine-specific), alongside `NETRADIO_HARVEST_DIRS`
and `NETRADIO_CACHE_ROOT`:

| Variable | Required | Meaning |
|---|---|---|
| `NETRADIO_SIG_BUCKET` | yes, for uploads | the signature bucket's name |
| `NETRADIO_AWS_CLI` | no | path to the `aws` executable; unset, the harvester resolves `aws` from `PATH` |
| `NETRADIO_SIG_AWS_PROFILE` | no | an `--profile` to pass the CLI; unset, the CLI's own default profile applies |
| `NETRADIO_SIG_S3_ENDPOINT` | no | an `--endpoint-url` for an S3-compatible provider; unset, the CLI's own default endpoint applies |

AWS credentials are the CLI's own concern (environment, `~/.aws/credentials`, or the profile
above); the harvester reads no credentials itself. The store is **on** only when both a
bucket is named and the AWS CLI resolves to an executable; with the store on, a sign whose
upload of either object fails writes no row, and the file is signed again on a later pass.

**A dark store is a local-only sign, and it is visible.** With the store off — no bucket, or
no `aws` resolvable — the harvester still signs, but the row carries no `uploaded_etag` and
nothing reaches the bucket: the feed's read-back rule for a `signed` row with no etag (below)
is what applies. The harvester does not present a dark store as a bucketed one: the row's
absent etag is the mark, and the feed re-feeds the key.

**A file that changes is signed again.** A file whose size or modification time no longer
matches its row is re-signed, whatever the row said before — so a re-cut part can be offered
again under the same key, and a `no_space` or `missing_sidecar` delay is retried without any
action from the feed.

**A re-offer takes the old sidecar away first.** When you replace a file under a key that
already has a sidecar, remove the old sidecar before the new audio's final rename, and write
the new sidecar after it. In the window between the two, the old sidecar would vouch for
bytes it never described: the harvester reads the pair as a finished feed, and its length
check can pin a wrong `length_mismatch` on the new file — a verdict whose row then covers
those bytes for good. With the old sidecar gone, the file reads as unfinished for exactly
that window, and the first complete pair the harvester can see is the new audio with its own
sidecar.

## The ledger

`.harvest/ledger.json` in the harvester's own checkout: a JSON object, one row per key,
written only by the harvester.

```json
{
  "u1f0e8d2ba9c4d6f8a1b2": {
    "key": "u1f0e8d2ba9c4d6f8a1b2",
    "size": 34210304,
    "mtime": 1789826000.123456,
    "status": "signed",
    "reason": null,
    "signed_at": "2026-09-19T12:00:00+00:00",
    "uploaded_etag": "9b2cf535f27731c974343645a3985328",
    "url": "https://example.invalid/watch?v=abc#t=3600,7200",
    "title": "A set, part 2",
    "artist": "some channel",
    "duration_s": 3600
  }
}
```

| Field | Meaning |
|---|---|
| `key` | the row's own key, the same as the object it is filed under |
| `size`, `mtime` | the file as signed; a changed file is signed again |
| `status` | `signed`, or `delayed` |
| `reason` | for `delayed`: one of the five reasons above; `null` otherwise |
| `signed_at` | when the signature was written; `null` on a delayed row |
| `uploaded_etag` | the signature object's ETag in the bucket; absent when the signature is not there |
| `url`, `title`, `artist`, `duration_s` | carried from the sidecar, unchanged |

**The ledger is seeded at the harvester's first start**: one `signed` row for every key the
signature bucket already holds **with its companion sidecar beside it** — both objects,
verified by the listing — with `size`, `mtime`, `signed_at`, `url`, `title`, `artist`
and `duration_s` all empty (a seeded row has no file to name and no sidecar to carry), so the
ledger is the complete record of the pool from its first day. A signature whose sidecar is
NOT in the bucket is seeded `delayed` with `missing_sidecar` instead, so the feeder re-feeds
the key for a fresh sign that re-uploads both. On every later start the listing is rebuilt
into rows by the same rule, written to `.harvest/ledger.rebuild.json`, and compared with the
ledger: a key differs when the ledger says the bucket holds its signature (a `signed` or a
`delayed missing_sidecar` row) and the rebuild has no row for it, or when the rebuild has a
row and the ledger has none. When the differing keys are more than
`NETRADIO_LEDGER_REBUILD_MAX_DIFF_PCT` percent (default 10) of every key with a row on either
side, the harvester **refuses to start**: it writes the numbers to the state file's `sig_alert`
(`kind: "ledger"`) and exits non-zero without signing. Starting it once with
`scripts/harvest.py --run --accept-ledger-rebuild` merges the rebuild for that start. At or
under the threshold the rebuild's rows for keys the ledger lacks are added, no row the ledger
has is replaced, and the rows are reconciled against the listing: a `signed` row whose object
is gone loses its `uploaded_etag`, a `signed` row missing its etag whose object is present
gains it, a `signed` row whose sidecar has gone from the listing is demoted to `delayed` with
`missing_sidecar`, its `uploaded_etag` and `signed_at` cleared, and a `delayed
missing_sidecar` row whose signature and sidecar are both listed again is promoted back to
`signed`. A loss of signatures or of sidecars past `NETRADIO_RECONCILE_DROP_CAP` (default
0.10 of the signed rows) is left untouched on that side and reported as a `sig_alert` of
kind `store`.

### What a feed reads

The ledger is the one thing the feed reads back:

* **`signed` with an `uploaded_etag`** — the signature and its sidecar are in the bucket; the
  file is done with.
* **`signed` without an `uploaded_etag`** — the signature object is not in the bucket: the
  reconciliation found it gone, or the signer is running with no store configured and
  signed locally. With the store configured the harvester never writes this row for a live
  sign — a sign whose upload failed gets no row and is tried again. The key can be fed
  again.
* **`delayed` with `decode_failed`, `length_mismatch` or `too_long`** — a verdict on the file
  as fed. Feed the key again only as a changed file: a re-cut part has a new size and
  modification time, a file landed again with the time of the feed as its modification time
  counts as changed even when its bytes are the same, and the harvester signs a changed file
  again.
* **`delayed` with `no_space`** — automatic; the file stays, and the harvester retries it on
  later passes. Nothing for the feed to do.
* **`delayed` with `missing_sidecar`** — automatic, like `no_space`: the bucket's entry is
  incomplete, not the audio judged. A file under the key that is still in the directories is
  signed again as it is; a key with no file there is fed like a key with no row, and the fresh
  sign uploads both objects.
* **no row** — not yet signed; the feed's list wants it.

Nothing is renamed, moved, or written beside the audio: the ledger is the mark.

## The bucket layout

The signature `<key>.npy`, exactly as the pool has always held it, and beside it `<key>.json`
— the sidecar as the harvester saw it. The key already carries its `u` prefix.

## The two files the harvester reads back

Two files in the harvester's checkout tell the harvester what not to do.

* **`.harvest/PAUSED`** — the pause flag. While the file exists the harvester signs nothing
  and scores nothing, and it notices within about twenty seconds.
* **`data/rulings.json`** — the retired set, committed in the harvester's repo: `{key: reason}`,
  one entry per key the search must never propose again, for any mystery, present or future.
  An empty `{}` means nothing is ruled out; to rule a key out by hand, add it to the file. The
  harvester re-reads it on every pass and refuses to run without it — a search that has
  forgotten every ruling hands back records already rejected. The reasons are for the human
  reading the file; the search reads the keys alone. A ruled key whose file is fed again is
  still signed, but it is scored only when its signature changed (a different ETag for the
  `.npy` than the row held before).

## The hand tool

```bash
set -a && . ./.env && set +a        # the settings live in .env: the directories, the cache root
.venv/bin/python scripts/harvest.py --sign-one <key>
```

Signs the one file whose stem is this key, in the configured directories, through the same
path the loop uses — including the ledger row. It reconciles the ledger first, and it does
not score. Useful for reproducing one sign by hand, or for forcing a re-sign after a file
has been replaced. Like every other mode, it needs its settings from `.env`
(`NETRADIO_HARVEST_DIRS` at a minimum) and refuses, naming them, without.
