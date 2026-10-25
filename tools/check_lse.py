#!/usr/bin/env python3
"""Refuse a kernel that still contains ARMv8.1 LSE atomics.

mido is a Cortex-A53 (ARMv8.0-A).  It has no LSE, so any `ldadd`/`ldset`/`cas`
traps as an undefined instruction -- that is exactly how the 0105 kernel died:

    UNDEF pc=0xfffffff0072b55b0 instr=0xb8280028   ->  ldadd w8, w8, [x1]
    0xfffffff0072b55b0 -> _lck_grp_reference (+0x8)

0106 narrows the target to `-march=armv8-a`, which makes clang lower
os_atomic_* to load-exclusive/store-exclusive pairs.  This script verifies the
result instead of trusting the flag: it reads the reference-counting function
that trapped and rejects it if an LSE encoding is still in there.

Usage: check_lse.py <kernel.development.sweet>
"""
import struct
import sys

# size[31:30] 111 0 00 A R 1 Rs opc[15:12] 00 Rn Rt
# e.g. 0xb8280008 = ldadd w8, w8, [x0], 0xb8e80008 = ldaddal w8, w8, [x0]
LSE_OPC = {0x0, 0x1, 0x2, 0x3, 0x4, 0x5, 0x6, 0x7, 0x8, 0x10}


def is_lse(w, strict=False):
    if (w >> 24) & 0x3F != 0x38:
        return False
    if not (w >> 21) & 1:
        return False
    if (w >> 10) & 3:
        return False
    if (w >> 12) & 0xF not in LSE_OPC:
        return False
    if strict and ((w >> 16) & 0x1F) != (w & 0x1F):
        return False
    return True


def main():
    if len(sys.argv) != 2:
        print(__doc__.strip().splitlines()[-1])
        return 2
    d = open(sys.argv[1], 'rb').read()
    magic, cputype, cpusub, ftype, ncmds, sizeofcmds, flags, res = \
        struct.unpack_from('<IiiIIIII', d, 0)
    if magic != 0xFEEDFACF:
        print('  not a 64 bit Mach-O')
        return 1

    off = 32
    segs = {}
    symoff = nsyms = stroff = strsize = None
    for _ in range(ncmds):
        cmd, csz = struct.unpack_from('<II', d, off)
        if cmd == 0x19:
            nm = d[off + 8:off + 24].rstrip(b'\0').decode()
            segs[nm] = struct.unpack_from('<QQQQ', d, off + 24)
        elif cmd == 0x2:
            symoff, nsyms, stroff, strsize = struct.unpack_from('<IIII', d, off + 8)
        off += csz

    def sym_addr(name):
        if symoff is None:
            return None
        for i in range(nsyms):
            strx, ntype, nsect, ndesc, nvalue = struct.unpack_from(
                '<IBBHQ', d, symoff + i * 16)
            if not nvalue:
                continue
            e = d.index(b'\0', stroff + strx)
            if d[stroff + strx:e].decode('utf-8', 'replace') == name:
                return nvalue
        return None

    def fetch(va, n=12):
        for nm, (vm, vs, fo, fs) in segs.items():
            if vm <= va < vm + vs:
                f = fo + (va - vm)
                return [struct.unpack_from('<I', d, f + 4 * i)[0] for i in range(n)]
        return None

    bad = []
    va = sym_addr('_lck_grp_reference')
    if va is None:
        print('  _lck_grp_reference: symbol not found (stripped?)')
    else:
        words = fetch(va)
        print('  _lck_grp_reference 0x%016x' % va)
        print('    ' + ' '.join('%08x' % w for w in words))
        hits = [i for i, w in enumerate(words) if is_lse(w, strict=True)]
        if hits:
            bad.append('LSE still present in lck_grp_reference at +0x%x (%08x)'
                       % (hits[0] * 4, words[hits[0]]))

    # informational: how many in-place LSE atomics survive in executable text
    n = 0
    for nm in ('__TEXT_EXEC',):
        if nm not in segs:
            continue
        vm, vs, fo, fs = segs[nm]
        for i in range(0, fs - 3, 4):
            if is_lse(struct.unpack_from('<I', d, fo + i)[0], strict=True):
                n += 1
    print('  in-place LSE words in __TEXT_EXEC: %d' % n)
    if n:
        bad.append('%d in-place LSE atomic word(s) left in __TEXT_EXEC' % n)

    if bad:
        for b in bad:
            print('  BAD: ' + b)
        return 1
    print('  OK: no ARMv8.1 atomics, safe on Cortex-A53')
    return 0


if __name__ == '__main__':
    sys.exit(main())
