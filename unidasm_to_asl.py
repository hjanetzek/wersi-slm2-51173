#!/usr/env python3
"""
Convert unidasm Z8 disassembly to ASL assembler source.

Usage:
    python3 unidasm_to_asl.py sr0106_original.bin > firmware.asm

Disassembles the ROM using unidasm, converts syntax to ASL format,
and emits data sections as DB directives. The output should assemble
to a byte-exact copy of the input ROM.
"""

import subprocess, os, re, sys

UNIDASM = os.path.expanduser("~/bastel/mame/mame/unidasm")
ROM_SIZE = 4096

# Data regions (not executable code — emit as DB)
DATA_RANGES = [
    (0x0000, 0x000C, "vectors", "Interrupt vector table (6 vectors × 2 bytes)"),
    (0x0101, 0x0192, "param_table", "Synthesis output parameter table (R13 = sub*3 + mode*3 + 1)"),
    (0x0192, 0x01B2, "dispatch", "Dispatch table B (opcode types 1-3). Entries 0,4,8,12 are unreachable dead data"),
    (0x01B2, 0x0232, "dispatch", "Dispatch table A (opcode type 0, 64 entries)"),
    (0x0232, 0x023A, "frac_freq_table", "DDS fractional frequency table (8 entries, read by dac_output_setup at $0DF8)"),
    # LDEI chain at $025F-$02DF is now disassembled as code (64x LDEI @R12, @RR10 + RET)
    (0x0FC0, 0x1000, "lut4", "Quarter-sine scaling LUT"),
]

# Known entry points: address → (label, section_header_or_None)
# Section headers trigger a "=== ... ===" banner.
# Labels are emitted as "label:" before the instruction.
LABELS = {
    0x000C: ("reset_entry", "RESET ENTRY"),
    0x000F: ("irq4_handler", "IRQ4 — Phase Accumulator + Micro-Op Dispatch"),
    0x0019: ("micro_silence", "Micro-op: Init/silence ($19). Output $80 (center), R7=$40, R5=reg[$1E]"),
    # Mode A sub 0-5: 8× oversampled linear scan. 8 steps per phase advance.
    # Phase+=1 per 8 IRQ4s. T=$80. All steps output interpolated samples.
    # Steps: $20→$2C→$37→$44→$51→$5E→$6A→$76→(reg[$1E]=$20)
    0x0020: ("micro_8x_step1", "Micro-op: 8× oversample step 1/8 ($20). OR PHASE,#$40 for @R7 indirect access"),
    0x002C: ("micro_8x_step2", "Micro-op: 8× oversample step 2/8 ($2C). ADD R14,@R7"),
    0x0037: ("micro_8x_step3", "Micro-op: 8× oversample step 3/8 ($37). INC R7, lookup @R7 fwd"),
    0x0041: (None, None),  # overflow target within step3
    0x0044: ("micro_8x_step4", "Micro-op: 8× oversample step 4/8 ($44). DEC R7, lookup @R7 bwd"),
    0x0051: ("micro_8x_step5", "Micro-op: 8× oversample step 5/8 ($51). Double add @R7"),
    0x005E: ("micro_8x_step6", "Micro-op: 8× oversample step 6/8 ($5E). Accumulate + INC R7"),
    0x006A: ("micro_8x_step7", "Micro-op: 8× oversample step 7/8 ($6A). AND PHASE,#$3F (wrap) + accumulate"),
    0x0076: ("micro_8x_step8", "Micro-op: 8× oversample step 8/8 ($76). Indexed 40h(PHASE) lookup, R5=reg[$1E]"),
    # Mode A sub 6: 4× oversampled linear scan. 4 steps per phase advance.
    # Phase+=1 per 4 IRQ4s. T=$80. Steps: $82→$8F→$9B→$A4→(reg[$1E]=$82)
    0x0082: ("micro_4x_step1", "Micro-op: 4× oversample step 1/4 ($82). INC+AND PHASE, ADD R15+=R14"),
    0x008F: ("micro_4x_step2", "Micro-op: 4× oversample step 2/4 ($8F). Lookup 40h(R7)→R15"),
    0x009B: ("micro_4x_step3", "Micro-op: 4× oversample step 3/4 ($9B). ADD R14+=R15"),
    0x00A4: ("micro_4x_step4", "Micro-op: 4× oversample step 4/4 ($A4). Lookup 40h(R7)→R14, R5=reg[$1E]"),
    # Mode A sub 7: 2× oversampled linear scan. 2 steps per phase advance.
    # Phase+=1 per 2 IRQ4s. T=$80. Steps: $AC→$B8→(reg[$1E]=$AC)
    0x00AC: ("micro_2x_step1", "Micro-op: 2× oversample step 1/2 ($AC). Output R15, INC+AND PHASE, R15=waveform[PHASE]"),
    0x00B8: ("micro_2x_step2", "Micro-op: 2× oversample step 2/2 ($B8). Output R14, R15=(R15+R14)/2, R14=waveform[PHASE]"),
    # Mode A sub 8-13: direct linear scan. 1 output per IRQ4, phase+=R14(=T).
    # T=$01-$20 (fast timer). No oversampling. R14=freq step (constant).
    0x00C4: ("micro_1x_dds", "Micro-op: 1× direct DDS ($C4). Output waveform[PHASE], PHASE+=R14, AND #$3F"),
    # Mode B: variable-duty DDS. Only $CF outputs. $DA/$E9 skip (no DAC output).
    # @PHASE indirect ($40-$7F). R14=step, R15=wrap point (duty cycle control).
    0x00CF: ("micro_duty_dds", "Micro-op: Variable-duty DDS ($CF→$DA→$E9). Only $CF outputs to DAC"),
    0x00DA: ("micro_duty_skip1", "Micro-op: Duty DDS skip entry ($DA). No output. Advance until carry→$40→$CF"),
    0x00E6: (".skip_continue", None),
    0x00E9: ("micro_duty_skip2", "Micro-op: Duty DDS skip continue ($E9). No output. Advance until carry→$40→$CF"),
    0x00F2: ("micro_wrap_dds", "Micro-op: Programmable-wrap DDS ($F2). Mode C sub 0-10"),
    0x023A: ("init", "RESET / INITIALIZATION"),
    0x023C: ("init_srp", None),  # SRP #$00
    0x0250: ("init_enable_irq", None),  # CLR IMR; EI; DI; LD IPR; LD IMR; CLR IRQ; EI
    0x025D: ("spin", None),
    0x025F: ("ldei_chain_start", None),  # start of LDEI bulk copy
    0x02E0: ("irq3_handler", "IRQ3 — RAUD Command Dispatch"),
    0x0302: ("irq3_first_time", "First-time parameter writeback"),
    0x0326: ("cmd_dispatch", "Command dispatch"),
    0x0330: ("update_path", "UPDATE — Synthesis parameter update"),
    0x0374: ("voice_param_update", "VOICE PARAMETER UPDATE"),
    0x0389: ("stop_handler", "STOP — Voice off"),
    0x03A5: ("irq3_full_setup", "FULL VOICE SETUP"),
    0x0412: ("synthesis_mode_c", "Mode C — Programmable-wrap DDS ($F2). MODE bits 5:4 = $10 (fall-through)"),
    0x04DD: ("synthesis_mode_b", "Mode B — Variable-duty DDS ($CF). MODE bits 5:4 = $00"),
    0x050E: ("synthesis_mode_a", "Mode A — Oversampled wavetable + direct DDS ($20-$C4). MODE bits 5:4 = $20"),
    0x051E: (".mode_a_sub_gt9", None),  # sub>9 resampler
    0x054C: (".mode_a_sub9", None),  # sub=9 resampler
    0x0576: (".mode_a_sub_lt9", None),  # sub<9 LDEI copy
    0x057B: ("synthesis_loop_entry", "Synthesis loop entry"),
    0x058E: ("micro_program_interpreter", "Micro-program interpreter"),
    0x05BC: ("op_load_r8r9", "Outer synth functions ($05BC-$0CA4)"),
    0x05CA: ("op_load_1617", None),
    0x05D7: ("op_load_1415", None),
    0x05E4: ("op_copy_1617_to_r8r9", None),
    0x05EB: ("op_copy_1415_to_r8r9", None),
    0x05F2: ("op_store_r8r9_to_1617", None),
    0x05F8: ("op_copy_1415_to_1617", None),
    0x0600: ("op_store_r8r9_to_1415", None),
    0x0606: ("op_copy_1617_to_1415", None),
    0x060F: ("op_regpair_op_08", "Register pair dispatch"),
    0x0613: ("op_regpair_op_16", None),
    0x0617: ("op_regpair_op_14", None),
    0x0677: (".regpair_exit", None),
    0x0680: ("op_add_imm_to_r8r9", "Add/subtract dispatch functions"),
    0x06B2: ("op_add_1617_to_r8r9", None),
    0x072C: ("op_neg16_multiply", "Negate + multiply entry points"),
    0x074D: ("multiply_8x16_r8r9", "8x16 multiply variants"),
    0x077A: ("op_mul14_to_1617", None),
    0x07E9: ("multiply_acc_r8r9", None),
    0x086D: ("op_mul_1617_by_imm", "16x8 multiply variants"),
    0x08CD: ("cmp_s_r8r9_imm", "Comparison + conditional branch"),
    0x0968: ("branch_dispatch", "Branch dispatch"),
    0x097B: ("op_computed_jump", "Computed micro-program jump"),
    0x09C8: ("op_loop_primary", "Loop control ($09C8-$0A55)"),
    0x09E3: ("op_loop_secondary", None),
    0x0A2D: ("op_conditional_counter", None),
    0x0A58: ("op_swap_1415_1617", None),
    0x0A69: ("op_neg_1617", "Negate reg[$16:$17]"),
    0x0AC3: ("op_vibrato1", "Vibrato 1 — alternating coefficient multiply ($80 opcode, 4-byte)"),
    0x0AEB: (".vib1_use_coeff2", None),
    0x0AF1: ("op_vibrato2", "Vibrato 2 — sine phase modulation ($90 opcode, 4-byte)"),
    0x0B08: (".vib2_save_r9", None),
    0x0B20: (".vib2_iteration", None),
    0x0B37: (".vib2_last_iter", None),
    0x0B3D: (".vib2_sine_lookup", None),
    0x0B47: (".vib2_no_negate", None),
    0x0B81: (".vib2_add_to_acc", None),
    0x0B8E: (".vib2_zero_sine", None),
    0x0B81: (".vib2_exit", None),
    0x0B8E: (".vib2_clr_r8", None),  # CLR R8 within fm_common
    0x0B94: ("op_lfsr_noise", "LFSR pitch modulation source (16-bit PRNG + envelope S&H)"),
    0x0BA5: (".counter_nonzero", None),
    0x0BA9: (".check_zero", None),
    0x0BB3: (".full_lfsr_step", None),
    0x0BC2: (".no_feedback", None),
    0x0BEC: (".short_lfsr", None),
    0x0BFB: (".mul_loop", None),
    0x0C02: (".mul_skip", None),
    0x0C20: (".diff_neg", None),           # negative old state: SUB + SBC #$FF
    0x0C25: (".store_state", None),
    0x0C41: ("op_lfsr_mul_1617", "LFSR step + multiply → reg[$16:$17]"),
    0x0C73: ("op_lfsr_mul_1415", "LFSR step + multiply → reg[$14:$15]"),
    0x0CA5: ("finalize_output", "Finalize: ACC clamp + 12x16 multiply + envelope + pitch output"),
    0x0CCA: ("finalize_output.entry", None),  # INC $1B then fall through (from opcode handlers)
    0x0CCC: ("finalize_output.multiply", None),  # 12x16 multiply: R10:R11 = ACC × coeff
    0x0D63: ("finalize.pitch_acc_positive", None),  # ACC was positive (no carry from multiply bit 11)
    0x0DBD: (".mul_exsla_bit0", None),
    0x0DCB: (".mul_exsla_bit1", None),
    0x0DD9: (".modulation_dir_check", None),
    0x0DED: ("finalize_output.check_underflow", None),  # fallthrough, also jumped from check_underflow_ld_r12_r10
    0x0DF8: ("finalize_output.pitch_output", None),  # SPH fractional + param table pointer
    0x0E19: (".f2_special_check", None),  # fall-through from dac_output_setup, not a call target
    0x0E2A: ("synthesis_loop_reentry", "Synthesis loop re-entry (ECLK poll). R13 = param table index for synthesis_output (byte0 offset). Set by pitch_output or synthesis_pass_done"),
    0x0E62: ("synthesis_output", "SYNTHESIS OUTPUT — parameter table read"),
    0x0EA6: (".synthesis_pass_done", None),
    0x0EBE: ("irq1_handler", "IRQ1 — Timer Setup (ECLK-triggered)"),
    0x0CDC: (".mul_bit1", None),
    0x0CE8: (".mul_bit2", None),
    0x0CF4: (".mul_bit3", None),
    0x0D00: (".mul_bit4", None),
    0x0D0C: (".mul_bit5", None),
    0x0D18: (".mul_bit6", None),
    0x0D24: (".mul_bit7", None),
    0x0D30: (".mul_bit8", None),       # switch from PITCH_LO (R13) to PITCH_HI (R12)
    0x0D3C: (".mul_bit9", None),
    0x0D48: (".mul_bit10", None),
    0x0D54: (".mul_bit11", None),      # last iteration — adds ACC at weight 1.0 (no final shift)
    0x0D60: (".acc_sign_check", None),  # carry = ACC sign test (not magnitude overflow)
    0x0ECF: ("finalize.pitch_acc_negative", "ACC was negative (carry from multiply bit 11 + large unsigned ACC)"),
    0x0F2D: (".mul_exsla_bit0", None),
    0x0F3B: (".mul_exsla_bit1", None),
    0x0F49: (".modulation_dir_check", None),
    0x0F6B: (".overflow_shift_check", None),
    0x0F6D: (".overflow_shift_check_i", None),
    0x0F72: (".overflow_shift_check_ii", None),
    0x0F7B: ("finalize_output.check_underflow_ld_r12_r10", None),  # JP Z from pitch_acc_positive when reg[$1A]=0
    0x0F80: ("finalize_output.overflow_shift", None),  # JP C from pitch_acc_positive add path
    0x0F92: ("finalize_output.underflow_shift", "Normalize underflow: shift R12:R11 left until R12 >= $1E"),
    # Internal branch targets (no section headers, just labels)
    0x0041: (".micro_lookup_overflow", None),
    0x058C: ("interpreter_reentry", None),
    0x0619: ("regpair_dispatch_entry", None),
    0x0915: ("compare_signed", None),
    0x092F: ("compare_unsigned", None),
    0x093A: ("compare_common", None),
    0x0947: ("branch_greater", None),
    0x0954: ("branch_equal", None),
    0x095E: ("branch_less", None),
    0x09D5: ("loop_branch_target", None),
    0x09DD: ("loop_counter_check", None),
    0x09F2: ("loop_reload_check", None),
    0x0A3F: (".loop_special", None),
    0x0A44: (".loop_special_dec_1c", None),  # DEC $1C within loop_special
    0x0A4B: (".loop_special_check_ff", None),  # CP @$1B, #$FF within loop_special
    0x03D7: (".pitch_env_from_acc2", None),  # mode bit 7 clear: load pitch env + EXSLA
    0x03E9: (".load_micro_program", None),  # common: load micro-program via LDEI sled
    0x0BF4: (".coeff_exit", None),
    # finalize_output subsystem: finalize_output → finalize_check → multiply_12x16
    # → pitch_acc_positive → check_underflow are one logical flow. Internal targets:
    0x0CB6: (".finalize_sub43_check", None),
    0x0CBD: (".finalize_check", None),
    0x0CC1: (".finalize_clamp_max", None),  # LD R8, #$FF; LD R9, #$FF
    0x0CC5: (".finalize_reset_counter", None),  # LD $1B, #$3D
    # 0x0CEC: multiply_12x16_phase2 — removed: patched $0A97 JP MI,$0CEC→$0CCC
    #   No longer a jump target. Code at $0CEC is iteration 3 of multiply_12x16.
    # overflow/underflow paths share exit targets with normal envelope:
    0x0E06: ("finalize_output.pitch_output.underflow_shift_entry", None),  # entry from normalize/overflow clamp paths
    # synthesis_output flow — T byte splits oversampled vs 1x DDS:
    #
    # .oversampling (T≥$40, sub 0-7):
    #   Read byte1. CP reg[$1E], #$C4:
    #   Already oversampled (<$C4) → common (no R5 change)
    #   Was 1x DDS (≥$C4) → .switch_to_oversampling: DI, prime R14/R15, force R5
    #
    # .no_oversampling (T<$40, sub 8+):
    #   CP reg[$1E], #$C4:
    #   Already 1x DDS (≥$C4) → .update_phase_increment: R14=T, no R5 change
    #   Was oversampled (<$C4) → .switch_to_direct_sampling: DI, R14=T, force R5=$C4
    0x0E71: (".oversampling", None),                  # UPPER: T≥$40, read byte1 and check current mode
    0x0E78: (".switch_to_oversampling", None),       # 1x DDS→oversample: DI, prime R14/R15, force R5
    0x0E82: (".no_oversampling", None),              # LOWER: T<$40, check current mode
    0x0E87: (".update_phase_increment", None),       # 1x DDS steady: update R14=T, no R5 change
    0x0E8D: (".switch_to_direct_sampling", None),    # oversample→1x DDS: DI, set R14=T, force R5=$C4
    0x0E94: (".synthesis_output_common", None),
    # Add/subtract dispatch functions ($0680-$0729)
    0x0690: ("op_add_imm_to_1617", None),
    0x06A1: ("op_add_imm_to_1415", None),
    0x06BB: ("op_add_1415_to_r8r9_sat", None),
    0x06CB: ("op_add_r8r9_to_1617", None),
    0x06D4: ("op_add_1415_to_1617", None),
    0x06DD: ("op_add_r8r9_to_1415", None),
    0x06E6: ("op_add_1617_to_1415", None),
    0x06EF: ("op_sub_1617_from_r8r9", None),
    0x06F8: ("op_sub_1415_from_r8r9", None),
    0x0708: ("op_sub_r8r9_from_1617", None),
    0x0711: ("op_sub_1415_from_1617", None),
    0x071A: ("op_sub_r8r9_from_1415", None),
    0x0723: ("op_sub_1617_from_1415", None),
    # Negate + multiply ($072C-$074C)
    0x0736: ("op_neg14_multiply", None),
    0x0740: ("op_mul_by_16", None),
    0x0744: ("op_mul_by_11", None),
    0x0748: ("op_mul_by_imm", None),
    # Multiply variants ($074D-$07DC)
    0x076C: ("op_neg14_mul_to_1617", None),
    0x0776: (".neg14_zero_fallback", None),  # UNREACHABLE without $0773 patch ($8D→$AD)
    0x077A: ("op_mul14_to_1617", None),
    0x077E: ("op_mulimm_to_1617", None),
    0x0783: ("multiply_to_1617", None),
    0x07A4: ("op_neg16_mul_to_1415", None),
    0x07AE: (".neg16_zero_fallback", None),  # UNREACHABLE without $07AB patch ($8D→$AD)
    0x07B2: ("op_mul14_to_1415", None),
    0x07B6: ("op_mulimm_to_1415", None),
    0x07BB: ("multiply_to_1415", None),
    0x07DC: ("op_mul16_acc_r8r9", None),
    0x07E0: ("op_mul14_acc_r8r9", None),
    0x07E4: ("op_mulimm_acc_r8r9", None),
    # Multiply-accumulate to reg pairs
    0x080B: ("op_mul14_acc_1617", None),
    0x080F: ("op_mulimm_acc_1617", None),
    0x083C: ("op_mul16_acc_1415", None),
    0x0840: ("op_mulimm_acc_1415", None),
    # Multiply-accumulate shared cores (entered from multiple mul_acc functions)
    0x0814: ("multiply_acc_core_1617", None),
    0x0845: ("multiply_acc_core_1415", None),
    # 16x8 multiply variants
    0x089D: ("op_mul_1415_by_imm", None),
    # Comparison + conditional branch ($08CD-$0965)
    0x08D1: ("cmp_u_r8r9_imm", None),
    0x08D5: ("cmp_s_1617_imm", None),
    0x08D9: ("cmp_u_1617_imm", None),
    0x08DD: ("cmp_s_1415_imm", None),
    0x08E1: ("cmp_u_1415_imm", None),
    0x08E5: ("cmp_u_r8r9_1617", None),
    0x08ED: ("cmp_u_r8r9_1415", None),
    0x08F5: ("cmp_u_1617_1415", None),
    0x08FD: ("cmp_u_1617_r8r9", None),
    0x0905: ("cmp_u_1415_1617", None),
    0x090D: ("cmp_u_1415_r8r9", None),
    # Loop control reload variants
    0x09F9: ("op_loop_reload_18_from_16", None),
    0x0A06: ("op_loop_reload_19_from_16", None),
    0x0A13: ("op_loop_reload_18_from_14", None),
    0x0A20: ("op_loop_reload_19_from_14", None),
    # Negate + special
    0x0A72: ("op_neg_1415", None),
    0x0A7B: ("op_wavetable_acc", None),
}

# Per-address inline comments — appended to the instruction's comment field
ADDR_COMMENTS = {
    # IRQ4 handler: timer dither + micro-op dispatch
    0x000F: [
        "IRQ4 handler: fractional timer dither + micro-op dispatch.",
        "SPH holds an 8-bit dither pattern (from pitch_output ROM[$0232] table).",
        "Each IRQ4: rotate pattern left, carry adds 0 or 1 to timer reload.",
        "This dithers the sample rate by ±1 timer tick, giving 3 bits of",
        "sub-sample pitch resolution (8 dither patterns = 0/8 to 7/8 fractional).",
    ],
    0x0011: "R6 += carry (0 or 1) — dithered timer value",
    0x0013: "reload T0 with dithered value",
    0x0015: "restore R6 = base timer value from TIMER_VAL (reg[$1D])",
    0x0017: "dispatch to micro-op at address R4:R5 (R4=ZERO=$00)",
    # Mode A oversampling hierarchy:
    # sub 0-5 ($20): 8× oversample — 8 IRQ4s per phase step, heavy interpolation
    # sub 6   ($82): 4× oversample — 4 IRQ4s per phase step
    # sub 7   ($AC): 2× oversample — 2 IRQ4s per phase step, linear interp
    # sub 8+  ($C4): 1× direct DDS — 1 IRQ4 per output, phase+=R14
    # All sub 0-7 use T=$80 (same timer rate), differ only in interpolation depth.
    # Sub 8-13 use T=$01-$20 (fast timer), R14=T=phase step.
    # The sub 7/8 boundary separates two incompatible R14 usage modes.
    0x0020: [
        "8x oversampled linear scan (Mode A sub 0-5). Lowest pitch range.",
        "8 IRQ4 steps per single phase advance. T=$80.",
        "All 8 steps output interpolated samples. Phase advances once (step 7).",
        "R14/R15 = working accumulators (read+written by chain).",
        "Chain: $20->$2C->$37->$44->$51->$5E->$6A->$76->(reg[$1E]=$20).",
    ],
    0x0082: [
        "4x oversampled linear scan (Mode A sub 6). Mid-low pitch.",
        "4 IRQ4 steps per phase advance. T=$80.",
        "Chain: $82->$8F->$9B->$A4->(reg[$1E]=$82).",
    ],
    0x00AC: [
        "2x oversampled linear scan (Mode A sub 7). Mid pitch.",
        "2 IRQ4 steps per phase advance. T=$80.",
        "Step1: output R15, INC+wrap PHASE, fetch new R15.",
        "Step2: output R14, interpolate R15=(R15+R14)/2, fetch new R14.",
        "R14/R15 are WORKING registers (sample values, not freq step!).",
    ],
    0x00C4: [
        "1x direct DDS (Mode A sub 8-13). High pitch range.",
        "1 IRQ4 per output. Phase+=R14 per IRQ4. Timer T=R14=$01-$20.",
        "R14 is READ-ONLY freq step constant (set by synthesis_output from T byte).",
        "T and R14 are the same value: timer period = phase step = constant pitch.",
        "",
        "WARNING: sub 0-7 use R14 as working sample; sub 8+ use R14 as freq step.",
        "Crossing the sub 7/8 boundary mid-note corrupts R14 interpretation.",
        "See WIP_ocean_crash.md.",
    ],
    # micro_dds_cf chain: variable-duty DDS ($CF→$DA→$E9→$CF)
    0x00CF: [
        "Variable-duty DDS (Mode B). PHASE=$40-$7F = waveform table direct.",
        "Only $CF outputs to DAC. $DA/$E9 advance phase without output (skip zone).",
        "R14 = freq step (added every IRQ4), R15 = skip entry offset.",
        "Cycle: $CF scans $40→$7F outputting samples. When PHASE crosses $7F (MI),",
        "  SUB R15 adjusts skip entry. $DA/$E9 advance through $80-$FF (no output).",
        "  On carry (wrap past $FF): reset PHASE=$40, back to $CF.",
        "R15 controls duty cycle: smaller R15 = longer skip = narrower pulse.",
    ],
    0x00D1: "advance phase by freq step",
    0x00D3: "PHASE < $80? → stay in output scan",
    0x00D5: "crossed $7F: adjust skip entry point by subtracting R15",
    0x00D7: "enter skip zone (no more DAC output until reset)",
    0x00DA: "map phase into skip range (SUB $81)",
    0x00DD: "advance through skip zone",
    0x00DF: "carry = wrapped past $FF?",
    0x00E1: "yes: reset phase to $40 (waveform table base)",
    0x00E3: "back to output scan",
    0x00E6: "no carry: continue skip",
    0x00E9: "keep advancing (no DAC output)",
    0x00EB: "carry?",
    0x00ED: "reset to $40",
    0x00EF: "back to output scan",
    # irq3_full_setup: mode bit 7 selects pitch envelope source
    0x03C5: "save ACC3_HI, init LFSR state to $DB",
    0x03CD: "mode bit 7: fixed formant? (1=freq doesn't track pitch)",
    0x03D0: "bit 7 clear → relative formant: load pitch env from ACC2_LO + EXSLA",
    0x03D2: "bit 7 set → fixed formant: no pitch tracking (clear envelope)",
    0x03D5: "skip EXSLA setup",
    0x03D7: "initial pitch envelope = ACC2_LO (reg[$15])",
    0x03DA: "read EXSLA routing from Port 3",
    0x03E3: "merge EXSLA bits [7:6] into PITCH_HI (reg[$12])",
    0x03E9: "R4:R5 = call address for LDEI sled ($02:computed)",
    0x03EB: "R5 = $DD (end of LDEI sled)",
    0x03ED: "R11 = slave_ram[$F4] (harmonic byte → sled offset)",
    0x03EF: "R12 = harmonic count from slave RAM",
    0x03F1: "R5 -= 2*harmonics (each LDEI is 2 bytes)",
    0x03F5: "R11 = slave_ram[$F5]",
    0x03F7: "R11 = slave_ram[$F5] (micro-program data source pointer)",
    0x03F9: "R12 = $1D (LDEI destination: reg[$1D] onward = micro-program storage)",
    0x03FB: "CALL @RR4 → computed jump into LDEI sled at $02xx, loads N bytes from slave RAM",
    0x03FD: "PROG_CTR = reg[$1D] (first micro-program byte = start address)",
    # op_neg_mul zero-negate fallback (UNREACHABLE without $0773/$07AB patch)
    # When COM+INC produces zero (original was $00), JR NZ is not taken.
    # With original JP ($8D, unconditional): skips to interpreter, dead code follows.
    # With patched JP GT ($AD): GT is false when Z=1, falls through to fallback.
    # COP has same pattern: env_r_saturate_dC uses conditional LBEQ for zero case.
    0x076C: "R12 = ACC2_HI (secondary accumulator high)",
    0x076E: "negate: two's complement step 1",
    0x0770: "negate: two's complement step 2 (Z=1 if original was $00)",
    0x0771: "non-zero → multiply with negated coefficient",
    0x0773: "PATCHED: JP GT (never true when Z=1) → fall through. ORIGINAL: JP (unconditional) → dead code",
    0x0776: "FIXME UNREACHABLE without patch: use ACC_HI as fallback coefficient when source is zero",
    0x0778: "→ multiply with ACC_HI instead of negated ACC2_HI",
    0x07A4: "R12 = ACC3_HI (tertiary accumulator high)",
    0x07A6: "negate: two's complement step 1",
    0x07A8: "negate: two's complement step 2 (Z=1 if original was $00)",
    0x07A9: "non-zero → multiply with negated coefficient",
    0x07AB: "PATCHED: JP GT (never true when Z=1) → fall through. ORIGINAL: JP (unconditional) → dead code",
    0x07AE: "FIXME UNREACHABLE without patch: use ACC_HI as fallback coefficient when source is zero",
    0x07B0: "→ multiply with ACC_HI instead of negated ACC3_HI",

    # op_vibrato1: alternating coefficient multiply ($80 opcode)
    0x0AC3: [
        "Vibrato 1: alternating coefficient multiply. Opcode $80, 4-byte.",
        "Layout: {opcode, coeff1, coeff2, loop_count}",
        "Uses BLOCK_CTR (reg[$1C]) as toggle between two coefficients.",
        "  $1C==0: reload loop counter from FREQ[3], toggle $1C",
        "  $1C!=0: DEC counter, multiply by coeff1 or coeff2 based on toggle",
        "Alternates R8:R9 *= coeff1 and R8:R9 += coeff2 × R8:R9.",
        "Creates vibrato by periodically scaling ACC between two rates.",
    ],
    0x0AC6: "MODE bit 6 clear → skip vibrato, go to finalize_output.multiply",
    0x0ACB: "LOOP1_CTR (reg[$18]) == 0?",
    0x0ACE: "non-zero → already running, go to coeff_step",
    0x0AD0: "toggle BLOCK_CTR (alternate coefficient selection)",
    0x0AD2: "reload loop counter from FREQ[3]",
    0x0AD7: "counter == 0 → no iterations, skip to finalize",
    0x0ADC: "DEC PROG_CTR (back up to re-read opcode on next pass)",
    0x0ADE: "DEC LOOP1_CTR",
    0x0AE0: "BLOCK_CTR toggle state? 0 = use coeff2, else = use coeff1",
    0x0AE5: "R12 = FREQ[1] (coefficient 1)",
    0x0AE8: "→ multiply_8x16_r8r9 (R8:R9 *= R12)",
    0x0AEB: "R12 = FREQ[2] (coefficient 2)",
    0x0AEE: "→ multiply_acc_r8r9 (R8:R9 += R12 × R8:R9)",
    # op_vibrato2: sine phase modulation ($90 opcode)
    0x0AF1: [
        "Vibrato 2: sine-wave phase modulation. Opcode $90, 4-byte.",
        "Layout: {opcode, saved_R9, phase_increment, freq_param}",
        "Phase accumulator in reg[$14:$15], starts at $4000 (quarter-wave).",
        "Each iteration: phase += increment, sine ROM lookup at $0FC0-$0FFF,",
        "multiply sine × freq_param, sign-correct by quadrant, add to R8:R9.",
        "Uses reg[$16:$17] as frequency, reg[$18:$19] as phase step.",
        "Creates periodic pitch modulation (vibrato/tremolo).",
    ],
    0x0AF4: "MODE bit 6 clear → skip vibrato",
    0x0AF9: "BLOCK_CTR == 0? (first call)",
    0x0AFC: "non-zero → already initialized, go to iteration",
    0x0AFE: "first call: load loop count from FREQ[1]",
    0x0B05: "loop count == 0 → skip",
    0x0B08: "save R9 to FREQ[1] (overwrite loop count with ACC_LO)",
    0x0B0A: "save R8 to SAVED_HI (reg[$11])",
    0x0B0D: "reg[$19] = FREQ[2] (phase increment)",
    0x0B12: "reg[$17] = FREQ[3] (frequency param)",
    0x0B16: "phase accumulator = $4000 (quarter-wave start)",
    0x0B20: "DEC loop counter",
    0x0B27: "phase += increment",
    0x0B31: "freq += param",
    0x0B37: "phase_acc += phase_step",
    0x0B3D: "R13 = phase high byte (quadrant selector)",
    0x0B3F: "test quadrant bit 6",
    0x0B42: "quadrant 2 or 4: negate R13 (COM+INC)",
    0x0B47: "mask to 0-127 (half-wave index)",
    0x0B4A: "zero sine → skip multiply",
    0x0B4C: "index into sine ROM table at $0FC0-$0FFF",
    0x0B4F: "R12:R13 = $0F:index → ROM lookup",
    0x0B51: "R13 = sine table value",
    0x0B53: "8x8 multiply: sine × reg[$16:$17]",
    0x0B69: "scale result down (÷8): RCF+RRC+SRA×2",
    0x0B76: "quadrant 3 or 4: negate result (COM+INCW)",
    0x0B81: "add saved ACC offset: R8:R9 += saved values",
    0x0B8B: "→ finalize_output.multiply",
    0x0B8E: "zero sine: CLR R8:R9, then add saved offset",
    # op_lfsr_noise: LFSR pitch modulation
    0x0B94: [
        "op_lfsr_noise: 16-bit Galois LFSR pitch modulation",
        "Micro-program opcode $10, 4-byte instruction: {opcode, running_state, coefficient, counter/lfsr_low}",
        "Three paths based on BLOCK_CTR (reg[$1C]):",
        "  counter==0: reload from FREQ[3], do short LFSR, advance if zero",
        "  counter>0:  decrement, 8-bit LFSR step (high byte only, XOR $1D)",
        "  counter<0:  full 16-bit LFSR step + pitch envelope S&H from COP",
        "Exit: differenced noise x coefficient -> R8:R9 -> finalize_output.multiply",
    ],
    0x0B96: "block counter == 0?",
    0x0B99: "non-zero → check sign",
    0x0B9B: "reload block counter from FREQ[3] (LFSR low byte / reload value)",
    0x0B9E: "block counter = reload value",
    0x0BA0: "still zero after reload?",
    0x0BA5: "negative → full 16-bit LFSR step",
    0x0BA7: "positive → just decrement counter",
    0x0BA9: "non-zero → 8-bit LFSR step (short path)",
    0x0BAB: "counter exhausted → clear and advance past 4-byte instruction",
    0x0BAD: "advance micro-program counter by 4",
    0x0BB0: "back to interpreter",
    0x0BB3: "point to LFSR low byte (FREQ[3])",
    0x0BB6: "rotate low byte left (16-bit LFSR shift)",
    0x0BB8: "rotate high byte left (carry chain from low)",
    0x0BBA: "carry set → apply feedback polynomial",
    0x0BBC: "XOR low byte with $87 (polynomial feedback)",
    0x0BBF: "XOR high byte with $1D (polynomial = $1D87)",
    0x0BC2: "restore R10 to instruction base",
    0x0BC5: "set IRQ1 pending (software flag) → forces synthesis_output on next reentry poll",
    0x0BC8: "check bus state (RARC) for envelope read",
    0x0BCB: "bus busy → skip envelope S&H",
    0x0BCD: "R12 = Port 3 (EXSLA routing bits)",
    0x0BCF: "R13 = Port 1 (pitch envelope from master COP)",
    0x0BD1: "re-check bus (may have changed during read)",
    0x0BD4: "bus changed → discard",
    0x0BD6: "mode bit 7 = fixed formant? (skip pitch envelope update)",
    0x0BD9: "fixed formant → skip pitch envelope update",
    0x0BDB: "reg[$1A] = pitch envelope value (modulation depth)",
    0x0BDD: "extract EXSLA routing from Port 3",
    0x0BE4: "merge EXSLA bits into reg[$12] (coefficient high bits)",
    0x0BEC: "8-bit LFSR: clear carry, rotate high byte left",
    0x0BEF: "carry → XOR with $1D (8-bit feedback)",
    0x0BF4: "R11 = FREQ[2] (multiply coefficient)",
    0x0BF7: "8-bit multiply loop counter",
    0x0BF9: "clear accumulator",
    0x0BFB: "shift coefficient right",
    0x0BFD: "bit clear → skip add",
    0x0BFF: "accumulate: R13 += LFSR high byte (reg[$11])",

    0x0C02: "shift result right",
    0x0C04: "loop 8 times",
    0x0C06: "round up (2x ADC for bias correction)",
    0x0C0A: "R11 = coefficient >> 1",
    0x0C0C: "clear R12 (sign extension)",
    0x0C0E: "center: product -= coefficient/2 (zero-mean noise)",
    0x0C12: "R11 = centered product (new running state)",
    0x0C14: "point to FREQ[1] (old running state)",
    0x0C15: "sign of old running state?",
    0x0C18: "negative → sign-extend subtraction",
    0x0C1A: "delta = new - old (positive old, SBC R4=0)",
    0x0C20: "delta = new - old (negative old, SBC #$FF for sign-extend)",
    0x0C25: "store new running state to FREQ[1]",
    0x0C27: "scale delta left 4 times (×16)",
    0x0C37: "mask to upper nibble",
    0x0C3A: "R8:R9 += scaled delta (accumulate into ACC)",
    0x0C3E: "→ finalize_output.multiply (12×16 multiply with coefficient)",
    0x0CA5: "called by micro_program_interpreter",
        # overflow_shift
    0x0CCC: [
        "12x16 fixed-point multiply",
        "Formula: R10:R11 = R8:R9 * COEFF / 2048",
        "COEFF is 1.11 fixed-point (1 integer bit, 11 fractional bits):",
        "  bit 11 (PITCH_HI bit 3) = integer bit (weight x1.0)",
        "  bits 10:0 = fractional (weight x0.0 to x0.999)",
        "  Range: 0.0 to ~2.0.  Unity (x1.0) at COEFF=$800",
        "PITCH_HI[5:4] = EXSLA staging (not used by multiply)",
        "PITCH_HI[7:6] = EXSLA bits (used by pitch_acc_positive AFTER multiply)",
        "12 unrolled iterations: 8 from PITCH_LO + 4 from PITCH_HI[3:0]",
        "NOTE: no final RRC after last iteration -> bit 11 has weight 1.0",
        "When COEFF > $800 (multiplier > 1.0): result can exceed ACC",
        "  -> carry after last ADC -> JP C, overflow",
    ],
    0x0CCE: "R13=PITCH_LO",
    0x0CD0: "clear result accumulator R10:R11",
    0x0CD4: "coeff bit 0 (PITCH_LO bit 0)",
    0x0CE0: "coeff bit 1",
    0x0CEC: "coeff bit 2",
    0x0CF8: "coeff bit 3",

    0x0D04: "coeff bit 4",
    0x0D10: "coeff bit 5",
    0x0D1C: "coeff bit 6",
    0x0D28: "coeff bit 7 (PITCH_LO bit 7, last of low byte)",
    0x0D34: "coeff bit 8 (PITCH_HI bit 0)",
    0x0D40: "coeff bit 9",
    0x0D4C: "coeff bit 10",
    0x0D58: [
        "Coeff bit 11 = PITCH_HI bit 3 (integer bit, weight 1.0).",
        "Last iteration: adds ACC at FULL weight (no final RRC shift).",
        "When bit 11=1, carry from ADC is an ACC SIGN TEST:",
        "  ACC positive (< $8000): partial + ACC fits → carry=0 → pitch_acc_positive",
        "  ACC negative (>= $8000): partial + large unsigned → carry=1 → pitch_acc_negative",
        "Bit 11 must be 1 for sign detection to work. If 0, carry is random.",
    ],
    0x0D5A: "bit 11 clear → skip ADD, carry from previous RRC (unreliable)",
    0x0D5C: "bit 11 set → add ACC at weight 1.0 (result ≈ ACC × (1 + fraction))",
    0x0D60: "carry = ACC sign: positive → pitch_acc_positive, negative → pitch_acc_negative",
    0x0D66: "LD R12, R10 - returns to finalize_output.check_underflow",
    0x0D75: "skip bit0 envelope direction handled at 0xDD9",
    0x0DEA: "returns to pitch_output",
    0x0DF5: "returns to pitch_output[.underflow_shift_entry]",
    # pitch_output: compute fractional dither + R13 table index
    0x0DF8: [
        "Paths from finalize.envelope and finalize.pitch_acc_negative join here"
        "pitch_output: prepare synthesis_output parameters",
        "Inputs: R12 = multiply result high (magnitude), R11 = low byte",
        "        R13 = sub mode (from check_underflow/normalize/overflow)",
        "R11 bits 7:5 select fractional freq dither pattern from ROM table at $0232.",
        "Dither pattern stored in SPH, rotated each IRQ4 for sub-sample pitch accuracy.",
    ],
    0x0DFA: "extract R11 bits 7:5: SWAP nibbles",
    0x0DFC: "RR: bits 7:5 now at 2:0",
    0x0DFE: "isolate 3-bit index (0-7)",
    # synthesis_output flow annotations
    0x0E62: [
        "synthesis_output: read param table and switch micro-op mode.",
        "Inputs:",
        "  R13 = param table index (from pitch_output, points to Synthesis output param table byte0)",
        "  R11 = fractional dither pattern (from pitch_output ROM[$0232+i])",
        "  R10 = multiply result magnitude (from R12 at pitch_output)",
        "ROM[$01:R13] = 3-byte entry: byte0=T (timer/freq step), byte1=micro-op, byte2=PRE0.",
        "",
        "T byte determines which mode family the new sub belongs to:",
        "  T >= $40: oversampled modes (8x/4x/2x). Timer runs at T=$80.",
        "  T <  $40: direct 1x DDS mode ($C4). Timer runs at T=$01-$20.",
        "",
        "UPPER PATH (T>=$40 → oversampled modes, sub 0-7):",
        "  Reads byte1 (new micro-op: $20/$82/$AC).",
        "  If reg[$1E]<$C4 (already oversampled): just update reg[$1E]. No R5 change.",
        "    The running chain picks up new reg[$1E] via LD R5,reg[$1E] at chain end.",
        "  If reg[$1E]>=$C4 (was 1x DDS): .switch_to_oversampling: DI, R14=R15=waveform[PHASE],",
        "    force R5 to new oversample chain. Safe transition: R14/R15 primed as samples.",
        "",
        "LOWER PATH (T<$40 → 1x DDS, sub 8+):",
        "  If reg[$1E]>=$C4 (already 1x DDS): update R14=T (new freq step), update reg[$1E].",
        "    No R5 change needed — $C4 reloads R5 from reg[$1E] every IRQ4.",
        "  If reg[$1E]<$C4 (was oversampled): .switch_to_direct_sampling: DI, R14=T, force R5=$C4.",
        "    Chain transitions are safe: chain runs to last step before reading new reg[$1E].",
        "    Race only when R5=$19 (micro_init): sets R7=$40, then $C4 reads 40h($40)=CRASH.",
        "    See WIP_ocean_crash.md.",
    ],
    0x0E65: "R12=$01: ROM high byte for param table at $01xx",
    0x0E67: "save dither pattern to SPH — IRQ4 rotates SPH, carry dithers T0 ±1",
    0x0E69: "byte0 = T (timer reload for oversampled / freq step for 1x DDS)",
    0x0E6B: "advance to byte1",
    0x0E6C: "T >= $40? split oversampled vs 1x DDS",
    0x0E6F: "T < $40 → 1x DDS path (sub 8+)",
    0x0E71: "byte1 = new oversample micro-op ($20/$82/$AC)",
    0x0E73: "currently in 1x DDS mode? (reg[$1E] >= $C4)",
    0x0E76: "already oversampled → common (no R5 override); was 1x DDS → switch_to_oversampling",
    0x0E78: "1x DDS → oversample: DI, prime R14/R15 as waveform samples",
    0x0E79: "R15 = current waveform sample at PHASE",
    0x0E7C: "R14 = R15 (both primed for oversample chain working registers)",
    0x0E7E: "force R5 = new oversample micro-op",
    0x0E82: "T < $40: currently in 1x DDS? (reg[$1E] >= $C4)",
    0x0E85: "was oversampled → switch_to_direct_sampling; already 1x DDS → update_phase_increment",
    0x0E87: "1x DDS steady: update R14 = T = new freq step",
    0x0E89: "byte1 = $C4 (same mode, new freq step only)",
    0x0E8B: "update reg[$1E], R5 unchanged — $C4 reloads from reg[$1E] each IRQ4",
    0x0E8D: "oversample → 1x DDS: DI (chain transitions safe — chain completes first)",
    0x0E8E: "R14 = T = freq step for 1x DDS",
    0x0E90: "byte1 = $C4 (micro_1x_dds)",
    0x0E92: "force R5=$C4 — if IRQ4 ran micro_init before DI, R7=$40 → CRASH",
    0x0E94: "reg[$1E] = new micro-op for next IRQ4 dispatch",
    0x0EB6: "restore R11 = dither pattern from SPH",
    0x0E96: "re-enable interrupts",
    0x0E97: "advance to byte2",
    0x0E98: "byte2 = PRE0 (timer prescaler: bits 7:2 = divisor, 0=64)",
    0x0E9A: "set timer prescaler hardware register",
    0x0E9C: [
        "Set pitch from normalized multiply result (NOT the T byte!).",
        "R10 = high byte of ACC * PITCH_coeff / 2048, normalized to >= $1E.",
        "This IS the actual pitch: timer period = R10 * prescaler * 4 ext clocks.",
        "IRQ4 dithers ±1 via SPH for fractional pitch (3 extra bits from R11).",
    ],
    0x0E9E: "TIMER_LOAD = R10. IRQ1 writes TIMER_LOAD→T0 to start timer",
    0x0EA0: "FLAGS bit 0: 0→interpreter (normal), 1→pass_done (first pass only)",
    # synthesis_pass_done: runs ONCE after first RAUD setup.
    # Enables IRQ1 (ECLK), which triggers irq1_handler to start the DAC timer.
    # After this, the voice is "live" — IRQ4 fires micro-ops at the sample rate.
    0x0EA6: [
        "synthesis_pass_done: runs ONCE at end of initial RAUD setup.",
        "OR IMR,#$02: enable IRQ1 (ECLK input on P3.3) — wait for COP's first ECLK.",
        "When COP sends the next ECLK pulse, irq1_handler fires:",
        "  loads T0 timer, starts T0, enables IRQ4, forces first IRQ4",
        "  → micro-op dispatch begins → DAC output starts.",
        "After this one-shot, FLAGS bit 0 is cleared → subsequent passes go",
        "through micro_program_interpreter instead (normal synthesis loop).",
    ],
    0x0EA9: "re-enable interrupts",
    0x0EAA: "set P01M to output mode (Port 0 = DAC data, Port 1 = bus)",
    0x0EAD: "toggle Port 2 bus control bits (release slave RAM bus)",
    0x0EB0: "set P2M for RARC operation",
    0x0EB3: "clear FLAGS bit 0 → next synthesis_output_common goes to interpreter, not here",
    0x0EB6: "restore R11 = dither pattern from SPH",
    0x0EB8: "R13 -= 2: undo the 2x INC in synthesis_output → R13 back to byte0 offset",
    0x0EBB: "→ synthesis_loop_reentry: poll for ECLK, then call synthesis_output",
    # irq1_handler: triggered by ECLK, starts the DAC timer chain
    0x0EBE: [
        "irq1_handler: triggered by ECLK (COP envelope clock, ~200Hz per voice).",
        "LD T0, TIMER_LOAD: load T0 counter with R6 (base pitch value).",
        "Starts timer T0 which generates IRQ4 at the sample rate.",
        "Only fires ONCE per ECLK cycle (disables IRQ1, enables IRQ4).",
        "After IRET, IRQ4 fires immediately (forced pending) → first micro-op runs.",
    ],
    0x0EC0: "start T0 timer with T_OUT toggle (TMR bits 1:0 = $03)",
    0x0EC3: "R5 = reg[$1E] — load current micro-op for IRQ4 dispatch",
    0x0EC5: "disable IRQ1 (one-shot: don't re-trigger until next synthesis_pass_done)",
    0x0EC8: "enable IRQ4 (timer T0 interrupt → micro-op dispatch at sample rate)",
    0x0ECB: "force IRQ4 pending → first micro-op fires immediately after IRET",
    0x0ECE: "return from interrupt → IRQ4 fires → micro-op chain begins",
    # pitch_output → synthesis_loop_reentry: R13 computed
    0x0E0F: "R13 += mode_offset (\$20=ModeA, \$00=ModeB, \$10=ModeC)",
    0x0E11: "R12 = 1 (for R13*3+1 computation)",
    0x0E13: "R12 = 1 + (sub + mode_offset)",
    0x0E15: "R13 = (sub + mode_offset) * 2",
    0x0E17: "R13 = (sub+mode_offset)*2 + 1 + (sub+mode_offset) = (sub+mode_offset)*3 + 1",
    0x0E19: "$F2 DDS amplitude gate: check if waveform exceeds threshold",
    0x0E1C: "not $F2 → synthesis_loop_reentry with R13 = (sub+mode)*3+1",
    0x0E1E: "re-read param table entry (R12=\$01, R13 from pitch_output)",
    0x0E20: "R12 = ROM byte (threshold for $F2 gate)",
    0x0E22: "threshold × 2",
    0x0E24: "compare threshold against WAVE_R15 - $F2 does not modify WAVE_R15, it is a loop marker",
    0x0E26: "waveform < threshold → normal (keep running)",
    0x0E28: "waveform >= threshold → force silence: R13=\$2E = Mode C sub=15 (T=\$00, op=micro_init)",
    # synthesis_loop_reentry — ECLK poll + bus handshake for EXSLA/pitch envelope
    0x0E2A: "test IRQ bit 1 (ECLK pending from COP or LFSR software set)",
    0x0E2D: "ECLK pending → call synthesis_output",
    0x0E2F: "bus check: RARC bit 2 clear = bus available",
    0x0E32: "bus busy → retry",
    0x0E34: "stage 1: read Port 3 for EXSLA bits",
    0x0E36: "RR: shift EXSLA (bits 2:1) → bits 1:0",
    0x0E38: "SWAP: bits 1:0 → bits 5:4",
    0x0E3A: "keep only bits 5:4 = EXSLA (staging position)",
    0x0E3D: "clear staging area in PITCH_HI (bits 5:4)",
    0x0E40: "merge EXSLA into PITCH_HI bits 5:4 (temporary)",
    0x0E43: "read Port 1 = pitch envelope from master COP",
    0x0E45: "re-check bus (may have changed during Port 1 read)",
    0x0E48: "bus changed → restart (discard both EXSLA and pitch)",
    0x0E4A: "mode bit 7 = fixed formant? (skip pitch envelope update)",
    0x0E4D: "override set → restart (don't update pitch envelope)",
    0x0E4F: "commit: store pitch envelope (bus verified stable)",
    0x0E51: "stage 2: promote EXSLA from bits 5:4 to final bits 7:6",
    0x0E53: "RL×2: shift bits 5:4 → bits 7:6",
    0x0E57: "keep only bits 7:6 = EXSLA (final position)",
    0x0E5A: "clear old bits 7:6 in PITCH_HI",
    0x0E5D: "commit: merge EXSLA into PITCH_HI bits 7:6",
    0x0E60: "back to poll loop",
    0x0E01: "table base $32 + index",
    0x0E04: "R11 = ROM[$0232+i] = fractional dither pattern (0-7 of 8 bits set)",
    0x0E06: [
        "8th RR restores PITCH_ENV to original value after",
        "7 RR's in pitch_acc_positive/overflow multiply. Needed when ECLK is",
        "already pending or bus not free (PITCH_ENV not refreshed from Port 1).",
        "Also entry from underflow_shift cap (R12=$1E, R11=0 = no dither).",
    ],
    0x0E08: [
        "Calculate synthesis mode table pointer",
        "R10 = R12 (save magnitude for synthesis_output pitch calc)"
    ],
    # normalize
    0x0F92: [
        "normalize: floating-point normalization of multiply result.",
        "R12:R11 = mantissa (multiply result), R13 = exponent (sub mode).",
        "Shifts mantissa left (x2) and INC exponent until R12 >= $1E.",
        "Each sub INCrement selects a faster timer / simpler micro-op chain.",
        "R11 bits 7:5 become the fractional dither index at pitch_output",
        "(3 bits of sub-sample pitch precision from the normalize remainder).",
        "Sub caps at $0F (15). If still < $1E at cap: force R12=$1E, R11=0",
        "(no dither = exact pitch, enters underflow_shift_entry skipping dither lookup).",
    ],
    0x0F95: "sub < 15 → keep normalizing",
    0x0F97: "INC sub (exponent++)",
    0x0F98: "clear carry for clean left shift",
    0x0F99: "RLC R11:R12 = mantissa x2 (shift bits up, dither bits accumulate in R11)",
    0x0F9D: "R12 >= $1E? mantissa normalized → pitch_output (R11 has dither in bits 7:5)",
    0x0FA3: "loop: keep shifting until normalized or capped",
    0x0FA5: "cap: force R12=$1E (minimum normalized magnitude)",
    0x0FA7: "R11=0 → SPH=$00 at synthesis_output → no timer dither (exact integer pitch)",
    0x0FA9: "→ underflow_shift_entry: skip dither table lookup, R11=0 passes through to SPH",
    # overflow_shift
    0x0F84: "reload sub from mode byte",
    0x0F89: "DEC sub — overflow means signal is too large for current sub",
    0x0F8B: "if sub still >= 0, use decremented sub",
    0x0F8E: "sub went negative → restore to 0",
}

# Working register aliases — emitted as EQU and substituted in synthesis code.
# In setup/init code (before micro-ops) these registers may have other meanings,
# so substitution only applies at addr >= REG_ALIAS_MIN_ADDR.
REG_ALIASES = {
    "R0":  ("PORT0_DAC",  "Port 0 DAC"),
    "R1":  ("PORT1",      "Port 1 readback (pitch envelope from master COP)"),
    "R2":  ("PORT2",      "Port 2 readback (bus state: bit 2=RARC)"),
    "R3":  ("PORT3",      "Port 3 readback (EXSLA bits 2:1, ECLK bit 3)"),
    "R4":  ("ZERO",       "always 0 (cleared at synthesis_loop_entry)"),
    "R5":  ("MICRO_OP",   "current micro-op dispatch address (IRQ4 target)"),
    "R6":  ("TIMER_LOAD", "timer reload value (written to T0 by IRQ1)"),
    "R7":  ("PHASE",      "waveform phase accumulator / table index"),
    "R8":  ("ACC_HI",     "accumulator high byte (frequency deviation)"),
    "R9":  ("ACC_LO",     "accumulator low byte"),
    # "R10": ("MUL_HI",     "multiply result high / general temp"),
    # "R11": ("MUL_LO",     "multiply result low / general temp"),
    "R14": ("WAVE_R14",  "frequency step / phase increment"),
    "R15": ("WAVE_R15",  "previous waveform sample (interpolation)"),
}

# Memory-mapped register aliases (absolute addresses $10-$3C range)
# These are NOT working registers — they're accessed via direct addressing.
MEM_REG_ALIASES = {
    0x10: ("MODE",        "mode register (from slave_ram[$FA])"),
    0x11: ("SAVED_HI",    "saved state (LFSR high byte / coeff running offset)"),
    0x12: ("PITCH_HI",    "pitch coeff high: [7:6]=EXSLA [3:0]=coeff.hi (slave_ram[$FC] + EXSLA from Port 3)"),
    0x13: ("PITCH_LO",    "pitch coeff low = coeff.lo (from slave_ram[$FD])"),
    0x14: ("ACC2_HI",     "secondary accumulator high (NOT R14!)"),
    0x15: ("ACC2_LO",     "secondary accumulator low (NOT R15!)"),
    0x16: ("ACC3_HI",     "tertiary accumulator high"),
    0x17: ("ACC3_LO",     "tertiary accumulator low"),
    0x18: ("LOOP1_CTR",   "primary loop counter"),
    0x19: ("LOOP2_CTR",   "secondary loop counter"),
    0x1A: ("PITCH_ENV",   "pitch envelope (from Master, read via Port 1 from slave ram bus-idle address 0xff)"),
    0x1B: ("PROG_CTR",    "micro-program counter (indexes $1D-$3C)"),
    0x1C: ("BLOCK_CTR",   "block/phase counter (LFSR countdown / coeff toggle)"),
    0x1D: ("TIMER_VAL",   "timer reload / pitch (set by synthesis_output)"),
    0x1E: ("MICRO_OP_NEXT",    "micro-op chain address (set by synthesis_output)"),
}

# Address range where register aliases apply (synthesis code, not setup)
REG_ALIAS_MIN_ADDR = 0x000F  # IRQ4 handler start

# Build reverse map: address → label name (for resolving jump targets)
ADDR_TO_LABEL = {addr: label for addr, v in LABELS.items() if v is not None for label, _ in [v] if label}

# SFR name → (absolute address, comment)
SFR_INFO = {
    "SPH":  ("0FEh", "phase accumulator high"),
    "SPL":  ("0FFh", "stack pointer low"),
    "FLAGS": ("0FCh", "flags register"),
    "IMR":  ("0FBh", "interrupt mask"),
    "IRQ":  ("0FAh", "interrupt request"),
    "IPR":  ("0F9h", "interrupt priority"),
    "P01M": ("0F8h", "port 0/1 mode"),
    "P3M":  ("0F7h", "port 3 mode"),
    "P2M":  ("0F6h", "port 2 mode / RARC"),
    "PRE0": ("0F5h", "timer 0 prescaler"),
    "T0":   ("0F4h", "timer 0 counter"),
    "PRE1": ("0F3h", "timer 1 prescaler"),
    "T1":   ("0F2h", "timer 1 counter"),
    "TMR":  ("0F1h", "timer mode register"),
    "SIO":  ("0F0h", "serial I/O"),
    "P0":   ("000h", "port 0 / DAC output"),
    "P1":   ("001h", "port 1 / slave data bus"),
    "P2":   ("002h", "port 2 / bus control"),
    "P3":   ("003h", "port 3 / ECLK,EXSLA,RAUD"),
}
SFR_MAP = {k: v[0] for k, v in SFR_INFO.items()}


def is_data(addr):
    """Check if an address is in a data region."""
    for start, end, _ in DATA_RANGES:
        if start <= addr < end:
            return True
    return False


def disassemble(rom, start, end):
    """Disassemble a range using unidasm. Returns list of (addr, raw_bytes, mnemonic, operands)."""
    with open("/tmp/_dis.bin", "wb") as f:
        f.write(rom[start:end])
    result = subprocess.run(
        [UNIDASM, "/tmp/_dis.bin", "-arch", "z8", "-basepc", hex(start)],
        capture_output=True, text=True,
    )
    instructions = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        m = re.match(r"\s*([0-9a-f]+):\s+((?:[0-9a-f]{2}\s*)+)\s+(\S+)\s*(.*)", line)
        if m:
            addr = int(m.group(1), 16)
            raw = bytes.fromhex(m.group(2).replace(" ", ""))
            mnemonic = m.group(3)
            operands = m.group(4).strip()
            instructions.append((addr, raw, mnemonic, operands))
    return instructions


def convert_operands(operands, raw_bytes=None, instr_addr=0):
    """Convert unidasm operand syntax to ASL syntax.
    Returns (asm_operands, comment) tuple."""
    ops = operands
    comment = ""

    # Collect SFR comments for any SFR referenced in operands
    sfr_comments = []
    for name in sorted(SFR_INFO.keys(), key=len, reverse=True):
        if re.search(r'\b' + name + r'\b', ops):
            sfr_comments.append(f"{name}={SFR_INFO[name][1]}")

    if sfr_comments:
        # Show "address=NAME (description)" so reader learns the shorthand
        parts = []
        for name in sorted(SFR_INFO.keys(), key=len, reverse=True):
            if re.search(r'\b' + name + r'\b', ops):
                addr, desc = SFR_INFO[name]
                parts.append(f"{addr[:-1]}={name} ({desc})")
        comment = "; " + ", ".join(parts)

    # Fix $D7 indexed write: unidasm has a bug — it disassembles
    # $D7 $DC $01 as "LD R12, 01h(R13)" but the correct Z8 decoding is
    # "LD 01h(R12), R13" (r1=high nibble=source, r2=low nibble=base).
    # unidasm swaps the register roles AND shows it as a load instead of store.
    # Fix: swap operands to "LD offset(base), source" with corrected registers.
    if raw_bytes and len(raw_bytes) == 3 and raw_bytes[0] == 0xD7:
        src_reg = (raw_bytes[1] >> 4) & 0xF   # high nibble = source
        base_reg = raw_bytes[1] & 0xF          # low nibble = base
        offset = raw_bytes[2]
        offset_str = f"0{offset:02X}h" if offset >= 0xA0 else f"{offset:02X}h"
        ops = f"{offset_str}(R{base_reg:d}), R{src_reg:d}"
        return apply_reg_aliases(ops, instr_addr), comment

    return apply_reg_aliases(convert_operands_inner(ops), instr_addr), comment


def apply_reg_aliases(ops, instr_addr):
    """Replace working register names and memory register addresses with aliases."""
    if instr_addr < REG_ALIAS_MIN_ADDR:
        return ops
    # Working registers: R10, R11 before R1 (sorted by length descending)
    for reg, (alias, _) in sorted(REG_ALIASES.items(), key=lambda x: -len(x[0])):
        ops = re.sub(r'\b' + reg + r'\b', alias, ops)
    # Memory-mapped registers: replace "1Bh" with "PROG_CTR" etc.
    # Only replace when used as direct register address, NOT as immediate (#1Bh)
    # or as part of a larger hex number (01Bh). Match: standalone "XXh" not preceded by # or digit.
    for addr, (alias, _) in sorted(MEM_REG_ALIASES.items(), reverse=True):
        hex_str = f"{addr:02X}h"
        # Match "XXh" that is:
        # - not preceded by # (immediate) or 0-9/A-F (part of larger hex)
        # - not inside @XXh (indirect) — actually @XXh IS a register, so DO replace
        ops = re.sub(r'(?<![#0-9A-Fa-f])' + hex_str + r'\b', alias, ops)
    return ops


def convert_operands_inner(ops):
    """Apply SFR replacements, hex formatting, and label resolution."""
    # Replace SFR names with absolute addresses
    for name in sorted(SFR_MAP.keys(), key=len, reverse=True):
        ops = re.sub(r'\b' + name + r'\b', SFR_MAP[name], ops)

    # Resolve jump/call targets to labels.
    # Only resolve in JP/JR/CALL/DJNZ operands — not in LD/ADD/etc. where
    # hex values are register addresses or immediates, not code targets.
    # This is handled by the caller (emit_code) which checks the mnemonic.

    # Fix hex values starting with A-F that need a leading 0
    ops = re.sub(r'(?<=#)([A-Fa-f][0-9A-Fa-f]*h)', r'0\1', ops)
    ops = re.sub(r'(?<=, )([A-Fa-f][0-9A-Fa-f]*h)', r'0\1', ops)
    ops = re.sub(r'^([A-Fa-f][0-9A-Fa-f]*h)', r'0\1', ops)

    return ops


def build_opcode_map(rom):
    """Build mapping: table_addr → list of opcodes that use it."""
    table_a_map = {}  # table_addr → [opcodes]
    table_b_map = {}
    for opc in range(256):
        typ = opc & 0x03
        if typ == 0:
            # Type A: index = (opcode >> 1) & $7E → table at $01B2 + index
            index = (opc >> 1) & 0x7E
            table_addr = 0x01B2 + index
            table_a_map.setdefault(table_addr, []).append(opc)
        else:
            # Type B: index = ((opcode << 1) & $3E) + $92 → table at $0100 + index
            index = ((opc << 1) & 0x3E) + 0x92
            table_addr = 0x0100 + index
            table_b_map.setdefault(table_addr, []).append(opc)
    return table_a_map, table_b_map


def format_opcode_list(opcodes, max_show=4):
    """Format a list of opcodes as compact hex string."""
    if not opcodes:
        return "DEAD"
    shown = [f"${o:02X}" for o in opcodes[:max_show]]
    s = ",".join(shown)
    if len(opcodes) > max_show:
        s += f"..({len(opcodes)})"
    return s


def emit_dispatch_table(rom, start, end, comment):
    """Emit a dispatch table with address → label + opcode mapping."""
    table_a_map, table_b_map = build_opcode_map(rom)

    lines = []
    lines.append(f"; === {comment} ===")
    lines.append(f"        ORG     {start:04X}h")
    for addr in range(start, end, 2):
        hi = rom[addr]
        lo = rom[addr + 1]
        target = (hi << 8) | lo
        label = ADDR_TO_LABEL.get(target, f"${target:04X}")

        # Find which opcodes map to this entry
        opcodes = table_a_map.get(addr, []) or table_b_map.get(addr, [])
        opc_str = format_opcode_list(opcodes)

        lines.append(f"        DW      {target:04X}h      ; opc {opc_str:16s} → {label}")
    return lines


def emit_vectors(rom, start, end, comment=""):
    """Emit IRQ vector table as DW with vector names."""
    vector_names = ["IRQ0/P3.2", "IRQ1/P3.3", "IRQ2/P3.1", "IRQ3/P3.0", "IRQ4/T0", "IRQ5/T1"]
    lines = []
    if comment:
        lines.append(f"; === {comment} ===")
    lines.append(f"        ORG     {start:04X}h")
    for i in range(min((end - start) // 2, len(vector_names))):
        addr = start + i * 2
        target = (rom[addr] << 8) | rom[addr + 1]
        label = ADDR_TO_LABEL.get(target, f"${target:04X}")
        hex_hi = f"0{rom[addr]:02X}h" if rom[addr] >= 0xA0 else f"{rom[addr]:02X}h"
        hex_lo = f"0{rom[addr+1]:02X}h" if rom[addr+1] >= 0xA0 else f"{rom[addr+1]:02X}h"
        lines.append(f"        DW      {target:04X}h      ; {vector_names[i]:12s} → {label}")
    return lines


def emit_lut4(rom, start, end, comment=""):
    """Emit a lookup table as DB with max 4 bytes per line."""
    lines = []
    if comment:
        lines.append(f"; === {comment} ===")
    lines.append(f"        ORG     {start:04X}h")
    addr = start
    idx = 0
    while addr < end:
        row_end = min(addr + 4, end)
        chunk = rom[addr:row_end]
        hex_bytes = ", ".join(
            f"0{b:02X}h" if b >= 0xA0 else f"{b:02X}h" for b in chunk
        )
        lines.append(f"        DB      {hex_bytes:32s}; [{idx}]-[{idx+len(chunk)-1}]")
        idx += len(chunk)
        addr = row_end
    return lines


def emit_data(rom, start, end, comment=""):
    """Emit a data range as DB directives, with labels for branch targets."""
    lines = []
    if comment:
        lines.append(f"; === {comment} ===")
    lines.append(f"        ORG     {start:04X}h")

    # Emit byte-by-byte where labels are needed, row-by-row otherwise
    addr = start
    while addr < end:
        # Check if any label falls in the next 16 bytes
        row_end = min(addr + 16, end)
        labels_in_row = [a for a in range(addr, row_end) if a in ADDR_TO_LABEL]

        if not labels_in_row:
            # No labels — emit full row
            chunk = rom[addr:row_end]
            hex_bytes = ", ".join(
                f"0{b:02X}h" if b >= 0xA0 else f"{b:02X}h" for b in chunk
            )
            lines.append(f"        DB      {hex_bytes}")
            addr = row_end
        else:
            # Labels present — split row at each label
            for label_addr in sorted(labels_in_row):
                if addr < label_addr:
                    chunk = rom[addr:label_addr]
                    hex_bytes = ", ".join(
                        f"0{b:02X}h" if b >= 0xA0 else f"{b:02X}h" for b in chunk
                    )
                    lines.append(f"        DB      {hex_bytes}")
                lines.append(f"{ADDR_TO_LABEL[label_addr]}:")
                addr = label_addr
            # Emit remaining bytes in this row
            if addr < row_end:
                chunk = rom[addr:row_end]
                hex_bytes = ", ".join(
                    f"0{b:02X}h" if b >= 0xA0 else f"{b:02X}h" for b in chunk
                )
                lines.append(f"        DB      {hex_bytes}")
                addr = row_end

    return lines


def emit_param_table(rom, start, end, comment=""):
    """Emit the synthesis output parameter table with per-entry annotations.
    3 bytes per entry: byte0=T, byte1=micro_op_addr, byte2=PRE0.
    Read by synthesis_output: byte0 CP #$40 splits oversampled vs 1x DDS,
    byte1 → reg[$1E]/R5 (micro-op chain), byte2 → PRE0 ($F5, timer prescaler).
    3 modes × 16 sub-modes = 48 entries."""
    mode_names = ["Mode C", "Mode B", "Mode A"]
    lines = []
    if comment:
        lines.append(f"; === {comment} ===")
    lines.append(f";   byte0=T: bit7=1 oversampled (timer only), bit7=0 bits5:0=phase_inc (R14)")
    lines.append(f";   byte1=micro_op address ($20=8x, $82=4x, $AC=2x, $C4=1x, $CF=duty, $F2=wrap, $19=silence)")
    lines.append(f";   byte2=PRE0: timer prescaler (bits7:2=divisor 1-63, 0=64; bit1=modulo_n)")
    lines.append(f";     PRE0 extends pitch range: sub0-5 use prescaler 32→1 (halving per sub)")
    lines.append(f";     Actual pitch = R10 (normalized magnitude) * prescaler * 4 ext clocks")
    lines.append(f"        ORG     {start:04X}h")

    entry = 0
    for addr in range(start, min(end, start + 48 * 3), 3):
        mode = entry // 16
        sub = entry % 16
        timer = rom[addr]
        micro = rom[addr + 1]
        pre0 = rom[addr + 2]

        mode_str = mode_names[mode] if mode < 3 else f"mode{mode}"
        micro_label = ADDR_TO_LABEL.get(micro, f"${micro:02X}")

        hex_bytes = ", ".join(
            f"0{b:02X}h" if b >= 0xA0 else f"{b:02X}h"
            for b in [timer, micro, pre0]
        )
        lines.append(f"        DB      {hex_bytes:24s}; {mode_str:7s} sub={sub:2d}  T=${timer:02X} op={micro_label} PRE0=${pre0:02X}")
        entry += 1

    # Emit any remaining bytes past the 48 entries
    remaining_start = start + 48 * 3
    if remaining_start < end:
        chunk = rom[remaining_start:end]
        hex_bytes = ", ".join(
            f"0{b:02X}h" if b >= 0xA0 else f"{b:02X}h" for b in chunk
        )
        lines.append(f"        DB      {hex_bytes}")

    return lines


def emit_frac_freq_table(rom, start, end, comment=""):
    """Emit the DDS fractional frequency table with annotations.
    8 entries at $0232, each a bit pattern for SPH phase accumulator.
    Entry N has exactly N set bits (evenly spaced) for N/8 fractional
    timer increment. Used by dac_output_setup ($0DF8) via R11 table lookup.
    IRQ4's RL $FE rotates the pattern; carry feeds ADC R6,R4 for sub-integer
    timer rate resolution (3 extra bits → 11-bit effective pitch)."""
    lines = []
    if comment:
        lines.append(f"; === {comment} ===")
    lines.append(f";   Each entry has N set bits for N/8 fractional timer increment.")
    lines.append(f";   Bit patterns maximize temporal spacing (minimize jitter).")
    lines.append(f";   IRQ4: RL $FE; ADC R6,R4 → carry from rotation dithers timer rate.")
    lines.append(f"        ORG     {start:04X}h")

    for i in range(min(end - start, 8)):
        val = rom[start + i]
        pop = bin(val).count('1')
        hex_str = f"0{val:02X}h" if val >= 0xA0 else f"{val:02X}h"
        lines.append(f"        DB      {hex_str:8s}; [{i}] {val:08b}  {pop}/8 fractional")

    return lines


def collect_branch_targets(instructions):
    """Scan instructions for jump/call targets, return set of target addresses."""
    targets = set()
    for addr, raw, mnemonic, operands in instructions:
        if mnemonic in ("JP", "JR", "CALL", "DJNZ"):
            # Extract hex address from operands
            m = re.search(r'([0-9A-Fa-f]{3,4})h', operands)
            if m:
                target = int(m.group(1), 16)
                targets.add(target)
    return targets


# Global set of all instruction addresses (populated by emit_code)
ALL_INSTRUCTION_ADDRS = set()

def emit_code(rom, start, end):
    """Disassemble a code range and emit ASL instructions with labels."""
    instructions = disassemble(rom, start, end)
    for a, raw_i, mn, ops in instructions:
        ALL_INSTRUCTION_ADDRS.add(a)

    # Branch targets collected in pass 1 (main function)
    lines = []
    lines.append(f"        ORG     {start:04X}h")

    # Build caller map: target_addr → [(source_addr, source_function)]
    caller_map = {}
    for a, raw_i, mn, ops in instructions:
        if mn in ("JP", "JR", "CALL", "DJNZ"):
            m = re.search(r'([0-9A-Fa-f]{3,4})h', ops)
            if m:
                target = int(m.group(1), 16)
                src_func = None
                for sa in range(a, max(a - 500, -1), -1):
                    if sa in ADDR_TO_LABEL and not ADDR_TO_LABEL[sa].startswith("L_"):
                        src_func = ADDR_TO_LABEL[sa]
                        break
                caller_map.setdefault(target, []).append((a, src_func, mn))

    for addr, raw, mnemonic, operands in instructions:
        # Auto-label for branch targets that don't have a named label
        if addr not in LABELS and addr in ADDR_TO_LABEL:
            # Annotate with caller info
            callers = caller_map.get(addr, [])
            if callers:
                # Separate local (same function) from external callers
                this_func = None
                for sa in range(addr, max(addr - 500, -1), -1):
                    if sa in ADDR_TO_LABEL and not ADDR_TO_LABEL[sa].startswith("L_"):
                        this_func = ADDR_TO_LABEL[sa]
                        break
                external = [(a, f, mn) for a, f, mn in callers if f != this_func]
                label_name = ADDR_TO_LABEL[addr]
                if external:
                    srcs = ", ".join(f"{f}(${a:04X})" for a, f, mn in external[:4])
                    if len(external) > 4:
                        srcs += f"..({len(external)})"
                    lines.append(f"{label_name}:                                ; called from: {srcs}")
                else:
                    lines.append(f"{label_name}:")
            else:
                label_name = ADDR_TO_LABEL[addr]
                lines.append(f"{label_name}:")

        # Check for section header and/or label at this address
        if addr in LABELS and LABELS[addr] is not None:
            label, section = LABELS[addr]
            if section:
                lines.append("")
                lines.append(f"; {'=' * 60}")
                lines.append(f"; === {section} (${addr:04X}) ===")
                lines.append(f"; {'=' * 60}")
                lines.append(f"        ORG     {addr:04X}h")
            if label:
                # Count callers for this global label
                callers = caller_map.get(addr, [])
                if callers:
                    lines.append(f"{label}:                                ; {len(callers)} refs")
                else:
                    lines.append(f"{label}:")

        if mnemonic == "Illegal":
            hex_bytes = ", ".join(
                f"0{b:02X}h" if b >= 0xA0 else f"{b:02X}h" for b in raw
            )
            lines.append(f"        DB      {hex_bytes}  ; illegal at ${addr:04X}")
            continue

        ops, comment = convert_operands(operands, raw, addr)

        # Add per-address comments (string = EOL, list = block before instruction)
        if addr in ADDR_COMMENTS:
            entry = ADDR_COMMENTS[addr]
            if isinstance(entry, list):
                # Block comment: emit as comment lines before the instruction
                for block_line in entry:
                    lines.append(f"; {block_line}")
            else:
                # EOL comment: append to instruction line
                addr_comment = "; " + entry
                if comment:
                    comment = comment + "  " + addr_comment
                else:
                    comment = addr_comment

        # Resolve branch targets to labels (only for JP/JR/CALL/DJNZ)
        if mnemonic in ("JP", "JR", "CALL", "DJNZ"):
            def resolve_target(m):
                addr_str = m.group(0)
                target = int(addr_str.rstrip('h'), 16)
                if target in ADDR_TO_LABEL:
                    return ADDR_TO_LABEL[target]
                return addr_str
            ops = re.sub(r'\b([0-9][0-9A-Fa-f]{2,3}h)\b', resolve_target, ops)

        if ops:
            line = f"        {mnemonic:8s}{ops}"
            if comment:
                line = f"{line:48s}{comment}"
            lines.append(line)
        else:
            lines.append(f"        {mnemonic}")

    return lines


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <rom.bin> [output.asm]", file=sys.stderr)
        sys.exit(1)

    rom_path = sys.argv[1]
    rom = open(rom_path, "rb").read()
    assert len(rom) == ROM_SIZE, f"Expected {ROM_SIZE} bytes, got {len(rom)}"

    output = sys.stdout
    if len(sys.argv) > 2:
        output = open(sys.argv[2], "w")

    # Header
    output.write("; ================================================================\n")
    output.write("; SR0106 Z8611 Firmware — ASL assembler source\n")
    output.write("; AUTO-GENERATED by unidasm_to_asl.py — do not edit directly.\n")
    output.write("; All labels, comments, and section names are maintained in\n")
    output.write("; unidasm_to_asl.py. Re-run to regenerate after changes.\n")
    output.write("; ================================================================\n")
    output.write(f"; Source ROM: {rom_path}\n")
    output.write("; Target: ASL assembler (Z8601 CPU). Assembles to byte-exact ROM copy.\n")
    output.write(";\n")
    output.write("        CPU     Z8601\n")
    output.write("        ASSUME  RP:0\n")
    output.write("\n")
    output.write("; Working register aliases (synthesis code)\n")
    for reg, (alias, desc) in sorted(REG_ALIASES.items(), key=lambda x: int(x[0][1:])):
        output.write(f"{alias:16s}EQU     {reg:8s}; {desc}\n")
    output.write("\n")
    output.write("; Memory-mapped register aliases (absolute addresses)\n")
    for addr, (alias, desc) in sorted(MEM_REG_ALIASES.items()):
        output.write(f"{alias:16s}EQU     {addr:02X}h     ; {desc}\n")
    output.write("\n")

    # Build sorted list of all regions
    regions = []

    # Add data regions
    for start, end, dtype, comment in DATA_RANGES:
        regions.append((start, end, dtype, comment))

    # Add code regions (everything not data)
    data_set = set()
    for start, end, _, _ in DATA_RANGES:
        for a in range(start, end):
            data_set.add(a)

    # Find contiguous code ranges
    code_start = None
    for addr in range(ROM_SIZE):
        if addr not in data_set:
            if code_start is None:
                code_start = addr
        else:
            if code_start is not None:
                regions.append((code_start, addr, "code", ""))
                code_start = None
    if code_start is not None:
        regions.append((code_start, ROM_SIZE, "code", ""))

    # Sort by start address
    regions.sort()

    # Pass 1: scan all code regions for branch targets
    all_branch_targets = set()
    all_instructions = {}
    for start, end, rtype, comment in regions:
        if rtype == "code":
            instructions = disassemble(rom, start, end)
            all_instructions[(start, end)] = instructions
            all_branch_targets |= collect_branch_targets(instructions)

    # Register auto-labels for unresolved targets.
    # Cross-function targets stay global (L_XXXX), same-function are local (.L_XXXX).
    # We determine this by checking if all callers are in the same function.
    # Build caller→target map from all instructions.
    all_callers = {}  # target_addr → [(caller_addr, ...)]
    for (start, end), instrs in all_instructions.items():
        for a, raw, mn, ops in instrs:
            if mn in ("JP", "JR", "CALL", "DJNZ"):
                m = re.search(r'([0-9A-Fa-f]{3,4})h', ops)
                if m:
                    t = int(m.group(1), 16)
                    all_callers.setdefault(t, []).append(a)

    def find_parent_func(addr):
        """Find the enclosing named label for an address."""
        best = None
        for la, v in sorted(LABELS.items()):
            if v is None:
                continue
            label, _ = v
            if la <= addr and label:
                best = label
        return best

    for target in all_branch_targets:
        if target not in ADDR_TO_LABEL:
            # Check if all callers are in the same function as the target
            target_func = find_parent_func(target)
            callers = all_callers.get(target, [])
            all_local = callers and all(find_parent_func(c) == target_func for c in callers)
            if all_local and target_func:
                ADDR_TO_LABEL[target] = f".L_{target:04X}"
            else:
                ADDR_TO_LABEL[target] = f"L_{target:04X}"

    # Pass 2: emit each region
    for start, end, rtype, comment in regions:
        output.write("\n")
        if rtype == "dispatch":
            for line in emit_dispatch_table(rom, start, end, comment):
                output.write(line + "\n")
        elif rtype == "param_table":
            for line in emit_param_table(rom, start, end, comment):
                output.write(line + "\n")
        elif rtype == "frac_freq_table":
            for line in emit_frac_freq_table(rom, start, end, comment):
                output.write(line + "\n")
        elif rtype == "vectors":
            for line in emit_vectors(rom, start, end, comment):
                output.write(line + "\n")
        elif rtype == "lut4":
            for line in emit_lut4(rom, start, end, comment):
                output.write(line + "\n")
        elif rtype == "data":
            for line in emit_data(rom, start, end, comment):
                output.write(line + "\n")
        else:
            for line in emit_code(rom, start, end):
                output.write(line + "\n")

    output.write("\n        END\n")

    out_path = None
    if output != sys.stdout:
        out_path = output.name
        output.close()
        print(f"Wrote {out_path}", file=sys.stderr)

        # Validate ADDR_COMMENTS: check for addresses not on instruction boundaries
        orphan_comments = []
        for addr in sorted(ADDR_COMMENTS.keys()):
            # Check against instruction addresses and data ranges
            in_data = any(s <= addr < e for s, e, *_ in DATA_RANGES)
            if addr not in ALL_INSTRUCTION_ADDRS and not in_data:
                orphan_comments.append(addr)
        if orphan_comments:
            print(f"WARNING: {len(orphan_comments)} ADDR_COMMENTS not on instruction addresses:", file=sys.stderr)
            for addr in orphan_comments:
                entry = ADDR_COMMENTS[addr]
                preview = entry[0] if isinstance(entry, list) else entry
                if len(preview) > 60:
                    preview = preview[:60] + "..."
                print(f"  ${addr:04X}: {preview}", file=sys.stderr)

        # Iteratively promote .L_ locals that fail ASL scoping to globals
        print("Validating local label scoping...", file=sys.stderr)
        promote_locals_iteratively(rom_path, out_path)


def promote_locals_iteratively(rom_path, asm_path):
    """Iteratively promote .L_ local labels to L_ globals until ASL compiles clean.
    Each 'symbol undefined' error means a cross-function reference → promote to global."""
    max_iterations = 20
    for iteration in range(max_iterations):
        # Remove stale .p file to avoid false success
        import os as _os
        try:
            _os.remove("/tmp/_promote_test.p")
        except FileNotFoundError:
            pass
        result = subprocess.run(
            ["asl", asm_path, "-o", "/tmp/_promote_test.p"],
            capture_output=True, text=True,
        )
        # Parse errors from stderr: "firmware.asm(209):21: error: symbol undefined"
        errors = []
        lines = open(asm_path).readlines()
        for err_line in result.stderr.split("\n"):
            m = re.match(r".*\((\d+)\):\d+: error: symbol undefined", err_line)
            if m:
                lineno = int(m.group(1)) - 1
                if 0 <= lineno < len(lines):
                    # Find .L_XXXX references on this line
                    for sym in re.findall(r'\.L_[0-9A-Fa-f]{4}', lines[lineno]):
                        errors.append(sym)

        if not errors:
            print(f"  Iteration {iteration+1}: 0 errors — all labels resolved ✓", file=sys.stderr)
            break

        # Promote each failed local to global
        unique_errors = sorted(set(errors))
        print(f"  Iteration {iteration+1}: promoting {len(unique_errors)} locals to global: "
              f"{', '.join(unique_errors[:5])}{'...' if len(unique_errors)>5 else ''}", file=sys.stderr)

        content = open(asm_path).read()
        for sym in unique_errors:
            global_name = sym[1:]  # remove leading dot
            # Replace label definitions: ".L_XXXX:" → "L_XXXX:"
            content = content.replace(sym + ":", global_name + ":")
            # Replace label references in operands: use word boundary to avoid
            # matching hex addresses like 0FCh that contain the same digits
            content = re.sub(r'(?<!\w)' + re.escape(sym) + r'(?!\w)', global_name, content)
        with open(asm_path, "w") as f:
            f.write(content)
    else:
        print(f"  WARNING: still errors after {max_iterations} iterations", file=sys.stderr)


def export_callgraph(rom, output_path):
    """Export a Graphviz DOT file of the call/jump graph."""
    # Collect all code regions
    data_set = set()
    for start, end, _, _ in DATA_RANGES:
        for a in range(start, end):
            data_set.add(a)

    # Disassemble all code
    all_instructions = []
    code_start = None
    for addr in range(ROM_SIZE):
        if addr not in data_set:
            if code_start is None:
                code_start = addr
        else:
            if code_start is not None:
                all_instructions.extend(disassemble(rom, code_start, addr))
                code_start = None
    if code_start is not None:
        all_instructions.extend(disassemble(rom, code_start, ROM_SIZE))

    # Build edges: source_function → target_function
    # A "function" = the nearest named label at or before an instruction
    edges = set()

    def find_containing_function(addr):
        """Find the nearest global (non-local) label at or before addr."""
        for a in range(addr, max(addr - 500, -1), -1):
            label = ADDR_TO_LABEL.get(a)
            if label and not label.startswith("L_") and not label.startswith("."):
                return label
        return None

    for i, (addr, raw, mnemonic, operands) in enumerate(all_instructions):
        if mnemonic in ("JP", "JR", "CALL", "DJNZ"):
            m = re.search(r'([0-9A-Fa-f]{3,4})h', operands)
            if m:
                target = int(m.group(1), 16)
                src_label = find_containing_function(addr)
                dst_label = find_containing_function(target)

                if src_label and dst_label and src_label != dst_label:
                    is_call = mnemonic == "CALL"
                    edges.add((src_label, dst_label, is_call))

        # Detect fall-through: if the NEXT instruction is a different named function
        # and this instruction doesn't unconditionally transfer control
        if i + 1 < len(all_instructions):
            next_addr = all_instructions[i + 1][0]
            next_label = ADDR_TO_LABEL.get(next_addr)
            if next_label and not next_label.startswith("L_") and not next_label.startswith("."):
                # Check if current instruction is an unconditional transfer
                is_unconditional = (
                    (mnemonic == "JP" and "," not in operands and "@" not in operands) or
                    (mnemonic == "JR" and "," not in operands) or
                    mnemonic in ("RET", "IRET")
                )
                if not is_unconditional:
                    src_label = find_containing_function(addr)
                    dst_label = ADDR_TO_LABEL[next_addr]
                    if src_label and dst_label and src_label != dst_label:
                        edges.add((src_label, dst_label, False))

    # Add edges for indirect dispatch (JP @RR4 at $0017, JP @RR12 at $05BA)
    # These can't be detected from the disassembly — they use runtime register values.

    # Micro-op chains: collapse individual micro-ops into chain group nodes.
    # Each chain is defined by its entry point (reg[$1E] value from parameter table).
    # The micro-ops within a chain set R5 to chain to each other.
    micro_chains = {
        "chain_20": ("0x20 multi-step chain\\n(sub 0-5 Mode A)",
                     {"micro_acc_carry", "micro_add_indirect", "micro_lookup_fwd",
                      "micro_lookup_bwd", "micro_lookup_overflow", "micro_double_add",
                      "micro_acc_advance", "micro_wrap_acc", "micro_waveform_lookup"}),
        "chain_82": ("0x82 interpolating DDS\\n(sub 6 Mode A)",
                     {"micro_interp_dds_step1", "micro_interp_dds_step2",
                      "micro_interp_dds_step3", "micro_interp_dds_step4"}),
        "chain_AC": ("0xAC two-step chain\\n(sub 7 Mode A)",
                     {"micro_chain_a_step1", "micro_chain_a_step2"}),
        "chain_C4": ("0xC4 DDS waveform\\n(sub 8-13 Mode A)", {"micro_dds_waveform"}),
        "chain_CF": ("0xCF DDS variant\\n(Mode B)",
                     {"micro_dds_cf", "micro_dds_cf_skip", "micro_dds_cf_skip2"}),
        "chain_F2": ("0xF2 DDS wrap\\n(Mode C)", {"micro_dds_f2"}),
        "chain_19": ("0x19 init only\\n(sub 14-15)", {"micro_silence"}),
    }

    # Replace micro-op edges with chain group edges
    micro_to_chain = {}
    for chain_id, (chain_label, members) in micro_chains.items():
        for m in members:
            micro_to_chain[m] = chain_id

    # Rewrite edges: replace micro-op node names with their chain group
    new_edges = set()
    for src, dst, is_call in edges:
        new_src = micro_to_chain.get(src, src)
        new_dst = micro_to_chain.get(dst, dst)
        if new_src != new_dst:  # skip self-loops within same chain
            new_edges.add((new_src, new_dst, is_call))
    edges = new_edges

    # IRQ4 dispatches to chain groups (not individual micro-ops)
    for chain_id in micro_chains:
        edges.add(("irq4_handler", chain_id, False))

    # Interpreter dispatches to all outer functions via JP @RR12 (from dispatch table)
    table_a_map, table_b_map = build_opcode_map(rom)
    for tbl in (table_a_map, table_b_map):
        for addr in tbl:
            if addr < len(rom) - 1:
                target = (rom[addr] << 8) | rom[addr + 1]
                dst = find_containing_function(target)
                if dst:
                    edges.add(("micro_program_interpreter", dst, False))
    # Micro-op self-chaining (R5 set by each micro-op → next IRQ4 dispatch)
    edges.add(("micro_chain_a_step1", "micro_chain_a_step2", False))
    edges.add(("micro_chain_a_step2", "micro_chain_a_step1", False))

    # Cluster functions by section
    section_map = {}
    for addr, v in LABELS.items():
        if v is None:
            continue
        label, section = v
        if section and label:
            section_map[label] = section

    with open(output_path, "w") as f:
        f.write("digraph firmware {\n")
        f.write("  rankdir=TB;\n")
        f.write("  node [shape=box, fontsize=10, fontname=monospace];\n")
        f.write("  edge [fontsize=8, fontname=monospace];\n")
        f.write("  compound=true;\n\n")

        # Collect all nodes actually used in edges
        all_nodes = set(n for e in edges for n in (e[0], e[1]))

        # Cluster definitions
        clusters = {
            "IRQ Handlers": {"irq3_handler", "irq4_handler", "irq1_handler", "init"},
            "IRQ3 Commands": {"irq3_full_setup", "irq3_first_time", "cmd_dispatch",
                              "update_path", "voice_param_update", "stop_handler"},
            "Synthesis Modes": {"synthesis_mode_a", "synthesis_mode_b", "synthesis_mode_c",
                                "mode_a_sub_gt9", "mode_a_sub9", "mode_a_sub_lt9"},
            "Synthesis Pipeline": {"synthesis_output", "micro_program_interpreter",
                                   "finalize_output", "finalize_multiply_entry",
                                   "pitch_acc_positive", "pitch_acc_negative",
                                   "check_underflow", "dac_output_setup",
                                   "synthesis_loop_entry", "synthesis_loop_reentry",
                                   "synthesis_output_common",
                                   "interpreter_reentry", "f2_special_check",
                                   "finalize_check", "multiply_12x16"},
            "Inner Micro-ops": {n for n in all_nodes if n.startswith("chain_")},
            "Loop Control": {n for n in all_nodes
                            if any(n.startswith(p) for p in ["op_loop_", "op_conditional",
                                                              "op_computed", "loop_", "branch_"])
                            } - {"synthesis_loop_entry", "synthesis_loop_reentry"},
            "Vibrato / Noise": {n for n in all_nodes
                          if any(n.startswith(p) for p in ["op_vibrato", "op_lfsr",
                                                            "op_wavetable", "coeff_"])},
        }

        cluster_colors = {
            "IRQ Handlers": "lightyellow",
            "IRQ3 Commands": "wheat",
            "Synthesis Modes": "lightgreen",
            "Synthesis Pipeline": "lightblue",
            "Inner Micro-ops": "#FFB3B3",
            "Loop Control": "#D0FFD0",
            "Vibrato / Noise": "#FFE0B0",
        }

        clustered_nodes = set()
        for i, (name, members) in enumerate(clusters.items()):
            present = (members & all_nodes) - clustered_nodes  # avoid duplicates
            if not present:
                continue
            color = cluster_colors.get(name, "white")
            f.write(f"  subgraph cluster_{i} {{\n")
            f.write(f'    label="{name}";\n')
            f.write(f'    style=filled; fillcolor="{color}";\n')
            f.write(f"    color=gray60;\n")
            for n in sorted(present):
                f.write(f"    {n};\n")
            f.write("  }\n\n")
            clustered_nodes |= present

        # Classify nodes by role
        irq_nodes = {"irq3_handler", "irq4_handler", "irq1_handler", "init"}
        pipeline_nodes = {"synthesis_output", "micro_program_interpreter",
                          "finalize_output", "finalize_multiply_entry",
                          "pitch_acc_positive", "pitch_acc_negative",
                          "check_underflow", "dac_output_setup",
                          "synthesis_loop_entry", "synthesis_loop_reentry",
                          "interpreter_reentry"}
        mode_nodes = {"synthesis_mode_a", "synthesis_mode_b", "synthesis_mode_c",
                      "mode_a_sub_gt9", "mode_a_sub9", "mode_a_sub_lt9"}
        irq3_nodes = {"irq3_full_setup", "irq3_first_time", "cmd_dispatch",
                       "update_path", "voice_param_update", "stop_handler"}
        micro_op_nodes = {n for n in all_nodes if n.startswith("chain_")}
        # Outer synth dispatch functions (called by interpreter via dispatch table)
        synth_dispatch_nodes = {label for label in ADDR_TO_LABEL.values()
                                if any(label.startswith(p) for p in
                                       ["synth_", "cmp_", "multiply_", "branch_",
                                        "compare_", "regpair_"])}

        # Add chain group node labels
        for chain_id, (chain_label, _) in micro_chains.items():
            if chain_id in all_nodes:
                f.write(f'  {chain_id} [label="{chain_label}"];\n')
        f.write("\n")

        # Style nodes
        for label in sorted(set(n for e in edges for n in (e[0], e[1]))):
            if label in irq_nodes:
                f.write(f'  {label} [style=filled, fillcolor=lightyellow];\n')
            elif label in pipeline_nodes:
                f.write(f'  {label} [style=filled, fillcolor=lightblue];\n')
            elif label in mode_nodes:
                f.write(f'  {label} [style=filled, fillcolor=lightgreen];\n')
            elif label in irq3_nodes:
                f.write(f'  {label} [style=filled, fillcolor=wheat];\n')
            elif label in micro_op_nodes:
                f.write(f'  {label} [style=filled, fillcolor="#FFB3B3"];\n')
            elif label in synth_dispatch_nodes:
                f.write(f'  {label} [style=filled, fillcolor="#E0D0FF"];\n')

        f.write("\n")

        # Emit edges
        for src, dst, is_call in sorted(edges):
            color = "blue" if is_call else "gray40"
            f.write(f'  {src} -> {dst} [color={color}{", style=bold" if is_call else ""}];\n')

        f.write("}\n")

    print(f"Wrote {output_path} ({len(edges)} edges)", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--callgraph":
        rom_path = sys.argv[2]
        out_path = sys.argv[3] if len(sys.argv) > 3 else "callgraph.dot"
        rom = open(rom_path, "rb").read()
        assert len(rom) == ROM_SIZE
        # Build label maps
        ADDR_TO_LABEL.clear()
        ADDR_TO_LABEL.update({addr: label for addr, v in LABELS.items() if v is not None for label, _ in [v] if label})
        # Scan for auto-labels
        data_set = set()
        for start, end, _, _ in DATA_RANGES:
            for a in range(start, end):
                data_set.add(a)
        code_start = None
        for addr in range(ROM_SIZE):
            if addr not in data_set:
                if code_start is None:
                    code_start = addr
            else:
                if code_start is not None:
                    for target in collect_branch_targets(disassemble(rom, code_start, addr)):
                        if target not in ADDR_TO_LABEL:
                            ADDR_TO_LABEL[target] = f"L_{target:04X}"
                    code_start = None
        if code_start is not None:
            for target in collect_branch_targets(disassemble(rom, code_start, ROM_SIZE)):
                if target not in ADDR_TO_LABEL:
                    ADDR_TO_LABEL[target] = f"L_{target:04X}"

        export_callgraph(rom, out_path)
    else:
        main()
