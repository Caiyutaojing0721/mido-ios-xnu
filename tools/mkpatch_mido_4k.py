#!/usr/bin/env python3
"""Generate xnu/patches-mido/0102-mido-4k-pages.patch.

The mido panel never lit up past the yellow breadcrumb, and the reason is a
hardware fact, not a bug in our probe: **the Cortex-A53 has no 16KB translation
granule.**  Its ID_AA64MMFR0_EL1.TGran16 field is 0 by design (ARM DDI 0500
4.2.1; Aleph Security hit exactly this wall porting iOS to a Rock64 and had to
abandon A53 hardware for it).  With TCR_EL1.TG0/TG1 = 0b10 the PE cannot build
translations, so the first store after `MSR SCTLR_EL1` faults, the vector fetch
faults too, and the panel simply stops -- precisely "white + green + yellow".

santoni's payload, which does boot XNU on this class of core, is a 4KB build.
Our machine config (from sweet, a Kryo 470 board) has `__ARM_16K_PG__ 1`.

Two things have to change together, because XNU's 4KB configuration only spans
a 39-bit VA (see the table in osfmk/arm64/proc_reg.h):

  1. drop `__ARM_16K_PG__` from the SWEET board config  -> PGBYTES 4096,
     TCR_EL1 TG0/TG1 = 4KB, T0SZ/T1SZ = 25.

  2. stop using the macOS ARM_LARGE_MEMORY layout.  With ARM_LARGE_MEMORY the
     kernel links at 0xfffffe0007004000, i.e. -2TB, which needs 41 bits of VA
     and therefore cannot be reached through a 39-bit TTBR1.  Put the kernel
     back at the classic iOS base 0xfffffff007004000 (-64GB), which is also
     where santoni lives (payload virt_base 0xfffffff006000000).

  3. the splash V=P mapping in start.s assumed one 32MB block covered the whole
     panel buffer.  With 2MB blocks it does not: the buffer is 0x90001000 +
     0x5EEC00 = 5.9MB, so map four consecutive blocks (8MB).

Everything downstream adapts by itself: mkpayload takes virt_base from the
Mach-O __TEXT vmaddr, the shim computes PA from the payload header, and no shim
or tool has a kernel VA hardcoded.

Run from the repository root.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'out/mido4k-src/xnu')   # post-0004 files from the CI tree
P101 = os.path.join(ROOT, 'out/p101')            # start.s with 0098..0101 applied
WORK = os.path.join(ROOT, 'out/mido4k')
PATCH = os.path.join(ROOT, 'xnu/patches-mido/0102-mido-4k-pages.patch')

FILES = [
    ('makedefs/MakeInc.def', os.path.join(SRC, 'makedefs/MakeInc.def')),
    ('pexpert/pexpert/arm64/SWEET.h', os.path.join(SRC, 'pexpert/pexpert/arm64/SWEET.h')),
    ('osfmk/arm64/start.s', os.path.join(P101, 'osfmk/arm64/start.s')),
]

OLD_MEM = """LARGE_MEMORY_DEFINE=-UARM_LARGE_MEMORY
ARM64_PLKSEG_ADDR  =0xfffffff004004000
ARM64_LINK_ADDR    =0xfffffff007004000

# Use ARM_LARGE_MEMORY config for all MacOSX targets.
ifneq ($(filter $(PLATFORM),MacOSX),)
LARGE_MEMORY_DEFINE=-DARM_LARGE_MEMORY=1
ARM64_PLKSEG_ADDR  =0xfffffe0004004000
ARM64_LINK_ADDR    =0xfffffe0007004000
endif
"""

NEW_MEM = """LARGE_MEMORY_DEFINE=-UARM_LARGE_MEMORY
ARM64_PLKSEG_ADDR  =0xfffffff004004000
ARM64_LINK_ADDR    =0xfffffff007004000

# mido: do NOT use the macOS ARM_LARGE_MEMORY layout here.
#
# ARM_LARGE_MEMORY links the kernel at 0xfffffe0007004000 (-2TB), which needs a
# 41-bit kernel VA.  XNU's 4KB-page configuration only describes a 39-bit range
# (T0SZ/T1SZ = 25), so TTBR1 could never reach it.  mido has to run 4KB pages
# because the Cortex-A53 implements no 16KB translation granule, so the kernel
# goes back to the classic iOS base at -64GB, which is where the working
# santoni payload (virt_base 0xfffffff006000000) lives too.
#
# The MacOSX override below is deliberately gone; this patch is only ever
# applied by the mido workflow.
"""

OLD_16K = """#define __ARM_PAN_AVAILABLE__     1
#define __ARM_16K_PG__            1
"""

NEW_16K = """#define __ARM_PAN_AVAILABLE__     1
/*
 * No __ARM_16K_PG__ here.
 *
 * The Cortex-A53 (SDM8953) does not implement the 16KB translation granule:
 * ID_AA64MMFR0_EL1.TGran16 reads 0 by design (ARM DDI 0500, 4.2.1), SoC
 * designers cannot enable it.  Programming TCR_EL1.TG0/TG1 = 0b10 therefore
 * leaves every translation faulting, and the first store after
 * `MSR SCTLR_EL1` takes a data abort whose vector fetch also faults -- the
 * kernel dies silently right after the last pre-MMU breadcrumb.
 *
 * Defining this back for mido reproduces exactly that hang.
 */
"""

OLD_SPLASH = """\tand\t\tx12, x10, #(ARM_TT_L2_INDEX_MASK)
\tlsr\t\tx12, x12, #(ARM_TT_L2_SHIFT)
\tadd\t\tx13, x25, #(2 * PGBYTES)
\tstr\t\tx11, [x13, x12, lsl #TTE_SHIFT]
"""

NEW_SPLASH = """\tand\t\tx12, x10, #(ARM_TT_L2_INDEX_MASK)
\tlsr\t\tx12, x12, #(ARM_TT_L2_SHIFT)
\tadd\t\tx13, x25, #(2 * PGBYTES)
\tadd\t\tx13, x13, x12, lsl #TTE_SHIFT
\t/*
\t * A block is ARM_TT_L2_SIZE (32MB with 16KB pages, 2MB with 4KB pages).
\t * mido runs 4KB pages and its panel buffer is 0x90001000 + 0x5EEC00, so
\t * one block is not enough: map four consecutive blocks, 0x90000000 to
\t * 0x907FFFFF.  That also covers the breadcrumb band at 0x905D6700.
\t */
\tmov\t\tx16, #(ARM_TT_L2_SIZE)
\tstr\t\tx11, [x13]
\tadd\t\tx11, x11, x16
\tstr\t\tx11, [x13, #(1 << TTE_SHIFT)]
\tadd\t\tx11, x11, x16
\tstr\t\tx11, [x13, #(2 << TTE_SHIFT)]
\tadd\t\tx11, x11, x16
\tstr\t\tx11, [x13, #(3 << TTE_SHIFT)]
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
    print('%-32s %s' % (rel, tag))


def main():
    seed(WORK, 'post-0004')
    edit(WORK, 'makedefs/MakeInc.def', OLD_MEM, NEW_MEM,
         'drop the MacOSX ARM_LARGE_MEMORY override')
    edit(WORK, 'pexpert/pexpert/arm64/SWEET.h', OLD_16K, NEW_16K,
         'drop __ARM_16K_PG__ (A53 has no 16K granule)')
    edit(WORK, 'osfmk/arm64/start.s', OLD_SPLASH, NEW_SPLASH,
         'splash V=P: 4 x 2MB blocks instead of 1 x 32MB')

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
    seed(fresh, 'pre-0102')
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


def _header():
    return """mido: 4KB pages and the classic iOS kernel base (Cortex-A53 has no 16K granule)

The panel stopped right after the yellow breadcrumb, i.e. at `MSR SCTLR_EL1`,
because the Cortex-A53 implements no 16KB translation granule: its
ID_AA64MMFR0_EL1.TGran16 field is 0 by design (ARM DDI 0500 4.2.1).  With
TCR_EL1.TG0/TG1 = 0b10 every translation faults, the first store after the MMU
enable aborts, and the vector fetch aborts as well, so nothing is ever printed.
The santoni payload that does boot XNU on the same core class is a 4KB build.

Three changes, which only make sense together:

  * pexpert/pexpert/arm64/SWEET.h: drop `__ARM_16K_PG__`.  PGBYTES becomes 4096
    and TCR_EL1 gets TG0/TG1 = 4KB with T0SZ/T1SZ = 25.

  * makedefs/MakeInc.def: drop the MacOSX `-DARM_LARGE_MEMORY=1` override.
    ARM_LARGE_MEMORY links at 0xfffffe0007004000 (-2TB), which needs 41 bits of
    kernel VA; the 4KB configuration only describes 39 bits.  The kernel goes
    back to 0xfffffff007004000 (-64GB), the layout the working santoni payload
    uses (virt_base 0xfffffff006000000).

  * osfmk/arm64/start.s: the splash V=P mapping inserted a single block, which
    covered 32MB with 16KB pages but only 2MB with 4KB pages.  The panel buffer
    is 0x90001000 + 0x5EEC00, so map four consecutive blocks (8MB), which also
    covers the breadcrumb band at 0x905D6700.

Everything else follows: mkpayload derives virt_base from the Mach-O __TEXT
vmaddr, the shim computes PA from the payload header, and neither has a kernel
VA hardcoded.

Expected panel after this: white + green + yellow + magenta (constant V=P
crumbs) and then the 0098 early console text, because with a working MMU the
kernel finally reaches arm_vm_init.
"""


if __name__ == '__main__':
    sys.exit(main())
