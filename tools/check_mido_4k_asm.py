#!/usr/bin/env python3
"""Assemble the 0102 splash-mapping snippet on its own and check the encoding.

The four-block mapping is the hand-written part of the 4KB switch that the
compiler cannot police: a wrong `str` offset silently maps the wrong VA, and on
the phone that looks like "no breadcrumb at all".  This extracts the block from
the patched tree, assembles it for aarch64-freestanding with the real 4KB-page
constants, and verifies four stores at 8-byte stride plus a 2MB increment.
"""
import os
import re
import subprocess
import struct
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'out/mido4k/osfmk/arm64/start.s')
PY = r'C:/Users/才羽桃井/.workbuddy/binaries/python/envs/default/Scripts/python.exe'

PRELUDE = [
    '\t.text',
    '.macro MOV64 reg, val',
    '\tmovk\t\\reg, #(((\\val) >> 48) & 0xFFFF), lsl #48',
    '\tmovk\t\\reg, #(((\\val) >> 32) & 0xFFFF), lsl #32',
    '\tmovk\t\\reg, #(((\\val) >> 16) & 0xFFFF), lsl #16',
    '\tmovk\t\\reg, #(((\\val) >> 00) & 0xFFFF), lsl #00',
    '.endmacro',
    '#define TTE_SHIFT 3',
    '#define ARM_TT_L2_SIZE 0x200000',
    '#define ARM_TT_L2_SHIFT 21',
    '#define ARM_TT_L2_INDEX_MASK 0x3FFFFFFFE00000',
    '#define PGBYTES 4096',
    '\t.globl _t',
    '\t.p2align 2',
    '_t:',
]


def extract():
    with open(SRC, encoding='utf-8') as f:
        lines = f.read().split('\n')
    start = next(i for i, l in enumerate(lines)
                 if l.strip() == 'MOV64\tx10, 0x90000000')
    end = next(i for i in range(start, start + 40)
               if '3 << TTE_SHIFT' in lines[i])
    body = [l for l in lines[start:end + 1]
            if not l.lstrip().startswith(('/*', '*'))]
    # the attribute expression is a soup of header constants; keep the shape but
    # use a plain immediate so this assembles without the xnu headers
    body = [re.sub(r'MOV64\tx11, \(.*\)$', 'MOV64\tx11, 0x0060000090000605', l)
            for l in body]
    return body


def main():
    body = extract()
    print('extracted %d instruction lines' % len(body))
    text = '\n'.join(PRELUDE + body) + '\n'
    with tempfile.TemporaryDirectory() as td:
        s = os.path.join(td, 'splash.S')
        o = os.path.join(td, 'splash.o')
        with open(s, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        r = subprocess.run([PY, '-m', 'ziglang', 'cc', '-target',
                            'aarch64-freestanding', '-c', s, '-o', o],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print('ASSEMBLE FAILED')
            print(r.stderr[-3000:])
            print('---- input ----')
            print(text)
            return 1
        d = subprocess.run([PY, '-m', 'ziglang', 'objcopy', '-O', 'binary',
                            '--only-section=.text', o,
                            os.path.join(td, 'raw.bin')],
                           capture_output=True, text=True)
        raw = os.path.join(td, 'raw.bin')
        if not os.path.exists(raw):
            print('objcopy failed:', d.stderr[-800:])
            return 1
        code = open(raw, 'rb').read()

    words = [struct.unpack_from('<I', code, i)[0]
             for i in range(0, len(code) - 3, 4)]
    print('assembled %d bytes (%d instructions)' % (len(code), len(words)))

    ok = True
    # STR (immediate, unsigned offset, 64-bit): 0xF9000000, imm12 in bits 21:10
    stores = [(w >> 10) & 0xFFF for w in words
              if (w & 0xFFC00000) == 0xF9000000]
    print('str x11/x16 stores: %d, scaled offsets %s (expect 0,1,2,3)'
          % (len(stores), stores))
    if stores != [0, 1, 2, 3]:
        ok = False

    # MOVZ X16, #imm16, lsl #(16*hw)
    movz = [(((w >> 5) & 0xFFFF) << (((w >> 21) & 3) * 16))
            for w in words
            if (w & 0xFF800000) == 0xD2800000 and (w & 31) == 16]
    print('movz into x16: %s (expect [0x200000])'
          % [hex(v) for v in movz])
    if movz != [0x200000]:
        ok = False

    # ADD X11, X11, X16 (shifted register form) == 0x8B10016B
    adds = [w for w in words if w == 0x8B10016B]
    print('add x11, x11, x16: %d (expect 3)' % len(adds))
    if len(adds) != 3:
        ok = False

    print('RESULT:', 'OK' if ok else 'FAIL')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
