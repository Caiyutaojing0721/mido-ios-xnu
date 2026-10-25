#!/usr/bin/env python3
"""Generate xnu/patches-mido/0101-mido-crumb-color.patch.

0100 rebuilt both post-MMU breadcrumbs from compile-time constants, which was
the right fix, but it also "corrected" the fifth colour the wrong way round.

The console in shim/console.c stores a 24bpp pixel as

        p[0] = c >> 16;   p[1] = c >> 8;   p[2] = c;

i.e. memory order R,G,B.  The breadcrumbs however store a whole word with
`str w1, [x5], #4`, which writes little-endian: byte0 = W & 0xff.  So for the
crumb the byte order is inverted relative to the console:

        crumb word 0x0000FFFF -> bytes FF FF 00 -> R=FF G=FF B=00 -> yellow
        crumb word 0x00FFFF00 -> bytes 00 FF FF -> R=00 G=FF B=FF -> cyan

Green (0x0000FF00) and magenta (0x00FF00FF) are symmetric under the swap, and
the observed panel (white / green / yellow) confirms it: 0098's third crumb was
0x0000FFFF and it came out yellow.

So the fifth crumb must be 0x00FFFF00, which is exactly what 0099 had before
0100 changed it.  0100's binary is already flashed once; this patch only makes
the next build paint cyan instead of a second yellow.

Run from the repository root.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'out/p100')      # tree with 0098 + 0099 + 0100 applied
WORK = os.path.join(ROOT, 'out/p101')
PATCH = os.path.join(ROOT, 'xnu/patches-mido/0101-mido-crumb-color.patch')
T_START = 'osfmk/arm64/start.s'

OLD = '\tmov\t\tw1, #0x0000FFFF'
NEW = '\tmov\t\tw1, #0x00FFFF00'


def archive(path):
    if os.path.isdir(path):
        junk, n = path + '.junk', 0
        while os.path.exists(junk):
            n += 1
            junk = path + '.junk%d' % n
        subprocess.run(['mv', path, junk], check=False)


def seed(path, commit):
    archive(path)
    os.makedirs(os.path.join(path, 'osfmk/arm64'))
    with open(os.path.join(SRC, T_START), 'rb') as fi, \
            open(os.path.join(path, T_START), 'wb') as fo:
        fo.write(fi.read())
    subprocess.run(['git', 'init', '-q', '.'], cwd=path, check=True)
    subprocess.run(['git', 'config', 'core.autocrlf', 'false'], cwd=path, check=True)
    subprocess.run(['git', 'add', '-A'], cwd=path, check=True)
    subprocess.run(['git', '-c', 'user.email=a@b', '-c', 'user.name=a',
                    'commit', '-qm', commit], cwd=path, check=True)


def main():
    seed(WORK, 'post-0100')

    with open(os.path.join(WORK, T_START), encoding='utf-8') as f:
        ls = f.read().split('\n')

    hits = [i for i, l in enumerate(ls) if l == OLD]
    assert len(hits) == 2, ('expected the two post-MMU crumb fills', hits)
    # the first one is the 4th crumb? no: 0x0000FFFF appears in the yellow crumb
    # (pre-MMU, written by 0098 as mov w1,#0xFFFF) and in the fifth crumb.
    # Only the fifth is ours: it is the one preceded by MOV64 0x905D6D00.
    fifth = None
    for i in hits:
        for j in range(i - 6, i):
            if ls[j].lstrip().startswith('MOV64\tx9, 0x905D6D00'):
                fifth = i
    assert fifth is not None, 'fifth crumb not found'
    prev = [l for l in ls[fifth - 12:fifth]
            if l.strip().startswith('MOV64')]
    assert prev and '0x905D6D00' in prev[-1], prev
    ls[fifth] = NEW
    print('start.s: fifth crumb fill %s -> %s' % (OLD.strip(), NEW.strip()))

    with open(os.path.join(WORK, T_START), 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(ls))

    diff = subprocess.run(['git', 'diff'], cwd=WORK, capture_output=True,
                          text=True, check=True).stdout
    assert diff, 'empty diff'
    with open(PATCH, 'w', encoding='utf-8', newline='\n') as f:
        f.write(_header() + '\n' + diff)
    print('patch: %s' % os.path.relpath(PATCH, ROOT))

    fresh = WORK + '-verify'
    seed(fresh, 'pre-0101')
    r = subprocess.run(['git', 'apply', '--check', '-v', PATCH], cwd=fresh,
                       capture_output=True, text=True)
    print('git apply --check:', 'OK' if r.returncode == 0 else 'FAIL')
    if r.returncode != 0:
        print(r.stderr[-2000:])
        return 1
    subprocess.run(['git', 'apply', PATCH], cwd=fresh, check=True)
    same = open(os.path.join(fresh, T_START), 'rb').read() == \
        open(os.path.join(WORK, T_START), 'rb').read()
    print('replay identical:', same)
    return 0 if same else 1


def _header():
    return """mido: paint the fifth breadcrumb cyan (0100 wrote yellow)

shim/console.c stores 24bpp pixels as p[0]=R p[1]=G p[2]=B, but the breadcrumbs
store a whole word with `str w1, [x5], #4`, which is little-endian, so the byte
order is inverted.  0x0000FFFF therefore renders yellow (bytes FF FF 00), and
0100's "cyan fix" turned the fifth crumb into a second yellow block.

The panel already proved this: 0098's third crumb was 0x0000FFFF and it came out
yellow.  The correct cyan word is 0x00FFFF00 (bytes 00 FF FF).

Green and magenta are symmetric, so only this one block changes.

    white green yellow magenta yellow   (0100 build, count blocks: 5 = reached ret)
    white green yellow magenta cyan     (after this patch)
"""


if __name__ == '__main__':
    sys.exit(main())
