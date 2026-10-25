#!/usr/bin/env python3
"""Generate xnu/patches-mido/0105-mido-undef-context.patch.

The 0104 kernel is the first mido build that talks.  It prints

    SWEET EARLY 0: arm_init reached, boot_args untouched
    SWEET EARLY 1: arm_init entered (MMU on)
    SWEET EARLY 2: PE_init_platform done
    SWEET EARLY 3: ml_parse_cpu_topology done
    SWEET EARLY 4: cpu_bootstrap done
    SWEET EARLY PANIC: Undefined kernel instruction: pc=0xfffffff00872b5b0
                       instr=0xb2800028 @//sleh.c/:951

so the kernel gets through cpu_bootstrap() and dies before stage 5, i.e.
somewhere in rtclock_early_init(), kernel_debug_string_early() or
kernel_startup_bootstrap().

The panic PC is the interesting part.  The kernel image ends at
0xfffffff00844e328 (__LINKEDIT), and 0xfffffff00872b5b0 sits 0x2dd288 bytes
past that -- outside every Mach-O segment, and not a VA any segment ever
loaded.  The bootstrap KVA mapping covers all of RAM, so that VA does
translate; it just points at RAM nobody wrote, and the "instruction" the CPU
found there (0xb2800028, `orr x8, x1, #0x100000001`) is leftover garbage.
Nothing about it identifies who jumped there: a corrupted function pointer, a
wild return address and a bad indirect call all look identical.

So this patch spends one build on better evidence rather than on a guess:

  * sweet_early.c: add sweet_early_log(), a varargs sibling of
    sweet_early_panic() that shares the console.  No locks, no allocation, so
    it is safe in the exception handler.

  * sleh.c: before panicking on an undefined kernel instruction, dump the
    exception registers and the frame chain -- pc, lr, sp, fp, esr, far, elr,
    the instruction, and five frames of (return address, caller frame).  The
    bootstrap mapping covers all of RAM, so the reads cannot fault, and the
    chain is bounded and sanity checked so a garbage fp cannot walk away.
    The first "ret" that is not inside the kernel is the culprit's caller.

  * arm_init.c: split the silent window with two more marks -- stage 10 after
    rtclock_early_init(), stage 11 after kernel_debug_string_early() -- so the
    screen alone says whether the fault is in rtclock_early_init() or later,
    without even reading the panic.

Run from the repository root.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'out/midoctx-src/xnu')       # post-0104 (sleh.c/h: post-0103)
WORK = os.path.join(ROOT, 'out/midoundef')
PATCH = os.path.join(ROOT, 'xnu/patches-mido/0105-mido-undef-context.patch')

FILES = [
    ('osfmk/arm64/sweet_early.h', os.path.join(SRC, 'osfmk/arm64/sweet_early.h')),
    ('osfmk/arm64/sweet_early.c', os.path.join(SRC, 'osfmk/arm64/sweet_early.c')),
    ('osfmk/arm64/sleh.c', os.path.join(SRC, 'osfmk/arm64/sleh.c')),
    ('osfmk/arm/arm_init.c', os.path.join(SRC, 'osfmk/arm/arm_init.c')),
]

# ------------------------------------------------------------- sweet_early.h

H_OLD = """void sweet_early_panic(const char *fmt, va_list *args);
void sweet_early_relocate(uint64_t base);
"""

H_NEW = """void sweet_early_panic(const char *fmt, va_list *args);
void sweet_early_log(const char *fmt, ...);
void sweet_early_relocate(uint64_t base);
"""

H_STUB_OLD = """static inline void sweet_early_disable(void) {}
"""

H_STUB_NEW = """static inline void sweet_early_log(const char *fmt, ...)
{
	(void)fmt;
}
static inline void sweet_early_disable(void) {}
"""

# ------------------------------------------------------------- sweet_early.c

C_OLD = """	va_copy(copy, *args);
	_doprnt(fmt, &copy, sweet_early_putc, 16);
	va_end(copy);
	sweet_early_putc('\\n');
	__asm__ volatile("dsb sy" ::: "memory");
}
"""

C_NEW = """	va_copy(copy, *args);
	_doprnt(fmt, &copy, sweet_early_putc, 16);
	va_end(copy);
	sweet_early_putc('\\n');
	__asm__ volatile("dsb sy" ::: "memory");
}

/*
 * Same console, but usable before panicking and from the exception handlers,
 * where the panic text alone does not say which code arrived at the bad
 * address. No locks and no allocation: this can run with no current thread.
 */
void
sweet_early_log(const char *fmt, ...)
{
	va_list args;

	if (early_disabled || early_panicking) {
		return;
	}
	va_start(args, fmt);
	_doprnt(fmt, &args, sweet_early_putc, 16);
	va_end(args);
	__asm__ volatile("dsb sy" ::: "memory");
}
"""

# ---------------------------------------------------------------- sleh.c

S_INC_OLD = """#include <arm64/instructions.h>
"""

S_INC_NEW = """#include <arm64/instructions.h>
#if defined(SWEET)
#include <arm64/sweet_early.h>
#endif
"""

S_HELPER_OLD = """static void
handle_uncategorized(arm_saved_state_t *state)
{
"""

S_HELPER_NEW = """#if defined(SWEET)
/*
 * mido: an undefined instruction in kernel mode means control reached an
 * address that was never loaded -- 0104 died at 0xfffffff00872b5b0, which is
 * 0x2dd288 bytes past __LINKEDIT and inside RAM nobody wrote. A wild return
 * and a wild indirect call are indistinguishable from the address alone, so
 * print the exception registers and the frame chain before panicking. The
 * bootstrap mapping covers all of RAM, so these loads cannot fault; the walk
 * is bounded and checks alignment and monotonicity before following a link.
 */
static void
sweet_undef_context(arm_saved_state_t *state, uint32_t instr)
{
	uint64_t esr, far, elr, fp;

	__asm__ volatile("mrs %0, esr_el1" : "=r"(esr));
	__asm__ volatile("mrs %0, far_el1" : "=r"(far));
	__asm__ volatile("mrs %0, elr_el1" : "=r"(elr));

	fp = get_saved_state_fp(state);

	sweet_early_log("UNDEF pc=%p lr=%p\\n",
	    (void *)get_saved_state_pc(state), (void *)get_saved_state_lr(state));
	sweet_early_log("  sp=%p fp=%p\\n",
	    (void *)get_saved_state_sp(state), (void *)fp);
	sweet_early_log("  esr=%p far=%p\\n", (void *)esr, (void *)far);
	sweet_early_log("  elr=%p instr=%x\\n", (void *)elr, instr);

	for (int i = 0; i < 5; i++) {
		uint64_t ret, next;

		if ((fp & 7u) != 0 || fp < 0xfffffff000000000ULL ||
		    fp >= 0xfffffff100000000ULL) {
			sweet_early_log("  FR%d fp unusable\\n", i);
			break;
		}
		next = *(volatile uint64_t *)fp;
		ret = *(volatile uint64_t *)(fp + 8);
		sweet_early_log("  FR%d ret=%p fp=%p\\n", i, (void *)ret, (void *)next);
		if (next <= fp) {
			break;
		}
		fp = next;
	}
}
#endif /* SWEET */

static void
handle_uncategorized(arm_saved_state_t *state)
{
"""

S_CALL_OLD = """		} else {
			panic("Undefined kernel instruction: pc=%p instr=%x", (void*)get_saved_state_pc(state), instr);
		}
"""

S_CALL_NEW = """		} else {
#if defined(SWEET)
			sweet_undef_context(state, instr);
#endif
			panic("Undefined kernel instruction: pc=%p instr=%x", (void*)get_saved_state_pc(state), instr);
		}
"""

# -------------------------------------------------------------- arm_init.c

A_OLD = """	cpu_bootstrap();
	sweet_early_mark(4, "cpu_bootstrap done");

	rtclock_early_init();

	kernel_debug_string_early("kernel_startup_bootstrap");
	kernel_startup_bootstrap();
	sweet_early_mark(5, "kernel_startup_bootstrap done");
"""

A_NEW = """	cpu_bootstrap();
	sweet_early_mark(4, "cpu_bootstrap done");

	rtclock_early_init();
	sweet_early_mark(10, "rtclock_early_init done");

	kernel_debug_string_early("kernel_startup_bootstrap");
	sweet_early_mark(11, "kernel_debug_string_early done");
	kernel_startup_bootstrap();
	sweet_early_mark(5, "kernel_startup_bootstrap done");
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
    print('%-26s %s' % (rel, tag))


def _header():
    return __doc__.strip() + '\n'


def main():
    seed(WORK, 'post-0104')
    edit(WORK, 'osfmk/arm64/sweet_early.h', H_OLD, H_NEW, 'log() prototype')
    edit(WORK, 'osfmk/arm64/sweet_early.h', H_STUB_OLD, H_STUB_NEW, 'log() stub for non-SWEET')
    edit(WORK, 'osfmk/arm64/sweet_early.c', C_OLD, C_NEW, 'log() implementation')
    edit(WORK, 'osfmk/arm64/sleh.c', S_INC_OLD, S_INC_NEW, 'include sweet_early.h')
    edit(WORK, 'osfmk/arm64/sleh.c', S_HELPER_OLD, S_HELPER_NEW, 'sweet_undef_context()')
    edit(WORK, 'osfmk/arm64/sleh.c', S_CALL_OLD, S_CALL_NEW, 'call it before the panic')
    edit(WORK, 'osfmk/arm/arm_init.c', A_OLD, A_NEW, 'marks 10 and 11')

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
    seed(fresh, 'pre-0105')
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
