#!/bin/bash
# Extract co-processor program from master ROM for Ghidra import.
#
# The co-proc runs from shared RAM at $E000-$FFFF (8KB).
# Master copies two chunks from ROM:
#   ROM $E200-$EAF0 → co-proc $E200-$EAF0 (2289 bytes, main code)
#   ROM $EAF1-$EB00 → co-proc $FFF0-$FFFF (16 bytes, vectors)
# The rest ($E000-$E1FF, $EAF1-$FFEF) is zeroed.
#
# This script builds a full 8KB image loadable at $E000 in Ghidra.

ROM="${1:-/home/jeff/bastel/mame/mame/roms/ex20/mk1_ic3_630469_w_s3_fixed.bin}"
OUT="${2:-cop_e000.bin}"

# ROM is loaded at $8000, so file offset = addr - $8000
# $E200 = file offset $6200
# $EB00 = file offset $6B00

# Create 8KB of zeros
dd if=/dev/zero bs=1 count=8192 of="$OUT" 2>/dev/null

# Copy main code: ROM $E200-$EAF0 → output offset $0200-$0AF0
dd if="$ROM" bs=1 skip=$((0x6200)) count=$((0xEAF1 - 0xE200)) seek=$((0x0200)) of="$OUT" conv=notrunc 2>/dev/null

# Copy vectors: ROM $EAF1-$EB00 → output offset $1FF0-$1FFF
dd if="$ROM" bs=1 skip=$((0x6AF1)) count=16 seek=$((0x1FF0)) of="$OUT" conv=notrunc 2>/dev/null

echo "Created $OUT (8192 bytes, base address \$E000)"
echo ""
echo "Ghidra import settings:"
echo "  Language: 6809:BE:16:default"
echo "  Base address: E000"
echo "  Size: 8192 bytes"
echo ""
echo "Vectors at \$FFF0:"
xxd -s $((0x1FF0)) -l 16 -g 2 "$OUT" | sed 's/^/  /'
echo ""
echo "  FFF0 Reserved = $(xxd -s $((0x1FF0)) -l 2 -p "$OUT")"
echo "  FFF2 SWI3     = $(xxd -s $((0x1FF2)) -l 2 -p "$OUT")"
echo "  FFF4 SWI2     = $(xxd -s $((0x1FF4)) -l 2 -p "$OUT")"
echo "  FFF6 FIRQ     = $(xxd -s $((0x1FF6)) -l 2 -p "$OUT")"
echo "  FFF8 IRQ      = $(xxd -s $((0x1FF8)) -l 2 -p "$OUT")"
echo "  FFFA SWI      = $(xxd -s $((0x1FFA)) -l 2 -p "$OUT")"
echo "  FFFC NMI      = $(xxd -s $((0x1FFC)) -l 2 -p "$OUT")"
echo "  FFFE RESET    = $(xxd -s $((0x1FFE)) -l 2 -p "$OUT")"
echo ""
echo "First bytes at \$E200:"
xxd -s $((0x0200)) -l 32 -g 1 "$OUT" | sed 's/^/  /'
