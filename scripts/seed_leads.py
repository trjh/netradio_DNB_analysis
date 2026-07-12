#!/usr/bin/env python3
"""Test the Discogs leads cheaply: queue each for SIGNATURE extraction, not acquisition.

    PYTHONPATH=scripts .venv/bin/python scripts/seed_leads.py --leads docs/DISCOGS_LEADS.md
    PYTHONPATH=scripts .venv/bin/python scripts/seed_leads.py --dry-run

This is NOT an acquisition path. `DISCOGS_LEADS.md` is a want-list of records worth having; this
queues a stream of each so the harvester can compute its chroma **signature** and test the
hypothesis "is this the mystery?" without obtaining the record. The harvester keeps signatures,
not a library (and only a brief excerpt of anything that scores, for aural check). A lead that
scores is a lead to **buy the record** -- via the private player, or Discogs, or Bandcamp -- not
to promote a stream rip into a source file.

`discogs_leads.py` says WHICH records to look for -- the 138 releases on the labels this DJ
actually played. This finds audio for them and puts it in the harvester's queue. The harvester
then streams each, reduces it to a chroma signature, drops the audio, and scores it against the
unsolved Mysteries.

A Discogs release is not a track. "Photek - One Nation / Say It" is a 12" with two sides, and the
A-side title is what a YouTube upload will be called. So we search per SIDE, splitting on " / ",
and we take a couple of results each -- one of them is usually the record and the others are
mixes it appears in, which are themselves fine candidates (the DJ may well have played the record
FROM such a mix).

Search only. No audio is downloaded here: `--flat-playlist` returns metadata, and the harvester
does the streaming later, on its own polite schedule. Seeding must not become its own crawl.
"""

import argparse
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import harvest as _h                                # noqa: E402

_WORD = re.compile(r"[a-z0-9]+")


def _words(t):
    stop = {"the", "a", "an", "original", "mix", "remix", "vip", "edit", "feat", "ft", "and"}
    return {w for w in _WORD.findall((t or "").lower()) if w not in stop}


def already_have():
    """[(artist_words, title_words)] for the originals on disk. A record we OWN is not a lead.

    Tim's point: the harvester exists to find records we do NOT have. Streaming one we already
    hold, to compute a signature we could compute locally in ten seconds, is a wasted fetch
    against someone else's servers -- the kind of thing that gets a crawler blocked, deservedly.
    """
    src = os.environ.get("NETRADIO_SOURCES_DIR") or ""
    out = []
    if os.path.isdir(src):
        for f in os.listdir(src):
            stem = re.sub(r"^\d{3}-", "", os.path.splitext(f)[0])
            art, _, ttl = stem.partition(" - ")
            a, t = _words(art), _words(ttl)
            if a and t:
                out.append((a, t))
    return out


def owned(lead, have):
    """True only if we hold THIS RECORD -- artist AND title must both match.

    The first version matched on word overlap alone, and so fired on the ARTIST: we hold
    "Hidden Agenda - The Flute Tune", so every Hidden Agenda lead was skipped as "already owned",
    including records we do not have. It threw away 135 of 138 leads.

    That is precisely backwards. An artist this DJ already played is MORE likely to be behind a
    Mystery Track, not less -- the whole point of mining his labels is that he kept going back to
    the same people. The filter was discarding its best leads.
    """
    art, _, ttl = lead.partition(" - ")
    a, t = _words(art), _words(ttl.split(" / ")[0])
    if not a or not t:
        return False
    for f_a, f_t in have:
        if not (a & f_a):                       # different artist -> different record
            continue
        if len(t & f_t) >= max(1, int(0.7 * len(t))):
            return True                         # same artist AND the same title
    return False

GAP_S = 2.0                # between searches; the harvester's own pacing handles the fetching
PER_SIDE = 2               # results to take per side of the record

_YEAR = re.compile(r"^\s*(19\d\d)\s+(.+?)\s*$")
_NOISE = re.compile(r"\s*\((?:[^)]*(?:mix|remix|vip|edit)[^)]*)\)\s*", re.I)


def parse_leads(path):
    """[(year, artist_title)] from DISCOGS_LEADS.md. Labels are headings; leads are `YYYY  title`."""
    out = []
    for line in open(path, encoding="utf-8"):
        m = _YEAR.match(line)
        if m:
            out.append((int(m.group(1)), m.group(2).strip()))
    return out


def sides(title):
    """A 12" is two sides and the Discogs title carries both. Search each -- an upload is named
    after ONE of them, not the pressing."""
    parts = [s.strip() for s in title.split(" / ") if s.strip()]
    return parts[:3] if parts else [title]


def search(query, n=PER_SIDE):
    """YouTube URLs for a query. Metadata only -- no audio. The harvester fetches, later."""
    cmd = ["yt-dlp", "-q", "--no-warnings", "--flat-playlist", "--print", "%(url)s",
           "ytsearch%d:%s" % (n, query)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [u.strip() for u in out.stdout.split("\n") if u.strip().startswith("http")]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--leads", default=os.path.join(_h.HOME, "docs", "DISCOGS_LEADS.md"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    leads = parse_leads(args.leads)
    have = already_have()
    before = len(leads)
    leads = [(y, t) for y, t in leads if not owned(t, have)]
    if args.limit:
        leads = leads[:args.limit]
    print("# %d Discogs lead(s); %d skipped -- we already hold the original"
          % (len(leads), before - len(leads)))

    found = queued = 0
    for i, (year, title) in enumerate(leads, 1):
        urls = []
        for side in sides(title):
            q = "%s %d drum and bass" % (_NOISE.sub(" ", side).strip(), year)
            urls.extend(search(q))
            time.sleep(GAP_S)
        urls = list(dict.fromkeys(urls))
        found += len(urls)
        if args.dry_run:
            print("  %3d/%d  %-58s %d url(s)" % (i, len(leads), title[:58], len(urls)))
            continue
        n = _h.add_to_queue(urls, "discogs-lead")
        queued += n
        print("  %3d/%d  %-58s +%d queued" % (i, len(leads), title[:58], n))

    print("\n# %d URL(s) found, %d new in the harvester queue" % (found, queued))
    if not args.dry_run:
        q = _h._load(_h.QUEUE, {"pending": [], "done": []})
        print("# queue: %d pending" % len(q["pending"]))
    print("# No audio was downloaded here. The harvester streams each one on its own schedule,\n"
          "# keeps the 12x N signature and throws the audio away.")


if __name__ == "__main__":
    main()
