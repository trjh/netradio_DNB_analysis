# labels/automated/ — machine-emitted label files

Script output only, committed but **freely overwritten on re-run** — nothing manual
ever lives here. Today: the `streamalign match-hints` pair files
(`<stem>.origNNN.match.hints.tsv` + `origNNN.<stem>.match.hints.tsv`) and the
`streamalign hints` per-capture file (`<stem>.hints.tsv`); future scripts'
suggestion/scratch emissions land here too. (The solve's `<stem>.auto.labels.tsv`
stays at the labels root on purpose — it reaches the sheet, and this directory
never does.)

The hand-authoritative `<stem>.labels.tsv` files stay in `labels/` proper and are
reached **only** through the human fold in Audacity — no automation writes them.
See `PROCESS.md` (the artifact table and step 9) for the full layout.
