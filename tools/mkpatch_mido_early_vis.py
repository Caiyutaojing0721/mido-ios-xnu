#!/usr/bin/env python3
"""Generate xnu/patches-mido/0104-mido-early-visible.patch.

The 0103 kernel finally gets past SCTLR_EL1 on mido: the panel shows all five
breadcrumbs (white from the shim, then green / yellow from 0098 before the MMU,
then magenta / cyan from 0100 after it).  Nothing else ever appears -- no
"SWEET EARLY" line, no panic text, no reboot.

That silence is deliberate, not a bug in the console: 0098 made sweet_early.c
stay quiet until arm_vm_init() calls sweet_early_relocate(), because back then
the bootstrap V=P mapping of the panel was an unverified guess and an unmapped
store would have been fatal.  The five breadcrumbs now prove the mapping is
real: start.s writes 0x905D6B80 and 0x905D6D00 through it with the MMU on and
both land.  So the blanket silence is now pure information loss -- every hang
between the jump out of start.s and arm_vm_init() looks identical.

This patch replaces the guess with the measurement:

  * sweet_early.c drops the early_fb_live gate on put/putc/mark.  Before
    relocation the console writes through the bootstrap V=P mapping that
    start.s installs (0x90000000-0x907FFFFF, four 2MB blocks at 4KB pages);
    after relocation it uses the kernel's own alias as before.  Panic text is
    visible throughout, which matters because PE_init_platform() panics on a
    bad ADT and that panic is currently invisible.

  * sweet_early_mark() draws its band one row above the start.s crumbs
    (y = HEIGHT-64 instead of HEIGHT-32) so the two sets never overlap: the
    bottom row stays white/green/yellow/magenta/cyan, the row above counts
    arm_init's own progress.

  * arm_init.c marks stage 0 *before* boot_args is dereferenced.  That splits
    the very first question in two: stage 0 but no stage 1 means the jump out
    of start.s landed and boot_args is bad, while no stage 0 at all means the
    jump itself (the KVA bootstrap mapping) never arrived.

Expected readings, bottom row present in all cases:

  no stage 0            -> the ret into arm_init() never lands (KVA tables)
  0 but no 1            -> boot_args unreachable / garbage
  1 but no 2            -> PE_init_platform(), i.e. the ADT
  2 .. 7 with gaps      -> the named arm_init step in the gap
  7 but no 8            -> arm_vm_init()'s page-table rebuild, pre-relocation
  8 and up              -> the kernel's own panel alias works

Run from the repository root.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'out/midoearly-src/xnu')     # post-0098/0101/0103 files
WORK = os.path.join(ROOT, 'out/midoearly')
PATCH = os.path.join(ROOT, 'xnu/patches-mido/0104-mido-early-visible.patch')

FILES = [
    ('osfmk/arm64/sweet_early.c', os.path.join(SRC, 'osfmk/arm64/sweet_early.c')),
    ('osfmk/arm/arm_init.c', os.path.join(SRC, 'osfmk/arm/arm_init.c')),
]

# ---------------------------------------------------------------- sweet_early

OLD_NOTE = """/*
 * The bootstrap page tables are not known to cover the panel, and an
 * unmapped write is fatal this early. Stay silent until arm_vm_init()
 * has installed a real alias and calls sweet_early_relocate().
 */
static unsigned int early_fb_live;
"""

NEW_NOTE = """/*
 * early_fb_live is set by sweet_early_relocate() once arm_vm_init() has
 * installed the kernel's own alias for the panel.
 *
 * Before relocation the console goes through the bootstrap V=P mapping that
 * start.s builds (0x90000000-0x907FFFFF, four 2MB blocks at 4KB pages).
 * 0098 stayed silent because that mapping was then an unverified guess and an
 * unmapped store is fatal this early.  The start.s breadcrumbs at 0x905D6B80
 * and 0x905D6D00 are written through exactly that mapping with the MMU on, so
 * it is now a measured fact rather than a guess -- and staying silent only
 * makes every hang between start.s and arm_vm_init() look the same.
 *
 * The pre-relocation band is drawn one row above the start.s crumbs so the two
 * sets stay readable apart.
 */
static unsigned int early_fb_live;
"""

OLD_PUT = """	volatile uint8_t *p;

	if (!early_fb_live) {
		return;
	}
	p = EARLY_FB + (uintptr_t)y * EARLY_STRIDE + (uintptr_t)x * early_bpp;
"""

NEW_PUT = """	volatile uint8_t *p;

	p = EARLY_FB + (uintptr_t)y * EARLY_STRIDE + (uintptr_t)x * early_bpp;
"""

OLD_PUTC = """	if (early_disabled || !early_fb_live) {
		return;
	}
"""

NEW_PUTC = """	if (early_disabled) {
		return;
	}
"""

OLD_MARK = """	if (early_disabled || !early_fb_live || stage > 20) {
		return;
	}
	uint32_t color = 0xff00ff00u | ((stage & 7u) * 32u << 16);
	for (unsigned int y = EARLY_HEIGHT - 32; y < EARLY_HEIGHT; y++) {
		for (unsigned int x = stage * 48; x < (stage + 1) * 48; x++) {
			sweet_early_put(x, y, color);
		}
	}
"""

NEW_MARK = """	if (early_disabled || stage > 20) {
		return;
	}
	uint32_t color = 0xff00ff00u | ((stage & 7u) * 32u << 16);
	/* The bottom 32 rows belong to the start.s crumbs; stay above them. */
	for (unsigned int y = EARLY_HEIGHT - 64; y < EARLY_HEIGHT - 32; y++) {
		for (unsigned int x = stage * 48; x < (stage + 1) * 48; x++) {
			sweet_early_put(x, y, color);
		}
	}
"""

# ------------------------------------------------------------------- arm_init

OLD_INIT = """	sweet_early_init(args);
	sweet_early_mark(1, "arm_init entered (MMU on)");
"""

NEW_INIT = """	/*
	 * Stage 0 runs before boot_args is touched.  With the KVA alias live but
	 * the panel reachable only through the bootstrap V=P mapping, "0 visible,
	 * 1 missing" means the jump out of start.s landed and boot_args is what
	 * faults, while "nothing at all" means the jump never arrived.
	 */
	sweet_early_mark(0, "arm_init reached, boot_args untouched");
	sweet_early_init(args);
	sweet_early_mark(1, "arm_init entered (MMU on)");
"""


def archive(path):
    if os.path.isdir(path):
        junk, n = path + '.junk', 0
        while os.path.exists(junk):
            n += 1
            junk = path + '.junk%d' % n
        subprocess.run(['mv', path, junk], check=False)


def seed(path, commit):
    archive(path)
    for rel, src in FILES:
        dst = os.path.join(path, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(src, 'rb') as fi, open(dst, 'wb') as fo:
            fo.write(fi.read())
    subprocess.run(['git', 'init', '-q', '.'], cwd=path, check=True)
    subprocess.run(['git', 'config', 'core.autocrlf', 'false'], cwd=path, check=True)
    subprocess.run(['git', 'add', '-A'], cwd=path, check=True)
    subprocess.run(['git', '-c', 'user.email=a@b', '-c', 'user.name=a',
                    'commit', '-qm', commit], cwd=path, check=True)


def edit(path, rel, old, new, tag):
    p = os.path.join(path, rel)
    with open(p, encoding='utf-8') as f:
        text = f.read()
    assert text.count(old) == 1, (tag, text.count(old))
    with open(p, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text.replace(old, new))
    print('%-34s %s' % (rel, tag))


def _header():
    return """mido: make the early console visible before arm_vm_init

The 0103 kernel gets past SCTLR_EL1: the panel shows all five breadcrumbs --
white from the shim, green and yellow from 0098 before the MMU, magenta and
cyan from 0100 after it -- and then nothing, forever.

That silence is 0098's doing.  It gated sweet_early.c on early_fb_live, which
only sweet_early_relocate() sets, and that call sits deep inside arm_vm_init().
The gate was correct when the bootstrap V=P mapping of the panel was an
unverified guess: a store through an unmapped VA at that point is fatal and
would have been indistinguishable from the fault the probe was looking for.

The breadcrumbs have since settled that question.  start.s writes 0x905D6B80
and 0x905D6D00 through the bootstrap mapping with the MMU already on, and both
land on screen.  The mapping is real, so the blanket silence now only destroys
information: a fault in the jump out of start.s, in boot_args, in
PE_init_platform()'s ADT walk and in arm_vm_init()'s page-table rebuild all
produce the identical picture.

  * sweet_early.c: drop the early_fb_live gate from put/putc/mark.  Before
    relocation the console uses the bootstrap V=P mapping; afterwards it uses
    the kernel alias exactly as before.  Panic output is visible throughout,
    which matters because PE_init_platform() panics on a malformed ADT and
    that panic is currently invisible.

  * sweet_early_mark(): draw the band one row above the start.s crumbs
    (y = HEIGHT-64 rather than HEIGHT-32), so the bottom row keeps the five
    start.s colours and the row above counts arm_init's own progress.

  * arm_init.c: mark stage 0 before boot_args is dereferenced.  Stage 0
    without stage 1 means the jump landed and boot_args is what faults; no
    stage 0 at all means the jump -- the KVA bootstrap mapping -- never
    arrived.

Read the bottom row as the start.s status and the row above as arm_init's
progress: no stage 0 means the KVA tables are wrong, 0 without 1 means
boot_args is unreachable, 1 without 2 means the ADT, 7 without 8 means
arm_vm_init()'s rebuild, and 8 or more means the kernel's own panel alias is
live.
"""


def main():
    seed(WORK, 'post-0103')
    edit(WORK, 'osfmk/arm64/sweet_early.c', OLD_NOTE, NEW_NOTE,
         'note: the V=P mapping is measured, not guessed')
    edit(WORK, 'osfmk/arm64/sweet_early.c', OLD_PUT, NEW_PUT,
         'put(): drop the early_fb_live gate')
    edit(WORK, 'osfmk/arm64/sweet_early.c', OLD_PUTC, NEW_PUTC,
         'putc(): drop the early_fb_live gate')
    edit(WORK, 'osfmk/arm64/sweet_early.c', OLD_MARK, NEW_MARK,
         'mark(): drop the gate, move the band up one row')
    edit(WORK, 'osfmk/arm/arm_init.c', OLD_INIT, NEW_INIT,
         'stage 0 before boot_args is dereferenced')

    diff = subprocess.run(['git', 'diff'], cwd=WORK, capture_output=True,
                          text=True, check=True).stdout
    assert diff, 'empty diff'
    with open(PATCH, 'w', encoding='utf-8', newline='\n') as f:
        f.write(_header() + '\n' + diff)
    added = sum(1 for l in diff.split('\n') if l.startswith('+') and not l.startswith('+++'))
    removed = sum(1 for l in diff.split('\n') if l.startswith('-') and not l.startswith('---'))
    print('patch: %d removed, %d added -> %s'
          % (removed, added, os.path.relpath(PATCH, ROOT)))

    fresh = WORK + '-verify'
    seed(fresh, 'pre-0104')
    r = subprocess.run(['git', 'apply', '--check', '-v', PATCH], cwd=fresh,
                       capture_output=True, text=True)
    print('git apply --check:', 'OK' if r.returncode == 0 else 'FAIL')
    if r.returncode != 0:
        print(r.stderr[-2000:])
        return 1
    subprocess.run(['git', 'apply', PATCH], cwd=fresh, check=True)
    same = all(open(os.path.join(fresh, rel), 'rb').read() ==
               open(os.path.join(WORK, rel), 'rb').read() for rel, _ in FILES)
    print('replay identical:', same)
    return 0 if same else 1


if __name__ == '__main__':
    sys.exit(main())
