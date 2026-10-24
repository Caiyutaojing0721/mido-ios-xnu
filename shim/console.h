#pragma once
#include <stdint.h>

/*
 * Early console framebuffer. sweet's splash region starts at 0x9c000000;
 * polaris (Mi MIX 2S) uses the 1080x2160 cont_splash_region at 0x9d400000;
 * mido (Redmi Note 4X) uses the 1080x1920 cont_splash_region at 0x90001000
 * (/reserved-memory/splash_region@0x90001000, measured from the stock DTB).
 * Platform selects the geometry; the XNU early console takes the same values
 * from boot_args (patch 0083).
 */
#ifdef POLARIS
#define PLATFORM_NAME	"polaris"
#define FB_BASE		0x9d400000UL
#define FB_H		2160
#define FB_BPP		4
#elif defined(MIDO)
#define PLATFORM_NAME	"mido"
#define FB_BASE		0x90001000UL
#define FB_H		1920
/*
 * MEASURED, not assumed: the santoni reference ADT -- which does display on
 * this SoC family -- declares reg size 0x2A3000 (= 720*1280*3), stride 0x870
 * (= 720*3) and depth 24. The ABL splash buffer is therefore RGB888. Writing
 * it as 32 bpp makes the pitch 1080 bytes too wide, so every console row
 * drifts 360 px -- the "double image with a diagonal ghost" on the device.
 */
#define FB_BPP		3
#else
#define PLATFORM_NAME	"sweet"
#define FB_BASE		0x9c000000UL
#define FB_H		2400
#define FB_BPP		4
#endif
#define FB_W		1080
#define FB_STRIDE	(FB_W * FB_BPP)

#define RED	0xffff0000U
#define BLUE	0xff0000a0U
#define WHITE	0xffffffffU
#define YELLOW	0xffffff00U
#define GREEN	0xff00ff00U

extern volatile uint32_t *con_fb;

/* Use the geometry the display hardware is actually scanning. */
void con_config(void *fb, int w, int h, int stride_bytes, int bpp);

void con_clear(uint32_t bg);
void con_skip(void);
void con_str(const char *label, const char *s);
void con_hex(const char *label, uint64_t v);
void con_hex8(const char *label, uint32_t v);
/* label, then "a=xxxxxxxx b=xxxxxxxx" style pairs of 32-bit values */
void con_hex2(const char *label, const char *a, uint32_t va, const char *b, uint32_t vb);
