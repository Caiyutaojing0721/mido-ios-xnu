#!/usr/bin/env python3
"""Generate xnu/patches-mido/0106-mido-armv8-baseline.patch.

0105 finally printed the undefined-instruction context, and it named the culprit
exactly:

    UNDEF pc=0xfffffff0072b55b0  instr=0xb8280028  esr=0x2000000

    0xfffffff0072b55b0 -> _lck_grp_reference (+0x8)

That pc is inside __TEXT_EXEC and the word in the Mach-O file at that address is
0xb8280028 as well, so memory and image agree: the kernel really was compiled
with this instruction.  0xb8280028 is an ARMv8.1 LSE atomic --

    __atomic_add_fetch(p, 1, __ATOMIC_RELAXED) -> 52800028  mov   w8, #1
                                                   b8280008  ldadd w8, w8, [x0]

-- so 0xb8280028 is `ldadd w8, w8, [x1]`, which is exactly what the source of
that function asks for:

    lck_grp_reference(lck_grp_t *grp, uint32_t *cnt)
    {
            if (cnt) {
                    os_atomic_inc(cnt, relaxed);      /* <- ldadd w8, w8, [x1] */
            }
            if (grp->lck_grp_attr_id & LCK_GRP_ATTR_ALLOCATED) {   /* ldr w8,[x0,#4]; tbnz */
                    os_ref_retain_raw(&grp->lck_grp_refcnt, NULL);
            }
    }

The instruction is legal ARMv8.1, but this board is a Cortex-A53: an
ARMv8.0-A core with no LSE, no CRC, no FP16 arithmetic.  Executing `ldadd`
there is an UNDEFINED instruction, which is precisely the ESR we got.  The
first reference-count bump during boot was the first LSE site to run, so the
kernel died right after the stage-11 mark.

The source of the flag is makedefs/MakeInc.def, added by the SWEET machine
config (0004):

    MACHINE_FLAGS_ARM64_SWEET = -DARM64_BOARD_CONFIG_SWEET  -march=armv8.2-a+crypto

SWEET (SM7150) and polaris (SDM845) are ARMv8.2 cores and can honour it; the
A53 cannot.  Nothing in xnu selects LSE conditionally -- there is no
__ARM_FEATURE_ATOMICS guard anywhere in the tree -- so the only lever is the
target architecture.  Pin the baseline to armv8-a: atomics then fall back to
load-exclusive/store-exclusive pairs, which every ARMv8.0 core implements, and
the compiler can no longer emit CRC32, RDMA or half-precision arithmetic
either.

`-march=armv8-a` with `-arch arm64` is a narrowing of the ISA baseline, not an
ABI change, and the SWEET/polaris builds lose only an optimisation.

Run from the repository root.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'out/midomarch-src/xnu')   # post-0004 file
WORK = os.path.join(ROOT, 'out/midomarch')
PATCH = os.path.join(ROOT, 'xnu/patches-mido/0106-mido-armv8-baseline.patch')

FILES = [
    ('makedefs/MakeInc.def', os.path.join(SRC, 'makedefs/MakeInc.def')),
]

OLD = (
    "MACHINE_FLAGS_ARM64_SWEET = -DARM64_BOARD_CONFIG_SWEET"
    "                -march=armv8.2-a+crypto\n"
)

NEW = """# mido: the Cortex-A53 is an ARMv8.0-A core, so state that and nothing more.
#
# -march=armv8.2-a let clang emit ARMv8.1 LSE atomics (ldadd/ldset/cas).  The
# A53 does not implement LSE, so the first os_atomic_inc() in the boot path
# trapped as UNDEFINED (lck_grp_reference+8, 0xb8280028 = "ldadd w8, w8, [x1]").
# armv8.2 also enables CRC32, RDMA and FP16 arithmetic, none of which the A53
# has.  Narrowing the baseline makes the atomics fall back to LL/SC pairs,
# which every ARMv8.0 core implements.
MACHINE_FLAGS_ARM64_SWEET = -DARM64_BOARD_CONFIG_SWEET                -march=armv8-a+crypto
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
    print('%-24s %s' % (rel, tag))


def _header():
    return """mido: compile for the ARMv8.0 baseline the A53 actually is

0105's undefined-instruction dump named the fault exactly:

    UNDEF pc=0xfffffff0072b55b0 instr=0xb8280028 esr=0x2000000
    0xfffffff0072b55b0 -> _lck_grp_reference (+0x8)

That instruction is ARMv8.1 LSE: 0xb8280028 is `ldadd w8, w8, [x1]`, and the
word in the Mach-O image at that address matches memory byte for byte, so the
kernel was compiled that way -- it is not corruption.  It comes from

    lck_grp_reference(lck_grp_t *grp, uint32_t *cnt)
        if (cnt) os_atomic_inc(cnt, relaxed);      -> mov w8,#1 ; ldadd w8,w8,[x1]

and the next two instructions (ldr w8,[x0,#4] / tbnz w8,#31) are the
LCK_GRP_ATTR_ALLOCATED test, so the faulting site is unambiguous.

A Cortex-A53 is ARMv8.0-A: it has no LSE (ARMv8.1), no CRC32, no RDMA and no
FP16 arithmetic.  `ldadd` there is an UNDEFINED instruction, which is exactly
the ESR we saw.  The first reference-count bump of the boot was simply the
first LSE site to execute.

The flag comes from the SWEET machine config in makedefs/MakeInc.def:

    MACHINE_FLAGS_ARM64_SWEET = -DARM64_BOARD_CONFIG_SWEET -march=armv8.2-a+crypto

which is correct for SWEET (SM7150) and polaris (SDM845) but not for this SoC.
xnu never selects LSE conditionally -- there is no __ARM_FEATURE_ATOMICS
guard anywhere in the tree -- so the target architecture is the only lever.
Pin it to armv8-a: os_atomic_* then lower to load-exclusive/store-exclusive
pairs, which every ARMv8.0 core implements.
"""


def main():
    seed(WORK, 'post-0004')
    edit(WORK, 'makedefs/MakeInc.def', OLD, NEW,
         'armv8.2-a+crypto -> armv8-a+crypto (no LSE on A53)')

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
    seed(fresh, 'pre-0106')
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
