#!/usr/bin/env python3
"""
Apply known bit-error patches to the Wersi SLM-2 Z8611 firmware (SR0106).

Usage:
    python3 apply_patches.py <input.bin> [output.bin]

If output is omitted, writes to sr0106_patched.bin in the current directory.
"""
import sys
import hashlib
import binascii

EXPECTED_SIZE = 4096

# When a patch is confirmed and fixed in maskromtool, comment it out — don't delete or rewrite.
PATCHES = [
    # (offset, original, patched, description)

    # FIXED IN MASKROMTOOL 2026-04-07:
    #(0x0663, 0x7F, 0x77, "JP $067F→$0677: fix mid-instruction target, adds CP R12,#$10 check (bit 3 stuck-HIGH, cert=CONFIRMED)"),

    # VERIFIED IN MASKROMTOOL
    # $015F param table Mode B sub=15: $99 is not a valid micro-op.
    #   All other unused sub entries have $19 (micro_init). $99→$19 = bit 7 stuck-HIGH, cert=5.
    (0x015F, 0x99, 0x19, "param table Mode B sub=15: $99→$19 micro_init (bit 7 stuck-HIGH, cert=CONFIRMED)"),

    # $09C9 synth_loop_primary (LOOP1): INC $3B should be INC $1B.
    #   Bit 5 stuck-HIGH ($1B→$3B). Without fix, LOOP1 doesn't advance the
    #   micro-program counter, causing it to read the opcode byte as the reload
    #   value and offset branch targets by 1. LOOP2 at $09E3 has correct INC $1B.
    #   Discovered via church organ infinite loop (WIP_church_organ_stuck.md).
    (0x09C9, 0x3B, 0x1B, "LOOP1: INC $3B→$1B, advance micro-prog counter (bit 5 stuck-HIGH, certCONFIRMED)"),

    # $059F Type-B dispatch mask: AND #$3E→AND #$1E (bit 5 stuck-HIGH)
    #   With $3E, bits 5:1 of the opcode select the Type-B function (32 entries).
    #   The high nibble (parameter data from module templates) is treated as
    #   part of the function selector → $F3 dispatches to LOOP1R14 instead of
    #   LOAD P14 (dC). With $1E, bits 4:1 select the function (16 entries),
    #   matching the COP which uses (opcode<<1)&$1E for its 16-entry y-op table.
    #   All 16 parameter variants of the same base opcode go to the same handler.
    #   Confirmed: bit 5 visibly LOW in mask ROM image.
    #   Fixes church organ stuck (opcode $F3 = LOAD dC, not LOOP1R14).
    (0x05A0, 0x3E, 0x1E, "Type-B dispatch: AND #$3E→#$1E, ignore param nibble in dispatch (bit 5 stuck-HIGH, cert=CONFIRMED)"),

    # $057F synthesis_loop_entry: CLR $18 → LD R11,$18 (bit 3 stuck-HIGH)
    #   $B0 $18 = CLR $18 (clear block repeat counter on note re-trigger)
    #   $B8 $18 = LD R11, $18 (saves $18 to R11, does NOT clear it)
    #   With LD: reg[$18] retains its value from the previous note.
    #   synth_wavetable_acc ($A0 standard freq envelope) sees non-zero $18
    #   → takes "running" path → adds block 0 rate ($9E00) on every ECLK
    #   instead of advancing through blocks. Causes wildly fluctuating
    #   pitch on 2nd/3rd note (acc_hi cycles: $9E,$3C,$DA,$78,$16...).
    #   Discovered via VCD synthesis trace of acguit capture.
    (0x057F, 0xB8, 0xB0, "synthesis_loop_entry: LD R11,$18→CLR $18, clear block counter on retrigger (bit 3 stuck-HIGH, cert=CONFIRMED)"),

    (0x0F3D, 0x00, 0x80, "TCM $12,#$00→#$80: fix overflow path EXSLA attenuation (bit 7 stuck-low, cert=CONFIRMED)"),

    # $0A97 synth_wavetable_acc: JP MI,$0CEC → JP MI,$0CCC (bit 5 stuck-HIGH)
    #   $EC→$CC in address low byte of JP MI instruction at $0A95.
    #   With $0CEC (multiply_12x16_phase2): enters multiply at iteration 3,
    #   SKIPPING LD R12,$12; LD R13,$13; CLR R10; CLR R11. Uses stale
    #   coefficient and accumulator → garbage pitch. With $0CCC (multiply_12x16):
    #   full multiply with fresh coefficient and cleared accumulator.
    #   Triggered when envelope block repeat counter reg[$18] >= $80 (MI flag set).
    #   Affects all standard frequency envelopes ($A0 handler) with high repeat counts.
    #   Discovered by OpenCV bit classifier (rank #2, cert=3).
    (0x0A97, 0xEC, 0xCC, "synth_wavetable_acc: JP MI,$0CEC→$0CCC, use full multiply not phase2 (bit 5 stuck-HIGH, cert=CONFIRMED)"),

    # $001E micro_init: LD R7,#$40→#$00 — REJECTED: maskrom confirms $40 is correct.
    #   R7 is set to #$40 by many micro-ops (dds variants, etc.) as a deliberate reset.
    #   The crash (PC=$00C7, R7=$40 → reg[$80] unmapped) is caused by normalize bumping
    #   sub_mode 7→8, then synthesis_output writing reg[$1E]=$C4 (micro_dds_waveform)
    #   before micro_chain_a_step1 has had a chance to advance R7 past 0x40.
    #   Root cause is elsewhere — see WIP_ocean_crash.md.
    # (0x001E, 0x40, 0x00, "micro_init: LD R7,#$40→#$00 — WRONG, maskrom shows $40 is correct"),

    # WRONG IN MASKROMTOOL
    (0x0E69, 0x82, 0xC2, "LDE→LDC: synth_output byte0 must read ROM table, not slave RAM (bit 6 stuck-low)"),
    # WRONG IN MASKROMTOOL
    (0x0E20, 0x82, 0xC2, "LDE→LDC: $F2 special path read from ROM, not slave RAM (bit 6 stuck-low, cert=0!)"),
    # WRONG IN MASKROMTOOL
    # (0x0646, 0x1D, 0x1F, "ADD #$1D→#$1F: regpair operand base must be $1F (first bytecode), not $1D (config byte) (bit 1 stuck-low, cert=44)"),

    # WRONG IN MASKROMTOOL
    # $0F40 envelope overflow EXSLA bit-7 path: RCF→CCF (bit 5 stuck-LOW)
    #   $CF→$EF. In the overflow variant of envelope_apply, the EXSLA bit-7
    #   attenuation section (reached when TCM $12,#$80 → Z, i.e., bit 7 set)
    #   uses RCF before RRC R12:R13 + SRA. The normal (non-overflow) path at
    #   $0DD0 also has RCF here
    # (0x0F40, 0xCF, 0xEF, "overflow EXSLA: RCF→CCF before RRC R12 (bit 5 stuck-LOW, cert=0)"),

    # $0773/$07AB synth_neg_mul: JP→JP GT (bit 5 stuck-LOW at both locations)
    #   $8D→$AD. synth_neg14_mul_to_1617 and synth_neg16_mul_to_1415 negate
    #   an accumulator (COM+INC), then multiply. When the original value is $00,
    #   the negate produces $00 (Z=1). With unconditional JP ($8D), execution
    #   skips to interpreter_reentry, making the following LD R12,ACC_HI + JR
    #   multiply dead code. With JP GT ($AD), GT=(Z=0 AND S==V) is false when
    #   Z=1, so execution falls through to the LD R12,ACC_HI path — using the
    #   primary accumulator as fallback coefficient when the source is zero.
    #   COP firmware has identical pattern (env_r_saturate_dC at $E5EB) using
    #   conditional LBEQ for the zero case, confirming this should be conditional.
    #   Same bit, same direction at both locations.
    # (0x0773, 0x8D, 0xAD, "synth_neg14_mul_to_1617: JP→JP GT, zero-negate fallback to ACC_HI (bit 5 stuck-LOW)"),
    # (0x07AB, 0x8D, 0xAD, "synth_neg16_mul_to_1415: JP→JP GT, zero-negate fallback to ACC_HI (bit 5 stuck-LOW)"),

    # PROBABLY GOOD
    # $0745 synth_mul_by_11: LD R12,$11→LD R12,$14 (2 bits: $11→$14)
    #   The dispatch table pairs functions as 16-then-14 (ACC3 then ACC2):
    #   $D0=mul_by_16, $D4=should be mul_by_14. COP confirms: $D4→muladd_dC_d8.
    #   reg[$11] (SAVED_HI) is not a standard accumulator — all other multiply
    #   functions use reg[$14] or reg[$16]. Two bits differ ($11 XOR $14 = $05),
    #   but COP comparison + Z8 dispatch pattern both confirm $14 is correct.
    # (0x0745, 0x11, 0x14, "synth_mul_by_14: LD R12,$11→$14, use ACC2_HI not SAVED_HI (2 bits, COP-confirmed)"),
]


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <input.bin> [output.bin]")
        sys.exit(1)

    input_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else "sr0106_patched.bin"

    rom = bytearray(open(input_path, "rb").read())

    if len(rom) != EXPECTED_SIZE:
        print(f"ERROR: Expected {EXPECTED_SIZE} bytes, got {len(rom)}")
        sys.exit(1)

    print(f"Input:  {input_path} ({len(rom)} bytes)")

    print(f"\nApplying {len(PATCHES)} patches:")
    for offset, orig, patched, desc in PATCHES:
        actual = rom[offset]
        if actual == patched:
            print(f"  ${offset:04X}: already ${patched:02X}  {desc}")
            continue
        if actual != orig:
            print(f"  ${offset:04X}: WARNING expected ${orig:02X} got ${actual:02X} — applying anyway")
        rom[offset] = patched
        print(f"  ${offset:04X}: ${orig:02X} → ${patched:02X}  {desc}")

    crc = binascii.crc32(bytes(rom)) & 0xFFFFFFFF
    sha1 = hashlib.sha1(bytes(rom)).hexdigest()
    open(output_path, "wb").write(rom)
    print(f"\nOutput: {output_path}")
    print(f"  CRC:  {crc:08x}")
    print(f"  SHA1: {sha1}")


if __name__ == "__main__":
    main()
