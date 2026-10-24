#!/usr/bin/env python3
"""mido (MSM8953) boot image / DTB extractor and FDT inspector.

Why this exists
---------------
Everything this project once had to guess about the Redmi Note 4X (mido) --
eMMC controller base, UART, kernel load address, memory map, framebuffer,
GIC/timer -- is described authoritatively in the DTB that ships inside the
device's own boot.img.  This tool pulls that DTB out of a stock/recovery
flashable ROM zip's boot.img and prints the properties the port needs, so
board constants stop being TODOs and start being measurements.

Safety
------
Read-only.  It never touches a device or writes outside the paths you pass on
the command line.

Usage
-----
    python3 tools/mido_dtb.py boot.img --dump
    python3 tools/mido_dtb.py boot.img --extract out/mido-rom/mido.dtb
    python3 tools/mido_dtb.py --dtb out/mido-rom/mido.dtb --props
    python3 tools/mido_dtb.py --dtb out/mido-rom/mido.dtb --prop /chosen:bootargs
    python3 tools/mido_dtb.py --dtb out/mido-rom/mido.dtb --json
"""

import argparse
import json
import struct
import sys

FDT_MAGIC = 0xD00DFEED
FDT_BEGIN_NODE = 0x1
FDT_END_NODE = 0x2
FDT_PROP = 0x3
FDT_NOP = 0x4
FDT_END = 0x9

BOOT_MAGIC = b'ANDROID!'


# --------------------------------------------------------------------------
# Android boot image
# --------------------------------------------------------------------------
class BootImage:
    """Minimal parser for the Android boot image header (v0/v1/v2)."""

    def __init__(self, blob):
        self.blob = blob
        if blob[:8] != BOOT_MAGIC:
            raise ValueError('not an Android boot image (bad ANDROID! magic)')
        (self.kernel_size, self.kernel_addr,
         self.ramdisk_size, self.ramdisk_addr,
         self.second_size, self.second_addr,
         self.tags_addr, self.page_size) = struct.unpack_from('<8I', blob, 8)

        if self.page_size == 0 or self.page_size > 65536:
            raise ValueError('implausible page size %d' % self.page_size)

        # v0 headers carry dt_size at 0x28; v1+ carry header_version there.
        self.hdr_field_28 = struct.unpack_from('<I', blob, 40)[0]
        self.hdr_field_2c = struct.unpack_from('<I', blob, 44)[0]
        self.name = blob[48:64].split(b'\0')[0]
        self.cmdline = blob[64:64 + 512].split(b'\0')[0]
        self.id = blob[576:608]
        self.extra_cmdline = blob[608:608 + 1024].split(b'\0')[0]

        # Heuristic: a real header_version is 0..4; the old dt_size slot can
        # hold any value, so prefer interpreting it as a version when sane.
        self.header_version = self.hdr_field_28 if self.hdr_field_28 <= 4 else None
        self.dt_size = None if self.header_version is not None else self.hdr_field_28

        p = self.page_size
        self.kernel_off = p
        p += self._padded(self.kernel_size)
        self.ramdisk_off = p
        p += self._padded(self.ramdisk_size)
        self.second_off = p
        p += self._padded(self.second_size)
        self.dt_off = p

    def _padded(self, size):
        return (size + self.page_size - 1) // self.page_size * self.page_size

    def kernel(self):
        return self.blob[self.kernel_off:self.kernel_off + self.kernel_size]

    def describe(self):
        lines = [
            'kernel      size=%-9d addr=0x%08x off=%d' % (self.kernel_size, self.kernel_addr, self.kernel_off),
            'ramdisk     size=%-9d addr=0x%08x off=%d' % (self.ramdisk_size, self.ramdisk_addr, self.ramdisk_off),
            'second      size=%-9d addr=0x%08x off=%d' % (self.second_size, self.second_addr, self.second_off),
            'tags_addr   0x%08x' % self.tags_addr,
            'page_size   %d' % self.page_size,
            'hdr@0x28    %d (header_version=%s dt_size=%s)' % (self.hdr_field_28, self.header_version, self.dt_size),
            'hdr@0x2c    0x%08x' % self.hdr_field_2c,
            'name        %r' % self.name.decode('utf-8', 'replace'),
            'cmdline     %s' % self.cmdline.decode('utf-8', 'replace'),
            'extra       %s' % self.extra_cmdline.decode('utf-8', 'replace'),
        ]
        k = self.kernel()
        if k[:2] == b'\x1f\x8b':
            arch = 'gzip-compressed'
        elif k[0x38:0x3c] == b'ARM\x64':
            arch = 'arm64 Image (uncompressed)'
        else:
            arch = 'unknown'
        lines.append('kernel fmt  %s' % arch)
        return '\n'.join(lines)


def find_appended_dtb(blob, start=0, end=None):
    """Locate a plausible appended DTB inside a kernel blob.

    Qualcomm MSM8953 mido has no separate dtb/dtbo partition: the DTB is glued
    to the end of the kernel image.  There is no QCDT table here, so the only
    reliable signal is the FDT magic plus a self-consistent totalsize.
    """
    end = len(blob) if end is None else end
    hits = []
    pos = start
    while True:
        pos = blob.find(b'\xd0\x0d\xfe\xed', pos)
        if pos < 0 or pos >= end:
            break
        if pos + 8 <= len(blob):
            total = struct.unpack_from('>I', blob, pos + 4)[0]
            if 0x100 <= total <= (len(blob) - pos) and total <= 0x200000:
                hits.append((pos, total))
        pos += 1
    return hits


# --------------------------------------------------------------------------
# Flattened device tree
# --------------------------------------------------------------------------
class Node:
    __slots__ = ('name', 'props', 'children', 'parent')

    def __init__(self, name, parent=None):
        self.name = name
        self.props = {}
        self.children = {}
        self.parent = parent

    def path(self):
        if self.parent is None:
            return '/'
        p = self.parent.path()
        return (p if p.endswith('/') else p + '/') + self.name


class Fdt:
    def __init__(self, blob):
        (magic, self.totalsize, off_struct, off_strings, off_rsvmap,
         self.version, self.last_comp_version, self.boot_cpuid_phys,
         self.size_strings, self.size_struct) = struct.unpack_from('>10I', blob, 0)
        if magic != FDT_MAGIC:
            raise ValueError('bad FDT magic 0x%08x' % magic)
        self.blob = blob[:self.totalsize]
        self.strings = blob[off_strings:off_strings + self.size_strings]
        self.reserve = []
        p = off_rsvmap
        while True:
            addr, size = struct.unpack_from('>QQ', blob, p)
            p += 16
            if addr == 0 and size == 0:
                break
            self.reserve.append((addr, size))
        self.root = self._parse_struct(blob, off_struct, off_struct + self.size_struct)

    def _name_at(self, off):
        end = self.strings.find(b'\0', off)
        return self.strings[off:end].decode('utf-8', 'replace')

    def _parse_struct(self, blob, p, limit):
        root = None
        cur = None
        while p < limit:
            tok = struct.unpack_from('>I', blob, p)[0]
            p += 4
            if tok == FDT_BEGIN_NODE:
                end = blob.find(b'\0', p)
                name = blob[p:end].decode('utf-8', 'replace')
                p = (end + 1 + 3) & ~3
                node = Node(name, cur)
                if cur is None:
                    root = node
                else:
                    cur.children[name] = node
                cur = node
            elif tok == FDT_END_NODE:
                cur = cur.parent
            elif tok == FDT_PROP:
                ln, nameoff = struct.unpack_from('>II', blob, p)
                p += 8
                val = blob[p:p + ln]
                p = (p + ln + 3) & ~3
                cur.props[self._name_at(nameoff)] = val
            elif tok == FDT_NOP:
                continue
            elif tok == FDT_END:
                break
            else:
                raise ValueError('unknown FDT token 0x%x at %d' % (tok, p - 4))
        return root

    # ---- helpers -------------------------------------------------------
    def walk(self, node=None):
        node = node or self.root
        yield node
        for c in node.children.values():
            yield from self.walk(c)

    def find(self, path):
        if path in ('/', ''):
            return self.root
        cur = self.root
        for part in path.strip('/').split('/'):
            if part not in cur.children:
                raise KeyError('no such node: %s' % path)
            cur = cur.children[part]
        return cur

    def find_by_unit_address(self, addr):
        """Find the node whose @unit-address matches addr (hex int)."""
        want = '%x' % addr
        return [n for n in self.walk() if n.name.endswith('@' + want)]

    def find_by_compatible(self, needle):
        out = []
        for n in self.walk():
            comp = n.props.get('compatible', b'')
            if needle.encode() in comp:
                out.append(n)
        return out

    def get_str(self, path, prop, default=None):
        n = self.find(path)
        if prop not in n.props:
            return default
        return n.props[prop].split(b'\0')[0].decode('utf-8', 'replace')

    def get_u32(self, path, prop):
        n = self.find(path)
        if prop not in n.props:
            return None
        v = n.props[prop]
        if len(v) == 4:
            return struct.unpack('>I', v)[0]
        return list(struct.unpack('>%dI' % (len(v) // 4), v))

    def reg(self, path):
        """Return [(base, size), ...] for a node's `reg`, 1 or 2 cells each."""
        n = self.find(path)
        v = n.props.get('reg')
        if v is None:
            return []
        ac = self._addr_cells(n), self._size_cells(n)
        step = (ac[0] + ac[1]) * 4
        out = []
        for off in range(0, len(v) - step + 1, step):
            parts = struct.unpack('>%dI' % (ac[0] + ac[1]), v[off:off + step])
            base = 0
            for x in parts[:ac[0]]:
                base = (base << 32) | x
            size = 0
            for x in parts[ac[0]:]:
                size = (size << 32) | x
            out.append((base, size))
        return out

    def _addr_cells(self, node):
        n = node
        while n is not None:
            if '#address-cells' in n.props:
                return struct.unpack('>I', n.props['#address-cells'])[0]
            n = n.parent
        return 1

    def _size_cells(self, node):
        n = node
        while n is not None:
            if '#size-cells' in n.props:
                return struct.unpack('>I', n.props['#size-cells'])[0]
            n = n.parent
        return 1

    def tree_str(self, node=None, depth=0, with_props=True):
        node = node or self.root
        label = node.name if node.name else '/'
        out = ['  ' * depth + label]
        if with_props:
            for k in sorted(node.props):
                out.append('  ' * (depth + 1) + '%s = %s' % (k, fmt_value(node.props[k])))
        for c in node.children.values():
            out.append(self.tree_str(c, depth + 1, with_props))
        return '\n'.join(out)

    def to_json(self):
        def conv(node):
            return {
                'name': node.name,
                'props': {k: fmt_value(v) for k, v in node.props.items()},
                'children': [conv(c) for c in node.children.values()],
            }
        return conv(self.root)


def fmt_value(v):
    if len(v) == 0:
        return '<empty>'
    try:
        s = v.decode('ascii')
        if s.isprintable() and len(s) > 0:
            return '"%s"' % s.replace('\0', '\\0')
    except UnicodeDecodeError:
        pass
    if len(v) % 4 == 0 and len(v) <= 64:
        return '<' + ' '.join('0x%x' % x for x in struct.unpack('>%dI' % (len(v) // 4), v)) + '>'
    return 'hex:' + v[:32].hex()


# --------------------------------------------------------------------------
# Board-constant report
# --------------------------------------------------------------------------
def report(fdt, boot=None):
    """Print the specific constants the iOS port needs, with provenance."""
    L = []
    if boot is not None:
        L.append('== boot.img ==')
        L.append(boot.describe())
        L.append('')

    L.append('== memory ==')
    for n in fdt.find_by_compatible('memory'):
        for base, size in fdt.reg(n.path()):
            L.append('  %-28s base=0x%08x size=0x%08x (%.2f GiB)'
                     % (n.path(), base, size, size / 1073741824))
    if fdt.root.props.get('#address-cells'):
        L.append('  root #address-cells=%d #size-cells=%d'
                 % (struct.unpack('>I', fdt.root.props['#address-cells'])[0],
                    struct.unpack('>I', fdt.root.props.get('#size-cells', b'\0\0\0\1'))[0]))
    for n in fdt.walk():
        if n.name in ('reserved-memory',):
            for c in n.children.values():
                rs = fdt.reg(c.path())
                if rs:
                    L.append('  reserved %-22s 0x%08x-0x%08x (%s)'
                             % (c.name, rs[0][0], rs[0][0] + rs[0][1],
                                c.props.get('compatible', b'').split(b'\0')[0].decode('utf-8', 'replace')))
    L.append('')

    L.append('== cpus ==')
    cpus = [n for n in fdt.walk() if n.name.startswith('cpu@')]
    L.append('  cpu nodes: %d' % len(cpus))
    for c in sorted(cpus, key=lambda x: x.name):
        en = c.props.get('enable-method', b'').split(b'\0')[0].decode('utf-8', 'replace')
        comp = c.props.get('compatible', b'').split(b'\0')[0].decode('utf-8', 'replace')
        L.append('    %-10s %-22s enable-method=%s' % (c.name, comp, en or '-'))
    L.append('')

    L.append('== interrupts / timers ==')
    for name in ('interrupt-controller', 'timer'):
        for n in fdt.find_by_compatible(name):
            rs = fdt.reg(n.path())
            if rs:
                L.append('  %-34s %-20s base=0x%08x size=0x%08x'
                         % (n.path(), name, rs[0][0], rs[0][1]))
    L.append('')

    L.append('== uart (earlycon) ==')
    for n in fdt.find_by_compatible('uart'):
        rs = fdt.reg(n.path())
        if rs:
            L.append('  %-34s base=0x%08x' % (n.path(), rs[0][0]))
    L.append('')

    L.append('== storage (eMMC / SDHCI) ==')
    for n in fdt.find_by_compatible('sdhci'):
        rs = fdt.reg(n.path())
        comp = n.props.get('compatible', b'').split(b'\0')[0].decode('utf-8', 'replace')
        if rs:
            L.append('  %-34s %-28s base=0x%08x size=0x%08x'
                     % (n.path(), comp, rs[0][0], rs[0][1]))
    L.append('')

    L.append('== display / framebuffer ==')
    for n in fdt.walk():
        comp = n.props.get('compatible', b'')
        if any(k in comp for k in (b'simple-framebuffer', b'msm', b'mdss', b'framebuffer')):
            rs = fdt.reg(n.path())
            if rs and ('fb' in n.name or 'splash' in n.name or 'mdss' in n.name
                       or 'simple-framebuffer' in comp.decode('utf-8', 'replace')):
                L.append('  %-34s 0x%08x size=0x%08x  compat=%s'
                         % (n.path(), rs[0][0], rs[0][1],
                            comp.split(b'\0')[0].decode('utf-8', 'replace')))
    L.append('')

    L.append('== chosen ==')
    for k in ('bootargs', 'linux,initrd-start', 'linux,initrd-end', 'stdout-path'):
        v = fdt.root.children.get('chosen')
        if v is not None and k in v.props:
            L.append('  %-24s %s' % (k, fmt_value(v.props[k])))
    return '\n'.join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('boot', nargs='?', help='path to boot.img (or recovery.img)')
    ap.add_argument('--dtb', help='path to an already extracted .dtb')
    ap.add_argument('--extract', metavar='OUT', help='write the appended DTB here')
    ap.add_argument('--dump', action='store_true', help='print the full device tree')
    ap.add_argument('--props', action='store_true', help='print board constants report')
    ap.add_argument('--prop', metavar='PATH:PROP', help='print one property')
    ap.add_argument('--json', action='store_true', help='emit the tree as JSON')
    ap.add_argument('--no-props', action='store_true', help='suppress the default report')
    args = ap.parse_args(argv)

    boot = None
    if args.dtb:
        blob = open(args.dtb, 'rb').read()
        hits = [(0, len(blob))]
    else:
        if not args.boot:
            ap.error('need a boot image or --dtb')
        raw = open(args.boot, 'rb').read()
        boot = BootImage(raw)
        hits = find_appended_dtb(raw, boot.kernel_off,
                                 boot.kernel_off + boot.kernel_size)
        if not hits:
            print('no appended DTB found in kernel blob', file=sys.stderr)
            return 2
        print(boot.describe(), file=sys.stderr)
        print('appended DTB candidates: %s' % [(o, s) for o, s in hits], file=sys.stderr)
        # The DTB is the last thing appended to the kernel; prefer the hit that
        # self-terminates closest to the end of the kernel blob.
        off, total = max(hits, key=lambda t: t[0])
        blob = raw[off:off + total]

    fdt = Fdt(blob)

    if args.extract:
        with open(args.extract, 'wb') as f:
            f.write(blob)
        print('wrote %s (%d bytes)' % (args.extract, len(blob)), file=sys.stderr)

    if args.prop:
        path, prop = args.prop.rsplit(':', 1)
        n = fdt.find(path)
        print(fmt_value(n.props[prop]) if prop in n.props else '<missing>')
        return 0

    if args.props or not (args.dump or args.json):
        print(report(fdt, boot))
    if args.dump:
        print(fdt.tree_str())
    if args.json:
        print(json.dumps(fdt.to_json(), indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
