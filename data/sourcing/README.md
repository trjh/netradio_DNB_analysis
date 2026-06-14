# G4 pass 2 — sourcing dossiers

For each identified track whose original audio is **missing or a placeholder** (see
`scripts/g4_missing_sources.py` → `sourceable` list, 51 tracks as of 2026-06-14),
this folder collects a per-track **sourcing dossier**: where to buy/stream, the exact
release that carries the wanted version, format/quality, price, links, and a
confidence note. Acquiring is Tim's call — these surface options, they don't buy.

Each dossier carries the leads already in `track-metadata.json` (`fields`: discogs /
spotify / release / year) as the starting point, then adds verified acquisition
sources found by search.

## Status — pass-2 sweep complete (2026-06-14)

All 51 identified gaps now have a dossier (`014` done by hand; the other 50 via a
fan-out of one research agent per track). Breakdown of the 50-track sweep:

- **31 buy-digital** (lossless/download available — the easy ones),
- **11 buy-physical → rip** (vinyl/CD only, no legit digital),
- **5 streaming-only**, **3 not-applicable** (the "Net Radio — Promo…" station jingles).

No track came back with zero leads. Each `NNN-*.md` has the best release, where to
get it, format/quality, alternates, confidence, and a recommendation. Confidence is
the agent's, from web search — **verify the exact version before buying** (several
tracks have remix/version traps, e.g. track 035 Decoder where only a *different* 2013
remix is digital). Acquiring is Tim's call.

Unidentified "Mystery Track" spans (9) are **not** here — they need G3
(identification) first, which is itself blocked on tail placement.

## Dossier index (pass-2 sweep, 2026-06-14)

| trk | artist — title | status | conf |
|---|---|---|---|
| 005 | Jamie Myerson — You're My Life (incomplete) | streaming | high |
| 031 | Net Radio — Promo4 Peace | n/a (promo) | high |
| 032 | Skycutter & Kiki Mojo — Crystal Blue | buy physical→rip | high |
| 033 | Squarepusher — Problem Child | buy digital | high |
| 034 | The Advocate — You Talking To Me? | buy physical→rip | high |
| 035 | Decoder — Circuit Breaker | buy physical→rip | high |
| 036 | Slak & Snuggles — The Reckoning | buy physical→rip | high |
| 037 | Fierce & Nico — Input | buy physical→rip | high |
| 041 | The Sonar Circle — Strength | buy digital | high |
| 042 | Justin Tewn & Hunter A.D. — Science of Industry | buy physical→rip | high |
| 043 | Dave Wallace — Waves (Kid Loops remix) | buy digital | high |
| 044 | E-Z Rollers — Retro | buy digital | high |
| 045 | Dr S Gachet — It's All Gone Sideways | buy physical→rip | high |
| 046 | Arcon 2 — The Beckoning | buy digital | high |
| 047 | Alpha Omega — Realism | buy physical→rip | high |
| 048 | Downpour — Her Spectre Above Me Look | buy digital | high |
| 049 | G-Money — Falling | buy physical→rip | high |
| 050 | DJ 3D — Cairo | buy physical→rip | medium |
| 051 | E-Sassin — Nightrider | buy digital | high |
| 052 | B-Boy 3000 — Diet for Murder | buy digital | high |
| 053 | Net Radio — Promo6 Peace | n/a (promo) | high |
| 054 | Codename John — Dreams of Heaven | buy digital | high |
| 055 | John B — Secrets | streaming | high |
| 056 | Optical — Grey Odyssey | streaming | high |
| 057 | Matrix — Mute | streaming | medium |
| 058 | Codename John — Deep Inside | buy digital | high |
| 059 | Dillinja — Silver Blade | buy digital | high |
| 060 | Ed Rush & Fierce — Locust | buy digital | high |
| 061 | Codename John feat. Grooverider — Warned | buy digital | high |
| 062 | Boymerang — Still | buy digital | high |
| 063 | Lemon D — City Lights | streaming | high |
| 064 | Net Radio — Promo7 Club Groove | n/a (promo) | high |
| 065 | Dr. Know — Make Me Feel | buy digital | high |
| 066 | JMJ & Richie — Free La Funk (PFM Remix) | buy digital | high |
| 069 | PFM — Hypnotising | buy digital | high |
| 070 | Dead Calm — Urban Style (Original Mix) | buy digital | high |
| 071 | On Line (Original Mix) — Fokus | buy physical→rip | high |
| 072 | J Majik — Your Sound | buy digital | high |
| 073 | Hidden Agenda — On The Roof | buy digital | high |
| 075 | Goldie — Sea of Tears | buy digital | high |
| 077 | Dillinja — The Angels Fell | buy digital | high |
| 078 | Dillinja — Ja Know Ya Big | buy digital | high |
| 079 | Hidden Agenda Is It Love? — The Flute Tune | buy digital | high |
| 080 | Wax Doctor — The Spectrum | buy digital | high |
| 081 | JMJ & Richie — Universal Horn (J. Majik Remix) | buy digital | high |
| 082 | Kid Loops — Alien Resident | buy digital | high |
| 083 | Luger — Pass Agent | buy digital | high |
| 087 | System 7 — Rite of Spring (Doc Scott Remix) | buy digital | high |
| 088 | Makai (vs. Nico) — Omen (Director's Cut) | buy digital | high |
| 090 | (unknown) — Home (Jedi Knights Remix (Drowning In Time)) | buy digital | high |