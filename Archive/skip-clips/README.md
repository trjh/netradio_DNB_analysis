# Archived: the skip-clip review layer (2026-07-15)

`clips.py` rendered a "skip-check" audio clip per detected skip: 15 s of the two overlapping
recordings **averaged** on each side of the skip, for review in the player's `/clips` page.
The averaging was the flaw — two captures mixed on top of each other never gave an audible
coherent-vs-doubling signal (Tim: "i can't hear skips without the fuller context").

**Retired, not deleted.** Skip *detection* (`streamalign/skips.py`) and the decision store
(`skip_review.py`: rejections, confirm/reject, `apply_decisions`) stay live — the tail solve
depends on them. Skips now surface via `streamalign hints` as `note QUESTION: … [id …]` rows in
`<stem>.hints.tsv`, auditioned in Audacity against the real capture and ruled on with
`skip-confirm` / `skip-reject <id>`.

Also removed with this: the `skip-clips` subcommand, the player `/clips` route + `clips.html`,
and `skip_review.generate_clips` / `_prune_manifest` / `clips._offset_at` (the last moved to
`skips.offset_at`).
