#!/usr/bin/env python3
"""Prove from the binary that the zone window is the compact one.

_zone_set_map_sizes materialises ZONE_MAP_MAX and ZONE_MAP_VA_SIZE as
movz/movk pairs with lsl 32 (32 GiB / 128 GiB both fit in one halfword at
bit 32).  The macOS branch shows up as 0x2/0x8 and 0x20/0x8; the compact
branch as 0x2 << 32 (max) and 0x6 << 32 (VA size).

usage: check0111.py [kernel]
"""
import bisect
import struct
import sys

P = sys.argv[1] if len(sys.argv) > 1 else \
    "/home/midori/ios-port/ios16_on_polaris_mix2s/out/ci-kernel/kernel.development.sweet"
TARGET = "_zone_set_map_sizes"

d = open(P, "rb").read()
ncmds = struct.unpack_from("<I", d, 16)[0]
off = 32
segs = {}
symoff = stroff = nsyms = None
funcstarts = None
for _ in range(ncmds):
    cmd, size = struct.unpack_from("<II", d, off)
    if cmd == 0x19:
        name = d[off + 8:off + 24].rstrip(b"\0").decode()
        vm, vs, fo, fs = struct.unpack_from("<QQQQ", d, off + 24)
        if fs:
            segs[name] = (vm, vs, fo, fs)
    elif cmd == 2:
        symoff, nsyms, stroff = struct.unpack_from("<III", d, off + 8)
    elif cmd == 0x26:
        funcstarts = struct.unpack_from("<II", d, off + 8)
    off += size


def cstr(o):
    e = d.index(b"\0", o)
    return d[o:e].decode("utf-8", "replace")


syms = []
for i in range(nsyms):
    strx, ntype, nsect, ndesc, nvalue = struct.unpack_from("<IBBHQ", d, symoff + i * 16)
    if nvalue and (ntype & 0x0E) == 0x0E:
        syms.append((nvalue, cstr(stroff + strx)))
syms.sort()
addrs = [s[0] for s in syms]

hit = [a for a, n in syms if n == TARGET]
if not hit:
    print("FAIL: %s not in the symbol table" % TARGET)
    sys.exit(1)
va = hit[0]

# function size from LC_FUNCTION_STARTS
size = 0x400
if funcstarts:
    foff, fsize = funcstarts
    text_vm = segs["__TEXT"][0]
    p = foff
    cur = text_vm
    starts = []
    while p < foff + fsize:
        val = 0
        shift = 0
        while True:
            b = d[p]
            p += 1
            val |= (b & 0x7F) << shift
            shift += 7
            if not b & 0x80:
                break
        if val == 0:
            break
        cur += val
        starts.append(cur)
    if va in starts:
        idx = starts.index(va)
        nxt = starts[idx + 1] if idx + 1 < len(starts) else text_vm + segs["__TEXT"][1]
        size = nxt - va

body = None
for nm, (vm, vs, fo, fs) in segs.items():
    if vm <= va < vm + vs:
        body = d[fo + (va - vm): fo + (va - vm) + size]
        break
if body is None:
    print("FAIL: cannot read %s" % TARGET)
    sys.exit(1)

print("%s @ 0x%016x  size=0x%x" % (TARGET, va, size))
imm32 = set()
regs = {}
for i in range(0, len(body) - 3, 4):
    w = struct.unpack_from("<I", body, i)[0]
    rd = w & 31
    if (w & 0xFF800000) == 0xD2800000:      # MOVZ 64
        hw = (w >> 21) & 3
        regs[rd] = (w >> 5) & 0xFFFF
        if hw == 2:
            imm32.add(regs[rd] << 32)
    elif (w & 0xFF800000) == 0xF2800000:    # MOVK 64
        hw = (w >> 21) & 3
        v = regs.get(rd, 0) | (((w >> 5) & 0xFFFF) << (hw * 16))
        regs[rd] = v
        if hw == 2:
            imm32.add(v)

names = {
    0x200000000: "8 GiB",
    0x600000000: "24 GiB",
    0x800000000: "32 GiB",
    0x2000000000: "128 GiB",
}
for v in sorted(imm32):
    print("  const 0x%x  (%s)" % (v, names.get(v, "?")))

G128 = 0x2000000000
G32_VA = 0x600000000
bad = []
if G128 in imm32:
    bad.append("ZONE_MAP_VA_SIZE is still 128 GiB")
if G32_VA not in imm32:
    bad.append("ZONE_MAP_VA_SIZE is not 24 GiB")
if bad:
    for b in bad:
        print("  BAD: " + b)
    sys.exit(1)
print("  OK: zone window is the compact 24 GiB layout")
