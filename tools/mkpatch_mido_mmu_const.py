#!/usr/bin/env python3
"""Generate xnu/patches-mido/0100-mido-mmu-const-crumbs.patch.

0099 put two breadcrumbs past the MMU boundary, but both of them opened with

        ldr     x9, [x20, #40]          /* Video.v_baseAddr */

i.e. they read boot_args through the *physical* pointer that was valid while the
MMU was off.  With the MMU on that load goes through the page tables, so the
very first instruction of the "did the MMU come up?" probe can fault on its own.
On the panel that looks identical to SCTLR_EL1 itself faulting, which is exactly
the ambiguity the probe was supposed to remove.

Rewrite both breadcrumbs to use compile-time mido geometry only (1080x1920,
stride 3240, 24bpp, bottom band is row 1920-32 = 1888 -> 0x905D6E00), with no
loads at all before the first store, and a dsb sy so the Normal-NC writes reach
the bus before the kernel moves on.

Also fix the second colour: 0x00FFFF00 is yellow in this BGR-ordered 24bpp
framebuffer (bytes 00 FF FF), the intended cyan is 0x0000FFFF (bytes FF FF 00).

Reading the panel now really is unambiguous:
    white + green + yellow                SCTLR_EL1 or the isb faults
    ... + magenta                         the MMU is up and V=P is real
    ... + cyan                            the return into C is reached

Run from the repository root.
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'out/p99')       # tree with 0098 + 0099 applied
WORK = os.path.join(ROOT, 'out/p100')
PATCH = os.path.join(ROOT, 'xnu/patches-mido/0100-mido-mmu-const-crumbs.patch')
T_START = 'osfmk/arm64/start.s'

# row 1920-32, stride 3240, base 0x90001000
BAND = 0x90001000 + (1920 - 32) * 3240          # 0x905D6E00


def const_block(addr, color, what):
    return [
        '\t/*',
        '\t * Breadcrumb (%s).' % what[0],
        '\t * %s' % what[1],
        '\t * Compile-time geometry: after SCTLR_EL1 no load may happen before',
        '\t * the first store, or a page-table miss here would masquerade as the',
        '\t * MMU write itself failing.',
        '\t */',
        '\tMOV64\tx9, 0x%08X' % addr,
        '\tmov\t\tw1, #%s' % color,
        '\tmov\t\tx2, #32',
        '2:\tmov\t\tx3, #64',
        '\tmov\t\tx5, x9',
        '3:\tstr\t\tw1, [x5], #4',
        '\tsubs\tx3, x3, #1',
        '\tb.ne\t3b',
        '\tadd\t\tx9, x9, #3240',
        '\tsubs\tx2, x2, #1',
        '\tb.ne\t2b',
        '\tdsb\t\tsy',
        '1:',
    ]


def rewrite(ls, color, block, tag):
    """Replace the crumb whose colour is `color` with `block`."""
    hits = [i for i, l in enumerate(ls) if l == '\tmov\t\tw1, #%s' % color]
    assert len(hits) == 1, (tag, hits)
    col = hits[0]
    # start: the '\t/*' that opens the comment directly above
    start = col
    while start > 0 and not ls[start].lstrip().startswith('/*'):
        start -= 1
    # end: the first '1:' at or after the colour line
    end = col
    while ls[end] != '1:':
        end += 1
        assert end < col + 40, (tag, 'end not found')
    ls[start:end + 1] = block
    print('start.s: rewrote the %s breadcrumb (was %d lines, now %d)'
          % (tag, end - start + 1, len(block)))


def main():
    if os.path.isdir(WORK):
        junk = WORK + '.junk'
        n = 0
        while os.path.exists(junk):
            n += 1
            junk = WORK + '.junk%d' % n
        subprocess.run(['mv', WORK, junk], check=False)
    os.makedirs(os.path.join(WORK, 'osfmk/arm64'))
    with open(os.path.join(SRC, T_START), 'rb') as fi, \
            open(os.path.join(WORK, T_START), 'wb') as fo:
        fo.write(fi.read())

    subprocess.run(['git', 'init', '-q', '.'], cwd=WORK, check=True)
    subprocess.run(['git', 'config', 'core.autocrlf', 'false'], cwd=WORK, check=True)
    subprocess.run(['git', 'add', '-A'], cwd=WORK, check=True)
    subprocess.run(['git', '-c', 'user.email=a@b', '-c', 'user.name=a',
                    'commit', '-qm', 'post-0099'], cwd=WORK, check=True)

    with open(os.path.join(WORK, T_START), encoding='utf-8') as f:
        ls = f.read().split('\n')

    rewrite(ls, '0x00FF00FF',
            const_block(BAND + 288 * 4, '0x00FF00FF',
                        ('the MMU is now on',
                         'V=P must be real for this to land.')),
            'magenta')
    rewrite(ls, '0x00FFFF00',
            const_block(BAND + 384 * 4, '0x0000FFFF',
                        ('past CPACR/TPIDR/MIDR',
                         'The return into C is reached.')),
            'cyan')

    with open(os.path.join(WORK, T_START), 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(ls))

    diff = subprocess.run(['git', 'diff'], cwd=WORK, capture_output=True,
                          text=True, check=True).stdout
    assert diff, 'empty diff'
    with open(PATCH, 'w', encoding='utf-8', newline='\n') as f:
        f.write(_header() + '\n' + diff)

    added = sum(1 for l in diff.split('\n')
                if l.startswith('+') and not l.startswith('+++'))
    removed = sum(1 for l in diff.split('\n')
                  if l.startswith('-') and not l.startswith('---'))
    ctx = sum(1 for l in diff.split('\n') if l.startswith(' '))
    print('patch: %d context, %d removed, %d added -> %s'
          % (ctx, removed, added, os.path.relpath(PATCH, ROOT)))

    # replay
    fresh = WORK + '-verify'
    if os.path.isdir(fresh):
        junk = fresh + '.junk'
        n = 0
        while os.path.exists(junk):
            n += 1
            junk = fresh + '.junk%d' % n
        subprocess.run(['mv', fresh, junk], check=False)
    os.makedirs(os.path.join(fresh, 'osfmk/arm64'))
    with open(os.path.join(SRC, T_START), 'rb') as fi, \
            open(os.path.join(fresh, T_START), 'wb') as fo:
        fo.write(fi.read())
    subprocess.run(['git', 'init', '-q', '.'], cwd=fresh, check=True)
    subprocess.run(['git', 'config', 'core.autocrlf', 'false'], cwd=fresh, check=True)
    r = subprocess.run(['git', 'apply', '--check', '-v', PATCH], cwd=fresh,
                       capture_output=True, text=True)
    print('git apply --check:', 'OK' if r.returncode == 0 else 'FAIL')
    if r.returncode != 0:
        print(r.stderr[-2000:])
        return 1
    subprocess.run(['git', 'apply', PATCH], cwd=fresh, check=True)
    same = open(os.path.join(fresh, T_START), 'rb').read() == \
        open(os.path.join(WORK, T_START), 'rb').read()
    print('  %-28s replay identical: %s' % (T_START, same))
    print('RESULT:', 'OK' if same else 'MISMATCH')
    return 0 if same else 1


def _header():
    return """mido: make the post-MMU breadcrumbs independent of boot_args

0099's two post-MMU breadcrumbs began with

        ldr     x9, [x20, #40]          /* Video.v_baseAddr */

reading boot_args through the physical pointer that is only valid while the MMU
is off.  After SCTLR_EL1 that load goes through the page tables, so the probe
could fault on its own first instruction -- indistinguishable on the panel from
SCTLR_EL1 faulting, which is the ambiguity the probe exists to remove.

Rewrite both breadcrumbs with compile-time mido geometry (1080x1920, stride
3240, 24bpp, band at row 1920-32 -> 0x905D6E00), no loads before the first
store, and a trailing dsb sy so the Normal-NC writes reach the bus.

Fix the second colour too: 0x00FFFF00 is yellow in this BGR-ordered 24bpp
framebuffer, cyan is 0x0000FFFF.

    white + green + yellow        SCTLR_EL1 or the isb faults
    ... + magenta                 the MMU is up and V=P is real
    ... + cyan                    the return into C is reached
"""


if __name__ == '__main__':
    sys.exit(main())
