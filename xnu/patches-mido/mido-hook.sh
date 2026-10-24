#!/bin/sh
# mido-hook.sh — wire the mido (Redmi Note 4X) driver sources into an
# ios-sweet XNU checkout. Run this INSIDE your xnu tree (the one darwin-xnu-
# build checks out), after the shared sweet/polaris patches are applied.
#
# What it does:
#   1. copies xnu/patches-mido/src/mido_*.c -> bsd/dev/
#   2. appends the three files to bsd/conf/files (guarded: no double insert)
#   3. adds the extern declarations + init calls into bsd/dev/sweet_ufs.c
#
# It is deliberately conservative: it refuses to insert twice and tells you
# exactly what it changed. Re-run after a `git checkout` of those files.
#
# Usage (from the repo root that contains xnu/patches-mido/):
#   sh xnu/patches-mido/mido-hook.sh /path/to/xnu-tree
set -eu

HOOKDIR=$(cd "$(dirname "$0")" && pwd)
SRC="$HOOKDIR/src"
XNU="${1:-.}"

[ -d "$XNU/bsd/dev" ] || { echo "error: $XNU/bsd/dev not found; pass the xnu tree path"; exit 1; }

echo "==> copying mido driver sources into $XNU/bsd/dev"
for f in mido_board mido_emmc mido_gpu; do
	cp "$SRC/$f.c" "$XNU/bsd/dev/$f.c"
	echo "    bsd/dev/$f.c"
done

echo "==> registering in bsd/conf/files"
python3 - <<PY
import io, sys, os
p = os.path.join("$XNU", "bsd/conf/files")
s = open(p, encoding="utf-8", errors="replace").read()
added = []
for f in ("mido_board", "mido_emmc", "mido_gpu"):
    line = "bsd/dev/%s.c\t\tstandard" % f
    if line not in s:
        added.append(line)
if added:
    # append after the last 'standard' line block that mentions polaris
    ins = "\n".join(added) + "\n"
    # insert before the trailing mem.c standard line if present
    if "bsd/dev/mem.c" in s:
        s = s.replace("bsd/dev/mem.c\t\tstandard", ins + "bsd/dev/mem.c\t\tstandard", 1)
    else:
        s = s.rstrip("\n") + "\n" + ins
    open(p, "w", encoding="utf-8").write(s)
    print("    added:", ", ".join(a.split('/')[-1] for a in added))
else:
    print("    already registered")
PY

echo "==> hooking init calls into bsd/dev/sweet_ufs.c"
python3 - <<PY
import os
p = os.path.join("$XNU", "bsd/dev/sweet_ufs.c")
s = open(p, encoding="utf-8", errors="replace").read()

# 1) extern declarations after polaris_gpu_init() declaration
ext_decl = (
    "extern void polaris_gpu_init(void);\n"
    "extern void mido_board_init(void);\n"
    "extern void mido_emmc_init(void);\n"
    "extern void mido_gpu_init(void);\n"
)
if "extern void mido_board_init(void);" not in s:
    if "extern void polaris_gpu_init(void);" in s:
        s = s.replace("extern void polaris_gpu_init(void);", ext_decl, 1)
    else:
        s = ext_decl + s
    print("    added externs")

# 2) init calls after the polaris_gpu_init(); call
calls = (
    "\tpolaris_gpu_init();\n"
    "\tmido_board_init();\n"
    "\tmido_emmc_init();\n"
    "\tmido_gpu_init();\n"
)
if "mido_board_init();" not in s:
    if "polaris_gpu_init();" in s:
        s = s.replace("\tpolaris_gpu_init();", calls, 1)
        print("    added init calls")
    else:
        print("    WARNING: could not find polaris_gpu_init(); call site; add manually")

open(p, "w", encoding="utf-8").write(s)
PY

echo "==> done. Rebuild with the xnu workflow (machine_config=SWEET)."
echo "    Board is selected at runtime via boot-arg 'mido=1'."
