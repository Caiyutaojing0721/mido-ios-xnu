#!/usr/bin/env python3
"""Assemble a fastboot-bootable mido boot image around a foreign bare-metal shim.

Why this tool exists
--------------------
`fastboot boot out/shim-mido-santoni-boot.img` failed with

    Booting   FAILED (remote: 'dtb not found')

The reference image (Redmi 4X / santoni, MSM8940) is laid out as

    [ arm64 Image (image_size = 0x2b000) ][ DTB table: N santoni DTBs ]

i.e. the shim's own `image_size` marks the end of code and the DTB table
starts exactly there. mido's ABL walks that table and matches `qcom,msm-id`:

    mido stock DTB      : qcom,msm-id = 0x125  (MSM8953), board-id = 0xb
    santoni DTB table   : qcom,msm-id = 0x12f  (MSM8940/8917 family)

No entry matches 0x125, hence "dtb not found". The fix is to replace the
shim's DTB table with mido's own DTB, keeping the canonical
`[Image][DTB...]` shape that both the stock mido boot.img and the santoni
reference use.

What it does
------------
1. reads the arm64 Image header of the shim to get `image_size`;
2. builds kernel = shim[:image_size] + mido DTB   (default), or
                       shim + mido DTB            (--keep-shim-dtbs, fallback
                                                   when the shim parses its own
                                                   table);
3. packs an Android boot header v0 with the geometry of the *stock mido*
   boot.img (page 2048, base 0, kernel 0x80008000, ramdisk 0x81000000,
   tags 0x80000100, os_version 0x1e000191, second_addr 0);
4. verifies the result: 7 header fields vs the stock image, an FDT present
   past `image_size` whose msm-id is the expected one, and the SWEETXNU
   payload still parsing.

    mk_mido_boot.py --shim out/santoni/kernel.bin \
                    --dtb dtb/mido_stock/mido.dtb \
                    --payload out/mido-payload-santoni.bin \
                    --out out/shim-mido-boot-v2.img \
                    --stock out/mido-rom/boot.img
"""

import argparse
import os
import struct
import sys

FDT_MAGIC = b'\xd0\x0d\xfe\xed'
ARM64_MAGIC = b'ARM\x64'

# Geometry of the stock mido boot.img (measured with tools/mido_dtb.py).
STOCK_GEOMETRY = dict(
    page_size=2048,
    kernel_addr=0x80008000,
    ramdisk_addr=0x81000000,
    second_addr=0,
    tags_addr=0x80000100,
    os_version=0x1e000191,
)
STOCK_CMDLINE = (
    'androidboot.hardware=qcom msm_rtb.filter=0x237 ehci-hcd.park=3 '
    'lpm_levels.sleep_disabled=1 androidboot.bootdevice=7824900.sdhci '
    'earlycon=msm_hsl_uart,0x78af000 loop.max_part=7 '
    'androidboot.usbconfigfs=true '
    'androidboot.boot_devices=soc/7824900.sdhci debug=0x14e serial=3'
)
MIDO_MSM_ID = 0x125            # MSM8953


# ---------------------------------------------------------------- helpers ---

def arm64_image_size(shim):
    if shim[0x38:0x3c] != ARM64_MAGIC:
        raise SystemExit('shim is not an arm64 Image (magic %r at 0x38)'
                         % shim[0x38:0x3c])
    size = struct.unpack_from('<Q', shim, 0x10)[0]
    if not (0 < size <= len(shim)):
        raise SystemExit('arm64 image_size 0x%x is out of range (blob 0x%x)'
                         % (size, len(shim)))
    return size


def fdt_props(buf):
    """Return {propname: value} for the FDT root node.

    Delegates to tools/mido_dtb.py, which is the parser already used to
    measure this board, so there is exactly one FDT implementation here.
    """
    if buf[:4] != FDT_MAGIC:
        return {}
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from mido_dtb import Fdt
    try:
        return dict(Fdt(buf).root.props)
    except Exception:
        return {}


def fdt_table(buf, start):
    """Yield (offset, total_size) for each FDT in a contiguous appended table."""
    off = start
    while off + 8 <= len(buf) and buf[off:off + 4] == FDT_MAGIC:
        total = struct.unpack_from('>I', buf, off + 4)[0]
        if total < 16 or off + total > len(buf):
            break
        yield off, total
        off += total


def uscan(buf, start):
    out = []
    i = start
    while True:
        j = buf.find(FDT_MAGIC, i)
        if j < 0:
            return out
        out.append(j)
        i = j + 1


def align(n, a):
    return (n + a - 1) // a * a


def pack_boot_img(kernel, ramdisk, cmdline):
    g = STOCK_GEOMETRY
    page = g['page_size']
    hdr = bytearray(page)
    hdr[0:8] = b'ANDROID!'
    struct.pack_into('<8I', hdr, 8,
                     len(kernel), g['kernel_addr'],
                     len(ramdisk), g['ramdisk_addr'],
                     0, g['second_addr'],
                     g['tags_addr'], page)
    struct.pack_into('<2I', hdr, 40, 0, g['os_version'])   # dt_size, os_version
    hdr[48:64] = b'mido\x00' + b'\x00' * 11
    cmd = cmdline.encode()
    if len(cmd) >= 512:
        raise SystemExit('cmdline too long')
    hdr[64:64 + len(cmd)] = cmd
    hdr[576:608] = b'\x00' * 32                            # id
    hdr[608:608 + 16] = b'\x00' * 16                       # extra_cmdline

    out = bytearray(hdr)
    out += kernel + b'\x00' * (align(len(kernel), page) - len(kernel))
    out += ramdisk + b'\x00' * (align(len(ramdisk), page) - len(ramdisk))
    return bytes(out)


def header_fields(buf):
    ks, ka, rs, ra, ss, sa, tags, page = struct.unpack_from('<8I', buf, 8)
    f28, f2c = struct.unpack_from('<2I', buf, 40)
    return dict(kernel_addr=ka, ramdisk_addr=ra, second_addr=sa, tags=tags,
                page=page, dt_size=f28, os_version=f2c)


# ------------------------------------------------------------------ main ---

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--shim', required=True,
                    help='bare-metal shim kernel blob (arm64 Image, e.g. out/santoni/kernel.bin)')
    ap.add_argument('--dtb', required=True, help='mido device tree blob')
    ap.add_argument('--payload', required=True, help='SWEETXNU payload (goes in the ramdisk slot)')
    ap.add_argument('--out', required=True, help='output boot.img')
    ap.add_argument('--stock', default='out/mido-rom/boot.img',
                    help='stock mido boot.img used to verify the header fields')
    ap.add_argument('--cmdline', default=STOCK_CMDLINE)
    ap.add_argument('--keep-shim-dtbs', action='store_true',
                    help='append the mido DTB *after* the shim\'s own DTB table '
                         'instead of replacing it (fallback if the shim parses its own table)')
    a = ap.parse_args()

    shim = open(a.shim, 'rb').read()
    dtb = open(a.dtb, 'rb').read()
    payload = open(a.payload, 'rb').read()

    image_size = arm64_image_size(shim)
    dbuf = open(a.dtb, 'rb').read()
    if dbuf[:4] != FDT_MAGIC:
        raise SystemExit('--dtb is not an FDT')
    dprops = fdt_props(dbuf)
    msm_id = struct.unpack('>I', dprops.get('qcom,msm-id', b'\x00' * 4)[:4])[0]
    if msm_id != MIDO_MSM_ID:
        raise SystemExit('DTB qcom,msm-id is 0x%x, mido needs 0x%x'
                         % (msm_id, MIDO_MSM_ID))

    if a.keep_shim_dtbs:
        kernel = shim + dtb
        mode = 'shim + mido DTB (shim table kept)'
    else:
        head = shim[:image_size]
        dropped = list(fdt_table(shim, image_size))
        kernel = head + dtb
        mode = ('shim[:0x%x] + mido DTB (dropped %d foreign DTBs)'
                % (image_size, len(dropped)))

    img = pack_boot_img(kernel, payload, a.cmdline)
    with open(a.out, 'wb') as f:
        f.write(img)

    print('mode        :', mode)
    print('kernel blob : 0x%x (%d)  [shim code 0x%x + dtb 0x%x]'
          % (len(kernel), len(kernel), image_size, len(dtb)))
    print('payload     : 0x%x (%d)' % (len(payload), len(payload)))
    print('image       : %d bytes -> %s' % (len(img), a.out))

    # ---- verification -------------------------------------------------
    ok = True
    if os.path.exists(a.stock):
        s = header_fields(open(a.stock, 'rb').read(64))
        o = header_fields(img[:64])
        diffs = [k for k in s if s[k] != o[k]]
        print('\nheader vs stock mido boot.img:')
        for k in s:
            print('  %-13s stock=0x%08x ours=0x%08x  %s'
                  % (k, s[k], o[k], 'OK' if s[k] == o[k] else 'DIFF'))
        ok &= not diffs

    # DTB reachable past image_size, and it is the one ABL will match
    ks = struct.unpack_from('<I', img, 8)[0]
    kblob = img[STOCK_GEOMETRY['page_size']:STOCK_GEOMETRY['page_size'] + ks]
    cands = list(fdt_table(kblob, image_size)) if not a.keep_shim_dtbs else \
        [(o, struct.unpack_from('>I', kblob, o + 4)[0]) for o in uscan(kblob, image_size)]
    print('\nDTB candidates past image_size=0x%x:' % image_size)
    match = None
    for off, total in cands:
        pr = fdt_props(kblob[off:off + total])
        mid = pr.get('qcom,msm-id')
        bid = pr.get('qcom,board-id')
        mid_v = struct.unpack('>I', mid[:4])[0] if mid else None
        model = (pr.get('model') or b'').rstrip(b'\x00').decode('ascii', 'replace')
        tag = 'MATCH' if mid_v == MIDO_MSM_ID else 'skip '
        print('  @0x%-7x size=%-7d msm-id=%s board-id=%s %s %s'
              % (off, total,
                 '0x%x' % mid_v if mid_v is not None else '?',
                 '0x%x' % struct.unpack('>I', bid[:4])[0] if bid else '?',
                 tag, model[:40]))
        if mid_v == MIDO_MSM_ID and match is None:
            match = off
    if match is None:
        print('  !! no DTB with msm-id 0x%x -- ABL will report "dtb not found"' % MIDO_MSM_ID)
        ok = False
    else:
        print('  -> ABL will match the DTB at 0x%x' % match)

    # payload sanity
    rs = struct.unpack_from('<I', img, 16)[0]
    roff = STOCK_GEOMETRY['page_size'] + align(ks, STOCK_GEOMETRY['page_size'])
    pl = img[roff:roff + rs]
    if pl[:7] != b'SWEETXN':
        print('\n!! ramdisk slot is not a SWEETXNU payload')
        ok = False
    else:
        ver, nseg = struct.unpack_from('<II', pl, 8)
        virt, entry = struct.unpack_from('<QQ', pl, 0x10)
        adt_off, adt_size = struct.unpack_from('<QQ', pl, 0x20)
        phys, memsz = struct.unpack_from('<QQ', pl, 0x38)
        print('\npayload: SWEETXNU v%d nsegs=%d virt=0x%x entry=0x%x adt=0x%x'
              % (ver, nseg, virt, entry, adt_size))
        print('         phys=0x%x mem=0x%x (%.2f GiB)'
              % (phys, memsz, memsz / 2 ** 30))

    print('\nsha256:', __import__('hashlib').sha256(img).hexdigest())
    print('RESULT :', 'OK' if ok else 'PROBLEMS ABOVE')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
