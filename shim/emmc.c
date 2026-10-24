/*
 * Redmi Note 4X (mido) eMMC / SDHCI probe (read-only).
 *
 * mido has eMMC 5.1 on SDCC1 (SDHCI), NOT UFS like sweet/polaris. This probe
 * only READS the SDHCI registers ABL/Android left behind and prints their
 * state plus the capability register. There is deliberately no write path:
 * no DMA setup, no clock/power control, no CMD writes. A real
 * root-on-eMMC backend needs an owned, reversible SDHCI driver first
 * (see docs/MIDO.md), and this file must gain it before any payload uses
 * rd=emmcroot0.
 *
 * Addresses are measured from the stock boot.img DTB:
 *   /soc/sdhci@7824900 (compatible qcom,sdhci-msm)
 *   reg = <0x7824900 0x500>, <0x7824000 0x800>, <0x7824e00 0x200>, ...
 *   boot cmdline: androidboot.bootdevice=7824900.sdhci
 * The 0x500 window at 0x7824900 holds the SDHCI standard register set
 * (PRESENT_STATE @0x24, CAPABILITIES @0xfc); 0x7824000 is the vendor HC block.
 *
 * Register map: SDHCI (SD Host Controller Standard Spec).
 */
#include <stdint.h>
#include "console.h"

#if defined(MIDO)
#ifndef MIDO_SDCC1
/* SDHCI core (standard register set) for the eMMC controller on MSM8953. */
#define MIDO_SDCC1	0x07824900UL
#endif
#ifndef MIDO_SDCC1_HC
/* Vendor HC block paired with the above (diagnostics only; never written). */
#define MIDO_SDCC1_HC	0x07824000UL
#endif
#ifndef MIDO_SDCC1_WIN
#define MIDO_SDCC1_WIN	0x500UL
#endif

#define REG_SDHCI_DMA		0x00
#define REG_SDHCI_PRESENT	0x24
#define REG_SDHCI_HOST_CTRL	0x28
#define REG_SDHCI_PWR_CTRL	0x2c
#define REG_SDHCI_BLKSZ		0x30
#define REG_SDHCI_TRANS		0x38
#define REG_SDHCI_CMD		0x3c
#define REG_SDHCI_CAPA0		0xfc
#define REG_SDHCI_CAPA1		0x100

static inline uint32_t emmc_rd(uint32_t off)
{
	return *(volatile uint32_t *)(MIDO_SDCC1 + off);
}

/*
 * Print only. No SDHCI write is ever issued here; a mistake that writes to
 * the controller could reprogram the very device we booted from.
 */
void test_emmc(void)
{
	con_str("EMMC", "milestone 5 probe (read-only)");
	uint32_t prs = emmc_rd(REG_SDHCI_PRESENT);
	uint32_t ctrl = emmc_rd(REG_SDHCI_HOST_CTRL);
	uint32_t pwr = emmc_rd(REG_SDHCI_PWR_CTRL);
	uint32_t capa0 = emmc_rd(REG_SDHCI_CAPA0);
	uint32_t capa1 = emmc_rd(REG_SDHCI_CAPA1);

	con_hex2("EMMC", "base=", MIDO_SDCC1, "prs=", prs);
	con_hex2("EMMC", "ctrl=", ctrl, "pwr=", pwr);
	/* SDHCI present state: bit1 = card, bit0 = re-tuning, bits = CMD/DAT. */
	con_hex2("EMMC", "cd=", (prs >> 16) & 3u, "inh=", prs & 3u);
	con_hex2("EMMC CAP", "0=", capa0, "1=", capa1);
	/*
	 * Decode CAPA0 so the screen alone tells us whether this is really the
	 * eMMC 5.1 host: spec version 7 == SDHCI 5.0 (needed for HS400),
	 * bits 15:8 = base clock in MHz, bits 21:16 = max block length.
	 */
	con_hex2("EMMC VER", "spec=", (capa0 >> 0) & 0xffu,
		 "clk_mhz=", (capa0 >> 8) & 0xffu);
	con_hex2("EMMC FEAT", "blk=", ((capa0 >> 16) & 0x3fu) << 10,
		 "adma2=", (capa0 >> 19) & 1u);
	con_hex2("EMMC ADDR", "bit64=", (capa0 >> 27) & 1u,
		 "hc=", MIDO_SDCC1_HC);
	con_hex("EMMC WIN", MIDO_SDCC1_WIN);

	/*
	 * We cannot tell with a read whether the card is usable for the root
	 * filesystem. The XNU side (mido_emmc) is a disabled gate until a real
	 * SDHCI/root backend exists; see docs/MIDO.md.
	 */
	con_str("EMMC", "read-only register probe only; no root-on-eMMC backend");
}
#else
/* Non-mido builds: no eMMC probe at all. */
void test_emmc(void) { }
#endif
