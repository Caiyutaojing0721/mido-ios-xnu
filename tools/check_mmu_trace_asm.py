#!/usr/bin/env python3
"""Assemble the parts of 0099 that the C compiler cannot see.

The low vector table and the red-block painter are pure assembly and they run
before any of the kernel's infrastructure exists, so a wrong branch target or
an unencodable immediate would only show up as another silent freeze on the
device.  Pull those lines out of the patched start.s, wrap them in the two
macros they need, and let the assembler check them here.

Run from the repository root.
"""
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
START = os.path.join(ROOT, 'out/p99/osfmk/arm64/start.s')
ZIG = os.path.join(os.path.dirname(ROOT), 'tools/zig-0.16/zig')

PRELUDE = r"""
	.text
.macro MOV64 reg, val
	movk \reg, #(((\val) >> 48) & 0x000000000000FFFF), lsl #48
	movk \reg, #(((\val) >> 32) & 0x000000000000FFFF), lsl #32
	movk \reg, #(((\val) >> 16) & 0x000000000000FFFF), lsl #16
	movk \reg, #(((\val) >> 00) & 0x000000000000FFFF), lsl #00
.endmacro
#define EXT(x) x
#define LEXT(x) x:
"""

EPILOGUE = r"""
	.align 12, 0
	.globl _end_marker
_end_marker:
"""


def extract():
    with open(START, encoding='utf-8') as f:
        ls = f.read().split('\n')
    beg = next(i for i, l in enumerate(ls) if l.strip() == 'LEXT(LowExceptionVectorBase)')
    # the vector table is the 16 slots; the painter follows the closing .align
    end = beg
    while not (ls[end].strip().startswith('.align') and '12' in ls[end]):
        end += 1
    body = ls[beg:end + 1]
    # painter: from its .align 2 to the trailing "b 3b"
    p = end + 1
    while 'Llow_vector_paint' not in ls[p]:
        p += 1
    q = p
    while ls[q].strip() != 'b\t\t3b':
        q += 1
    body += ls[p - 1:q + 1]
    body = [re.sub(r'LEXT\((.*?)\)', r'\1:', l) for l in body]
    body = [re.sub(r'EXT\((.*?)\)', r'\1', l) for l in body]
    return body


def zig_cmd():
    """tools/zig-0.16/zig is a shell wrapper around "python -m ziglang", which
    Windows cannot execute directly -- read the interpreter out of it."""
    if not os.path.exists(ZIG):
        return None
    with open(ZIG, encoding='utf-8', errors='replace') as f:
        txt = f.read()
    m = re.search(r'exec\s+"([^"]+)"', txt)
    if m:
        return [m.group(1), '-m', 'ziglang']
    return None


def main():
    body = extract()
    text = PRELUDE + '\n'.join(body) + EPILOGUE
    cmd = zig_cmd()
    if cmd is None:
        print('skip: zig wrapper not found at %s' % ZIG)
        return 0
    with tempfile.TemporaryDirectory() as td:
        s = os.path.join(td, 'mmu_trace.s')
        o = os.path.join(td, 'mmu_trace.o')
        with open(s, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        r = subprocess.run(cmd + ['cc', '-target', 'aarch64-freestanding',
                                  '-c', s, '-o', o],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print('ASSEMBLE FAILED')
            print(r.stderr[-3000:])
            print('---- input ----')
            print(text)
            return 1
        print('assemble: OK (%d bytes of object)' % os.path.getsize(o))
        # 16 slots, each 0x80 bytes, each holding a b to the painter
        b = os.path.join(td, 'mmu_trace.bin')
        r = subprocess.run(cmd + ['objcopy', '-O', 'binary',
                                  '--only-section=.text', o, b],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print('objcopy failed:', r.stderr[-500:])
            return 1
        raw = open(b, 'rb').read()
        print('text: %d bytes (vector table needs 0x800)' % len(raw))
        import struct
        bad = 0
        for i in range(16):
            off = i * 0x80
            if off + 4 > len(raw):
                print('  slot %2d: beyond end of text' % i)
                bad += 1
                continue
            w = struct.unpack_from('<I', raw, off)[0]
            # b <label>: 000101 imm26
            if (w >> 26) != 0b000101:
                print('  slot %2d: not a b instruction (%#x)' % (i, w))
                bad += 1
            else:
                imm = w & 0x3FFFFFF
                if imm & (1 << 25):
                    imm -= (1 << 26)
                target = off + imm * 4
                if not (0 <= target < len(raw)):
                    print('  slot %2d: branch out of range -> %d' % (i, target))
                    bad += 1
        print('vector slots checked: %d bad' % bad)
        if bad:
            return 1
        # the painter must also have been emitted: look for the wfi loop tail
        if struct.pack('<I', 0xD503207F) not in raw:   # wfi
            print('wfi not found in text')
            return 1
        print('painter: wfi/halt loop present')
    return 0


if __name__ == '__main__':
    sys.exit(main())
