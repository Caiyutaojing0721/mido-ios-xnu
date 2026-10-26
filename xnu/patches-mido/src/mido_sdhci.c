/*
 * Redmi Note 4X (mido) eMMC 5.1 root-device driver (DIAGNOSTIC MILESTONE).
 *
 * mido has eMMC 5.1 on SDCC1 (SDHCI), NOT UFS. This revision is a
 * take-over + register dump only: it maps the SDHCI window the bootloader /
 * ABL left behind, prints the present-state / capabilities / clock / power so
 * the serial console alone tells us whether the host is alive and what the
 * card wants, then refuses to mount (ENXIO) so the boot stalls after the
 * dump instead of panicking. No command is issued yet: the SDHCI command /
 * data path and the eMMC CMD sequence come in the next milestone once we
 * know the host state from this dump.
 *
 * Register window (stock boot.img DTB + shim/emmc.c):
 *   /soc/sdhci@7824900  reg = <0x7824900 0x500>  (standard SDHCI set)
 *                       hc-reg = <0x7824000 0x1000> (vendor HC block)
 * The 0x500 core window holds PRESENT_STATE @0x24, CAPABILITIES @0xfc,
 * HOST_CTRL @0x28, PWR_CTRL @0x2c, BLKSZ @0x30, TRANS @0x38, CMD @0x3c.
 * These offsets are the ones shim/emmc.c validated against real hardware.
 *
 * Modeled on bsd/dev/sweet_ufs.c (the working UFS root in this tree) for the
 * XNU KPI: ml_io_map, SecureDTLookupEntry/SecureDTGetProperty, printf, the
 * bdevsw/cdevsw + devfs + IOFindBSDRoot registration shape.
 */
#if defined(ARM64_BOARD_CONFIG_SWEET)
#include <sys/param.h>
#include <sys/systm.h>
#include <sys/conf.h>
#include <sys/disk.h>
#include <sys/buf.h>
#include <sys/dev.h>
#include <pexpert/pexpert.h>
#include <pexpert/device_tree.h>
#include <libkern/libkern.h>
#include <machine/machine_routines.h>

extern vm_offset_t ml_io_map(vm_offset_t phys_addr, vm_size_t size);

/* SDHCI standard register window (core). Vendor HC block is at 0x07824000. */
#ifndef MIDO_SDCC1_CORE
#define MIDO_SDCC1_CORE		0x07824900UL
#endif
#ifndef MIDO_SDCC1_CORE_SIZE
#define MIDO_SDCC1_CORE_SIZE	0x500UL
#endif

/* shim/emmc.c-validated offsets within the core window. */
#define REG_PRESENT	0x24
#define REG_HOSTCTL	0x28
#define REG_PWRCTL	0x2c
#define REG_BLKSZ	0x30
#define REG_TRANS	0x38
#define REG_CMD		0x3c
#define REG_CAPA0	0xfc
#define REG_CAPA1	0x100

static volatile uint32_t *g_core;	/* mapped VA of the core window */

static inline uint32_t
rd32(uint32_t off)
{
	return *(volatile uint32_t *)(g_core + off / sizeof(uint32_t));
}

static int
mido_sdhci_map(void)
{
	DTEntry node;
	uintptr_t const *reg = NULL;
	unsigned int size = 0;
	uint64_t base = MIDO_SDCC1_CORE;
	uint64_t winsz = MIDO_SDCC1_CORE_SIZE;

	if (SecureDTLookupEntry(NULL, "/mido-sdhci", &node) == kSuccess &&
	    SecureDTGetProperty(node, "reg", (void const **)&reg, &size) == kSuccess &&
	    size >= 2 * sizeof(uintptr_t)) {
		base = reg[0];
		winsz = reg[1];
	}
	vm_offset_t va = ml_io_map((vm_offset_t)base, (vm_size_t)winsz);
	if (!va) {
		printf("MIDO SDHCI: ml_io_map(0x%llx, 0x%llx) failed\n", base, winsz);
		return ENOMEM;
	}
	g_core = (volatile uint32_t *)va;
	printf("MIDO SDHCI: core window mapped va=%p phys=0x%llx\n", g_core, base);
	return 0;
}

/*
 * Dump the SDHCI host state. Read-only: no command is issued, so this cannot
 * disturb the controller the bootloader is still using.
 */
void
mido_sdhci_diag(void)
{
	if (!g_core && mido_sdhci_map() != 0) {
		return;
	}
	uint32_t prs   = rd32(REG_PRESENT);
	uint32_t hc    = rd32(REG_HOSTCTL);
	uint32_t pwr   = rd32(REG_PWRCTL);
	uint32_t blksz = rd32(REG_BLKSZ);
	uint32_t trans = rd32(REG_TRANS);
	uint32_t capa0 = rd32(REG_CAPA0);
	uint32_t capa1 = rd32(REG_CAPA1);

	printf("MIDO SDHCI DIAG:\n");
	printf("  PRESENT  0x%08x\n", prs);
	printf("    card_inserted=%u cmd_inhibit=%u dat_inhibit=%u\n",
	    (prs >> 16) & 1u, prs & 1u, (prs >> 1) & 1u);
	printf("    clk_stable=%u buf_wr=%u buf_rd=%u\n",
	    (prs >> 3) & 1u, (prs >> 10) & 1u, (prs >> 11) & 1u);
	printf("  HOSTCTL  0x%08x  PWRCTL 0x%08x\n", hc, pwr);
	printf("  BLKSZ    0x%08x  TRANS  0x%08x\n", blksz, trans);
	printf("  CAPA0    0x%08x  CAPA1  0x%08x\n", capa0, capa1);
	printf("    spec_ver=0x%x baseclk=%uMHz adma2=%u v3_64=%u\n",
	    capa0 & 0xffu, (capa0 >> 8) & 0xffu, (capa0 >> 19) & 1u,
	    (capa0 >> 27) & 1u);
	printf("    max_block_len_log2=0x%x (=%u bytes)\n",
	    (capa0 >> 16) & 0x3u, 1u << ((capa0 >> 16) & 0x3u) << 10);
}

/*
 * IOFindBSDRoot hook target for rd=emmcroot0. Diagnostic only: dump and
 * refuse to mount so the boot stalls after the dump (no panic) while we
 * learn the host state. The next milestone returns 0 here and fills *dev.
 */
int
mido_emmc_rootdev(dev_t *dev)
{
	(void)dev;
	mido_sdhci_diag();
	printf("MIDO SDHCI: diagnostic only; root-on-eMMC not yet implemented\n");
	return ENXIO;
}
#endif /* ARM64_BOARD_CONFIG_SWEET */
