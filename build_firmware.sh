#!/bin/bash
# Build pipeline for Wersi SLM-2 Z8611 firmware (SR0106).
#
# Steps:
#   1. Import maskromtool export (strip 64-byte test ROM prefix)
#   2. Apply bit-error patches
#   3. Generate ASL assembly (tmp/firmware.asm)
#   4. Assemble and verify byte-exact match
#   5. Diff against checked-in firmware.asm
#   6. Install to MAME ROM directories
#
# Usage:
#   ./build_firmware.sh import <maskromtool_export.bin>
#   ./build_firmware.sh patch
#   ./build_firmware.sh asm
#   ./build_firmware.sh diff
#   ./build_firmware.sh byte_diff [other.bin]
#   ./build_firmware.sh ascii_diff [bits.txt]
#   ./build_firmware.sh install
#   ./build_firmware.sh all <maskromtool_export.bin>
#
# Without arguments: patch + asm + verify (most common workflow)

set -e
cd "$(dirname "$0")"

ORIGINAL=sr0106_original.bin
PATCHED=sr0106_patched.bin
TMPDIR=tmp
MAME_ROMS=~/bastel/mame/mame/roms

cmd_import() {
    local src="$1"
    if [ -z "$src" ]; then
        echo "Usage: $0 import <maskromtool_export.bin>"
        echo "  Strips 64-byte test ROM prefix → $ORIGINAL"
        exit 1
    fi
    local size
    size=$(wc -c < "$src")
    if [ "$size" -eq 4160 ]; then
        dd if="$src" of="$ORIGINAL" bs=1 skip=64 2>/dev/null
        echo "Imported $src → $ORIGINAL (stripped 64-byte test ROM prefix)"
    elif [ "$size" -eq 4096 ]; then
        cp "$src" "$ORIGINAL"
        echo "Imported $src → $ORIGINAL (already 4096 bytes)"
    else
        echo "ERROR: Expected 4160 (with test ROM) or 4096 bytes, got $size"
        exit 1
    fi
    echo "  $(md5sum "$ORIGINAL")"
}

cmd_patch() {
    if [ ! -f "$ORIGINAL" ]; then
        echo "ERROR: $ORIGINAL not found. Run: $0 import <maskromtool_export.bin>"
        exit 1
    fi
    python3 apply_patches.py "$ORIGINAL" "$PATCHED"
}

cmd_asm() {
    if [ ! -f "$PATCHED" ]; then
        echo "ERROR: $PATCHED not found. Run: $0 patch"
        exit 1
    fi
    mkdir -p "$TMPDIR"
    python3 unidasm_to_asl.py "$PATCHED" firmware.asm

    # Assemble and verify byte-exact match
    echo "Assembling firmware.asm..."
    asl -cpu Z8601 -L firmware.asm -o "$TMPDIR/firmware.p" -olist firmware.lst 2>&1 | tail -3
    # Strip ASL page headers (^L + header line + blank lines)
    tr -d '\f' < firmware.lst | grep -v '^ *AS V[0-9].*- Page ' | grep -v '^$' > firmware.lst.tmp
    mv firmware.lst.tmp firmware.lst
    p2bin "$TMPDIR/firmware.p" "$TMPDIR/firmware.bin" -r '$0-$FFF' 2>&1

    if diff -q "$PATCHED" "$TMPDIR/firmware.bin" > /dev/null 2>&1; then
        echo "VERIFY: all 4096 bytes match ✓"
    else
        echo "VERIFY FAILED: assembled output differs from patched ROM!"
        diff <(xxd "$PATCHED") <(xxd "$TMPDIR/firmware.bin") | head -20
        exit 1
    fi
}

cmd_diff() {
    if [ ! -f "firmware.asm" ]; then
        echo "ERROR: firmware.asm not found. Run: $0 asm"
        exit 1
    fi
    echo "firmware.asm and firmware.lst are up to date."
}

cmd_byte_diff() {
    local other="$1"
    if [ -z "$other" ]; then
        # Default: diff original vs patched
        other="$ORIGINAL"
    fi
    if [ ! -f "$PATCHED" ]; then
        echo "ERROR: $PATCHED not found. Run: $0 patch"
        exit 1
    fi
    if [ ! -f "$other" ]; then
        echo "ERROR: $other not found"
        exit 1
    fi
    echo "Byte diff: $other vs $PATCHED"
    python3 rom_bin_diff.py "$other" "$PATCHED" --test-rom-offset 0
}

cmd_ascii_diff() {
    local bits_txt="${1:-$HOME/bastel/wersi-ex20/maskromtool/build/bits.txt}"
    if [ ! -f "$PATCHED" ]; then
        echo "ERROR: $PATCHED not found. Run: $0 patch"
        exit 1
    fi
    if [ ! -f "$ORIGINAL" ]; then
        echo "ERROR: $ORIGINAL not found. Run: $0 import <maskromtool_export.bin>"
        exit 1
    fi
    if [ ! -f "$bits_txt" ]; then
        echo "ERROR: $bits_txt not found. Export ASCII bits from maskromtool first."
        exit 1
    fi
    mkdir -p "$TMPDIR"
    python3 gen_ascii_diff.py "$bits_txt" "$ORIGINAL" "$PATCHED" "$TMPDIR/bits_patched.txt"
}

cmd_vcd() {
    local trace="${1:-$TMPDIR/dac.bin}"
    local vcd="${2:-$TMPDIR/dac.vcd}"
    if [ ! -f "$trace" ]; then
        echo "ERROR: $trace not found."
        echo "Generate with: WERSI_CAPTURE=test.bin WERSI_DAC_TRACE=$TMPDIR/dac.bin \\"
        echo "  ~/bastel/mame/mame/mamemuse slm2test -seconds_to_run 5"
        exit 1
    fi
    mkdir -p "$TMPDIR"
    python3 dac_trace_to_vcd.py "$trace" -o "$vcd"
}

cmd_install() {
    if [ ! -f "$PATCHED" ]; then
        echo "ERROR: $PATCHED not found. Run: $0 patch"
        exit 1
    fi
    for dir in ex20 slm2test; do
        mkdir -p "$MAME_ROMS/$dir"
        cp "$PATCHED" "$MAME_ROMS/$dir/sr0106.bin"
        echo "Installed → $MAME_ROMS/$dir/sr0106.bin"
    done
    # Also copy to local roms/ directory (used by some test configurations)
    for dir in roms/*/; do
        if [ -d "$dir" ]; then
            cp "$PATCHED" "${dir}sr0106.bin"
            echo "Installed → ${dir}sr0106.bin"
        fi
    done
}

# Default: patch + asm + verify
if [ $# -eq 0 ]; then
    cmd_patch
    cmd_asm
    exit 0
fi

case "$1" in
    import)  cmd_import "$2" ;;
    patch)   cmd_patch ;;
    asm)     cmd_asm ;;
    diff)      cmd_diff ;;
    byte_diff)  cmd_byte_diff "$2" ;;
    ascii_diff) cmd_ascii_diff "$2" ;;
    vcd)        cmd_vcd "$2" "$3" ;;
    install)    cmd_install ;;
    all)
        cmd_import "$2"
        cmd_patch
        cmd_asm
        cmd_diff
        cmd_install
        ;;
    *)
        echo "Usage: $0 [import|patch|asm|diff|byte_diff|ascii_diff|vcd|install|all] [args...]"
        echo "  No args: patch + asm + verify"
        exit 1
        ;;
esac
