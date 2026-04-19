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

    # $0F94 underflow_normalize cap #$0F: TESTED #$0D (bit 1), did NOT eliminate
    #   silence for caves. Maskrom confirms $0F is correct.

    # TEST: patch silence table entries to DDS ($20/$C4/$05) — VERIFIED: eliminates silence
    # fixes invalid reads on 19->C4 transition but high pitch for SEQUNC sound
    # (0x018B, 0x00, 0x40, "TEST: param table 4-shift R14 $00→$20 (repeat 3-shift phase step)"),
    # (0x018C, 0x19, 0xC4, "TEST: param table 4-shift micro-op $19→$C4 (DDS instead of silence)"),
    # (0x018E, 0x00, 0x40, "TEST: param table 5-shift R14 $00→$20 (repeat 3-shift phase step)"),
    # (0x018F, 0x19, 0xC4, "TEST: param table 5-shift micro-op $19→$C4 (DDS instead of silence)"),

    # TEST: normalize cap $0F→$0D — prevents R13_mode from reaching silence entries
    # (0x0F94, 0x0F, 0x0D, "TEST: underflow_normalize cap #$0F→#$0D (bit 1, maskrom shows $0F)"),

    # $001E micro_init: LD R7,#$40→#$00 — REJECTED: maskrom confirms $40 is correct.
    #   R7 is set to #$40 by many micro-ops (dds variants, etc.) as a deliberate reset.
    #   The crash (PC=$00C7, R7=$40 → reg[$80] unmapped) is caused by normalize bumping
    #   sub_mode 7→8, then synthesis_output writing reg[$1E]=$C4 (micro_dds_waveform)
    #   before micro_chain_a_step1 has had a chance to advance R7 past 0x40.
    #   Root cause is elsewhere — see WIP_ocean_crash.md.
    # (0x001E, 0x40, 0x00, "micro_init: LD R7,#$40→#$00 — WRONG, maskrom shows $40 is correct"),

    # $0E85 synthesis_output.phase_step: JR C→JR NC (bit 7 stuck-LOW)
    #   $7B→$FB. The lower path (T<$40, sub 8+) checks CP reg[$1E],#$C4 to decide
    #   whether to DI+force R5. With JR C ($7B), reg[$1E]<$C4 (chain running) takes
    #   the DI path — OPPOSITE of the upper path which treats <$C4 as "safe, no override".
    #   With JR NC ($FB), both paths have consistent logic:
    #     reg[$1E] < $C4 (chain running): non-DI, let chain finish naturally
    #     reg[$1E] >= $C4 (DDS running): DI + force R5 for safe transition
    #   Fixes: chain→$C4 race condition causing unmapped register reads at PC=$00C7.
    #   Without fix, IRQ4 fires micro_init/chain step between CP and DI, corrupting R7.
    #   Discovered via EX-20 voice4 trace (287 crashes/sec on ocean program).
    # (0x0E85, 0x7B, 0xFB, "synthesis_output.phase_step: JR C→JR NC, fix chain→C4 IRQ4 race (bit 7 stuck-LOW, cert=TBD)"),

    # WRONG IN MASKROMTOOL
    (0x0E69, 0x82, 0xC2, "LDE→LDC: synth_output byte0 must read ROM table, not slave RAM (bit 6 stuck-low)"),
    # WRONG IN MASKROMTOOL
    (0x0E20, 0x82, 0xC2, "LDE→LDC: $F2 special path read from ROM, not slave RAM (bit 6 stuck-low, cert=0!)"),
    # WRONG IN MASKROMTOOL
    # (0x0646, 0x1D, 0x1F, "ADD #$1D→#$1F: regpair operand base must be $1F (first bytecode), not $1D (config byte) (bit 1 stuck-low, cert=44)"),
    # WRONG IN MASKROMTOOL
    # (0x001E, 0x40, 0x00, "LD      PHASE, #40h -> #00"),
    # WRONG IN MASKROMTOOL
    #(0x0519, 0x09, 0x08, "CP      R13, #09h -> #08"),
    # WRONG IN MASKROMTOOL - changes caves/ocean from completly broken to somewhat ok
    # (0x0BFD, 0xFB, 0xEB, "JR      NC, .mul_skip -> C"),

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
    ##(0x0745, 0x11, 0x14, "synth_mul_by_14: LD R12,$11→$14, use ACC2_HI not SAVED_HI (2 bits, COP-confirmed)"),

    # $0D60 finalize_output multiply overflow: JP C→JP NC (bit 7 stuck-LOW)
    #   $7D→$FD. After the 12×16 multiply, carry from final ADC means result
    #   overflowed 16 bits. With JP C ($7D), overflow path fires when result is
    #   too large. With JP NC ($FD), overflow path fires when result fits — INVERTED.
    #   TEST ONLY: checking if preventing overflow path fixes ocean mode bouncing.
    #   If correct, the overflow path would never fire for normal results, and
    #   the voice would stay in $C4 mode instead of bouncing to chain modes.
    # REJECTED: $0D68 JP Z,$0F7B→$0F6B (bit 4) — redirects PITCH_ENV=0
    #   to overflow_mode_dispatch. Prevents mode bouncing but doesn't fix noise.
    # (0x0D68, 0x7B, 0x6B, "envelope_apply: JP Z,skip_mod→JP Z,overflow_mode_disp (bit 4 stuck-HIGH)"),

    # TEST VARIANTS for multiply path selection at $0D60:
    # Original: $7D = JP C  (carry → overflow path, no carry → envelope_apply)
    # Test A:   $FD = JP NC (no carry → overflow path, carry → envelope_apply) — noise OK, voice leak
    # Test B:   $8D = JP always (always overflow path, never envelope_apply)
    # Test C:   $0D = JP never (always envelope_apply, never overflow path)
    # Uncomment ONE:
    # (0x0D5A, 0xFB, 0xDB, "SKIP LAST ACC"),
    ### (0x0D60, 0x7D, 0xFD, "TEST A: JP C→JP NC — inverted path select (bit 7)"),
    # (0x0D60, 0x7D, 0x8D, "TEST B: JP C→JP always — always overflow path (bit 4)"),
    ###(0x0DEA, 0x7D, 0x0D, "TEST G: JP      C->JP never - finalize_output.overflow_shift"),
    ###(0x0DF5, 0x7D, 0x0D, "TEST G: JP      C->JP never - finalize_output.normalize_loop"),
    ## (0x0D5A, 0xFB, 0x7B, "TEST D: JR NC->C, .mul_overflow_check - fixes noise for some notes"),
    #(0x0D60, 0x7D, 0x0D, "TEST C: JP C→JP never — always envelope_apply (bit 7+4)"),
    #(0x0D66, 0x6D, 0x8D, "TEST E: JP Z→JP true — skip pitch_env mod"),
    #(0x0ED6, 0x6D, 0x8D, "TEST F: JP Z→JP true — skip pitch_env mod overflow path"),

    # $0C40 LFSR exit: JP finalize_output.multiply → JP finalize_output.entry
    #   $CC→$CA (2 bits: bits 1,2). LFSR handler at $0C3E jumps directly to
    #   finalize_output.multiply ($0CCC), bypassing the ACC clamp at $0CA5.
    #   With $0CCA (finalize_output.entry): INC PROG_CTR first, then multiply.
    #   When PROG_CTR reaches $3D, finalize_output clamps ACC_HI to $2D/$40/$80,
    #   preventing ACC from growing past the overflow threshold.
    #   Without clamp: LFSR ACC random-walks freely, ACC_HI regularly >= $80,
    #   causing 50% of ECLK cycles to trigger overflow → mode bouncing.
    # (0x0C40, 0xCC, 0xCA, "TEST: LFSR exit → finalize_output.entry (with ACC clamp) instead of .multiply (2 bits)"),

    # $0C0A LFSR centering: RRC→SRA R11 (bit 4 stuck-LOW)
    #   $C0→$D0. In the LFSR coefficient multiply, R11 = coefficient ($FF for
    #   ocean). RRC R11 divides by 2 → R11=$7F. SUB R13,R11 centers the product
    #   symmetrically around zero (range ±127). ACC random-walks through zero →
    #   normalize/overflow fires on every zero-crossing → mode bouncing.
    #   With SRA R11: arithmetic shift preserves sign bit. SRA $FF → $FF (sign
    #   is 1, stays 1). SUB R13,$FF → product - 255 → ALWAYS NEGATIVE (-2 to -255).
    #   ACC accumulates large negative values → multiply always overflows →
    #   overflow path fires consistently → stable sub, correct noise output.
    #   The overflow path IS the correct processing path for LFSR noise.
    #   Discovered via test stub at $0FAC: OR ACC_HI,#$80 proved that keeping
    #   ACC negative eliminates all mode bouncing and unmapped register reads.
    # (0x0C0A, 0xC0, 0xD0, "LFSR centering: RRC→SRA R11, bias product negative (bit 4 stuck-LOW, cert=TBD)"),

    # $0D68 envelope_apply: JP Z,$0F7B→$0F6B (bit 4 stuck-HIGH)
    #   When PITCH_ENV=0 (no pitch modulation / fixed formant), the current code
    #   sends R10 to skip_modulation → mode_dispatch → normalize. With small R10
    #   (from LFSR zero-crossings), normalize fires → sub jumps up → mode switch
    #   → super high pitch. With $6B: goes to overflow_mode_dispatch instead,
    #   which DECs sub and goes to pitch_output, bypassing normalize.
    #   When PITCH_ENV=0, there's no envelope to normalize against — the
    #   normalize mechanism is only meaningful when PITCH_ENV scales the result.
    #   Without pitch modulation, the raw multiply magnitude shouldn't determine sub.
    # REJECTED: (0x0D68, 0x7B, 0x6B) — redirecting PITCH_ENV=0 to overflow_mode_dispatch
    #   didn't fix the sound. Issue is in the multiply result, not the path.

    # $0F97 normalize: INC R13 → NOP (disable sub mode switching)
    #   $DE→$FF (2 bits). Normalize still shifts R12:R11 left (mantissa ×2)
    #   to bring R12 into $1E+ range, producing a valid TIMER_VAL and dither
    #   pattern. But sub (R13) stays at the MODE-assigned value — no mode
    #   switching, no micro-op chain change, no R14 incompatibility.
    #   The pitch computation adjusts for signal magnitude via the mantissa
    #   shift without changing the synthesis algorithm.
    #   Best test result so far: eliminates mode bouncing while preserving
    #   correct pitch tracking for all voices.
    #(0x0F97, 0xDE, 0xFF, "TEST: normalize INC R13→NOP, keep sub fixed (2 bits)"),
    (0x0F97, 0xDE, 0xCE, "TEST: normalize INC R13→INC R12, increment mantissa not sub (bit 4)"),
    #(0x0F9A, 0xEB, 0xEC, "TEST: RLC     R11 -> R12"),

    # $0F94 normalize cap: #$0F → #$07 (bit 3 stuck-LOW)
    #   Cap at sub=7 instead of 15. Prevents sub from crossing the 7/8
    #   boundary (oversampled ↔ DDS). For sub=7: caps immediately
    #   (R13=7 >= 7 → force R12=$1E). For sub < 7: INC up to 7 max.
    #   The 7/8 boundary is where R14 meaning changes (sample ↔ freq step).
    #(0x0F94, 0x0F, 0x07, "TEST: normalize cap #$0F→#$07, prevent sub crossing 7/8 boundary (bit 3)"),

    # $0FA3 normalize loop: JR always → JR GE (bit 4 stuck-HIGH)
    #   $8B→$9B. After CP R12,#$1E with R12<$1E: S=1,V=0 → GE false.
    #   Loop never repeats → normalize max 1 iteration, sub INC by 1.
    # (0x0FA3, 0x8B, 0x9B, "TEST: normalize JR always→JR GE, max 1 iteration (bit 4)"),

    # TEST: Proper ACC sign check at $0D60 via stub at $0FAC.
    #   Original JP C at $0D60 uses carry from multiply bit 11 as sign proxy.
    #   Replace with explicit TM ACC_HI, #$80 (test actual sign bit).
    #   Stub at $0FAC (9 bytes in NOP space):
    #     TM ACC_HI, #$80        ; test sign bit directly
    #     JP NZ, $0ECF           ; negative → pitch_acc_negative
    #     JP $0D63               ; positive → pitch_acc_positive
    # (0x0D60, 0x7D, 0x8D, "TEST: JP C→JP always, redirect to sign check stub"),
    # (0x0D61, 0x0E, 0x0F, "TEST: stub addr high ($0FAC)"),
    # (0x0D62, 0xCF, 0xAC, "TEST: stub addr low"),
    # (0x0FAC, 0xFF, 0x76, "TEST: TM ACC_HI, #$80"),
    # (0x0FAD, 0xFF, 0xE8, "TEST: TM operand R8"),
    # (0x0FAE, 0xFF, 0x80, "TEST: TM mask #$80"),
    # (0x0FAF, 0xFF, 0xED, "TEST: JP NZ (negative ACC)"),
    # (0x0FB0, 0xFF, 0x0E, "TEST: target high $0ECF"),
    # (0x0FB1, 0x8D, 0xCF, "TEST: target low"),
    # (0x0FB2, 0x02, 0x8D, "TEST: JP (positive ACC)"),
    # (0x0FB3, 0x3A, 0x0D, "TEST: target high $0D63"),
    # (0x0FB4, 0xFF, 0x63, "TEST: target low"),

    # Stub routine at $0FAC: OR ACC_HI,#$80; JP finalize_output.multiply
    # Clamp ACC to negative (set sign bit). ACC stays in $8000-$FFFF.
    # Multiply always produces large unsigned result → always takes overflow path
    # → stable sub (no normalize bouncing). Overflow path produces correct noise.
    (0x0FAC, 0xFF, 0x46, "TEST: OR ACC_HI, #$80 (clamp negative)"),
    (0x0FAD, 0xFF, 0xE8, "TEST: OR operand (ACC_HI = R8)"),
    (0x0FAE, 0xFF, 0x80, "TEST: OR mask #$80 (set sign bit)"),
    (0x0FAF, 0xFF, 0x8D, "TEST: JP finalize_output.multiply"),
    (0x0FB0, 0xFF, 0x0C, "TEST: JP target high"),
    (0x0FB1, 0x8D, 0xCC, "TEST: JP target low"),
    # (0x0FB2, 0x02, 0xff, "TEST: NOP"),
    # (0x0FB3, 0x3A, 0xff, "TEST: NOP"),

    (0x0D60, 0x7D, 0x8D, "TEST: JP C→JP always, redirect to sign check stub"),
    (0x0D61, 0x0E, 0x0F, "TEST: stub addr high ($0FAC)"),
    (0x0D62, 0xCF, 0xB2, "TEST: stub addr low"),
    
    (0x0FB2, 0x02, 0x76, "TEST: TM ACC_HI, #$80"),
    (0x0FB3, 0x3A, 0xE8, "TEST: TM operand R8"),
    (0x0FB4, 0xFF, 0x80, "TEST: TM mask #$80"),
    (0x0FB5, 0xFF, 0xED, "TEST: JP NZ (negative ACC)"),
    (0x0FB6, 0xFF, 0x0E, "TEST: target high $0ECF"),
    (0x0FB7, 0x8D, 0xCF, "TEST: target low"),
    (0x0FB8, 0x02, 0x8D, "TEST: JP (positive ACC)"),
    (0x0FB9, 0x3A, 0x0D, "TEST: target high $0D63"),
    (0x0FBA, 0xFF, 0x63, "TEST: target low"),


    # (0x0E8C, 0x07, 0x03, "8B 07 JR      .synthesis_output_common -> "),
    #(0x0C26, 0xAB, 0xAD, "*cirtcuit bend LFSR"),
    #(0x0BF8, 0x08, 0x04, "*cirtcuit bend LFSR - loop"),
    ## NICE (0x0C18, 0x5B, 0xDB, "*cirtcuit bend LFSR - loop"),
    ## SUPER NICE LASER (0x0C37, 0x56, 0x46, "*cirtcuit bend LFSR - loop"),
    #(0x0F7C, 0xEA, 0xEB, "*cirtcuit bend LFSR LD      R12, R10 -> R11"),
    #(0x0F97, 0xDE, 0xFF, "*cirtcuit bend LFSR - INC R13 -> NOP"),

    # REJECTED candidates (investigated, not bit errors):
    # $0975 branch_dispatch: TCM→TM R10, #$40
    #   Bit 4 stuck-LOW ($76→$66). The mode-conditional branch check XORs the
    #   branch target byte with the mode register, then tests bit 6. TCM (complement
    #   test) inverts the logic: never skips the branch → falls through to
    #   synth_computed_jump with register-indirect offset that overflows for most
    #   reg[$14] values. TM (direct test) correctly skips when bit 6 is clear.
    #   Discovered via church organ infinite loop (WIP_church_organ_stuck.md).
    #   NB: FIXES CHURCH - PROBABLY INCORRECT THOUGH
    # (0x0975, 0x66, 0x76, "branch_dispatch: TCM→TM R10,#$40, fix mode-conditional branch skip (bit 4 stuck-LOW, cert=TBD)"),

    # was $7D (bit 1, cert=24) but $0677 (bit 3, cert=4) has cleaner code flow:
    # $0677: CP R12,#$10 → JP C,$0CCA (finalize) / JP $058C (interpreter)
    # (0x0EC2, 0x03, 0x43, "TMR $03→$43: enable T_OUT on T0 for DAC strobe (bit 6 stuck-low)"),

    # $1D at $0882/$0436 is DIFFERENT: interpreter counter adds LDEI block base
    # to a start_value that already skips 2 config bytes (start_value=2 → $1F).
    # Regpair dispatch maps 0-based bytecode index → absolute reg address,
    # so base must be $1F to protect reg[$1D] (counter start) and reg[$1E] (micro-op addr).
    # Crash proof: operand offset 1 + $1D base → writes accumulator to reg[$1E] → $00FF crash.
    #
    # REVERTED patches (confirmed correct in original ROM):
    # $03A5 DI — intentional, prevents IRQ4 during synchronous synthesis.
    # $03A8 IMR=$08 — correct. Global enable restored by EI at $0E96.
    #   Was patched to $88 before ECLK→IRQ1 connection was understood.
    # $0ECA OR IMR,#$10 — correct. IRET restores IMR bit 7 automatically.
    #
    # REJECTED candidates (investigated, not bit errors):
    #(0x0E88, 0xEB, 0xFB, "LD R14,R11→R15,R11: lower path byte0 must not clobber DDS freq (bit 4 stuck-low)"),
    #(0x0E8F, 0xEB, 0xFB, "LD R14,R11→R15,R11: lower DI path same fix (bit 4 stuck-low)"),
    # $0E6E CP #$40→#$C0 — breaks drawbar C#5. The $40 threshold correctly
    #   separates upper/lower paths. Lower path byte0 comes from ROM table.
    # $0179 byte 0 — was thought to be slave RAM read. Now understood as ROM
    #   table entry ($01 = bit mask for sub=8). The $0E69 LDE→LDC fix makes
    #   this read from ROM correctly.
    # $0E75 CP #$C4→#$C5 — was thought to prevent DI path for $C4 DDS.
    #   With the $0E69 LDC fix, byte0 is deterministic from ROM and the
    #   upper/lower path split works correctly. The DI threshold at $C4
    #   is the original correct value. The DI section's atomic update of
    #   R14/R15/R5 is needed for micro-op chain transitions.
    # $0E84 CP #$C4→#$C5 — same as $0E75, lower path DI threshold.
    # $0EA7 OR IMR,#$02→OR IRQ,#$02 ($FB→$FA, bit 0) — DISPROVED.
    #   Would create continuous synthesis_output loop (no ECLK wait).
    #   Result: no sound on any program. The OR IMR,#$02 IS needed to
    #   re-enable IRQ1 mask after synthesis_pass_done. The 5ms pitch
    #   stepping is ECLK-gated by design, not a bit error.
    # (0x0EA7, 0xFB, 0xFA, "OR IMR→OR IRQ,#$02: continuous pitch update loop (bit 0 stuck-LOW, cert=TBD)"),
    #
    # UNDER INVESTIGATION:
    # $051C JR C→JR NC (bit 7, cert=37): Would make sub<9 fall through to
    #   R14 init at $051E. But also skips LDEI chain needed for waveform table.
    # $0E7C LD R14,R15→LD R14,#$EF (bit 2, cert=2): Very low certainty but
    #   #$EF as fixed constant doesn't make sense for per-voice freq step.

    # $0BC5 OR→TCM: synth_lfsr_noise sets IRQ1 pending as software flag,
    #   bypassing ECLK synchronization. TCM would test without modify,
    #   preserving master envelope S&H timing. (bit 5 stuck-low, cert=14)
    # So far no effect found
    # (0x0BC5, 0x46, 0x66, "OR→TCM IRQ,#$02: don't force IRQ1 pending, preserve ECLK sync (bit 5 stuck-low, cert=14)"),

    #
    # CERTAINTY=0 CANDIDATES (from WIP_low_certainty_bits.md):
    # Uncomment individually to test:
    #(0x0338, 0x1B, 0x1F, "UPDATE path: LD $1B→$1F, counter writes to wrong reg (bit 2 stuck-low, cert=0)"),
    #(0x077B, 0x14, 0x94, "synth dispatch: LD R12,reg[$14]→reg[$94] multiply source (bit 7 stuck-low, cert=0)"),
    #(0x0F52, 0x8B, 0xAB, "overflow path: JR→JR GT, unconditional becomes conditional (bit 5 stuck-low, cert=0)"),
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
