#!/usr/bin/env python3
"""Assemble the two rewritten post-MMU breadcrumbs on their own.

Extracts the "Breadcrumb (...)" blocks from the 0100 tree, drops the comment
lines, supplies a stand-alone MOV64 macro, then assembles with zig.  A typo in
the store loop would otherwise only show up as a dead kernel on the phone.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'out/p100/osfmk/arm64/start.s')
PY = r'C:/Users/才羽桃井/.workbuddy/binaries/python/envs/default/Scripts/python.exe'

PRELUDE = [
    '\t.text',
    '.macro MOV64 reg, val',
    '\tmovk\t\\reg, #(((\\val) >> 48) & 0xFFFF), lsl #48',
    '\tmovk\t\\reg, #(((\\val) >> 32) & 0xFFFF), lsl #32',
    '\tmovk\t\\reg, #(((\\val) >> 16) & 0xFFFF), lsl #16',
    '\tmovk\t\\reg, #(((\\val) >> 00) & 0xFFFF), lsl #00',
    '.endmacro',
    '\t.globl _t',
    '\t.p2align 2',
    '_t:',
]


def extract():
    ls = open(SRC, encoding='utf-8').read().split('\n')
    blocks = []
    for i, l in enumerate(ls):
        if 'Breadcrumb (' in l and 'MMU is now on' in l or 'past CPACR' in l:
            beg = i - 1
            while ls[beg].strip() != '/*':
                beg -= 1
            end = i
            while ls[end].strip() != '1:':
                end += 1
            body = [x for x in ls[beg:end + 1]
                    if not x.lstrip().startswith(('/*', '*', '*/'))]
            blocks.append((l.strip(), body))
    return blocks


def main():
    blocks = extract()
    print('found %d breadcrumb blocks' % len(blocks))
    src = list(PRELUDE)
    for name, body in blocks:
        insns = [l.strip() for l in body if l.strip()]
        print('  %-46s %d instructions' % (name[:44], len(insns)))
        print('     first: %s' % insns[0])
        print('     last : %s' % insns[-1])
        src += body
    src += ['\tret']
    s = os.path.join(ROOT, 'out/const_crumb_check.s')
    o = os.path.join(ROOT, 'out/const_crumb_check.o')
    with open(s, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(src) + '\n')
    r = subprocess.run([PY, '-m', 'ziglang', 'cc', '-target', 'aarch64-freestanding',
                        '-c', '-o', o, s], capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    print('assembler:', out[:800] if out else '(silent)')
    ok = os.path.exists(o) and r.returncode == 0
    if ok:
        print('object: %d bytes' % os.path.getsize(o))
        d = subprocess.run([PY, '-m', 'ziglang', 'objdump', '-d', o],
                           capture_output=True, text=True)
        lines = [l for l in d.stdout.split('\n') if 'movz' in l or 'dsb' in l
                 or 'str' in l or 'add' in l]
        for l in lines[:14]:
            print('   ' + l.strip())
    print('RESULT:', 'OK' if ok else 'FAIL')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
