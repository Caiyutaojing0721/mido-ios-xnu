#!/usr/bin/env python3
"""Compile-check the fbcon block that 0003 installs into pe_serial.c.

We cannot build pe_serial.c itself without the XNU tree, so we lift the added
lines straight out of the patch, wrap them in enough stubs to satisfy the
compiler, and cross-compile for aarch64.
"""
import os
import re
import subprocess
import sys

PATCH = 'xnu/patches/0003-sweet-framebuffer-serial-console.patch'
OUT = 'out/fbcon_syntaxcheck.c'
ZIG = 'C:/Users/才羽桃井/.workbuddy/binaries/python/envs/default/Scripts/python.exe'

STUBS = r'''
#ifndef NULL
#define NULL ((void *)0)
#endif

typedef unsigned char      uint8_t;
typedef unsigned int       uint32_t;
typedef unsigned long long uint64_t;

typedef struct DTEntryOpaque *DTEntry;
enum { kSuccess = 0 };

int SecureDTFindEntry(const char *propName, const char *propValue, DTEntry *entry);
int SecureDTGetProperty(DTEntry entry, const char *propName,
    void const **propValue, unsigned int *propSize);

void *ml_io_map_wcomb(uint64_t paddr, uint64_t size);

#define SERIAL_SWEET_FBCON 0x20

struct pe_serial_functions {
	void (*init)(void);
	unsigned int (*transmit_ready)(void);
	void (*transmit_data)(uint8_t);
	unsigned int (*receive_ready)(void);
	uint8_t (*receive_data)(void);
	int device;
};

void register_serial_functions(struct pe_serial_functions *fns);

#define SECURITY_READ_ONLY_LATE(type) type

/* from 0012-sweet-keep-debug-console-after-ttbr0.patch */
void sweet_early_mark(int stage, const char *what);

/* ---- patch payload follows ---- */
'''


def extract():
    # prefer the block captured after replaying 0003 -> 0012 -> 0096
    if os.path.exists('out/fbcon_new_block.txt'):
        return open('out/fbcon_new_block.txt', encoding='utf-8').read().rstrip('\n').split('\n')
    lines = open(PATCH, encoding='utf-8').read().split('\n')
    start = next(i for i, l in enumerate(lines) if l.startswith('@@ -697,6 +697,'))
    body = []
    for l in lines[start + 1:]:
        if l.startswith('@@') or l.startswith('diff --git'):
            break
        if l.startswith('+'):
            body.append(l[1:])
    return body


def main():
    body = extract()
    print('extracted %d added lines' % len(body))
    open(OUT, 'w', encoding='utf-8').write(STUBS + '\n'.join(body) + '\n\nint main(void) { return 0; }\n')

    cmd = [ZIG, '-m', 'ziglang', 'cc', '-target', 'aarch64-freestanding', '-std=gnu11',
           '-c', '-o', 'out/fbcon_check.o', '-Wall', '-Wextra', '-Wno-unused-function', OUT]
    print('$ ' + ' '.join(cmd[1:]))
    r = subprocess.run(cmd, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    print(out or '(clean)')
    errors = [l for l in out.split('\n') if ' error: ' in l]
    has_obj = __import__('os').path.exists('out/fbcon_check.o')
    print('object produced:', has_obj)
    ok = has_obj and not errors
    print('RESULT:', 'OK (compiled clean)' if ok else 'FAIL')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
