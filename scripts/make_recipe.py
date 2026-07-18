"""Emit `chroma/_recipe.json` — the recipe contract workers assert against.

This is one of the two bucket-bootstrap scripts (with `make_canary.py`): together they recreate
everything in the bucket that is NOT a harvested signature, so a wiped bucket is a rebuild, not a
loss. See the player repo's docs/PLAN_edge_chroma_fanout.md "Disaster recovery".

The recipe is `chroma_recipe.recipe_dict()` — the single source of truth for how a signature is
computed. Run:

    PYTHONPATH=scripts python3 scripts/make_recipe.py > _recipe.json
    # then upload (private bucket/endpoint — see the player runbook):
    aws --endpoint-url <ep> s3 cp _recipe.json s3://<bucket>/chroma/_recipe.json
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chroma_recipe                                 # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out", help="write here instead of stdout")
    ap.add_argument("--no-toolchain", action="store_true",
                    help="omit detected library versions (for a machine-independent contract)")
    args = ap.parse_args()
    text = json.dumps(chroma_recipe.recipe_dict(with_toolchain=not args.no_toolchain), indent=1)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print("wrote %s" % args.out, file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
