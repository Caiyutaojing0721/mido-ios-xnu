#!/usr/bin/env python3
"""Build the Apple Device Tree for the REDMI NOTE 4X (mido) XNU bring-up.

Reuses helpers and the iPhone X (D22AP) product node from mkadt.py. mido is
Qualcomm MSM8953: 8x Cortex-A53 (two clusters of four), Adreno 506, eMMC 5.1
(NOT UFS -- this is the big difference from sweet/polaris).

Two independent sources of truth are used here, and they are not the same kind:

* MEASURED device constants -- MMIO addresses, panel geometry, MPIDR values --
  come from the phone's own boot.img DTB (`crDroid15 mido` package), read with
  `python3 tools/mido_dtb.py <boot.img>`.
* NODE NAMES AND COMPATIBLE STRINGS are dictated by what the XNU patches in
  this tree actually match on, and were cross-checked against a santoni
  (Redmi 4X / MSM8940) image that is known to boot iOS on eMMC. Getting these
  wrong does not fail loudly: the driver simply never instantiates. The three
  that matter:
      root      compatible must contain "xiaomi,sweet"   (0016 platform expert)
      cpus      compatible must contain "qcom,kryo470"   (0018 SweetCPU)
      disp0     compatible must contain "sweet,display"  (0046 IOMFB)
      fbcon     NODE NAME must be "sweet-fbcon"          (0003 SecureDTFindEntry)
      eMMC      NODE NAME is what the root driver looks up
  Anything still marked TODO below is genuinely unknown and must be measured on
  the phone; the tree is structurally valid but this is a scaffold, not a
  verified port.

  mkadt_mido.py OUT.adt [--dump] [--ram 3|4]
                [--dram-base 0x98000000] [--dram-size 0x28000000]
                [--canvas x|panel] [--sdhci-node NAME] [--sdhci-compat CSV]
"""
import argparse, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mkadt as M  # adt serialize/parse, d22ap_product, u32/u64/cstr/node, constants

# --- mido / MSM8953 MEASURED constants -------------------------------------
# Provenance: the boot.img DTB shipped in the crDroid 15 (mido) recovery
# package, read with tools/mido_dtb.py. Each value carries the DT node it came
# from so it can be re-checked against a dump.
#
# Kernel-managed RAM window. The DTB's /memory node is all-zero (the Qualcomm
# bootloader patches it at runtime), so the DTB does NOT prove installed RAM.
# 0x98000000 + 0x28000000 ends at 0xC0000000: inside the low 2 GiB bank, above
# the splash carve-out (0x90001000 + 0x13ff000), and valid on both the 3 GiB and
# 4 GiB variants. The santoni image that boots uses exactly this window, so the
# same numbers are used for the payload header (mkpayload --phys-base) and the
# ADT -- they must agree or XNU's memory map and boot_args disagree.
MIDO_DRAM_BASE = 0x98000000
MIDO_DRAM_SIZE = 0x28000000           # 640 MiB
# GIC-400 (two A53 clusters, NO redistributor): /soc/interrupt-controller@b000000
# compatible "qcom,msm-qgic2", reg = <0xb000000 0x2000>, <0xb002000 0x2000>.
MIDO_GICD, MIDO_GICD_SIZE = 0x0b000000, 0x1000
MIDO_GICC, MIDO_GICC_SIZE = 0x0b002000, 0x2000
SOC_BASE = 0x01000000                 # arm-io ranges child base (reg is relative)
# Continuous-splash framebuffer: /reserved-memory/splash_region@0x90001000,
# label "cont_splash_mem", size 0x13ff000. One 1080x1920x4 image is 0x7e9000, so
# the region holds several buffers; we hand XNU the first one.
MIDO_FB = 0x90001000
MIDO_FB_W, MIDO_FB_H = 1080, 1920
# MEASURED convention, not a guess: the santoni reference ADT -- the one that
# actually displays on this SoC family -- declares
#   reg size 0x2A3000 == 720 * 1280 * 3,  stride 0x870 == 720 * 3,  depth 24
# i.e. the ABL splash framebuffer is RGB888, NOT ARGB8888. With 32 bpp the
# pitch is 1080 bytes too wide and every console row drifts 360 px, which is
# exactly the "double image with a diagonal ghost" seen on the device.
MIDO_FB_DEPTH = 24
MIDO_FB_STRIDE = MIDO_FB_W * MIDO_FB_DEPTH // 8
MIDO_FB_SIZE = MIDO_FB_W * MIDO_FB_H * MIDO_FB_DEPTH // 8
# eMMC: alias sdhc1 -> /soc/sdhci@7824900, compatible "qcom,sdhci-msm".
# The boot.img cmdline independently confirms it:
#   androidboot.bootdevice=7824900.sdhci androidboot.boot_devices=soc/7824900.sdhci
# reg = <0x7824900 0x500>, <0x7824000 0x800>, <0x7824e00 0x200>, <0x119d0000 0x400>
# Linux calls 0x7824900 "hc_mem" (SDHCI registers) and 0x7824000 "core_mem"
# (vendor core). The santoni ADT -- the one that boots -- names them
# reg=<core> and hc-reg=<hc>, so the same mapping is used here.
MIDO_EMMC_CORE, MIDO_EMMC_CORE_SIZE = 0x07824000, 0x1000
MIDO_EMMC_HC, MIDO_EMMC_HC_SIZE = 0x07824900, 0x00000500
# Debug UART: earlycon=msm_hsl_uart,0x78af000 -> /soc/serial@78af000 (BLSP1 UART2).
MIDO_UART, MIDO_UART_SIZE = 0x078af000, 0x1000
# Architected timer: /soc/timer@b120000, PPIs <1 1>..<1 4> = INTID 17..20.
MIDO_TIMER, MIDO_TIMER_SIZE = 0x0b120000, 0x1000
TIMEBASE = 19200000
# The santoni ADT that boots iOS says 0x14 (20). Its timer block is the same
# MSM8953-family one, so this is measured-by-proxy rather than guessed.
VTIMER_INTID = 20
# Real MPIDR values straight from the DTB's cpu@<unit-address> nodes:
# 0,1,2,3 then 0x100,0x101,0x102,0x103 -> two clusters of four A53 each.
MIDO_MPIDR = (0x000, 0x001, 0x002, 0x003, 0x100, 0x101, 0x102, 0x103)
MIDO_CLUSTER_SIZE = 4

# Canvas presets for /arm-io/disp0 + /chosen/display-scale.
#   x     : iPhone X logical canvas; patch 0094 scales it into the 1080x1920 panel
#   panel : the panel's own size, for kernels that do not do that scaling
CANVAS_X = (1125, 2436, 3)
CANVAS_PANEL = (MIDO_FB_W, MIDO_FB_H, 2)


def build(seed=None, dram_base=MIDO_DRAM_BASE, dram_size=MIDO_DRAM_SIZE,
          canvas='x', sdhci_node='mido-sdhci',
          sdhci_compat=('qcom,sdhci-msm', 'generic-sdhci'),
          fb_depth=MIDO_FB_DEPTH):
    """mido tree: 8x A53 (MPIDR-measured), GIC-400, /sweet-fbcon, eMMC root node."""
    canvas_w, canvas_h, display_scale = CANVAS_X if canvas == 'x' else CANVAS_PANEL
    seed = seed if seed is not None else os.urandom(64)
    phandle = iter(range(1, 1000))
    ph = lambda: ('AAPL,phandle', M.u32(next(phandle)))

    chosen = M.node('chosen', [
        ph(),
        ('dram-base', M.u64(dram_base)),
        ('dram-size', M.u64(dram_size)),
        ('random-seed', seed),
        ('debug-enabled', M.u32(1)),
        ('firmware-version', M.cstr('mido-shim')),
        ('system-firmware-version', M.cstr('mido-shim')),
        ('boot-manifest-hash', M.BOOT_MANIFEST_HASH),
        ('crypto-hash-method', M.cstr('sha2-384')),
        ('display-scale', M.u32(display_scale)),
        ('unique-chip-id', M.u64(M.SWEET_ECID)),
        ('chip-id', M.u32(0x8015)),         # A11 (iPhone X userland expectation)
        ('certificate-production-status', M.u32(0)),
        ('effective-production-status-ap', M.u32(0)),
        ('certificate-security-mode', M.u32(1)),
        ('effective-security-mode-sep', M.u32(1)),
    ], [M.node('memory-map', [ph()]),
        M.node('asmb', [ph(), ('lp-sip0', M.u64(0x6f)), ('lp-sip1', M.u32(1))])])

    # 8 Cortex-A53 cores as two clusters of four, using the MPIDR values the
    # device's own DTB declares (0..3, 0x100..0x103). The compatible string
    # keeps "qcom,kryo470" because SweetCPU's IONameMatch is exactly that;
    # "arm,cortex-a53" states what the silicon really is.
    cpu_nodes = []
    for core in range(8):
        cluster = core // MIDO_CLUSTER_SIZE
        core_in_cluster = core % MIDO_CLUSTER_SIZE
        compat = (M.cstr('arm,cortex-a53') + M.cstr('qcom,kryo470') + M.cstr('ARM,v8'))
        cpu_nodes.append(M.node(f'cpu{core}', [
            ph(),
            ('device_type', M.cstr('cpu')),
            ('compatible', compat),
            ('reg', M.u32(MIDO_MPIDR[core])),   # MPIDR Aff1:Aff0, NOT logical ID
            ('cpu-id', M.u32(core)),
            ('cluster-id', M.u32(cluster)),
            ('cluster-type', M.u32(0)),          # homogeneous CLUSTER_TYPE_SMP
            ('die-cluster-id', M.u32(cluster)),
            ('cluster-core-id', M.u32(core_in_cluster)),
            ('l2-cache-id', M.u32(core)),
            ('l3-cache-id', M.u32(0)),
            ('state', M.cstr('running' if core == 0 else 'stopped')),
            ('timebase-frequency', M.u32(TIMEBASE)),
            ('clock-frequency', M.u32(2016000000)),
            ('bus-frequency', M.u32(100000000)),
            ('memory-frequency', M.u32(1866000000)),
            ('peripheral-frequency', M.u32(TIMEBASE)),
            ('fixed-frequency', M.u32(TIMEBASE)),
        ]))
    cpus = M.node('cpus', [ph(), ('#address-cells', M.u32(1)),
                           ('#size-cells', M.u32(0))], cpu_nodes)

    # GIC-400: distributor + CPU interface, measured addresses (no redistributor).
    gic = M.node('gic', [
        ph(),
        ('compatible', M.cstr('qcom,msm-qgic2') + M.cstr('arm,gic-400')),
        ('interrupt-controller', b''),
        ('#interrupt-cells', M.u32(3)),
        ('virtual-timer-intid', M.u32(VTIMER_INTID)),
        ('reg', M.u64(MIDO_GICD - SOC_BASE, MIDO_GICD_SIZE,
                      MIDO_GICC - SOC_BASE, MIDO_GICC_SIZE)),
    ])

    arm_io = M.node('arm-io', [
        ph(),
        ('device_type', M.cstr('sweet-io')),
        ('compatible', M.cstr('sweet-io') + M.cstr('arm-io')),
        ('#address-cells', M.u32(2)),
        ('#size-cells', M.u32(2)),
        ('ranges', M.u64(0, SOC_BASE, 0x100000000 - SOC_BASE)),
    ], [gic,
        M.node('wdt', [ph(), ('no-pmu', M.u32(1))]),
        # Main display: IOMobileFramebuffer matches 'sweet,display' (patch 0046)
        # and reads the canvas size from here.
        M.node('disp0', [
            ph(),
            ('device_type', M.cstr('display-subsystem')),
            ('compatible', M.cstr('sweet,display')),
            ('canvas-width', M.u32(canvas_w)),
            ('canvas-height', M.u32(canvas_h)),
        ]),
        # Debug console: measured /soc/serial@78af000 (earlycon target).
        M.node('uart0', [
            ph(),
            ('device_type', M.cstr('serial')),
            ('compatible', M.cstr('mido,uart')),
            ('reg', M.u64(MIDO_UART, MIDO_UART_SIZE)),
        ]),
        # Architected timer block: measured /soc/timer@b120000.
        M.node('timer', [
            ph(),
            ('compatible', M.cstr('arm,armv8-timer')),
            ('reg', M.u64(MIDO_TIMER, MIDO_TIMER_SIZE)),
        ])])

    # XNU must be able to find the console framebuffer to print anything at all:
    # patch 0003 does SecureDTFindEntry("name", "sweet-fbcon"), so the node name
    # is a hard requirement, not a label.
    fbcon = M.node('sweet-fbcon', [
        ph(),
        ('reg', M.u64(MIDO_FB, MIDO_FB_W * MIDO_FB_H * fb_depth // 8)),
        ('width', M.u32(MIDO_FB_W)),
        ('height', M.u32(MIDO_FB_H)),
        ('stride', M.u32(MIDO_FB_W * fb_depth // 8)),
        ('depth', M.u32(fb_depth)),
    ])

    # eMMC host. The block-I/O driver is NOT in this tree yet (see docs/MIDO.md);
    # the node records the measured windows in the shape the known-booting
    # santoni driver reads: name -> reg (vendor core) + hc-reg (SDHCI) +
    # bus-width + non-removable. --sdhci-node lets the same ADT drive that
    # reference kernel, which looks the node up as "santoni-sdhci".
    emmc = M.node(sdhci_node, [
        ph(),
        ('compatible', b''.join(M.cstr(c) for c in sdhci_compat)),
        ('device_type', M.cstr('sdhci')),
        ('reg', M.u64(MIDO_EMMC_CORE, MIDO_EMMC_CORE_SIZE)),
        ('hc-reg', M.u64(MIDO_EMMC_HC, MIDO_EMMC_HC_SIZE)),
        ('bus-width', M.u32(8)),
        ('non-removable', b''),
    ])

    defaults = M.node('defaults', [ph(), ('no-effaceable-storage', M.u32(1))])
    buttons = M.node('buttons', [
        ph(),
        ('button-names', M.cstr('volup') + M.cstr('voldown') + M.cstr('hold')),
        ('home-button-type', M.u32(2)),
        ('opposed-power-vol-buttons', M.u32(1)),
    ])

    product_props = M.d22ap_product()
    product = [M.node('product', [ph()] + product_props)] if product_props else []

    # iPhone X identity for the userland; "xiaomi,sweet" is what the platform
    # expert in patch 0016 matches, "xiaomi,mido" is the truth on the board.
    compat = (M.cstr('D22AP') + M.cstr('iPhone10,3') + M.cstr('xiaomi,sweet')
              + M.cstr('xiaomi,mido') + M.cstr('qcom,msm8953') + M.cstr('AppleARM'))
    return M.node('device-tree', [
        ph(),
        ('compatible', compat),
        ('model', M.cstr('iPhone10,3')),
        ('target-type', M.cstr('D22')),
        ('target-sub-type', M.cstr('D22AP')),
        ('manufacturer', M.cstr('Apple Inc.')),
        ('#address-cells', M.u32(2)),
        ('#size-cells', M.u32(2)),
    ], [chosen, defaults, cpus, arm_io, fbcon, buttons, emmc] + product)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('out')
    ap.add_argument('--dump', action='store_true')
    ap.add_argument('--ram', type=int, choices=(3, 4), default=None,
                    help='installed RAM, only used to sanity-check the window')
    ap.add_argument('--dram-base', default=hex(MIDO_DRAM_BASE))
    ap.add_argument('--dram-size', default=hex(MIDO_DRAM_SIZE))
    ap.add_argument('--canvas', choices=('x', 'panel'), default='x',
                    help="x = iPhone X logical canvas (patch 0094 scales it); "
                         "panel = 1080x1920 for kernels without that scaling")
    ap.add_argument('--sdhci-node', default='mido-sdhci',
                    help="eMMC node name; the santoni reference kernel wants "
                         "'santoni-sdhci'")
    ap.add_argument('--sdhci-compat', default='qcom,sdhci-msm,generic-sdhci',
                    help='comma separated compatible strings for the eMMC node')
    ap.add_argument('--fb-depth', type=int, choices=(24, 32), default=MIDO_FB_DEPTH,
                    help='bits per pixel; stride follows (24 = mido/santoni 24-bit '
                         'splash, 32 = sweet/polaris style). Both are supported by '
                         'xnu/patches-mido/0096-mido-fbcon-24bpp.patch')
    a = ap.parse_args()
    dram_base = int(a.dram_base, 0)
    dram_size = int(a.dram_size, 0)
    if a.ram is not None and dram_base + dram_size > a.ram * (1 << 30):
        ap.error(f'window 0x{dram_base:x}+0x{dram_size:x} does not fit in {a.ram} GiB')
    root = build(dram_base=dram_base, dram_size=dram_size, canvas=a.canvas,
                 sdhci_node=a.sdhci_node,
                 sdhci_compat=tuple(s.strip() for s in a.sdhci_compat.split(',') if s.strip()),
                 fb_depth=a.fb_depth)
    blob = M.adt.serialize(root)
    open(a.out, 'wb').write(blob)
    if a.dump:
        M.adt.dump(M.adt.parse(blob)[0])
    print(f'{a.out}: {len(blob)} bytes', file=sys.stderr)
