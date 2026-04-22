#!/usr/bin/env python3
"""
Generate synthetic voice_capture.bin files for slm2test verification.

Creates capture files that exercise specific firmware paths with known inputs.
Each test plays a note, waits, then stops — producing a WAV that can be
compared against expected behavior.

Usage:
    python3 generate_test_capture.py --list
    python3 generate_test_capture.py --test silence -o test_silence.bin
    python3 generate_test_capture.py --test all -o test_all.bin

Replay:
    WERSI_CAPTURE=test_silence.bin \
      ./mamemuse slm2test -seconds_to_run 5 -wavwrite test_silence.wav

Full tracing:
    WERSI_CAPTURE=test_all.bin WERSI_DAC_TRACE=dac.bin \
      ./mamemuse slm2test -seconds_to_run 60 -wavwrite test_all.wav
"""
import argparse
import math
import struct

RECORD_SIZE = 16 + 256  # header + slave RAM

# ============================================================
# Waveform generators
# ============================================================

def gen_sine(length):
    """Generate a single-cycle sine waveform (unsigned 8-bit, center=$80)."""
    return bytes([int(128 + 127 * math.sin(2 * math.pi * i / length)) for i in range(length)])

def gen_sawtooth(length):
    """Generate a single-cycle sawtooth waveform (unsigned 8-bit, $01..$FF).
    Sawtooth is easier to identify visually in DAC traces — the linear ramp
    and sharp reset make phase/interpolation issues immediately obvious."""
    return bytes([int(1 + 254 * i / (length - 1)) for i in range(length)])

WAVE_BASS   = gen_sawtooth(64)
WAVE_TENOR  = gen_sawtooth(64)
WAVE_ALT    = gen_sawtooth(32)
WAVE_SOPRAN = gen_sawtooth(16)

# ============================================================
# Micro-program (FREQ block) presets
# ============================================================

def make_freq_block(opcodes, start_offset=2):
    """Build a 32-byte FREQ block.
    FREQ[0] = start_offset (micro-program counter init).
    FREQ[1] = 0 (initial reg[$1E], overwritten by synthesis_output).
    FREQ[2..] = opcodes + zero padding."""
    block = bytearray(32)
    block[0] = start_offset
    block[1] = 0x00
    for i, b in enumerate(opcodes):
        block[2 + i] = b
    return bytes(block)

# Opcode $01 = LOAD_R8R9 (3 bytes: opcode, high, low)
# Sets R8:R9 = immediate value, then interpreter continues to finalize
FREQ_SIMPLE_GAIN = make_freq_block([0x01, 0x40, 0x00])  # R8:R9 = $4000 (half gain)
FREQ_FULL_GAIN   = make_freq_block([0x01, 0x7F, 0xFF])  # R8:R9 = $7FFF (max gain)
FREQ_ZERO         = make_freq_block([])                   # empty → immediate finalize

# Opcode $10 = LFSR noise (4 bytes: opcode, running_state, coefficient, counter/lfsr_low)
# 16-bit Galois LFSR (polynomial $1D87) for random pitch modulation.
# counter<0: full 16-bit step + envelope S&H on every tick
# coefficient: 8-bit multiply strength ($FF=max noise, $40=mild)
# Exits to finalize_output.multiply → ACC gets noise-modulated signal.
FREQ_LFSR_STRONG  = make_freq_block([0x00, 0x00, 0x10, 0x00, 0xFF, 0x80])  # REPEAT NOP + LFSR(state=0, coeff=$FF, counter=$80)
FREQ_LFSR_MILD    = make_freq_block([0x00, 0x00, 0x10, 0x00, 0x40, 0x80])  # REPEAT NOP + LFSR(state=0, coeff=$40, counter=$80)
FREQ_LFSR_SLOW    = make_freq_block([0x00, 0x00, 0x10, 0x00, 0xFF, 0x04])  # REPEAT NOP + LFSR(state=0, coeff=$FF, counter=$04)

# Caves-style: LOAD base + COND_CTR ramp-in + LFSR
# Structure: NOP×3, LOAD_R8R9(base), NOP×2, COND_CTR(16), LFSR
# The LOAD sets ACC to a base value BEFORE LFSR starts. The COND_CTR
# repeats finalize 16 times to establish stable pitch, then LFSR loops.
# LFSR noise range is ±2048 around the base.
# Base must be $4000-$D800 to keep ACC above normalize threshold ($2C00).
def make_caves_freq(base_hi=0x80):
    """Build a caves-style FREQ block: LOAD(base) + ramp-in + LFSR."""
    block = bytearray(32)
    block[0] = 0x02   # start_offset
    block[1] = 0xFF   # initial reg[$1E]
    # Module 0 (offset 2-7): NOP
    # Module 1 (offset 8-13): LOAD_R8R9
    block[8] = 0x01   # LOAD_R8R9 opcode (type $1, param nibble 0 → R9=$00)
    block[9] = base_hi # R8 = base high byte
    block[10] = 0x00
    # Module 2 (offset 14-19): NOP padding + LFSR bytecodes
    # offset 14-15: $00 $10 = COND_CTR(16) ramp-in
    block[14] = 0x00
    block[15] = 0x10   # param=16 repeats
    # offset 16-19: $10 $00 $FF $80 = LFSR (reached after COND_CTR)
    block[16] = 0x10   # LFSR opcode
    block[17] = 0x00   # state
    block[18] = 0xFF   # coeff (max)
    block[19] = 0x80   # counter (-128 = always full step)
    return bytes(block)

FREQ_CAVES_FIXED  = make_caves_freq(0x80)  # ACC=$8000 (safe center)
FREQ_CAVES_ORIG   = make_caves_freq(0xFF)  # ACC=$FF00 (original, wraps!)
FREQ_OCEAN_FIXED  = make_caves_freq(0x80)  # Same as caves_fixed (ocean+LOAD)

# Opcode $A0 = synth_wavetable_acc (2 bytes) — standard frequency envelope
# Reads reg[$14:$15] as coefficient, multiplies into R8:R9.
# This is the standard path for pitched voices (non-LFSR).
FREQ_WAVETABLE    = make_freq_block([0x00, 0x00, 0xA0, 0x00])  # REPEAT NOP + wavetable_acc

# ============================================================
# Slave RAM builder
# ============================================================

# Mode register (sram[$FA] = reg[$10]):
#   bits 3:0 = sub-mode (0-15)
#   bit 4    = Mode B (when bit 5 clear)
#   bit 5    = Mode A (overrides bit 4)
#   bit 6    = coeff multiply enable
#   bit 7    = pitch envelope override
MODE_C       = 0x10   # Mode C (wrap DDS $F2), bits 5:4 = $10
MODE_B       = 0x00   # Mode B (duty DDS $CF), bits 5:4 = $00
MODE_A       = 0x20   # Mode A (oversampled + direct DDS), bits 5:4 = $20
MODE_A_NOPITCH = 0xA0 # Mode A + bit 7 (disable pitch wheel/envelope modulation)

def make_slave_ram(cmd=0x01, mode=0x28, fc=0x05, fd=0x99,
                   f4=0x1F, f5=0x80, f6=0x01, f7=0x00,
                   f9=0x43, fb=0x00, fe=0x00, ff=0x80,
                   freq_block=None, waveform=None):
    """Build a 256-byte slave RAM image.

    Key addresses:
      $F4 = LDEI byte count indicator (for micro-program copy)
      $F5 = micro-program source offset in slave RAM
      $F6 = waveform source offset in slave RAM
      $F7 = R14 init (freq step for Default/Mode B, resampler seed for Mode A)
      $F8 = command byte (bit 0=setup, bit 1=stop)
      $F9 = TMR value (written to Z8 TMR register via voice_param_update)
            $43 = T0 continuous + T_OUT enable (normal operation)
            Loaded by LDEI: sram[$F9]→TMR, sram[$FA]→T1, sram[$FB]→PRE1
      $FA = mode register (sub-mode + mode select)
      $FB = LFSR seed → reg[$11] (also PRE1 via voice_param_update)
      $FC:$FD = frequency parameters → reg[$12]:reg[$13]
      $FE:$FF = pitch envelope params → reg[$14]:reg[$15]
    """
    data = bytearray(256)

    # Place waveform data starting at sram[f6]
    wav = waveform or WAVE_BASS
    for i, b in enumerate(wav):
        if f6 + i < 0xF4:  # don't clobber parameter area
            data[f6 + i] = b

    # Place micro-program (FREQ block) starting at sram[f5]
    freq = freq_block or FREQ_SIMPLE_GAIN
    for i, b in enumerate(freq[:32]):
        addr = (f5 + i) & 0xFF
        if addr < 0xF4:  # don't clobber parameter area
            data[addr] = b

    # Parameter area ($F4-$FF)
    data[0xF4] = f4    # LDEI byte count (0x1F = 31 → copies 32 bytes)
    data[0xF5] = f5    # micro-program source offset
    data[0xF6] = f6    # waveform source offset
    data[0xF7] = f7    # R14 init / freq step
    data[0xF8] = cmd   # command (0x01=setup, 0x02=stop, 0x00=update)
    data[0xF9] = f9    # routing ($40=T_OUT enable)
    data[0xFA] = mode  # mode register
    data[0xFB] = fb    # reg[$11] LFSR seed
    data[0xFC] = fc    # freq high → reg[$12]
    data[0xFD] = fd    # freq low → reg[$13]
    data[0xFE] = fe    # pitch envelope → reg[$14]
    data[0xFF] = ff    # pitch envelope → reg[$15]

    return data

# ============================================================
# Capture record format
# ============================================================

def make_record(sram, slot=0, bank=1, exsla0=0, exsla1=0, cycles=0):
    """Create a 272-byte capture record (16-byte header + 256 sram)."""
    hdr = bytearray(16)
    hdr[0] = ord('R')
    hdr[1] = ord('A')
    hdr[2] = slot
    hdr[3] = bank
    hdr[4] = exsla0
    hdr[5] = exsla1
    hdr[6] = sram[0xF8]  # cmd
    hdr[7] = sram[0xFA]  # mode
    struct.pack_into('<Q', hdr, 8, cycles)
    return bytes(hdr) + bytes(sram)


def make_stop(slot=0, bank=0, cycles=0):
    """Create a STOP record."""
    data = make_slave_ram(cmd=0x02, mode=0x00)
    return make_record(data, slot=slot, bank=bank, cycles=cycles)


# ============================================================
# Test case definitions
# ============================================================

# Timing constants (master 68B09 @ 2 MHz, 1 cycle = 500ns)
NOTE_DURATION = 1_000_000  # 500ms note
GAP_DURATION  = 200_000    # 100ms silence between notes


def test_case(name, description, mode, sub, fc, fd, exsla=(0, 0),
              waveform=None, freq_block=None, f7=0x00, fb=0x00, fe=0x00, ff=0x80,
              f6=0x01, f4=0x1F, f5=0x80, f9=None):
    """Define a single test case. Returns (name, desc, records_fn).
    f9: if set, used as sram[$F9] for SETUP (→ R15). PARAM_UPDATE always uses $43 (TMR config)."""
    mode_val = mode | (sub & 0x0F)
    f9_setup = f9 if f9 is not None else 0x43

    def gen(cycle_start):
        cycle = cycle_start
        records = []

        # PARAM_UPDATE first — writes sram[$F9] to TMR (enables T_OUT for DAC latch)
        # Must come before SETUP: voice_param_update sets TMR then spins,
        # subsequent SETUP RAUD starts synthesis with T_OUT already configured.
        sram_tmr = make_slave_ram(
            cmd=0x04, mode=mode_val, fc=fc, fd=fd, fe=fe, ff=ff,
            f4=f4, f5=f5, f6=f6, f7=f7, f9=0x43, fb=fb,
            freq_block=freq_block or FREQ_SIMPLE_GAIN,
            waveform=waveform,
        )
        records.append(make_record(sram_tmr, bank=1,
                                   exsla0=exsla[0], exsla1=exsla[1],
                                   cycles=cycle))
        cycle += 100_000  # 50ms for TMR to take effect

        # SETUP — starts synthesis loop (T_OUT already enabled by PARAM_UPDATE)
        # f9 goes to reg[$0F] = R15 (wrap point for Mode C, sample count for Mode B)
        sram = make_slave_ram(
            cmd=0x01, mode=mode_val, fc=fc, fd=fd, fe=fe, ff=ff,
            f4=f4, f5=f5, f6=f6, f7=f7, f9=f9_setup, fb=fb,
            freq_block=freq_block or FREQ_SIMPLE_GAIN,
            waveform=waveform,
        )
        records.append(make_record(sram, bank=1,
                                   exsla0=exsla[0], exsla1=exsla[1],
                                   cycles=cycle))
        cycle += NOTE_DURATION

        # STOP
        records.append(make_stop(cycles=cycle))
        cycle += GAP_DURATION

        return records, cycle

    return (name, description, gen)


# --- The test suite ---

TESTS = [
    # 1. Silence — verifies init micro-op ($19)
    test_case("silence", "Mode A sub=14 → $19 init (should output $80)",
              MODE_A, 14, fc=0x05, fd=0x99),

    # 2. C4 DDS — simplest audio path
    test_case("c4_dds", "Mode A sub=8 → $C4 DDS, 64-sample sine, mid pitch",
              MODE_A, 8, fc=0x05, fd=0x99, exsla=(1, 1),
              waveform=WAVE_BASS, f7=0x04),

    # 3. C4 DDS high pitch
    test_case("c4_dds_high", "Mode A sub=10 → $C4 DDS, soprano range",
              MODE_A, 10, fc=0x04, fd=0x00, exsla=(1, 1),
              waveform=WAVE_SOPRAN, f7=0x08),

    # 4. C4 DDS low pitch
    test_case("c4_dds_low", "Mode A sub=8 → $C4 DDS, bass range",
              MODE_A, 8, fc=0x08, fd=0x00, exsla=(0, 0),
              waveform=WAVE_BASS, f7=0x01),

    # 5. AC 2-step chain — Alt range
    test_case("ac_chain", "Mode A sub=7 → $AC 2-step chain",
              MODE_A, 7, fc=0x06, fd=0x00, exsla=(0, 1),
              waveform=WAVE_TENOR),

    # 6. 82 4-step interpolating DDS — Tenor range
    test_case("interp_82", "Mode A sub=6 → $82 4-step interpolating DDS",
              MODE_A, 6, fc=0x07, fd=0x00, exsla=(1, 0),
              waveform=WAVE_TENOR),

    # 7. 20 8-step chain — Bass range
    test_case("chain_20", "Mode A sub=0 → $20 8-step accumulation chain",
              MODE_A, 0, fc=0x0A, fd=0x00, exsla=(0, 0),
              waveform=WAVE_BASS),

    # 8. Mode C wrap DDS — sub=3, R15=37, typical sampling mode mid-range
    test_case("mode_c_mid", "Mode C sub=3, R15=37, sawtooth wrap DDS",
              MODE_C, 3, fc=0x06, fd=0xA9, exsla=(1, 1),
              waveform=WAVE_BASS, f7=0x10, f9=0x25,
              freq_block=make_freq_block([], start_offset=0x2A)),

    # 9. Mode B duty DDS — sub=3, R15=21, typical formant mode
    test_case("mode_b_mid", "Mode B sub=3, R15=21, sawtooth duty DDS",
              MODE_B, 3, fc=0x06, fd=0xA9, exsla=(1, 1),
              waveform=WAVE_BASS, f7=0x10, f9=0x15,
              freq_block=make_freq_block([], start_offset=0x2A)),

    # 10. Alt resampler (sub=9, 32→64 via D7 writes)
    test_case("resample_alt", "Mode A sub=9 → Alt resampler (32→64) + $C4 DDS",
              MODE_A, 9, fc=0x05, fd=0x00, exsla=(1, 1),
              waveform=WAVE_ALT, f6=0x01, f7=0x04),

    # 11. Sopran resampler (sub=10, 16→64 via D7 writes)
    test_case("resample_sopran", "Mode A sub=10 → Sopran resampler (16→64) + $C4 DDS",
              MODE_A, 10, fc=0x04, fd=0x50, exsla=(1, 1),
              waveform=WAVE_SOPRAN, f6=0x01, f7=0x08),

    # 12. Sub<9 LDEI bulk copy (64 bytes direct)
    test_case("ldei_copy", "Mode A sub=5 → LDEI 64-byte waveform copy + $20 chain",
              MODE_A, 5, fc=0x09, fd=0x00, exsla=(0, 0),
              waveform=WAVE_BASS),

    # 13. Full gain test — max amplitude
    test_case("full_gain", "Mode A sub=8 → $C4 DDS with max gain FREQ",
              MODE_A, 8, fc=0x05, fd=0x99, exsla=(1, 1),
              waveform=WAVE_BASS, f7=0x04,
              freq_block=FREQ_FULL_GAIN),

    # 14. Zero gain test — should be silent despite waveform
    test_case("zero_gain", "Mode A sub=8 → $C4 DDS with empty FREQ (zero gain)",
              MODE_A, 8, fc=0x05, fd=0x99, exsla=(1, 1),
              waveform=WAVE_BASS, f7=0x04,
              freq_block=FREQ_ZERO),

    # 15. Mode B sub=15 — tests the $99→$19 fix
    test_case("mode_b_sub15", "Mode B sub=15 → should be $19 init (silence after fix)",
              MODE_B, 15, fc=0x05, fd=0x99),

    # --- LFSR noise modulation tests ---

    # 16. LFSR noise — ocean-like: Mode A sub=7, strong LFSR, no pitch modulation
    test_case("lfsr_ocean", "LFSR strong noise, Mode A sub=7, no pitch modulation (ocean-like)",
              MODE_A_NOPITCH, 7, fc=0x08, fd=0x64, exsla=(1, 1),
              waveform=WAVE_BASS, fb=0xDB,
              freq_block=FREQ_LFSR_STRONG),

    # 17. LFSR noise — Mode A sub=8 (direct DDS + LFSR)
    test_case("lfsr_dds", "LFSR strong noise, Mode A sub=8 (1x DDS + LFSR)",
              MODE_A_NOPITCH, 8, fc=0x08, fd=0x64, exsla=(1, 1),
              waveform=WAVE_BASS, fb=0xDB,
              freq_block=FREQ_LFSR_STRONG),

    # 18. LFSR mild noise — Mode A sub=7
    test_case("lfsr_mild", "LFSR mild noise (coeff=$40), Mode A sub=7",
              MODE_A_NOPITCH, 7, fc=0x08, fd=0x64, exsla=(1, 1),
              waveform=WAVE_BASS, fb=0xDB,
              freq_block=FREQ_LFSR_MILD),

    # 19. LFSR slow update — Mode A sub=7 (counter=$04, fewer LFSR ticks)
    test_case("lfsr_slow", "LFSR slow update (counter=4), Mode A sub=7",
              MODE_A_NOPITCH, 7, fc=0x08, fd=0x64, exsla=(1, 1),
              waveform=WAVE_BASS, fb=0xDB,
              freq_block=FREQ_LFSR_SLOW),

    # 20. LFSR noise — caves-like (original: ACC base=$FF00, wraps through zero)
    # test_case("lfsr_caves", "LFSR caves-style ACC=$FF00 base (WRAPS — original broken)",
    #           MODE_A_NOPITCH, 10, fc=0x05, fd=0x99, exsla=(1, 1),
    #           waveform=WAVE_BASS, fb=0xC9,
    #           freq_block=FREQ_CAVES_ORIG),

    # 21. LFSR caves FIXED — ACC base=$8000 (safe center, no wrapping)
    test_case("lfsr_caves", "LFSR caves-style ACC=$8000 base (safe — no wrapping)",
              MODE_A_NOPITCH, 10, fc=0x05, fd=0x99, exsla=(1, 1),
              waveform=WAVE_BASS, fb=0xC9,
              freq_block=FREQ_CAVES_FIXED),

    # 22. LFSR ocean FIXED — add LOAD($8000) base that ocean is missing
    test_case("lfsr_ocean_fixed", "LFSR ocean-style + LOAD ACC=$8000 base (fix missing base)",
              MODE_A_NOPITCH, 7, fc=0x08, fd=0x64, exsla=(1, 1),
              waveform=WAVE_BASS, fb=0xDB,
              freq_block=FREQ_OCEAN_FIXED),

    # 23. LFSR with pitch tracking (no pitch modulation disabled)
    test_case("lfsr_pitched", "LFSR noise with pitch envelope tracking",
              MODE_A, 7, fc=0x08, fd=0x64, exsla=(1, 1),
              waveform=WAVE_BASS, fb=0xDB, fe=0x40, ff=0x80,
              freq_block=FREQ_LFSR_STRONG),

    # --- EXSLA sweep ---
    # 22-25. EXSLA sweep — same note, all 4 EXSLA settings, FIXME exsla works together with port1 (store in PITCH_ENV,PITCH_HI)
    test_case("exsla_00", "EXSLA=00 (÷1, bass bank)",
              MODE_A, 8, fc=0x08, fd=0x00, exsla=(0, 0),
              waveform=WAVE_BASS, f7=0x02),
    test_case("exsla_01", "EXSLA=01 (÷2)",
              MODE_A, 8, fc=0x06, fd=0x00, exsla=(1, 0),
              waveform=WAVE_BASS, f7=0x04),
    test_case("exsla_10", "EXSLA=10 (÷4)",
              MODE_A, 8, fc=0x05, fd=0x00, exsla=(0, 1),
              waveform=WAVE_BASS, f7=0x06),
    test_case("exsla_11", "EXSLA=11 (÷8, soprano bank)",
              MODE_A, 8, fc=0x04, fd=0x50, exsla=(1, 1),
              waveform=WAVE_BASS, f7=0x08),

    # --- Mode C (programmable-wrap DDS $F2) ---
    # R15 = wrap point, R14 = 1 (from param table T byte).
    # Frequency = f_timer / R15. Setup code at $0412 computes wrap-boundary
    # value and fills reg[$40-$7E] with delta-adjusted waveform from slave RAM.
    # f7 = formant value (DC offset subtracted from samples, also division input).

    # Mode C sub=5 (PRE0=$05, T=$01): high IRQ4 rate, R15=37 (typical octave 0)
    test_case("mode_c_wrap37", "Mode C sub=5, R15=37 (wrap at 37), sawtooth",
              MODE_C, 5, fc=0x06, fd=0xA9, exsla=(1, 1),
              waveform=WAVE_BASS, f7=0x10, f9=0x25,
              freq_block=make_freq_block([], start_offset=0x2A)),  # library $2A = ACC=$8000

    # Mode C sub=8 (PRE0=$05, T=$08): slower timer, R15=40
    test_case("mode_c_wrap40", "Mode C sub=8, R15=40 (wrap at 40), sawtooth",
              MODE_C, 8, fc=0x06, fd=0xA9, exsla=(1, 1),
              waveform=WAVE_BASS, f7=0x10, f9=0x28,
              freq_block=make_freq_block([], start_offset=0x2A)),

    # Mode C sub=5, R15=37, no formant (f7=0) — direct copy path
    test_case("mode_c_noformant", "Mode C sub=5, R15=37, f7=0 (no formant delta)",
              MODE_C, 5, fc=0x06, fd=0xA9, exsla=(1, 1),
              waveform=WAVE_BASS, f7=0x00, f9=0x25,
              freq_block=make_freq_block([], start_offset=0x2A)),

    # Mode C sub=5, R15=5 (very short wrap — high frequency buzz)
    test_case("mode_c_wrap5", "Mode C sub=5, R15=5 (short wrap, high buzz)",
              MODE_C, 5, fc=0x06, fd=0xA9, exsla=(1, 1),
              waveform=WAVE_BASS, f7=0x10, f9=0x05,
              freq_block=make_freq_block([], start_offset=0x2A)),

    # --- Mode B (variable-duty DDS $CF/$DA/$E9) ---
    # R15 = skip entry offset (controls duty cycle).
    # R14 = freq step. Setup loads R15 samples with optional DC offset removal.

    # Mode B sub=5 (PRE0=$05, T=$01), R15=21 (typical formant mode)
    test_case("mode_b_duty21", "Mode B sub=5, R15=21, sawtooth, f7=0x10",
              MODE_B, 5, fc=0x06, fd=0xA9, exsla=(1, 1),
              waveform=WAVE_BASS, f7=0x10, f9=0x15,
              freq_block=make_freq_block([], start_offset=0x2A)),

    # Mode B sub=5, R15=21, no formant (f7=0) — LDEI bulk copy
    test_case("mode_b_noformant", "Mode B sub=5, R15=21, f7=0 (LDEI direct copy)",
              MODE_B, 5, fc=0x06, fd=0xA9, exsla=(1, 1),
              waveform=WAVE_BASS, f7=0x00, f9=0x15,
              freq_block=make_freq_block([], start_offset=0x2A)),

    # Mode B sub=8, R15=5 (short waveform, high pitch formant range)
    test_case("mode_b_duty5", "Mode B sub=8, R15=5 (short waveform), sawtooth",
              MODE_B, 8, fc=0x06, fd=0xA9, exsla=(1, 1),
              waveform=WAVE_BASS, f7=0x10, f9=0x05,
              freq_block=make_freq_block([], start_offset=0x2A)),
]

# ============================================================
# Capture file generation
# ============================================================

def generate_capture(test_names, output_path):
    """Generate a capture file with the specified tests."""
    records = []
    cycle = 0

    tests_run = []
    for name, desc, gen_fn in TESTS:
        if test_names != ["all"] and name not in test_names:
            continue
        new_records, cycle = gen_fn(cycle)
        records.extend(new_records)
        tests_run.append((name, desc))
        print(f"  [{name:20s}] {desc}")

    if not tests_run:
        print("No matching tests found.")
        return

    with open(output_path, 'wb') as f:
        for rec in records:
            f.write(rec)

    duration_s = cycle / 2_000_000  # master 68B09 @ 2 MHz
    print(f"\nWrote {len(records)} records ({len(tests_run)} tests) to {output_path}")
    print(f"Estimated duration: ~{duration_s:.1f}s")
    print(f"Replay: WERSI_CAPTURE={output_path} ./mamemuse slm2test "
          f"-seconds_to_run {int(duration_s) + 2} -wavwrite output.wav")


def main():
    parser = argparse.ArgumentParser(
        description='Generate synthetic test captures for slm2test')
    parser.add_argument('--test', nargs='+', default=None,
                        help='Test name(s) to generate, or "all"')
    parser.add_argument('--list', action='store_true',
                        help='List available tests')
    parser.add_argument('-o', '--output', default='test_capture.bin',
                        help='Output capture file')

    args = parser.parse_args()

    if args.list:
        print("Available tests:")
        print(f"  {'Name':20s}  Description")
        print(f"  {'-'*20}  {'-'*50}")
        for name, desc, _ in TESTS:
            print(f"  {name:20s}  {desc}")
        print(f"\nUse --test all to run all {len(TESTS)} tests")
        return

    if args.test is None:
        parser.print_help()
        return

    print(f"Generating: {', '.join(args.test)}\n")
    generate_capture(args.test, args.output)


if __name__ == '__main__':
    main()
