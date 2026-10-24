#!/usr/bin/env python3
"""Rewrite a SWEETXNU v1 payload with a different ADT / command line.

  payload_repack.py IN OUT [--adt FILE] [--cmdline STR]
                          [--phys-base 0x...] [--mem-size 0x...] [--dump]

Why this exists: the kernel segments in a built payload are already flat and
already rebased (mkpayload.py walked LC_DYLD_CHAINED_FIXUPS), so a payload can
be re-targeted at another board *without* recompiling XNU -- only the device
tree it hands to the kernel, the boot command line, and (if the kernel truly
moves) the physical window have to change. That is exactly what a "does the
reference kernel come up on my board" experiment needs.

The shim reads seg[i].file_off from the header, so the segments are re-laid
out contiguously here (8-byte aligned, table at 0x2a8 just like mkpayload.py)
and the ADT is appended behind them. kernel_end_va is a *virtual* address and
is carried over unchanged.

Layout (little endian, offsets from payload start), see tools/mkpayload.py:
  0x000 magic[8]="SWEETXNU"  0x008 u32 version, u32 nsegs
  0x010 virt_base  0x018 entry_va  0x020 adt_off  0x028 adt_size
  0x030 kernel_end_va  0x038 phys_base  0x040 mem_size
  0x048 cmdline[608]   0x2a8 seg[n] { vmaddr, vmsize, file_off, file_size }
"""
import argparse
import struct
import sys

MAGIC = b'SWEETXNU'
VERSION = 1
CMDLINE_LEN = 608
SEG_TABLE = 0x2a8


def parse(blob):
    if blob[:8] != MAGIC:
        raise SystemExit('not a SWEETXNU payload')
    version, nsegs = struct.unpack_from('<II', blob, 8)
    if version != VERSION:
        raise SystemExit(f'unsupported payload version {version}')
    head = dict(
        virt_base=struct.unpack_from('<Q', blob, 0x10)[0],
        entry_va=struct.unpack_from('<Q', blob, 0x18)[0],
        adt_off=struct.unpack_from('<Q', blob, 0x20)[0],
        adt_size=struct.unpack_from('<Q', blob, 0x28)[0],
        kernel_end=struct.unpack_from('<Q', blob, 0x30)[0],
        phys_base=struct.unpack_from('<Q', blob, 0x38)[0],
        mem_size=struct.unpack_from('<Q', blob, 0x40)[0],
        cmdline=blob[0x48:0x48 + CMDLINE_LEN].split(b'\0')[0].decode('utf-8', 'replace'),
        nsegs=nsegs,
    )
    segs = []
    for i in range(nsegs):
        vmaddr, vmsize, foff, fsize = struct.unpack_from('<4Q', blob, SEG_TABLE + i * 32)
        segs.append(dict(vmaddr=vmaddr, vmsize=vmsize, file_off=foff, file_size=fsize,
                         data=blob[foff:foff + fsize]))
        if len(segs[-1]['data']) != fsize:
            raise SystemExit(f'segment {i} runs past the end of the payload')
    adt = blob[head['adt_off']:head['adt_off'] + head['adt_size']]
    if len(adt) != head['adt_size']:
        raise SystemExit('ADT runs past the end of the payload')
    return head, segs, adt


def build(head, segs, adt, cmdline=None, phys_base=None, mem_size=None):
    cmd = (cmdline if cmdline is not None else head['cmdline']).encode()
    if len(cmd) >= CMDLINE_LEN:
        raise SystemExit(f'cmdline is {len(cmd)} bytes, limit is {CMDLINE_LEN - 1}')
    out = bytearray(SEG_TABLE + len(segs) * 32)
    offsets = []
    for seg in segs:
        while len(out) % 8:
            out.append(0)
        offsets.append(len(out))
        out += seg['data']
    while len(out) % 8:
        out.append(0)
    adt_off = len(out)
    out += adt
    while len(out) % 8:
        out.append(0)

    out[0:8] = MAGIC
    struct.pack_into('<II', out, 8, VERSION, len(segs))
    struct.pack_into('<Q', out, 0x10, head['virt_base'])
    struct.pack_into('<Q', out, 0x18, head['entry_va'])
    struct.pack_into('<Q', out, 0x20, adt_off)
    struct.pack_into('<Q', out, 0x28, len(adt))
    struct.pack_into('<Q', out, 0x30, head['kernel_end'])
    struct.pack_into('<Q', out, 0x38, head['phys_base'] if phys_base is None else phys_base)
    struct.pack_into('<Q', out, 0x40, head['mem_size'] if mem_size is None else mem_size)
    out[0x48:0x48 + CMDLINE_LEN] = cmd.ljust(CMDLINE_LEN, b'\0')
    for i, seg in enumerate(segs):
        struct.pack_into('<4Q', out, SEG_TABLE + i * 32,
                         seg['vmaddr'], seg['vmsize'], offsets[i], seg['file_size'])
    return bytes(out)


def dump(head, segs, adt):
    print(f"virt_base=0x{head['virt_base']:x} entry=0x{head['entry_va']:x}")
    print(f"phys_base=0x{head['phys_base']:x} mem_size=0x{head['mem_size']:x} "
          f"({head['mem_size'] / 2**30:.2f} GiB at 0x{head['phys_base']:x}"
          f"..0x{head['phys_base'] + head['mem_size']:x})")
    print(f"kernel_end_va=0x{head['kernel_end']:x} adt={len(adt)} bytes")
    print(f"cmdline: {head['cmdline']}")
    for i, seg in enumerate(segs):
        print(f"  [{i}] va=0x{seg['vmaddr']:012x} vsize=0x{seg['vmsize']:09x} "
              f"size=0x{seg['file_size']:08x}")
    # ADT sanity: the node header is two LITTLE-endian counts (see tools/adt.py);
    # property values are big-endian, but the counts are not.
    nprops, nchildren = struct.unpack_from('<II', adt, 0)
    print(f"ADT root: {nprops} props, {nchildren} children")
    if nprops > 64 or nchildren > 32:
        raise SystemExit('ADT does not look like a device tree (bad root header)')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('inp')
    ap.add_argument('out', nargs='?')
    ap.add_argument('--adt', help='replacement ADT file')
    ap.add_argument('--cmdline', help='replacement boot command line')
    ap.add_argument('--phys-base', help='override phys_base, e.g. 0x98000000')
    ap.add_argument('--mem-size', help='override mem_size, e.g. 0x28000000')
    ap.add_argument('--dump', action='store_true', help='only print the header')
    a = ap.parse_args()

    head, segs, adt = parse(open(a.inp, 'rb').read())
    if a.adt:
        adt = open(a.adt, 'rb').read()
    if a.dump:
        dump(head, segs, adt)
        return
    if not a.out:
        ap.error('OUT is required unless --dump is given')
    blob = build(head, segs, adt, cmdline=a.cmdline,
                 phys_base=int(a.phys_base, 0) if a.phys_base else None,
                 mem_size=int(a.mem_size, 0) if a.mem_size else None)
    open(a.out, 'wb').write(blob)

    # Re-parse what was written: a payload that does not round-trip is worse
    # than no payload at all, because the failure happens on the phone.
    head2, segs2, adt2 = parse(blob)
    assert adt2 == adt and len(segs2) == len(segs)
    for before, after in zip(segs, segs2):
        assert before['data'] == after['data'] and before['vmaddr'] == after['vmaddr']
    print(f"{a.out}: {len(blob)} bytes, {len(segs)} segments, ADT {len(adt)} bytes, "
          f"phys 0x{head2['phys_base']:x}+0x{head2['mem_size']:x}", file=sys.stderr)


if __name__ == '__main__':
    main()
