#!/bin/bash
# Build the shim and wrap it in a Qualcomm Android boot image.
#
#   PLATFORM=polaris shim/build.sh                 # Mi MIX 2S probe image
#   PLATFORM=polaris shim/build.sh payload.bin     # boot the XNU payload
#   shim/build.sh                                  # original sweet target
#
# Works with aarch64-linux-gnu-gcc, aarch64-unknown-linux-gnu-gcc, or the
# bundled Zig (Windows/macOS/Linux). Packing needs only Python.
set -euo pipefail
CALLER=$(pwd)
cd "$(dirname "$0")"
ROOT=$(cd .. && pwd)
TOOLS=$ROOT/tools
OUT=$ROOT/out
PLATFORM=${PLATFORM:-sweet}
PAYLOAD=${1:-}
# The script cd's into shim/, so resolve a relative payload against the
# caller's directory instead of silently failing to find it.
if [ -n "$PAYLOAD" ]; then
	case "$PAYLOAD" in
		/*) ;;
		*) PAYLOAD=$CALLER/$PAYLOAD ;;
	esac
fi
mkdir -p "$OUT"

PY=${PY:-}
if [ -z "$PY" ]; then
	for c in python python3 py; do
		if $c -c 'import sys' >/dev/null 2>&1; then PY=$c; break; fi
	done
fi
[ -n "$PY" ] || { echo 'error: no working python' >&2; exit 1; }

case "$PLATFORM" in
	polaris) CROSSFLAGS="-DPOLARIS" ;;
	mido)    CROSSFLAGS="-DMIDO" ;;
	*)       CROSSFLAGS="" ;;
esac

if [ -n "${CROSS:-}" ]; then
	CC="${CROSS}gcc"
	OBJCOPY="${CROSS}objcopy"
elif command -v aarch64-linux-gnu-gcc >/dev/null; then
	CC=aarch64-linux-gnu-gcc
	OBJCOPY=aarch64-linux-gnu-objcopy
elif command -v aarch64-unknown-linux-gnu-gcc >/dev/null; then
	CC=aarch64-unknown-linux-gnu-gcc
	OBJCOPY=aarch64-unknown-linux-gnu-objcopy
else
	ZIG=${ZIG:-}
	if [ -z "$ZIG" ]; then
		ZIG=$(ls "$ROOT"/../tools/zig-*/zig.exe "$ROOT"/../tools/zig-*/zig 2>/dev/null | head -1 || true)
	fi
	if [ -z "$ZIG" ] && command -v zig >/dev/null; then ZIG=zig; fi
	[ -n "$ZIG" ] || {
		echo 'error: no aarch64 cross compiler and no zig found' >&2
		echo '       set CROSS=aarch64-linux-gnu- or ZIG=/path/to/zig' >&2
		exit 1
	}
	CC="$ZIG cc -target aarch64-freestanding"
	OBJCOPY="$ZIG objcopy"
fi
CC=${CC:-aarch64-linux-gnu-gcc}
OBJCOPY=${OBJCOPY:-aarch64-linux-gnu-objcopy}

CFLAGS="-O2 -g0 -ffreestanding -nostdlib -fno-builtin -fno-pic -mgeneral-regs-only -mstrict-align \
	-fno-stack-protector -fno-asynchronous-unwind-tables -fno-unwind-tables \
	-mno-outline-atomics -Wall -Wextra $CROSSFLAGS ${EXTRA_CFLAGS:-}"
OBJS=()
for f in start.S shim.c console.c mmu.c fdt.c loader.c ufs.c emmc.c libc.c; do
	o=$OUT/${f%.*}.o
	$CC $CFLAGS -c "$f" -o "$o"
	OBJS+=("$o")
done

# C objects may not contain absolute relocations: the image is linked at 0
# but executes at the address ABL chose.
$PY "$TOOLS/elfcheck.py" abs64 "${OBJS[@]:1}"

$CC $CFLAGS -T shim.ld -Wl,--no-gc-sections -Wl,--build-id=none "${OBJS[@]}" -o "$OUT/shim.elf"
$PY "$TOOLS/elfcheck.py" relocs "$OUT/shim.elf"
$PY "$TOOLS/elf2image.py" "$OUT/shim.elf" "$OUT/Image"

# .bss/stacks are NOBITS; ABL copies kernel_size bytes, so pad to _end.
size=$($PY "$TOOLS/elfcheck.py" symbol "$OUT/shim.elf" _end)
if command -v truncate >/dev/null; then
	truncate -s "$size" "$OUT/Image"
else
	$PY -c "import os; os.truncate(r'$OUT/Image', $size)"
fi
$PY "$TOOLS/elfcheck.py" image "$OUT/Image"

# Ramdisk: an xnu payload (mkpayload.py output) if given, else empty newc.
if [ -n "$PAYLOAD" ]; then
	cp "$PAYLOAD" "$OUT/ramdisk.bin"
	echo "ramdisk: payload $PAYLOAD" >&2
	# ABL puts the ramdisk at 0x1000000; the stock DTB address is 0x1dea000
	# (polaris) / 0x1f00000 (sweet). A larger payload overlaps the DTB.
	if [ "$PLATFORM" = polaris ]; then
		rd_limit=$((0x1dea000 - 0x1000000))
		dtb_pa=0x1dea000
	elif [ "$PLATFORM" = mido ]; then
		# MEASURED: mido's stock boot.img keeps the DTB *inside* the kernel
		# blob (Image.gz followed by the DTB), not at a fixed physical address.
		# There is therefore no ramdisk/DTB collision like polaris has; the
		# ramdisk at 0x81000000 can grow until the cont_splash reservation at
		# 0x90001000 (~240 MiB).
		rd_limit=$((0x90001000 - 0x81000000))
		dtb_pa=0
	else
		rd_limit=$((0x1f00000 - 0x1000000))
		dtb_pa=0x1f00000
	fi
	psize=$(wc -c < "$PAYLOAD" | tr -d ' ')
	if [ "$psize" -ge "$rd_limit" ]; then
		echo "warning: payload is $psize bytes; ABL loads it at 0x1000000 and the DTB at $dtb_pa," >&2
		echo "         so the payload can overwrite the device tree. Expect a boot failure." >&2
	fi
else
	$PY "$TOOLS/mkcpio.py" "$OUT/ramdisk.bin"
fi

KERNEL=$OUT/kernel.bin
if [ "$PLATFORM" = polaris ]; then
	# polaris ABL rejects an uncompressed Image and drops to fastboot; it
	# accepts the same kernel gzip-compressed with the stock DTB appended.
	gzip -9 -n -c "$OUT/Image" > "$KERNEL"
elif [ "$PLATFORM" = mido ]; then
	# MEASURED from the device's stock boot.img: the kernel blob is a gzip
	# stream followed by the DTB, and kernel_size covers both (the FDT magic
	# sits 12,366,891 bytes into a 12,612,199-byte kernel blob and the DTB ends
	# exactly at the blob end). Reproduce that layout, or the ABL has no DTB.
	#
	# But the shim is NOT a Linux kernel: nothing would decompress a gzip
	# stream here, so the Image goes in raw. ABL locates the DTB by walking
	# FDT magic from the arm64 header's image_size, which works for a raw
	# Image exactly as it does for the santoni reference shim.
	MIDO_DTB=$OUT/mido-stock.dtb
	if [ ! -f "$MIDO_DTB" ]; then
		for cand in "$ROOT/dtb/mido_stock/mido.dtb" "$ROOT/dtb/boot_unpacked/dtb"; do
			if [ -f "$cand" ]; then
				cp "$cand" "$MIDO_DTB"
				break
			fi
		done
	fi
	# The stock mido kernel is gzip + appended DTB, so ABL definitely accepts
	# that shape. A raw Image + DTB is what the santoni reference uses, but the
	# mido ABL rejected our raw build with "dtb not found", so both layouts are
	# buildable here: set MIDO_KERNEL_GZIP=1 for the stock-exact one.
	if [ -n "${MIDO_KERNEL_GZIP:-}" ]; then
		gzip -9 -n -c "$OUT/Image" > "$KERNEL"
		echo "kernel: Image.gz (stock-exact, MIDO_KERNEL_GZIP=1)" >&2
	else
		cp "$OUT/Image" "$KERNEL"
	fi
	if [ -f "$MIDO_DTB" ]; then
		cat "$MIDO_DTB" >> "$KERNEL"
		echo "kernel: + $(wc -c < "$MIDO_DTB" | tr -d ' ') byte stock DTB" >&2
	else
		echo 'warning: no mido stock DTB found (expected dtb/mido_stock/mido.dtb).' >&2
		echo '         Extract it with: python3 tools/mido_dtb.py <boot.img> --extract dtb/mido_stock/mido.dtb' >&2
	fi
else
	cp "$OUT/Image" "$KERNEL"
fi
if [ "$PLATFORM" = polaris ]; then
	DTB=$OUT/polaris-dtb.bin
	if [ ! -f "$DTB" ]; then
		[ -d "$ROOT/dtb/polaris_stock/fdt" ] || {
			echo 'error: dtb/polaris_stock/fdt missing' >&2
			echo '       extract the stock boot.img and run tools/unpack_bootv2.py' >&2
			exit 1
		}
		$PY "$TOOLS/polaris_dtb.py" "$ROOT/dtb/polaris_stock/fdt" "$DTB"
	fi
	cat "$DTB" >> "$KERNEL"
	cat > "$OUT/polaris.cmdline" <<'EOF'
console=ttyMSM0,115200n8 androidboot.hardware=qcom androidboot.console=ttyMSM0 msm_rtb.filter=0x237 ehci-hcd.park=3 service_locator.enable=1 swiotlb=2048 androidboot.configfs=true loop.max_part=7 androidboot.usbcontroller=a600000.dwc3 androidboot.boot_devices=soc/1d84000.ufshc cgroup_disable=pressure androidboot.selinux=permissive
EOF
	# A payload build is the full XNU image; keep it apart from the probe image.
	IMG=$OUT/shim-polaris-boot.img
	[ -n "$PAYLOAD" ] && IMG=$OUT/shim-polaris-xnu.img
	$PY "$TOOLS/mkbootimg.py" -o "$IMG" \
		--kernel "$KERNEL" --ramdisk "$OUT/ramdisk.bin" \
		--header_version 1 --pagesize 4096 --base 0 \
		--kernel_offset 0x8000 --ramdisk_offset 0x1000000 \
		--tags_offset 0x100 --dtb_offset 0x1dea000 \
		--os_version 0x200001a2 \
		--cmdline "$(cat "$OUT/polaris.cmdline")"
elif [ "$PLATFORM" = mido ]; then
	# All geometry below is MEASURED from the stock crDroid boot.img header:
	#   kernel_addr 0x80008000, ramdisk_addr 0x81000000, tags_addr 0x80000100,
	#   page_size 2048, header v0, os_version 0x1e000191.
	# The DTB already rode along inside $KERNEL (see above), so no --dtb here.
	cat > "$OUT/mido.cmdline" <<'EOF'
console=ttyMSM0,115200n8 androidboot.hardware=qcom androidboot.console=ttyMSM0 msm_rtb.filter=0x237 ehci-hcd.park=3 androidboot.bootdevice=7824900.sdhci androidboot.boot_devices=soc/7824900.sdhci androidboot.selinux=permissive
EOF
	# A payload build is the full XNU image; keep it apart from the probe image.
	IMG=$OUT/shim-mido-boot.img
	[ -n "$PAYLOAD" ] && IMG=$OUT/shim-mido-xnu.img
	[ -n "${MIDO_KERNEL_GZIP:-}" ] && IMG=${IMG%.img}-gz.img
	# base 0 + absolute offsets reproduces the stock header field-for-field
	# (including second_addr == 0, which --base 0x80000000 would break).
	$PY "$TOOLS/mkbootimg.py" -o "$IMG" \
		--kernel "$KERNEL" --ramdisk "$OUT/ramdisk.bin" \
		--header_version 0 --pagesize 2048 --base 0 \
		--kernel_offset 0x80008000 --ramdisk_offset 0x81000000 \
		--tags_offset 0x80000100 --second_offset 0 \
		--os_version 0x1e000191 \
		--cmdline "$(cat "$OUT/mido.cmdline")"
else
	if [ -f "$ROOT/dtb/boot_unpacked/dtb" ]; then
		cat "$ROOT/dtb/boot_unpacked/dtb" >> "$KERNEL"
	fi
	IMG=$OUT/shim-boot.img
	[ -n "$PAYLOAD" ] && IMG=$OUT/shim-xnu.img
	$PY "$TOOLS/mkbootimg.py" -o "$IMG" \
		--kernel "$KERNEL" --ramdisk "$OUT/ramdisk.bin" \
		--header_version 2 --pagesize 4096 --base 0 \
		--kernel_offset 0x8000 --ramdisk_offset 0x1000000 \
		--tags_offset 0x100 --dtb_offset 0x1f00000 \
		--os_version 17.0.0 --os_patch_level 2035-06 \
		--cmdline "console=null"
fi

ls -l "$OUT/Image" "$OUT/ramdisk.bin" "$KERNEL"
ls -l "$IMG"
