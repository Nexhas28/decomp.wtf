#!/usr/bin/env python3
"""python tools/same.py BUILT ORIGINAL STAMP: fail unless the rebuilt ROM is your copy byte for byte; then write STAMP.
python tools/same.py --touch STAMP: just write STAMP (after a check that passed)."""
import hashlib
import sys

args = sys.argv[1:]
if args[0] != "--touch":
    built, original = (hashlib.sha1(open(p, "rb").read()).hexdigest() for p in args[:2])
    if built != original:
        sys.exit(f"{args[0]} differs from your copy {args[1]} (sha1 {built} vs {original})")
open(args[-1], "w", encoding="utf-8").write("ok\n")
