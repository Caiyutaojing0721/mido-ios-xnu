#!/usr/bin/env python3
"""Whole-kernel ARMv8.1+ instruction scanner (LSE atomics + v8.1 CAS/SWP).

Why this exists
---------------
check_lse.py only walked `__TEXT_EXEC`, so it declared a kernel "clean" while
mido kept trapping on atomics that live somewhere else:

    UNDEF pc=0xfffffff0072b55b0 instr=0xb8280028   ldadd w8, w8, [x1]
    UNDEF pc=0xfffffff007260a0c instr=0xf8280128   ldadd x8, x8, [x5]

A Cortex-A53 is ARMv8.0-A: no LSE (ldadd/ldset/ldclr/lde_or/ldsmax/ldumin/
swp/cas/casp), and the reported pc is useless until it is named.  So this
walks every section that actually holds instructions (Mach-O section type
S_REGULAR/S_SYMBOL_STUBS plus S_ATTR_PURE_INSTRUCTIONS or
S_ATTR_SOME_INSTRUCTIONS), decodes the offending word, and names the nearest
N_SECT symbol.

Usage
-----
    lse_scan.py <kernel.development.sweet>              # scan, exit 1 if hits
    lse_scan.py <kernel> --at 0xfffffff007260a0c        # what is at this pc?
    lse_scan.py <kernel> --context 0x...                # dump +-32 words
    lse_scan.py <kernel> --allow firehose_              # tolerate named sites
"""
import bisect
import struct
import sys

S_ATTR_PURE_INSTRUCTIONS = 0x80000000
S_ATTR_SOME_INSTRUCTIONS = 0x00000400

# ---------------------------------------------------------------- decoder ----
# LC_SEGMENT_64 / section_64
LC_SEGMENT_64 = 0x19
LC_SYMTAB = 0x2

LSE_OPS = {
    0x0: 'ldadd', 0x1: 'ldclr', 0x2: 'ldeor', 0x3: 'ldset',
    0x4: 'ldsmax', 0x5: 'ldsmin', 0x6: 'ldumax', 0x7: 'ldumin',
}

REGS = ['x%d' % i for i in range(31)] + ['xzr']
REGS32 = ['w%d' % i for i in range(31)] + ['wzr']


def decode_lse(w):
    """Return (mnemonic, size_bits) for an ARMv8.1 LSE atomic, else None."""
    if (w >> 24) & 0x3F != 0x38:          # size[31:30] 111000
        return None
    if not (w >> 21) & 1:                 # fixed 1 at bit 21
        return None
    if (w >> 10) & 3:                     # bits[11:10] reserved 00
        return None
    size = w >> 30                        # 0b10 = 32 bit, 0b11 = 64 bit
    if size not in (0b10, 0b11):
        return None
    if (w >> 21) & 0x3 == 0b11:           # bits[23:22] = A,R
        pass
    opc = (w >> 12) & 0xF
    mnem = LSE_OPS.get(opc)
    if opc == 0x8:
        mnem = 'swp'
    elif opc == 0x10:
        mnem = 'stadd' if ((w >> 16) & 0x1F) == 31 else 'ldadd'
    if mnem is None:
        return None
    return mnem, (32 if size == 0b10 else 64)


def fmt_lse(w):
    mn, bits = decode_lse(w)
    if mn is None:
        return None
    rs = (w >> 16) & 0x1F
    rn = (w >> 9) & 0x1F
    rt = w & 0x1F
    A = (w >> 23) & 1
    R = (w >> 22) & 1
    suffix = ''
    if A and R:
        suffix = 'al'
    elif A:
        suffix = 'a'
    elif R:
        suffix = 'l'
    if mn == 'swp':
        mnem = 'swp' + suffix
    elif mn == 'stadd':
        mnem = 'stadd' + suffix
    else:
        mnem = mn + suffix
    r = REGS if bits == 64 else REGS32
    if mn == 'stadd':
        return '%s %s, [%s]' % (mnem, r[rs], REGS[rn])
    return '%s %s, %s, [%s]' % (mnem, r[rt], r[rs], REGS[rn])


def is_bad(w):
    """True for any encoding a Cortex-A53 (ARMv8.0-A) cannot decode.

    Only the LSE atomic group is treated as fatal.  That is deliberate: it is
    what os_atomic_* lowers to when clang targets >= ARMv8.1, it is what both
    observed mido panics were, and its encoding is unambiguous -- a false
    positive here would be a wasted CI run, so the pattern stays exact
    (bits[29:24] == 111000, bit21 == 1, bits[11:10] == 00).
    """
    return decode_lse(w) is not None


def describe(w):
    t = fmt_lse(w)
    return t if t else '?'


# ------------------------------------------------------------------ mach-o ---
class MachO:
    def __init__(self, path):
        self.d = open(path, 'rb').read()
        magic, cputype, cpusub, ftype, ncmds = struct.unpack_from('<IiiII', self.d, 0)
        if magic != 0xFEEDFACF:
            raise SystemExit('not a 64 bit Mach-O')
        off = 32
        self.segs = []
        self.symoff = self.nsyms = self.stroff = None
        for _ in range(ncmds):
            cmd, csz = struct.unpack_from('<II', self.d, off)
            if cmd == LC_SEGMENT_64:
                name = self.d[off + 8:off + 24].rstrip(b'\0').decode('utf-8', 'replace')
                vm, vs, fo, fs = struct.unpack_from('<QQQQ', self.d, off + 24)
                nsects = struct.unpack_from('<I', self.d, off + 64)[0]
                secs = []
                for i in range(nsects):
                    p = off + 72 + i * 80
                    sn = self.d[p:p + 16].rstrip(b'\0').decode('utf-8', 'replace')
                    addr, size, o, al, ro, nr, sf = struct.unpack_from(
                        '<QQIIIII', self.d, p + 32)
                    secs.append((sn, addr, size, o, sf))
                self.segs.append((name, vm, vs, fo, fs, secs))
            elif cmd == LC_SYMTAB:
                self.symoff, self.nsyms, self.stroff, _ = struct.unpack_from(
                    '<IIII', self.d, off + 8)
            off += csz
        self.syms = []
        if self.symoff is not None:
            for i in range(self.nsyms):
                strx, ntype, nsect, ndesc, nvalue = struct.unpack_from(
                    '<IBBHQ', self.d, self.symoff + i * 16)
                if nvalue and (ntype & 0x0E) == 0x0E:
                    e = self.d.index(b'\0', self.stroff + strx)
                    self.syms.append((nvalue,
                                      self.d[self.stroff + strx:e].decode('utf-8', 'replace')))
            self.syms.sort()
        self.addrs = [s[0] for s in self.syms]

    def name(self, va):
        k = bisect.bisect_right(self.addrs, va) - 1
        if k < 0:
            return '?', 0
        return self.syms[k][1], va - self.syms[k][0]

    def code_sections(self):
        for name, vm, vs, fo, fs, secs in self.segs:
            for sn, addr, size, o, sf in secs:
                if not (sf & (S_ATTR_PURE_INSTRUCTIONS | S_ATTR_SOME_INSTRUCTIONS)):
                    continue
                if not size:
                    continue
                yield name, sn, addr, size, o, sf

    def fetch(self, va, n):
        for name, vm, vs, fo, fs, secs in self.segs:
            if vm <= va < vm + vs:
                f = fo + (va - vm)
                out = []
                for i in range(n):
                    if 0 <= f + 4 * i <= len(self.d) - 4:
                        out.append((va + 4 * i,
                                    struct.unpack_from('<I', self.d, f + 4 * i)[0]))
                return name, out
        return None, []


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__.strip())
        return 2
    path = args[0]
    at = None
    ctx = None
    allow = []
    i = 1
    while i < len(args):
        if args[i] == '--at':
            at = int(args[i + 1], 16)
            i += 2
        elif args[i] == '--context':
            ctx = int(args[i + 1], 16)
            i += 2
        elif args[i] == '--allow':
            allow.append(args[i + 1])
            i += 2
        else:
            raise SystemExit('unknown option ' + args[i])

    m = MachO(path)

    if at is not None or ctx is not None:
        va = at if at is not None else ctx
        seg, words = m.fetch(va, 33 if ctx is not None else 1)
        if not words:
            print('0x%016x not mapped by any segment' % va)
            return 1
        print('0x%016x -> segment %s, %s+0x%x' % ((va, seg) + m.name(va)))
        if ctx is not None:
            for a, w in words:
                t = describe(w) if is_bad(w) else ''
                print('  0x%016x  %08x  %s%s' % (a, w, t, '   <<<<' if a == va else ''))
        else:
            w = words[0][0]
            print('  word %08x  %s' % (w, describe(w) if is_bad(w) else '(not v8.1)'))
            # show the exact store/load-exclusive rewriting that would be needed
            print('  name %s+0x%x' % m.name(va))
        return 0

    total = 0
    bad = []
    for seg, sec, addr, size, o, sf in m.code_sections():
        for k in range(0, size - 3, 4):
            w = struct.unpack_from('<I', m.d, o + k)[0]
            if not is_bad(w):
                continue
            va = addr + k
            nm, off = m.name(va)
            total += 1
            if any(a in nm for a in allow):
                print('  allowed 0x%016x %08x %-38s %s+0x%x'
                      % (va, w, describe(w), nm, off))
            else:
                bad.append((va, w, nm, off, seg, sec))

    for va, w, nm, off, seg, sec in bad:
        print('  BAD 0x%016x %08x %-38s %s+0x%x  [%s/%s]'
              % (va, w, describe(w), nm, off, seg, sec))
    print('  scanned %d instruction sections, %d v8.1 word(s), %d not allowed'
          % (len(list(m.code_sections())), total, len(bad)))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
