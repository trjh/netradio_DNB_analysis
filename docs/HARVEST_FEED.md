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
| `fed_at` | yes | when the sidecar was written |

**No sidecar, no signature.** A file without a sidecar is not complete, and the harvester
does not read it, sign it, or record it — the sidecar is how the harvester knows the feed is
finished. A sidecar whose `key` differs from the audio file's stem is refused: the file is not
signed, no row is written for it, and the refusal is recorded in the harvester's issues list,
so a naming bug is visible rather than silent.

## What the harvester does with a file

1. It decodes the file with ffmpeg (mono float32 at 16 kHz) in a child process — the audio
   never persists; the decoded samples are dropped as soon as the signature and any excerpt
   are cut.
2. It computes the chroma signature — the pool's one recipe, published as `chroma/_recipe.json`.
3. It uploads `<key>.npy` to the signature bucket, verified, and the sidecar beside it as
   `<key>.json`.
4. It writes the ledger row.

A file can come back **delayed** instead of signed, with one of four reasons:

| Reason | Meaning | What the feed does |
|---|---|---|
| `length_mismatch` | the decoded length differs from the sidecar's `duration_s` by more than `max(10 s, 2 %)` — a hand-over that disagrees with its own label is not signed | a verdict on the file as fed |
| `too_long` | the file's length (the sidecar's claim, or the decode's own measure when the sidecar declares none) is over four hours — nothing is ever truncated | a verdict on the file as fed |
| `decode_failed` | ffmpeg could not decode it, or it is under 45 s (a signature shorter than that is not trusted) | a verdict on the file as fed |
| `no_space` | the cache policy had no room for the signature | transient: the file is retried on later passes until room is made |

A sidecar with no `duration_s` makes no length claim, and no claim is never a mismatch; only
the four-hour backstop applies to it.

**A file that changes is signed again.** A file whose size or modification time no longer
matches its row is re-signed, whatever the row said before — so a re-cut part can be offered
again under the same key, and a `no_space` delay is retried without any action from the feed.

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
| `reason` | for `delayed`: one of the four reasons above; `null` otherwise |
| `signed_at` | when the signature was written; `null` on a delayed row |
| `uploaded_etag` | the signature object's ETag in the bucket; absent when the signature is not there |
| `url`, `title`, `artist`, `duration_s` | carried from the sidecar, unchanged |

**The ledger is seeded at the harvester's first start**: one `signed` row for every key the
signature bucket already holds, with `size`, `mtime` and `signed_at` empty — so the ledger is
the complete record of the pool from its first day. On every later start the rows are
reconciled against the bucket's listing: a `signed` row whose object is gone loses its
`uploaded_etag`, and a `signed` row missing its etag whose object is present gains it.

### What a feed reads

The ledger is the one thing the feed reads back:

* **`signed` with an `uploaded_etag`** — the signature is in the bucket; the file is done with.
* **`signed` without an `uploaded_etag`** — the signature is not in the bucket (the upload
  failed, or the reconciliation found the object gone); the key can be fed again.
* **`delayed`, any reason but `no_space`** — a verdict on the file as fed. Feed the key again
  only when the file would differ: a re-cut part has a new size and modification time, and the
  harvester signs a changed file again.
* **`delayed` with `no_space`** — transient; the file stays, and the harvester retries it on
  later passes. Nothing for the feed to do.
* **no row** — not yet signed; the feed's list wants it.

Nothing is renamed, moved, or written beside the audio: the ledger is the mark.

## The bucket layout

The signature `<key>.npy`, exactly as the pool has always held it, and beside it `<key>.json`
— the sidecar as the harvester saw it. The key already carries its `u` prefix.

## The two files the harvester reads back

Two files another process writes tell the harvester what not to do. Both live in `.harvest/`
in the harvester's own checkout.

* **`.harvest/PAUSED`** — the pause flag. While the file exists the harvester signs nothing
  and scores nothing, and it notices within about twenty seconds.
* **`.harvest/rulings.json`** — the retired set: `{key: reason}`, one entry per key the search
  must never propose again, for any mystery, present or future. The harvester re-reads it on
  every pass and refuses to run without it — a search that has forgotten every ruling hands
  back records already rejected. The reasons are for the human reading the file; the search
  reads the keys alone.

## The hand tool

```bash
.venv/bin/python scripts/harvest.py --sign-one <key>
```

Signs the one file whose stem is this key, in the configured directories, through the same
path the loop uses — including the ledger row. It reconciles the ledger first, and it does not
score. Useful for reproducing one sign by hand, or for forcing a re-sign after a file has been
replaced.
