#!/usr/bin/env python3
"""Build xnu/patches-mido/0096-mido-fbcon-24bpp.patch.

0003 installs a 32bpp-only framebuffer console and 0012 patches into the middle
of it, so 0003 must stay byte-identical (shared with sweet/polaris). The mido
delta therefore goes into its own patch that applies *after* 0012.

Method: build a synthetic pe_serial.c from the old-side context of 0003 and
0012 at their declared line numbers, apply both for real, then perform the
surgery there and export the resulting diff. Context lines in the exported
patch are therefore the true post-0012 text.
"""
import os
import re
import shutil
import subprocess
import sys

P0003 = 'xnu/patches/0003-sweet-framebuffer-serial-console.patch'
P0012 = 'xnu/patches/0012-sweet-keep-debug-console-after-ttbr0.patch'
TARGET = 'pexpert/arm/pe_serial.c'
WORK = 'out/patch0096'
OUT = 'xnu/patches-mido/0096-mido-fbcon-24bpp.patch'
BLOCK = 'out/fbcon_new_block.txt'

T = '\t'


def hunks(path, target):
    """[(old_start, [(kind, text), ...])] for one file in one patch."""
    out, cur, want = [], None, False
    for line in open(path, encoding='utf-8').read().split('\n'):
        if line.startswith('diff --git '):
            want = line.split(' b/')[-1] == target
            continue
        if not want:
            continue
        if line.startswith('@@'):
            m = re.match(r'@@ -(\d+),(\d+) \+\d+,\d+ @@', line)
            out.append((int(m.group(1)), []))
        elif out and line[:1] in (' ', '-', '+') and len(line) > 1:
            out[-1][1].append((line[0], line[1:]))
        elif out and line[:1] in (' ', '-', '+') and len(line) == 1:
            out[-1][1].append((line[0], ''))
        elif out and line in ('', '-- ') and out[-1][1]:
            pass
    return out


def build(root):
    # 0012 numbers its pe_serial.c hunks against the *post-0003* file, so only
    # the early include hunk belongs in the synthetic base; the fbcon hunk must
    # be left to land inside the block 0003 inserts.
    hs = hunks(P0003, TARGET) + [h for h in hunks(P0012, TARGET) if h[0] < 100]
    path = os.path.join(root, TARGET)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    last = max(s + len(b) for s, b in hs) + 8
    lines = ['/* filler %d */' % (i + 1) for i in range(last)]
    for start, body in hs:
        i = start - 1
        for kind, text in body:
            if kind in (' ', '-'):
                lines[i] = text
                i += 1
    open(path, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
    return [(s, len(b)) for s, b in hs]


def apply(root, patch):
    r = subprocess.run(['git', 'apply', '--include=' + TARGET, '--verbose', patch],
                       cwd=root, capture_output=True, text=True)
    msg = (r.stdout + r.stderr).strip()
    print('apply %-58s rc=%d %s' % (os.path.basename(patch), r.returncode,
                                    msg.replace('\n', ' | ')[:160]))
    if r.returncode:
        raise SystemExit('failed to apply ' + patch)


def surgery(src):
    """Rewrite the fbcon block in place; returns (new_text, block_lines)."""
    lines = src.split('\n')

    def find(pred, what, start=0):
        for i in range(start, len(lines)):
            if pred(lines[i]):
                return i
        raise SystemExit('marker not found: ' + what)

    # 0. header comment: stride is no longer tied to 32bpp
    i = find(lambda l: 'stride' in l and 'bytes per line' in l, 'stride comment')
    lines[i] = lines[i].replace('bytes per line, 32bpp', 'bytes per line')
    lines.insert(i + 1, ' *   depth                      32 = ABGR8888, 24 = BGR888')

    # 1. declaration: byte pointer + bpp
    i = find(lambda l: l.strip() == 'static volatile uint32_t *fbcon_base;', 'fbcon_base decl')
    lines[i] = 'static volatile uint8_t *fbcon_pixels;'
    i = find(lambda l: l.strip() == 'static uint32_t fbcon_width, fbcon_height, fbcon_stride;',
             'geometry decl')
    lines[i] = 'static uint32_t fbcon_width, fbcon_height, fbcon_stride, fbcon_bpp;'

    # 2. fbcon_put(), inserted before fbcon_clear_row
    i = find(lambda l: l.strip() == 'fbcon_clear_row(uint32_t row)', 'clear_row def')
    assert lines[i - 1].strip() == 'static void', lines[i - 1]
    put = [
        '/*',
        ' * One pixel. At 32bpp the word goes straight into the mapped buffer; at',
        ' * 24bpp the splash is byte-packed, one byte per channel, so the channels',
        ' * have to be written out individually.',
        ' */',
        'static inline void',
        'fbcon_put(uint32_t x, uint32_t y, uint32_t argb)',
        '{',
        T + 'volatile uint8_t *p = fbcon_pixels + (unsigned long)y * fbcon_stride +',
        T + '    (unsigned long)x * fbcon_bpp;',
        '',
        T + 'if (fbcon_bpp == 4) {',
        T + T + '*(volatile uint32_t *)p = argb;',
        T + '} else {',
        T + T + 'p[0] = (uint8_t)(argb);',
        T + T + 'p[1] = (uint8_t)(argb >> 8);',
        T + T + 'p[2] = (uint8_t)(argb >> 16);',
        T + '}',
        '}',
        '',
    ]
    lines[i - 1:i - 1] = put

    # 3. clear_row: per-pixel through fbcon_put
    i = find(lambda l: l.strip() == 'volatile uint32_t *line = fbcon_base + y * (fbcon_stride / 4);',
             'clear_row line ptr')
    assert 'line[x] = FBCON_BG;' in lines[i + 2], lines[i:i + 4]
    # the closing brace of the x loop is already there, do not add another
    lines[i:i + 3] = [
        T + T + 'for (uint32_t x = 0; x < fbcon_width; x++) {',
        T + T + T + 'fbcon_put(x, y, FBCON_BG);',
    ]

    # 4. transmit_data: hoist the row, draw through fbcon_put
    i = find(lambda l: 'fbcon_base + (y0 + gy * FBCON_SCALE + sy)' in l, 'glyph line ptr')
    assert 'line[gx * FBCON_SCALE + sx] = px;' in lines[i + 4], lines[i:i + 6]
    lines[i:i + 5] = [
        T + T + T + 'uint32_t y = y0 + gy * FBCON_SCALE + sy;',
        T + T + T + 'for (uint32_t gx = 0; gx < 8; gx++) {',
        T + T + T + T + 'uint32_t px = (glyph[gy] & (1u << gx)) ? FBCON_FG : FBCON_BG;',
        T + T + T + T + 'for (uint32_t sx = 0; sx < FBCON_SCALE; sx++) {',
        T + T + T + T + T + 'fbcon_put(x0 + gx * FBCON_SCALE + sx, y, px);',
    ]

    # 5. probe: read depth, validate against bpp, map as bytes
    i = find(lambda l: 'SecureDTGetProperty(entry, "stride"' in l, 'stride read')
    close = find(lambda l: l.strip() == '}', 'stride read close', i)
    lines[close + 1:close + 1] = [
        T + 'if (SecureDTGetProperty(entry, "depth", (void const **)&val, &size) == kSuccess) {',
        T + T + 'fbcon_bpp = *val / 8;',
        T + '}',
        T + 'if (fbcon_bpp != 3 && fbcon_bpp != 4) {',
        T + T + '/* sweet and polaris splash out 32bpp: keep them unchanged. */',
        T + T + 'fbcon_bpp = 4;',
        T + '}',
    ]
    i = find(lambda l: 'fbcon_stride < fbcon_width * 4' in l, 'validation')
    lines[i] = lines[i].replace('fbcon_width * 4', 'fbcon_width * fbcon_bpp')
    i = find(lambda l: 'fbcon_base = (volatile uint32_t *)ml_io_map_wcomb' in l, 'mapping')
    lines[i] = lines[i].replace('fbcon_base = (volatile uint32_t *)',
                                'fbcon_pixels = (volatile uint8_t *)')
    i = find(lambda l: l.strip() == 'if (fbcon_base != NULL) {', 'null check')
    lines[i] = lines[i].replace('fbcon_base', 'fbcon_pixels')

    text = '\n'.join(lines)
    assert 'fbcon_base' not in text, 'stale fbcon_base reference left'

    # capture the whole block for the standalone compile check
    a = next(i for i, l in enumerate(lines) if l.startswith('/****') and i > 600)
    b = next(i for i, l in enumerate(lines) if l.strip() == '#if HIBERNATION' and i > a)
    block = lines[a:b]
    while block and block[-1] == '':
        block.pop()
    return text, block


def reset_dir(p):
    """The sandbox blocks rmtree; move stale trees aside instead."""
    if os.path.exists(p):
        junk = p + '.junk'
        n = 0
        while os.path.exists(junk):
            n += 1
            junk = p + '.junk%d' % n
        subprocess.run(['mv', p, junk], check=False)
    os.makedirs(p, exist_ok=True)


def main():
    reset_dir(WORK)
    placed = build(WORK)
    print('synthetic hunks placed at old starts:', [p[0] for p in placed])

    subprocess.run(['git', 'init', '-q'], cwd=WORK, check=True)
    subprocess.run(['git', 'add', '-A'], cwd=WORK, check=True)
    subprocess.run(['git', '-c', 'user.email=a@b', '-c', 'user.name=a',
                    'commit', '-qm', 'base'], cwd=WORK, check=True)

    apply(WORK, os.path.abspath(P0003))
    apply(WORK, os.path.abspath(P0012))
    # snapshot the post-0012 state so the exported diff is the mido delta only
    subprocess.run(['git', 'add', '-A'], cwd=WORK, check=True)
    subprocess.run(['git', '-c', 'user.email=a@b', '-c', 'user.name=a',
                    'commit', '-qm', 'post-0012'], cwd=WORK, check=True)

    path = os.path.join(WORK, TARGET)
    new_text, block = surgery(open(path, encoding='utf-8').read())
    open(path, 'w', encoding='utf-8').write(new_text)
    open(BLOCK, 'w', encoding='utf-8').write('\n'.join(block) + '\n')
    print('block captured: %d lines -> %s' % (len(block), BLOCK))

    diff = subprocess.run(['git', 'diff', '--', TARGET], cwd=WORK,
                          capture_output=True, text=True).stdout
    # strip the index line: blob hashes of the synthetic file mean nothing
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    open(OUT, 'w', encoding='utf-8').write(diff)

    ctx = [l for l in diff.split('\n') if l.startswith(' ')]
    bad = [l for l in ctx if 'filler' in l]
    added = [l for l in diff.split('\n') if l.startswith('+') and not l.startswith('+++')]
    removed = [l for l in diff.split('\n') if l.startswith('-') and not l.startswith('---')]
    print('patch: %d context, %d removed, %d added | synthetic context leaked: %d'
          % (len(ctx), len(removed), len(added), len(bad)))
    if bad:
        print('  leaked:', bad[:3])

    # final proof: fresh tree, replay the whole series in order
    fresh = WORK + '-verify'
    reset_dir(fresh)
    build(fresh)
    subprocess.run(['git', 'init', '-q'], cwd=fresh, check=True)
    apply(fresh, os.path.abspath(P0003))
    apply(fresh, os.path.abspath(P0012))
    apply(fresh, os.path.abspath(OUT))
    same = open(os.path.join(fresh, TARGET), encoding='utf-8').read() == new_text
    print('replay 0003 -> 0012 -> 0096 reproduces result:', same)
    print('RESULT:', 'OK' if same and not bad else 'FAIL')
    return 0 if same and not bad else 1


if __name__ == '__main__':
    sys.exit(main())
