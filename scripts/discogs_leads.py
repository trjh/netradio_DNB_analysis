#!/usr/bin/env python3
"""Find candidate records for the Mystery Tracks: same labels, same era, same styles.

    PYTHONPATH=scripts .env/bin/python scripts/discogs_leads.py --labels
    PYTHONPATH=scripts .env/bin/python scripts/discogs_leads.py --leads --out /tmp/leads.txt

The pool is the bottleneck, not the matching. Random 1990s D&B is a weak pool; the records this
particular DJ actually reached for are a strong one. We already know a lot of them -- 60-odd
tracks are identified, and every one names a label and a year.

So: read the labels off what we KNOW he played, then ask Discogs what else those labels put out
in 1995-1998 in the same styles. That is a targeted candidate list, and it costs nothing to make.

No browser, no scraping, no login. The Discogs WEBSITE 403s a fetch (which is why the marketplace
was unreachable), but api.discogs.com answers unauthenticated -- release lookup, search, the lot.
Reaching for a headless Chrome before checking whether there was an API would have been a lot of
work to arrive somewhere worse.

Rate-limited to Discogs' unauthenticated allowance (~25/min); we sit well under it.
"""

import argparse
import json
import re
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from streamalign import groundtruth as _gt        # noqa: E402

API = "https://api.discogs.com"
UA = "NetradioDNB/1.0 +https://github.com/trjh/netradio_DNB_analysis"
GAP_S = 3.0                     # well inside the unauthenticated allowance
YEARS = (1994, 1999)            # the broadcast is 1998; a record on air is 1998 or earlier
STYLES = {"drum n bass", "jungle", "drum and bass", "breakbeat", "downtempo"}

# Discogs' `labels` field is not just record labels: it also lists pressing plants, mastering
# houses, distributors and publishers. Mining "MPO" (a French pressing plant) for D&B leads finds
# every record it ever stamped, which is nothing to do with this DJ's taste. Only the labels that
# actually SIGNED music tell us anything.
NOT_A_LABEL = {
    "mpo", "the exchange", "copyright control", "vinyl distribution", "mastervoice",
    "sony music entertainment inc.", "universal studios, inc.", "mca music publishing",
    "the lab, glassboro, nj", "p.r. records limited", "warner chappell music",
    "emi music publishing", "bmg", "sony/atv", "universal music publishing group",
    "record industry", "gz media", "damont", "sonopress", "pallas", "optimal media",
}


def is_label(name):
    n = (name or "").strip().lower()
    if n in NOT_A_LABEL or not n:
        return False
    return not any(w in n for w in ("publishing", "pressing", "distribut", "copyright",
                                    "mastering", "plant", "studios, inc"))


def get(path, **params):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        return {"_error": str(exc)[:120]}


def resolve_by_name(artist, title):
    """Find a track's Discogs release by artist+title. Most of our metadata carries a MusicBrainz
    link, not a Discogs one, so the release ids alone would give us three labels out of sixty."""
    res = get("/database/search", q="%s %s" % (artist, title), type="release", per_page=5)
    time.sleep(GAP_S)
    for r in (res.get("results") or []):
        y = r.get("year")
        try:
            y = int(y)
        except (TypeError, ValueError):
            y = None
        if y and not (1990 <= y <= 2001):
            continue
        return r.get("id"), (r.get("label") or [])
    return None, []


_DISCOGS = re.compile(r"discogs\.com/(release|master)/(\d+)")


def known_releases():
    """Every Discogs release/master referenced anywhere in track-metadata.json.

    It is NOT just the tracks: 31 of the 41 Discogs links in that file live under `albums`, and
    most are /master/ URLs rather than /release/. Reading only `tracks`, and only `/release/`,
    found FIVE of them -- which is how the first pass reported "3 identified tracks carry a
    Discogs id" and quietly under-sampled this DJ's taste by an order of magnitude. Walk the whole
    document and take both kinds.
    """
    meta = json.load(open(os.path.join(_gt.REPO_ROOT, "track-metadata.json"), encoding="utf-8"))
    out, seen = [], set()
    for kind, rid in _DISCOGS.findall(json.dumps(meta)):
        key = (kind, int(rid))
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def labels_of(releases):
    """{label: count} across the releases we know he played -- his taste, as data."""
    labels = Counter()
    for kind, rid in releases:
        d = get("/%ss/%d" % (kind, rid))     # /releases/N or /masters/N
        time.sleep(GAP_S)
        if kind == "master" and not d.get("_error"):
            main = d.get("main_release")
            if main:
                d = get("/releases/%d" % main)
                time.sleep(GAP_S)
        if d.get("_error"):
            continue
        for lab in d.get("labels") or []:
            name = lab.get("name")
            if name:
                labels[name] += 1
    return labels


def labels_by_name(limit=None):
    """Labels for every identified track, resolved by artist+title. Slow but complete."""
    meta = json.load(open(os.path.join(_gt.REPO_ROOT, "track-metadata.json"), encoding="utf-8"))
    tracks = meta.get("tracks", meta)
    labels = Counter()
    done = 0
    for num, e in sorted(tracks.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 1e9):
        if not str(num).isdigit():
            continue
        a, t = e.get("artist"), e.get("title")
        if not a or not t or "mystery" in (t or "").lower() or a == "Net Radio":
            continue
        _rid, labs = resolve_by_name(a, t)
        for name in labs:
            labels[name] += 1
        done += 1
        if done % 10 == 0:
            print("  ... %d tracks resolved" % done, file=sys.stderr)
        if limit and done >= limit:
            break
    return labels


def leads_for(label, seen_ids):
    """Other releases on `label` in our window and styles -- the candidate list."""
    out = []
    res = get("/database/search", q=label, type="release",
              year="", format="Vinyl", per_page=100)
    time.sleep(GAP_S)
    for r in (res.get("results") or []):
        rid = r.get("id")
        year = r.get("year")
        if not rid or rid in seen_ids:
            continue
        try:
            y = int(year)
        except (TypeError, ValueError):
            continue
        if not (YEARS[0] <= y <= YEARS[1]):
            continue
        styles = {s.lower() for s in (r.get("style") or [])}
        if styles and not (styles & STYLES):
            continue
        if label.lower() not in " ".join(r.get("label") or []).lower():
            continue
        out.append({"id": rid, "title": r.get("title"), "year": y,
                    "label": label, "styles": sorted(styles)})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--labels", action="store_true", help="just show his labels, by frequency")
    ap.add_argument("--leads", action="store_true", help="find other releases on those labels")
    ap.add_argument("--top", type=int, default=8, help="how many labels to mine")
    ap.add_argument("--out", default="-")
    args = ap.parse_args()

    rel = known_releases()
    print("# %d identified track(s) carry a Discogs release id; resolving the rest by name"
          % len(rel), file=sys.stderr)
    labels = labels_of(rel) + labels_by_name()
    seen = {rid for _k, rid in rel}

    labels = Counter({k: v for k, v in labels.items() if is_label(k)})
    print("\n# The labels this DJ actually reached for (by count):")
    for name, n in labels.most_common(20):
        print("  %2d  %s" % (n, name))

    if args.labels or not args.leads:
        return

    out = sys.stdout if args.out == "-" else open(args.out, "w", buffering=1)
    print("""# DISCOGS LEADS — records worth looking for

> **What this is:** a shortlist of records the Mystery Tracks *might* be, generated by
> `scripts/discogs_leads.py`. Auto-generated — do not edit by hand; re-run it instead.
> **Read first:** [FINDING_MYSTERY_TRACKS](../FINDING_MYSTERY_TRACKS.md).

## Why this is needed

The matcher can only recognise a record it has been given a copy of. **The pool is the
bottleneck — not the matching.** So the useful question is not "how do we match better" but
"*which records are worth obtaining at all*".

Random 1990s drum & bass is a weak pool. The records **this particular DJ actually reached for**
are a strong one — and we already know about seventy of them, because they are the tracks we have
identified.

## How it was generated

1. Read every Discogs release referenced in `track-metadata.json` — the records we *know* he
   played. (Where a track has no Discogs link, resolve it by artist + title.)
2. Ask the **Discogs API** which **labels** those releases came out on. That is his taste,
   expressed as data.
3. Filter out the noise: Discogs' `labels` field also lists pressing plants, mastering houses,
   distributors and publishers. Mining a pressing plant finds every record it ever stamped, which
   says nothing about anyone's taste. Only labels that actually *signed* music are kept.
4. Ask the API what **else** those labels released in **%d–%d** in D&B/jungle styles.

The result is below: a list of *candidate records*, not a list of answers. It is a **hypothesis
about his taste** — he may well have played white labels and dubplates that are on no label at
all, and those are exactly the sort of record that ends up unidentified.

**Nothing here is audio.** This is a bibliography — catalogue metadata, telling you which
records would be worth having.

""" % YEARS, file=out)
    total = 0
    for name, _n in labels.most_common(args.top):
        found = leads_for(name, seen)
        if not found:
            continue
        print("## %s (%d)" % (name, len(found)), file=out)
        for f in sorted(found, key=lambda x: x["year"]):
            print("  %d  %s" % (f["year"], f["title"]), file=out)
            total += 1
        print("", file=out)
    print("# %d candidate release(s). These are what to look for audio of --\n"
          "# the matcher can only find what is in the pool." % total, file=out)


if __name__ == "__main__":
    main()
