# G3 — AcoustID application registration (draft)

Draft of the AcoustID **application** to register so G3 (identifying the "Mystery
Track" segments by acoustic fingerprint) can run. Tim registers it; the resulting
**application API key** replaces `ACOUSTID_API_KEY` in the analysis repo's gitignored
`.env`.

## Why we need this (and why the current key fails)

AcoustID has two key types:

- **Application API key** — identifies the app; **required for `lookup`** (passed as
  `client=`). This is what we need.
- **User API key** — per-user, only for **submitting** new fingerprints (`user=`).
  Should not be embedded in app code.

The key currently in `.env` (the old user key) returns `{"error": {"code": 4, "message":
"invalid API key"}}` on lookup — it's a user/account key, not an application key, so
it can't be used as `client=`. Registering an application gives the correct key.

- **Cost:** free for non-commercial use (this project qualifies — personal archival).
- **Terms to respect:** ≤ 3 lookups/second (our usage is a handful of one-off
  segments, far below this); non-commercial only.

## How to register (≈ 2 minutes)

1. Sign in at <https://acoustid.org/login> (MusicBrainz / Google / OpenID).
2. Go to <https://acoustid.org/new-application>.
3. Fill the form with the values below.
4. Copy the generated **API key** into `.env` as `ACOUSTID_API_KEY=...`
   (replacing the old user key). Then G3 lookups work — the pipeline is already built.

## Draft application form values

| field | value |
|---|---|
| **Name** | `netradio DNB mix — tracklist identifier` |
| **Version** | `1.0` |
| **Website** | `https://github.com/trjh/netradio_DNB_analysis` |

### Description / purpose (if a description field or note is requested)

> Non-commercial personal archival project. Reconstructing the tracklist of a single
> ~9-hour late-1990s drum & bass radio mix (captured from "Net Radio") that the owner
> recorded years ago. A handful of segments in the mix are unidentified ("Mystery
> Tracks"); this tool extracts a clean clip of each unknown segment and performs an
> AcoustID **lookup only** (no submissions) to suggest the recording/artist. Volume is
> tiny — on the order of 9 lookups total, one per unidentified segment — well within
> the 3 requests/second limit. The code is open source at the GitHub URL above.

## After the key is in place

The lookup pipeline is proven end-to-end (clip extraction → `fpcalc -json` →
`POST https://api.acoustid.org/v2/lookup`); only a valid application key is missing.
Note the separate blocker: 8 of the 9 Mystery Tracks sit in the **unplaced tail**
(see `NEXT_STEPS_FOR_TIM.md`), so until the tail is placed only track 67 is
fingerprintable. The key unblocks the fingerprint step regardless; full G3 coverage
also needs the tail-contiguity decision.
