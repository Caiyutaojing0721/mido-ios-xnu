#!/usr/bin/env python3
"""Turn a huge build log into two small files that survive a private repo.

Run logs of a private repository answer "Must have admin rights", and the
workflow has no token, so a red build used to be undiagnosable from outside.
The build log's lines are enormous (each one is a full clang command line), so
the last N bytes of it are nearly all command, no diagnoses.

Usage: ci_digest_log.py <build.log> [outdir]

Writes:
  errors.txt   lines that look like failures, truncated, last 400 of them
  log.tail.txt the last 3000 lines, truncated to 300 columns each
"""
import os
import re
import sys

ERRORISH = re.compile(
    r"error:"
    r"|error \["
    r"|cannot find"
    r"|undefined symbol"
    r"|instruction requires"
    r"|not supported"
    r"|No such file"
    r"|\*\*\* \["
)


def short(line, width=300):
    return line if len(line) <= width else line[:width] + " ..."


def main():
    if len(sys.argv) < 2:
        print(__doc__.strip().splitlines()[-1])
        return 2
    src = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else "."
    if not os.path.exists(src):
        print("no build log at %s" % src)
        return 1

    with open(src, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()

    errs = [l for l in lines if ERRORISH.search(l)]
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "errors.txt"), "w", encoding="utf-8") as f:
        for line in errs[-400:]:
            f.write(short(line) + "\n")
    with open(os.path.join(out, "log.tail.txt"), "w", encoding="utf-8") as f:
        for line in lines[-3000:]:
            f.write(short(line) + "\n")

    print("%d lines, %d failure-ish lines kept" % (len(lines), len(errs)))
    for line in errs[:20]:
        print("  " + short(line, 160))
    return 0


if __name__ == "__main__":
    sys.exit(main())
