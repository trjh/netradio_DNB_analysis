# G4 pass 2 — sourcing dossiers

For each identified track whose original audio is **missing or a placeholder** (see
`scripts/g4_missing_sources.py` → `sourceable` list, 51 tracks as of 2026-06-14),
this folder collects a per-track **sourcing dossier**: where to buy/stream, the exact
release that carries the wanted version, format/quality, price, links, and a
confidence note. Acquiring is Tim's call — these surface options, they don't buy.

Each dossier carries the leads already in `track-metadata.json` (`fields`: discogs /
spotify / release / year) as the starting point, then adds verified acquisition
sources found by search.

## Status
- **014** — Me'Shell NdegéOcello, "Stay (The Midnight Rockers Remix)" — done
  (`014-meshell-stay-midnight-rockers.md`). Tim's flagship request.
- Remaining 50 identified gaps: pending the pass-2 sweep. Many are DnB white-label /
  promo 12"s that may be vinyl-only and hard to source — each gets a dossier noting
  the best available option (or "no good source found").

Unidentified "Mystery Track" spans (9) are **not** here — they need G3
(identification) first.
