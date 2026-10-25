#!/usr/bin/env python3
"""Generate xnu/patches-mido/0103-mido-4k-va-and-page-size.patch.

0102 moved the port to 4KB pages, which is what the Cortex-A53 can actually do,
and the very next CI run failed to compile:

    osfmk/arm/pmap/pmap.c:10988:1: error: static assertion failed ...
    static_assert((_COMM_PAGE64_BASE_ADDRESS & ~ARM_TT_L1_OFFMASK) >= MACH_VM_MAX_ADDRESS);
    expression evaluates to '67645734912 >= 140737454800896'

That assert only exists in the `ARM_PGSHIFT == 12` branch of pmap.c (the 16KB
branch skips it with a comment saying the invariant is not true on macOS).  It
is XNU asking for one thing: with 4KB pages the comm page must be nested
*above* the maximum userspace VA.  On iOS that holds because user space is
capped at `_COMM_PAGE64_NESTING_START` = 0x0000000FC0000000 (63GB) and the comm
page lives at 64GB - 16KB.  Our build gets the macOS cap instead
(0x00007FFFFE000000, 128TB), because dxb builds with PLATFORM=MacOSX and
vm_param.h keys the choice off `XNU_PLATFORM_MacOSX`.

So two things are wrong for a 4KB mido kernel, and both are address-space
assumptions inherited from the macOS host build:

  1. osfmk/mach/arm/vm_param.h: MACH_VM_MAX_ADDRESS_RAW must be the iOS value
     (63GB).  The arm64 block of this header is arm-only, so x86_64 kernels in
     the same CI run are untouched.  Note that 128TB could never be described
     anyway: 4KB pages give T0SZ = 25, i.e. 512GB of user VA.

  2. osfmk/arm/arm_init.c: `PAGE_SHIFT_CONST` is chosen at runtime and the
     stock rule advertises 16KB on any arm64 device with more than 1GB of RAM.
     That is fine when the kernel was compiled with __ARM_16K_PG__ (then
     PAGE_SHIFT is pinned to ARM_PGSHIFT anyway), but mido is now compiled for
     4KB: PAGE_SHIFT follows PAGE_SHIFT_CONST, so a 3GB phone would report
     PAGE_SIZE 16384 while ARM_PGBYTES is 4096 and every page-table index,
     offset and rounding would disagree.  Pin it to ARM_PGSHIFT.

This is a compile-time plus early-boot fix only; it changes no device register
and no panel behaviour.  Combined with 0102 the kernel build should finally get
past pmap.c and the 0099/0100/0101 breadcrumbs should be joined by real console
output.

Run from the repository root.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'out/mido4kfix-src/xnu')   # post-0004/0010/0012 files
WORK = os.path.join(ROOT, 'out/mido4kfix')
PATCH = os.path.join(ROOT, 'xnu/patches-mido/0103-mido-4k-va-and-page-size.patch')

FILES = [
    ('osfmk/mach/arm/vm_param.h', os.path.join(SRC, 'osfmk/mach/arm/vm_param.h')),
    ('osfmk/arm/arm_init.c', os.path.join(SRC, 'osfmk/arm/arm_init.c')),
]

OLD_VA = """/* system-wide values */
#define MACH_VM_MIN_ADDRESS_RAW 0x0ULL
#if defined(XNU_PLATFORM_MacOSX) || defined(XNU_PLATFORM_DriverKit)
#define MACH_VM_MAX_ADDRESS_RAW 0x00007FFFFE000000ULL
#else
#define MACH_VM_MAX_ADDRESS_RAW 0x0000000FC0000000ULL
#endif
"""

NEW_VA = """/* system-wide values */
#define MACH_VM_MIN_ADDRESS_RAW 0x0ULL
/*
 * mido: keep the iOS cap even though dxb builds with PLATFORM=MacOSX.
 *
 * The macOS value (0x00007FFFFE000000, a 47-bit / 128TB user VA) is only
 * describable through a 47-bit TTBR0.  This port runs 4KB pages, so TCR_EL1
 * T0SZ = 25 and user space ends at 512GB; worse, pmap.c asserts -- in its
 * ARM_PGSHIFT == 12 branch only -- that the comm page nests above the maximum
 * user VA, and with the macOS cap that assertion cannot hold:
 *
 *   (_COMM_PAGE64_BASE_ADDRESS & ~ARM_TT_L1_OFFMASK) >= MACH_VM_MAX_ADDRESS
 *   (63GB)                                           >= (128TB)   -- false
 *
 * iOS satisfies it by capping user space at _COMM_PAGE64_NESTING_START
 * (0x0000000FC0000000 == 63GB) and putting the comm page at 64GB - 16KB.
 * Keep that value for every arm64 kernel built here.
 */
#define MACH_VM_MAX_ADDRESS_RAW 0x0000000FC0000000ULL
"""

OLD_PG = """\t\tif (args->memSize > 1ULL * 1024 * 1024 * 1024) {
\t\t\t/*
\t\t\t * arm64 device with > 1GB of RAM:
\t\t\t * kernel uses 16KB pages.
\t\t\t */
\t\t\tPAGE_SHIFT_CONST = PAGE_MAX_SHIFT;
\t\t} else {
\t\t\t/*
\t\t\t * arm64 device with <= 1GB of RAM:
\t\t\t * kernel uses hardware page size
\t\t\t * (4KB for H6/H7, 16KB for H8+).
\t\t\t */
\t\t\tPAGE_SHIFT_CONST = ARM_PGSHIFT;
\t\t}
"""

NEW_PG = """\t\t/*
\t\t * mido: always advertise the granule the kernel was compiled for.
\t\t *
\t\t * Stock XNU reports 16KB on every arm64 device with more than 1GB of
\t\t * RAM, which is safe only because every Apple arm64 SoC implements a
\t\t * 16KB granule (and those builds pin PAGE_SHIFT to ARM_PGSHIFT through
\t\t * __ARM_16K_PG__).  mido has neither: the Cortex-A53 has no TGran16, so
\t\t * the kernel is compiled for 4KB tables and PAGE_SHIFT follows this
\t\t * variable.  Reporting 16KB here would make PAGE_SIZE (16384) disagree
\t\t * with ARM_PGBYTES (4096) and poison every page-table index, offset and
\t\t * rounding operation.
\t\t */
\t\tPAGE_SHIFT_CONST = ARM_PGSHIFT;
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
    return """mido: iOS user-VA cap and a fixed 4KB kernel page size

0102 switched the port to 4KB pages -- the only granule a Cortex-A53 has -- and
the next CI run died in pmap.c:

    error: static assertion failed: ((0x0000000FFFFFC000ULL) & ~0x000000003fffffffULL) >= ((mach_vm_offset_t) 0x00007FFFFE000000ULL)
    static_assert((_COMM_PAGE64_BASE_ADDRESS & ~ARM_TT_L1_OFFMASK) >= MACH_VM_MAX_ADDRESS);

Both offenders are address-space assumptions inherited from the macOS host
build, not from the hardware.

  * osfmk/mach/arm/vm_param.h: MACH_VM_MAX_ADDRESS_RAW comes out as the macOS
    128TB value because dxb builds with PLATFORM=MacOSX, and the header keys
    that choice off `XNU_PLATFORM_MacOSX`.  The assert above only exists in the
    ARM_PGSHIFT == 12 branch, and it wants the comm page (64GB - 16KB) nested
    above user space; iOS manages that by capping user VA at 63GB
    (_COMM_PAGE64_NESTING_START).  128TB is unreachable here anyway: 4KB pages
    give T0SZ = 25, i.e. 512GB.  Use the iOS value unconditionally; the block
    is arm64-only, so the x86_64 kernels built in the same run are unaffected.

  * osfmk/arm/arm_init.c: PAGE_SHIFT_CONST is picked at runtime, and stock XNU
    advertises 16KB on any arm64 device with more than 1GB of RAM.  That is
    harmless when the kernel is compiled with __ARM_16K_PG__, because PAGE_SHIFT
    is then pinned to ARM_PGSHIFT, but mido is now a 4KB build and PAGE_SHIFT
    follows the variable.  On a 3GB phone PAGE_SIZE would read 16384 while
    ARM_PGBYTES is 4096, and every index, offset and rounding in the pmap would
    be wrong.  Pin PAGE_SHIFT_CONST to ARM_PGSHIFT.

No device register and no panel behaviour changes here.  Together with 0102 the
build should clear pmap.c, and the 0099/0100/0101 breadcrumbs should be followed
by the 0098 early console instead of silence.
"""


def main():
    seed(WORK, 'post-0004')
    edit(WORK, 'osfmk/mach/arm/vm_param.h', OLD_VA, NEW_VA,
         'arm64 user VA cap: 63GB, not the macOS 128TB')
    edit(WORK, 'osfmk/arm/arm_init.c', OLD_PG, NEW_PG,
         'PAGE_SHIFT_CONST: 4KB, not "16KB if RAM > 1GB"')

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
    seed(fresh, 'pre-0103')
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
