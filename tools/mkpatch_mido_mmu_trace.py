#!/usr/bin/env python3
"""Generate xnu/patches-mido/0099-mido-mmu-trace.patch.

0098 proved the MMU-off half of the window is healthy: with the old kernel the
panel showed white + green + yellow, i.e. control reached the instruction right
before SCTLR_EL1 is written.  Everything past that point is silent because the
early console is deliberately muted until arm_vm_init() calls
sweet_early_relocate(), and because the low vectors are plain "b ." loops --
an early fault therefore looks exactly like a freeze.

This patch restores the observation path across the MMU boundary:

  * start.s maps the splash once more into the existing V=P L2 table (one 32MB
    Normal-NC block), which is what makes the next two items possible.

  * a fourth breadcrumb (magenta) is drawn immediately after SCTLR_EL1 and the
    isb, still through the physical address.  It can only appear if the MMU
    came up and the V=P mapping is real.

  * the sixteen low vector slots jump to a stack-free painter that fills a red
    block at a fixed spot and halts.  It uses compile-time mido geometry, so it
    works with the MMU on and off.  A red block means "an exception happened",
    which was previously invisible.

Reading the panel:
    white + green + yellow            SCTLR_EL1 or the isb faults
    white + green + yellow + magenta  MMU is up; the fault is past it
    ... + red                         an exception was taken (see x position)

Run from the repository root.
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# files with 0098 applied (out/p98 is the replay tree of the previous patch)
SRC = os.path.join(ROOT, 'out/p98')
WORK = os.path.join(ROOT, 'out/p99')
PATCH = os.path.join(ROOT, 'xnu/patches-mido/0099-mido-mmu-trace.patch')
T_START = 'osfmk/arm64/start.s'


def read(rel):
    with open(os.path.join(WORK, rel), encoding='utf-8') as f:
        return f.read()


def write(rel, text):
    with open(os.path.join(WORK, rel), 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)


SPLASH_MAP = [
    '',
    '\t/*',
    '\t * Splash framebuffer: one 32MB Normal-NC block in the EXISTING V=P L2',
    '\t * table, so that code running with the MMU on can still reach the panel',
    '\t * through its physical address. Both the framebuffer and RAM use L1',
    '\t * index 0, so another create_bootstrap_mapping call would replace the RAM',
    '\t * L2 table; patch the single L2 entry instead.',
    '\t */',
    '\tMOV64\tx10, 0x90000000',
    '\tMOV64\tx11, (ARM_TTE_TYPE_BLOCK | ARM_TTE_VALID |'
    ' ARM_TTE_BLOCK_SH(SH_OUTER_MEMORY) |'
    ' ARM_TTE_BLOCK_ATTRINDX(CACHE_ATTRINDX_WRITECOMB) |'
    ' ARM_TTE_BLOCK_AF | ARM_TTE_BLOCK_PNX | ARM_TTE_BLOCK_NX)',
    '\torr\t\tx11, x11, x10',
    '\tand\t\tx12, x10, #(ARM_TT_L2_INDEX_MASK)',
    '\tlsr\t\tx12, x12, #(ARM_TT_L2_SHIFT)',
    '\tadd\t\tx13, x25, #(2 * PGBYTES)',
    '\tstr\t\tx11, [x13, x12, lsl #TTE_SHIFT]',
]


def crumb(x_dwords, color, what):
    """Breadcrumb block written through the physical address."""
    return [
        '',
        '\t/*',
        '\t * Breadcrumb (%s).' % what[0],
        '\t * %s' % what[1],
        '\t */',
        '\tldr\t\tx9, [x20, #40]\t\t\t/* Video.v_baseAddr */',
        '\tcbz\t\tx9, 1f',
        '\tldr\t\tx4, [x20, #56]\t\t\t/* Video.v_rowBytes */',
        '\tcbz\t\tx4, 1f',
        '\tldr\t\tx2, [x20, #72]\t\t\t/* Video.v_height */',
        '\tcmp\t\tx2, #32',
        '\tb.lo\t1f',
        '\tsub\t\tx2, x2, #32',
        '\tmadd\tx9, x2, x4, x9',
        '\tmov\t\tx3, #%d' % x_dwords,
        '\tadd\t\tx9, x9, x3, lsl #2',
        '\tmov\t\tw1, #%s' % color,
        '\tmov\t\tx2, #32',
        '2:\tmov\t\tx3, #64',
        '\tmov\t\tx5, x9',
        '3:\tstr\t\tw1, [x5], #4',
        '\tsubs\tx3, x3, #1',
        '\tb.ne\t3b',
        '\tadd\t\tx9, x9, x4',
        '\tsubs\tx2, x2, #1',
        '\tb.ne\t2b',
        '1:',
    ]


# The low-vector painter. Fixed geometry for mido so it needs neither a stack
# nor boot_args:
#   base 0x90001000, 1080x1920, 24bpp, stride 3240
#   row   = base + (1920 - 32) * 3240 = 0x905D6A80
#   x=512 px -> +512*3 = 1536 bytes -> 0x905D7080
PAINTER = [
    '',
    '/*',
    ' * Paint a red block and halt. All sixteen low vector slots land here.',
    ' *',
    ' * No stack, no boot_args, no C: this may run before or after the MMU is',
    ' * enabled, so everything is a compile-time constant for the mido panel',
    ' * (1080x1920, stride 3240 bytes, 24bpp):',
    ' *',
    ' *     0x90001000 + (1920 - 32) * 3240 + 512 * 3 = 0x905D7080',
    ' *',
    ' * The splash is mapped V=P in the bootstrap tables, so the same physical',
    ' * address is valid in both worlds.',
    ' */',
    '\t.align\t2',
    'Llow_vector_paint:',
    '\tMOV64\tx9, 0x905D7080',
    '\tmov\t\tw1, #0xFF\t\t\t/* red: R=0xFF in RGB888 */',
    '\tmov\t\tx2, #32',
    '1:\tmov\t\tx3, #64',
    '\tmov\t\tx5, x9',
    '2:\tstr\t\tw1, [x5], #4',
    '\tsubs\tx3, x3, #1',
    '\tb.ne\t2b',
    '\tadd\t\tx9, x9, #3240',
    '\tsubs\tx2, x2, #1',
    '\tb.ne\t1b',
    '\tdsb\t\tsy',
    '3:\twfi',
    '\tb\t\t3b',
]


def patch_start_s():
    src = read(T_START)
    ls = src.split('\n')

    # --- 1. splash V=P block, right after the KVA mapping is built ---
    hits = [i for i, l in enumerate(ls)
            if l.startswith('\tcreate_bootstrap_mapping x14, x15,')]
    assert len(hits) == 1, hits
    at = hits[0]
    ls[at + 1:at + 1] = SPLASH_MAP
    print('start.s: splash V=P block after line %d' % (at + 1))

    # --- 2. magenta breadcrumb after SCTLR_EL1 + isb ---
    hits = [i for i, l in enumerate(ls) if l == '\tMSR_SCTLR_EL1_X0']
    assert len(hits) == 1, hits
    at = hits[0]
    assert ls[at + 1] == '\tisb\t\tsy', repr(ls[at + 1])
    ls[at + 2:at + 2] = crumb(
        288,
        '0x00FF00FF',
        ('the MMU is now on',
         'Still a physical store, so it only lands if the V=P mapping is real.'),
    )
    print('start.s: magenta breadcrumb after line %d' % (at + 2))

    # --- 3. low vectors jump to the painter ---
    hits = [i for i, l in enumerate(ls) if l == 'LEXT(LowExceptionVectorBase)']
    assert len(hits) == 1, hits
    base = hits[0]
    n = 0
    for i in range(base, base + 80):
        if ls[i] == '\tb\t\t.':
            ls[i] = '\tb\t\tLlow_vector_paint'
            n += 1
    assert n == 16, 'expected 16 vector slots, rewrote %d' % n
    print('start.s: rewrote %d low vector slots' % n)

    # painter goes right after the table, i.e. after its closing .align 12
    end = base
    while not (ls[end].strip().startswith('.align')
               and '12' in ls[end]):
        end += 1
        assert end < base + 80, 'vector table end not found'
    ls[end + 1:end + 1] = PAINTER
    print('start.s: painter inserted after line %d' % (end + 1))

    write(T_START, '\n'.join(ls))


def main():
    for d in (WORK,):
        if os.path.isdir(d):
            junk = d + '.junk'
            n = 0
            while os.path.exists(junk):
                n += 1
                junk = d + '.junk%d' % n
            subprocess.run(['mv', d, junk], check=False)
    os.makedirs(os.path.join(WORK, 'osfmk/arm64'))

    with open(os.path.join(SRC, T_START), 'rb') as fi, \
            open(os.path.join(WORK, T_START), 'wb') as fo:
        fo.write(fi.read())

    subprocess.run(['git', 'init', '-q', '.'], cwd=WORK, check=True)
    subprocess.run(['git', 'config', 'core.autocrlf', 'false'], cwd=WORK, check=True)
    subprocess.run(['git', 'add', '-A'], cwd=WORK, check=True)
    subprocess.run(['git', '-c', 'user.email=a@b', '-c', 'user.name=a',
                    'commit', '-qm', 'post-0098'], cwd=WORK, check=True)

    patch_start_s()

    diff = subprocess.run(['git', 'diff'], cwd=WORK, capture_output=True,
                          text=True, check=True).stdout
    assert diff, 'empty diff'
    doc = __doc__.split('\n')
    header = '\n'.join(doc[0:0])  # placeholder, real header below
    header = _header()
    with open(PATCH, 'w', encoding='utf-8', newline='\n') as f:
        f.write(header + '\n' + diff)

    added = sum(1 for l in diff.split('\n') if l.startswith('+') and not l.startswith('+++'))
    removed = sum(1 for l in diff.split('\n') if l.startswith('-') and not l.startswith('---'))
    ctx = sum(1 for l in diff.split('\n') if l.startswith(' '))
    print('patch: %d context, %d removed, %d added -> %s'
          % (ctx, removed, added, os.path.relpath(PATCH, ROOT)))

    # --- replay on a pristine tree ---
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
    return """mido: trace the MMU boundary and let early faults show themselves

0098 showed white + green + yellow, so everything up to the SCTLR_EL1 write is
healthy.  Past that point the machine is silent for two reasons: the early
console is muted until arm_vm_init() relocates it, and the low vectors are
plain "b ." loops, so a fault and a freeze look identical.

Give the kernel a way to speak across the boundary:

  * map the splash into the existing V=P L2 table again (one 32MB Normal-NC
    block), which is what makes the rest possible;

  * draw a fourth breadcrumb (magenta) immediately after SCTLR_EL1 and the
    isb.  It is still a physical store, so it can only land if the MMU came up
    and the V=P mapping is real;

  * point all sixteen low vector slots at a stack-free painter that fills a
    red block at a fixed spot and halts.  It uses compile-time mido geometry,
    so it works with the MMU on and off, and turns "silent freeze" into "an
    exception was taken".

Reading the panel from the bottom band, left to right: white, green, yellow,
magenta, red.
    white only                   page-table build is at fault
    + green                      the MMU enable or the arm_init jump is
    + yellow + magenta           the MMU is up; whatever broke is past it
    + red                        the exception painter ran
"""


if __name__ == '__main__':
    sys.exit(main())
