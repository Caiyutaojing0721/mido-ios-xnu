#!/usr/bin/env python3
"""Generate xnu/patches-mido/0098-mido-early-console-defer.patch.

The kernel dies somewhere between the stage-0 white band (drawn with the MMU
off, straight through the physical address) and the first marker of the early
console.  Static review of the bootstrap page tables says the splash block is
mapped correctly, so stop guessing and change the shape of the experiment:

  * the early console no longer touches the framebuffer until arm_vm_init()
    has installed a real alias and calls sweet_early_relocate() -- so no
    marker can fault any more;

  * start.s drops the now unneeded bootstrap splash mapping and instead draws
    two breadcrumb blocks with the MMU still off:
        green  (x +96 dwords)  page tables and both mappings built
        yellow (x +192)        about to write SCTLR_EL1 (MMU on)

Together with the existing white stage-0 band that brackets the failure to
one of three windows.

Run from the repository root.
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# the two files as they appear in the CI build tree, i.e. with 0097 applied;
# extracted with:
#   tar -xzf xnu/out-mido/buildtree.tgz -C out/ci-src --strip-components=1 \
#       xnu/osfmk/arm64/start.s xnu/osfmk/arm64/sweet_early.c
SRC = os.path.join(ROOT, 'out/ci-src')
WORK = os.path.join(ROOT, 'out/p98')
PATCH = os.path.join(ROOT, 'xnu/patches-mido/0098-mido-early-console.patch')
T_START = 'osfmk/arm64/start.s'
T_EARLY = 'osfmk/arm64/sweet_early.c'

HEADER = """mido: keep the early console off the panel until its mapping is real

The kernel still stops right after the stage-0 white band, which is drawn
with the MMU off through the physical address.  Everything after that writes
the splash through a virtual address, and an unmapped VA there is fatal: the
low vectors are plain "b ." loops, so the machine just freezes.

The bootstrap tables do appear to map the splash block, but reviewing them has
not explained the freeze.  Change the experiment instead of the guess:

  * sweet_early.c keeps quiet until arm_vm_init() has installed the final
    alias for the splash and calls sweet_early_relocate().  Before that no
    code path touches the panel at all, so those markers cannot fault.

  * start.s drops the bootstrap splash mapping (nothing needs it now) and
    draws two breadcrumbs while the MMU is still off, i.e. with plain
    physical stores that cannot fail:
        green   page tables built, V=P and KVA mappings installed
        yellow  immediately before SCTLR_EL1 is written

With the existing white stage-0 band that splits the window in three: white
only means the table build (or something just before it) is at fault, white +
green means the MMU enable or the jump to arm_init() is, and white + green +
yellow with a silent screen means the fault is on the far side of the MMU.

Once the kernel survives, marks 8..20 plus fbcon print normally through the
kernel's own mapping.
"""


def read(rel):
    with open(os.path.join(WORK, rel), encoding='utf-8') as f:
        return f.read()


def write(rel, text):
    with open(os.path.join(WORK, rel), 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)


def lines_of(text):
    return text.split('\n')


def crumb(x_dwords, color, what):
    """Breadcrumb block: physical store, valid with the MMU off."""
    return [
        '',
        '\t/*',
        '\t * Breadcrumb (%s). The MMU is still off, so this writes the' % what,
        '\t * physical address from boot_args and cannot fault.',
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


def patch_start_s():
    src = read(T_START)
    ls = lines_of(src)

    # --- 1. replace the bootstrap splash mapping with a breadcrumb ---
    hits = [i for i, l in enumerate(ls) if 'MOV64\tx10, 0x9c000000' in l or
            'MOV64\tx10, 0x90000000' in l]
    assert len(hits) == 1, hits
    mark = hits[0]

    beg = mark
    while beg > 0 and ls[beg].strip() != '#if defined(SWEET)':
        beg -= 1
    assert ls[beg].strip() == '#if defined(SWEET)', ls[beg]

    end = mark
    while ls[end].strip() != '#endif':
        end += 1
    assert 'dsb' not in ls[end], 'wrong #endif'
    print('start.s: splash block spans lines %d..%d' % (beg + 1, end + 1))

    ls[beg:end + 1] = crumb(96, '0x0000FF00', 'page tables and mappings built')

    # --- 2. breadcrumb right before the MMU is switched on ---
    hits = [i for i, l in enumerate(ls) if l.rstrip() == '\tMOV64   x0, SCTLR_EL1_DEFAULT']
    assert len(hits) == 1, hits
    at = hits[0]
    assert 'Enable caches' in ls[at - 1], ls[at - 1]
    print('start.s: SCTLR_EL1 written at line %d' % (at + 1))

    ls[at:at] = crumb(192, '0x0000FFFF', 'about to enable the MMU')
    write(T_START, '\n'.join(ls))


def patch_sweet_early():
    src = read(T_EARLY)
    ls = lines_of(src)

    # --- flag, declared before sweet_early_put() uses it ---
    hits = [i for i, l in enumerate(ls)
            if l.startswith('static unsigned int early_stride = 3240, early_bpp = 3;')]
    assert len(hits) == 1, hits
    ls[hits[0] + 1:hits[0] + 1] = [
        '',
        '/*',
        ' * The bootstrap page tables are not known to cover the panel, and an',
        ' * unmapped write is fatal this early. Stay silent until arm_vm_init()',
        ' * has installed a real alias and calls sweet_early_relocate().',
        ' */',
        'static unsigned int early_fb_live;',
    ]

    # --- guard the single place that dereferences the framebuffer ---
    old = [
        '\tvolatile uint8_t *p = EARLY_FB + (uintptr_t)y * EARLY_STRIDE +',
        '\t    (uintptr_t)x * early_bpp;',
    ]
    hits = [i for i in range(len(ls) - 1) if ls[i:i + 2] == old]
    assert len(hits) == 1, hits
    ls[hits[0]:hits[0] + 2] = [
        '\tvolatile uint8_t *p;',
        '',
        '\tif (!early_fb_live) {',
        '\t\treturn;',
        '\t}',
        '\tp = EARLY_FB + (uintptr_t)y * EARLY_STRIDE + (uintptr_t)x * early_bpp;',
    ]

    # --- and make the two public entry points no-ops until then ---
    for want, repl in (
        ('\tif (early_disabled) {', '\tif (early_disabled || !early_fb_live) {'),
        ('\tif (early_disabled || stage > 20) {',
         '\tif (early_disabled || !early_fb_live || stage > 20) {'),
    ):
        hits = [i for i, l in enumerate(ls) if l == want]
        assert len(hits) == 1, (want, hits)
        ls[hits[0]] = repl

    # --- relocate: switch the base, arm the console, then draw ---
    beg = None
    for i, l in enumerate(ls):
        if l == 'sweet_early_relocate(uint64_t base)':
            beg = i
            break
    assert beg is not None
    body = [
        'sweet_early_relocate(uint64_t base)',
        '{',
        '\tearly_fb = (volatile uint8_t *)(uintptr_t)base;',
        '\tearly_row = 0;',
        '\tearly_col = 0;',
        '\tearly_started = 0;',
        '\tearly_fb_live = 1;',
        '\t__asm__ volatile("dsb sy" ::: "memory");',
        '\tsweet_early_mark(8, "leaving bootstrap TTBR0");',
        '\tsweet_early_mark(9, "kernel framebuffer VA live");',
        '}',
    ]
    # replace the existing body of relocate()
    end = beg + 1
    while ls[end] != '}':
        end += 1
    ls[beg:end + 1] = body
    write(T_EARLY, '\n'.join(ls))


def main():
    if os.path.isdir(WORK):
        junk = WORK + '.junk'
        n = 0
        while os.path.exists(junk):
            n += 1
            junk = WORK + '.junk%d' % n
        subprocess.run(['mv', WORK, junk], check=False)
    os.makedirs(os.path.join(WORK, 'osfmk/arm64'))

    for rel in (T_START, T_EARLY):
        with open(os.path.join(SRC, rel), 'rb') as fi, \
                open(os.path.join(WORK, rel), 'wb') as fo:
            fo.write(fi.read())

    subprocess.run(['git', 'init', '-q', '.'], cwd=WORK, check=True)
    subprocess.run(['git', 'config', 'core.autocrlf', 'false'], cwd=WORK, check=True)
    subprocess.run(['git', 'add', '-A'], cwd=WORK, check=True)
    subprocess.run(['git', '-c', 'user.email=a@b', '-c', 'user.name=a',
                    'commit', '-qm', 'post-0097'], cwd=WORK, check=True)

    patch_start_s()
    patch_sweet_early()

    diff = subprocess.run(['git', 'diff'], cwd=WORK, capture_output=True,
                          text=True, check=True).stdout
    assert diff, 'empty diff'
    with open(PATCH, 'w', encoding='utf-8', newline='\n') as f:
        f.write(HEADER + '\n' + diff)

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
    for rel in (T_START, T_EARLY):
        with open(os.path.join(SRC, rel), 'rb') as fi, \
                open(os.path.join(fresh, rel), 'wb') as fo:
            fo.write(fi.read())
    # must be its own repo, otherwise git applies against the enclosing one
    subprocess.run(['git', 'init', '-q', '.'], cwd=fresh, check=True)
    subprocess.run(['git', 'config', 'core.autocrlf', 'false'], cwd=fresh, check=True)
    r = subprocess.run(['git', 'apply', '--check', '-v', PATCH], cwd=fresh,
                       capture_output=True, text=True)
    print('git apply --check:', 'OK' if r.returncode == 0 else 'FAIL')
    if r.returncode != 0:
        print(r.stderr[-2000:])
        return 1
    subprocess.run(['git', 'apply', PATCH], cwd=fresh, check=True)
    same = True
    for rel in (T_START, T_EARLY):
        a = open(os.path.join(fresh, rel), 'rb').read()
        b = open(os.path.join(WORK, rel), 'rb').read()
        ok = a == b
        same = same and ok
        print('  %-28s replay identical: %s' % (rel, ok))
    print('RESULT:', 'OK' if same else 'MISMATCH')
    return 0 if same else 1


if __name__ == '__main__':
    sys.exit(main())
