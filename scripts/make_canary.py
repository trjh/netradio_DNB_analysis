"""Print the canary's key: the pool's key for a URL.

The harvester's self-test re-signs one known track's file whenever the feeder puts it back and
compares the result with the signature stored under its key (`NETRADIO_CANARY_KEY`). `--key <url>`
prints that key for a URL — the pool's one rule (`u` + sha1(url)[:20]), the same stem every
signature in the bucket is filed under:

    python scripts/make_canary.py --key '<the canary URL>'

(Single quotes: inside double quotes the shell would run any $(...) or backticks in the URL.)
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def sig_key_for(url):
    """The signature file name a URL is filed under: the pool's one key rule
    (docs/HARVEST_FEED.md) -- `u` + the first 20 hex of the SHA-1 of the URL as given,
    fragment included. The stem of every signature in the bucket is this rule, and it
    never changes. The harvester's `_sig_key` is this side's one copy of it, so this calls
    that rather than keeping a second."""
    import harvest
    return harvest._sig_key(url)[:-len(".npy")]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key", metavar="URL", required=True,
                    help="print the pool's key for URL (u + sha1(url)[:20]) and exit; the same "
                         "rule every signature in the bucket is filed under. Set "
                         "NETRADIO_CANARY_KEY in .env to the result to name the canary the "
                         "harvester re-signs and compares.")
    args = ap.parse_args()
    print(sig_key_for(args.key))


if __name__ == "__main__":
    main()
