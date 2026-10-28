#!/usr/bin/env python3
"""Recompute the @@ hunk line counts of a unified diff.

Hand-edited patches routinely end up with a hunk header whose counts do not
match the lines that follow.  `git apply` refuses such a patch unless it is run
with --recount, and dxb does not pass --recount, so a bad header means the patch
is skipped with only a warning in the build log -- which has silently produced a
byte-identical kernel at least once on this port.

Rewrite each `@@ -a,b +c,d @@` with the counts the hunk actually contains.  The
start line numbers are left alone: git searches for the context anyway.

Usage: fix_patch_counts.py [-w] FILE...
       -w   write the file back (default: report only)
"""
import re
import sys

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def process(path, write):
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines(True)

    out = []
    i = 0
    fixed = 0
    problems = 0
    while i < len(lines):
        line = lines[i]
        m = HUNK.match(line.rstrip("\n"))
        if not m:
            out.append(line)
            i += 1
            continue
        a, c = int(m.group(1)), int(m.group(3))
        body = []
        j = i + 1
        while j < len(lines) and not lines[j].startswith(
                ("@@", "diff --git", "--- ", "+++ ")):
            body.append(lines[j])
            j += 1
        # the trailing file markers belong to the next section, not this hunk
        while body and body[-1].startswith(("--- ", "+++ ")):
            body.pop()
        old = sum(1 for b in body if b[:1] in (" ", "-"))
        new = sum(1 for b in body if b[:1] in (" ", "+"))
        want = "@@ -%d,%d +%d,%d @@%s\n" % (a, old, c, new, m.group(5))
        if line != want:
            fixed += 1
            out.append(want)
        else:
            out.append(line)
        for b in body:
            if b[:1] not in (" ", "-", "+", "\\"):
                problems += 1
                print("%s: not a diff line: %r" % (path, b[:60]))
        out.extend(body)
        i = j

    if write and (fixed or problems):
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(out)
    return fixed, problems


def main(argv):
    write = "-w" in argv
    paths = [a for a in argv[1:] if a != "-w"]
    if not paths:
        print(__doc__)
        return 2
    bad = 0
    for p in paths:
        fixed, problems = process(p, write)
        status = "ok" if not fixed and not problems else "fixed %d" % fixed
        print("%-60s %s%s" % (p, status,
                              "" if not problems else " %d bad lines" % problems))
        bad += problems
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
