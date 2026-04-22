# Wersi SLM-2 Z8611 Firmware Reference

Chip: Zilog Z8611 (Z8 family, 4KB internal mask ROM), Wersi part SR0106.
ROM extracted from die photo via maskromtool/gatorom (`--decode-z86x1 -r 0`).

## Table of Contents

1. [Overview](#1-overview)
   - ROM Memory Map
2. [Hardware Interface](#2-hardware-interface)
   - 2.1 Port Configuration
   - 2.2 Pin Connections
   - 2.3 Slave RAM Layout
   - 2.4 Command Protocol
   - 2.5 Master Command Queue
3. [Register Architecture](#3-register-architecture)
   - 3.1 IRQ4 Registers (micro-op chain, audio rate)
   - 3.2 IRQ3 Registers (envelope loop, per ECLK)
   - 3.3 Shared / Configuration Registers
   - 3.4 Special Function Registers
   - 3.5 Two-Level Synthesis Pipeline
   - 3.6 Mode A Waveform Table + R14 Initialization
   - 3.7 Implementation Tricks
4. [Interrupt System](#4-interrupt-system)
   - 4.1 Boot Sequence
   - 4.2 IRQ3 — RAUD / Command Dispatch
   - 4.3 IRQ1 — Timer Setup
   - 4.4 IRQ4 — Phase Accumulator + Micro-Op Dispatch
   - 4.5 Signal Flow Summary
5. [Micro-Program VM](#5-micro-program-vm)
   - 5.1 Opcode Encoding
   - 5.2 Instruction Set (76 instructions)
   - 5.3 Execution Model
   - 5.4 Constraints
6. [Synthesis Modes](#6-synthesis-modes)
   - 6.1 Mode A ($050E) — Oversampled wavetable + direct DDS
   - 6.2 Mode B ($04DD) — Variable-duty DDS ($CF)
   - 6.3 Mode C ($0412) — Programmable-wrap DDS ($F2)
   - 6.4 Synthesis Output Parameter Table ($0101-$0190)
   - 6.5 Coefficient Multiply — Pitch Envelope Scaling
7. [Arithmetic Routines](#7-arithmetic-routines)
   - 7.1 16-bit Restoring Division
   - 7.2 Multiply Variants
   - 7.3 Pitch Envelope Processing
8. [ROM Tables](#8-rom-tables)
   - 8.1 Dispatch Tables
   - 8.2 Micro-Program Data ($0101-$018D)
   - 8.3 Quarter-Wave Sine Table ($0FC0-$0FFF)
   - 8.4 Pitch Fine Table (master side)
9. [ROM Bit Errors](#9-rom-bit-errors)
10. [Files](#10-files)

---

## 1. Overview

- **ROM**: 4096 bytes at $0000-$0FFF
- **Clock**: 12 MHz crystal
- **Synthesis**: Programmable DSP bytecode VM — single-cycle wavetable playback with pitch modulation (coefficient scaling, LFSR noise)
- **DAC**: 8-bit via Port 0 → DAC 0832, sample latch via T_OUT (pin P3.6)
- **Amplitude envelope**: in hardware (co-processor → DAC reference voltage)
- **Pitch envelope**: in firmware via reg[$1A], updated from co-processor

Architecture: A two-level interpreter.

**OUTER level** — micro-program interpreter at $058E. Runs synchronously per
RAUD trigger. Decodes a 32-byte voice program (loaded from slave RAM into
registers $1D-$3C) through a 76-instruction dispatch table. Produces a
scaled waveform sample in R8:R9.

**INNER level** — IRQ4 micro-op chain. Runs at pitch rate (timer T0 overflow).
Each tick executes one DDS micro-op ($0019-$00F2): phase accumulation via
RL SPH + ADC R6,R4, then dac ouput by current micro-op and calc next sample phase.

### ROM Memory Map

| Address Range | Size | Contents                                              |
|---------------|------|-------------------------------------------------------|
| $0000-$000B   | 12   | Interrupt vector table (6 × 2 bytes)                  |
| $000C-$000E   | 3    | Reset entry: `JP $023A`                               |
| $000F-$0017   | 9    | IRQ4 handler                                          |
| $0019-$0100   | 232  | Micro-op primitives (~20 IRET-terminated blocks)      |
| $0101-$018D   | 141  | Synthesis output parameter table (per sub-mode)       |
| $0192-$0232   | 161  | Dispatch tables (type B: $0192, type A: $01B2)        |
| $023A-$025D   | 36   | Reset/init code                                       |
| $025F-$02DF   | 129  | LDEI chain (64× `LDEI @R12, @RR10` + RET)             |
| $02E0-$042C   | 333  | IRQ3 handler + parameter load paths                   |
| $042D-$04B9   | 141  | 16-bit restoring division                             |
| $04BA-$057A   | 193  | Synthesis mode entry (default, A, B)                  |
| $057B-$05BB   | 65   | Synthesis loop + micro-program interpreter            |
| $05BC-$0CA4   | 1769 | Dispatch functions (76 synthesis micro-instructions)  |
| $0CA5-$0D62   | 190  | Finalize output + 12×16 multiply                      |
| $0D63-$0E29   | 199  | Pitch envelope processing + DAC output setup          |
| $0E2A-$0E61   | 56   | Synthesis loop re-entry (IRQ1 wait + envelope update) |
| $0E62-$0EBD   | 92   | Synthesis output stage (EI at $0E96, PRE0/R6 load)    |
| $0EBE-$0ECE   | 17   | IRQ1 handler                                          |
| $0ECF-$0FAB   | 221  | Envelope overflow paths + scaling                     |
| $0FC0-$0FFF   | 64   | Quarter-wave sine table                               |


## 2. Hardware Interface

### 2.1 Port Configuration

| Register   | Init | Runtime | Meaning                                                    |
|------------|------|---------|------------------------------------------------------------|
| P3M ($F7)  | $01  | $01     | P3.0 = external interrupt input (RAUD → IRQ3)              |
| P2M ($F6)  | $0C  | $00/$04 | Port 2: mixed I/O, mode switches for bus control           |
| P01M ($F8) | $1C  | $14/$0C | P0=DAC output, P1=ext memory; switches for bus access      |
| IPR ($F9)  | $27  | $27     | Priority: IRQ3 > IRQ4 > IRQ1 > IRQ0 > IRQ2 > IRQ5          |
| IMR ($FB)  | $88  | dynamic | IRQ3 + global enable; IRQ1/IRQ4 enabled by synthesis chain |
| SPL ($FF)  | $40  | $40     | Stack in register file (internal stack mode)               |

### 2.2 Pin Connections

| Pin | Port | Direction | Signal | Function |
|-----|------|-----------|--------|----------|
| 5 | P3.0 | Input | RAUD | Triggers IRQ3 — RAM Ausgabe Daten ("parameters ready, go read") |
| 8 | DS | — | DS | Data Strobe (dedicated pin, active low) |
| 9 | AS | — | AS | Address Strobe (dedicated pin, active low) |
| 10 | P3.5 | — | NC | Unconnected (verified on PCB) |
| 12 | P3.2 | Input | EXSLA1 | Pitch exponent bit 1 (from SLRAMB latch) |
| 13-20 | P0.0-P0.7 | Output | DAC | 8-bit waveform to DAC 0832 |
| 21-28 | P1.0-P1.7 | Bidir | Bus | Slave data bus (address/data mux via IC14) |
| 29 | P3.4 | — | NC | Unconnected (verified on PCB) |
| 30 | P3.3 | Input | ECLK | **IRQ1 input** — envelope clock from co-processor (per-voice, 200 Hz). Triggers IRQ1 pending in hardware, synchronizing synthesis output with envelope S&H settling. |
| 31-32 | P2.0-P2.1 | Output | Bus ctl | P2.0=DS/AS enable (IC7 OE), P2.1=unused |
| 33 | P2.2 | In/Out | RARC | Bus arbitration: input=read master state, output=signal done |
| 34 | P2.3 | Output | Bright | 80 Hz low-pass filter select |
| 35-38 | P2.4-P2.7 | Output | MUX | Audio routing: left, right, effects, WersiVoice |
| 39 | P3.1 | Input | EXSLA0 | Pitch exponent bit 0 (from SLRAMB latch). Also T_IN alternate function. |
| 40 | P3.6 | Output | DAC ILE | T_OUT → DAC 0832 Input Latch Enable (verified on PCB) |

### 2.3 Slave RAM Layout

Z8 addresses slave RAM via R10:R11 (16-bit pointer). Only R11 (low byte)
determines the physical offset — R10 goes to Port 0 as side-effect.

| Offset  | Size | Master Writer         | Z8 Reader                              | Purpose                                                           |
|---------|------|-----------------------|----------------------------------------|-------------------------------------------------------------------|
| $00-$9F | 160  | voice_cmd_0_setup     | Synthesis interpreter                  | Waveform samples (single-cycle wavetable)                         |
| $A0-$CF | 48   | voice_cmd_0_setup     | Synthesis interpreter                  | Voice config / frequency envelope data                            |
| $D0-$F3 | 36   | bulk write            | Not directly read                      | Extended voice data                                               |
| $F4     | 1    | voice_cmd_0_setup     | Pitch calc at $0339                    | Pitch/timer division value (typically $1F)                        |
| $F5-$F6 | 2    | voice_cmd_finish_raud | Param update at $0338                  | Micro-program / wavetable offset pointer                          |
| $F7     | 1    | voice_cmd_finish_raud | Full setup bulk copy                   | Frequency lookup result                                           |
| $F8     | 1    | Multiple commands     | IRQ3 entry at $02F6                    | **Command byte** (bit2=voice_param_update, bit1=stop, bit0=setup) |
| $F9     | 1    | voice_cmd_finish_raud | PARAM_UPDATE→TMR($F1); Setup→reg[$0F]  | TMR value (T_OUT config) / synthesis type                         |
| $FA     | 1    | voice_cmd_finish_raud | PARAM_UPDATE→T1($F2); Setup→reg[$10]   | T1 counter / mode flags                                           |
| $FB     | 1    | voice_cmd_finish_raud | PARAM_UPDATE→PRE1($F3); Setup→reg[$11] | PRE1 prescaler / synthesis parameters                             |
| $FC-$FD | 2    | voice_cmd_finish_raud | Full setup → reg[$12-$13]              | Frequency parameters                                              |
| $FE     | 1    | voice_cmd_finish_raud | Update/Setup → reg[$14]                | Pitch envelope parameter 1                                        |
| $FF     | 1    | voice_select_slave    | Update/Setup → reg[$15]                | Pitch exponent (from pitch_fine_table)                            |

**Note on $F9-$FB dual use**: voice_param_update ($0374) uses LDEI @R12, @RR10
starting at R12=$F1 to copy sram[$F9]→TMR, sram[$FA]→T1, sram[$FB]→PRE1.
During FULL SETUP, the same bytes are read differently: $F9→reg[$0F],
$FA→reg[$10] (mode), $FB→reg[$11] (LFSR seed). The dual mapping is
intentional — different cmd paths read different SFR/register targets.

### 2.4 Command Protocol

The master writes command + parameters to slave RAM $F4-$FF, then pulses RAUD.
Z8 IRQ3 reads $F8 and dispatches:

| $F8 Value      | Key Bits | Z8 Path                    | Action                                                            |
|----------------|----------|----------------------------|-------------------------------------------------------------------|
| $04            | bit 2    | VOICE PARAM UPDATE ($0374) | Timer SFR refresh: sram[$F9-$FB] → TMR/T1/PRE1, then JP spin      |
| $x1 (e.g. $39) | bit 0    | FULL SETUP ($03A5)         | Bulk copy $F9-$FF → regs, compute pitch, start synthesis          |
| $8A            | bit 1    | STOP ($0389)               | Silence voice, reset mode                                         |
| $00, $48       | none     | SYNTHESIS UPDATE ($0330)   | Reload micro-program pointer + pitch envelope, re-run interpreter |

Priority: bit 2 (voice param update) > bit 1 (stop) > bit 0 (full setup) > default (synthesis update).

**Critical: voice_param_update ends with `JP spin`** — it does NOT return
to the synthesis loop. This is the "idle" state after TMR is configured.
The next RAUD (typically FULL SETUP) starts synthesis fresh.

**Master command sequence** (verified from real EX-20 captures):
1. Boot: PARAM_UPDATE (cmd=$04) to all slots with F9=$40 (TMR = T_OUT on T0, no load/enable)
2. Note on: FULL SETUP (cmd=$39/$49, bit 0 + upper bits) starts synthesis
3. IRQ1's `OR TMR, #$03` preserves T_OUT bits and adds LOAD_T0 + ENABLE_T0
4. Note off: PARAM_UPDATE → STOP ($8A)

**Observed F9 (TMR) values**: $40 (boot idle), $43, $45, $48, $5D, $60, $A5.
All have T_OUT=T0 (bits 7:6 = 01). Varying lower bits configure T0 prescaler
and mode for different voice types.

**Bus protocol**: IRQ3 claims the bus at entry (`P2M=#$00`, RARC driven low).
Each exit path releases the bus (`P2M=#$04`, RARC = input/high-Z).
The master waits for RARC release before switching SLRAMB or sending
the next RAUD. No new IRQ3 arrives while the handler holds the bus.

**Re-entrancy**: IRQ3 resets SPL and calls EI at entry — it can preempt
a previous IRQ3 that was waiting in the synthesis loop (spin at $025D).
The stack reset discards the previous context. The IRQ4 micro-op chain
continues running independently via timer interrupts.

**Lifecycle**: PARAM_UPDATE (set TMR/T_OUT) → FULL SETUP (start synthesis) → SYNTHESIS UPDATE (tweak params) → PARAM_UPDATE + STOP (note off).

### 2.5 Master Command Queue

12-entry jump table at $AAA4 (68B09 master firmware):

| Cmd | Function                   | $F8        | Action                                              |
|-----|----------------------------|------------|-----------------------------------------------------|
| 0   | voice_cmd_0_setup          | (data)     | Bulk write WAVE block + FREQ block, $F4=$1F         |
| 1   | voice_cmd_1_setup_finish   | param\|$01 | Write $F5-$FE, trigger RAUD (full setup)            |
| 2   | voice_cmd_2_idle_ack       | $00        | Clear $F8, write $FE, RAUD (minimal update)         |
| 3   | voice_cmd_3_bank_select    | —          | Select slave bank + EXSLA, process next queue entry |
| 4   | voice_cmd_4_stop           | $04/$8A    | PARAM_UPDATE or STOP depending on state             |
| 5   | voice_cmd_5_param_update   | $04        | TMR/T1/PRE1 refresh via voice_param_update          |
| 6   | voice_cmd_6_wave_reload    | —          | Write WAVE block, process queue                     |
| 7   | voice_cmd_7_queue_pump     | —          | Just dispatch next queued command                   |
| 8   | voice_cmd_8_setup_small    | param\|$01 | Write $40 words waveform + RAUD                     |
| 9   | voice_cmd_9_setup_large    | param\|$01 | Write $55 words waveform + RAUD                     |
| A   | voice_cmd_A_probe          | param\|$01 | Write $10 bytes waveform + RAUD                     |
| B   | voice_cmd_B_setup_waveform | param\|$01 | Write $D4 bytes waveform + RAUD                     |


## 3. Register Architecture

The Z8 register file is partitioned into two non-overlapping domains that
correspond to the two-level synthesis pipeline. **The outer and inner levels
use completely different register sets** — there is no overlap.

IMPORTANT: R14 (address $0E) and reg[$14] (address $14) are DIFFERENT registers.
Working registers R0-R15 are at addresses $00-$0F. The "secondary accumulator"
at reg[$14:$15] is at addresses $14:$15.

### 3.1 IRQ4 Registers (micro-op chain, audio sample rate)

Written/read by the IRQ4 handler and micro-ops at ~10-30 kHz:

| Reg     | Addr | Role                                                    | Set by                       |
|---------|------|---------------------------------------------------------|------------------------------|
| R0      | $00  | **DAC output** → Port 0                                 | Every micro-op (mandatory)   |
| R4      | $04  | Dispatch high byte (always $00)                         | synthesis_loop_entry         |
| R5      | $05  | **Next micro-op address**                               | Every micro-op + DI handoff  |
| R6      | $06  | Timer T0 reload (pitch period)                          | IRQ4 handler from reg[$1D]   |
| R7      | $07  | **Phase index** (0-63 for waveform table)               | Micro-ops advance/wrap       |
| R14     | $0E  | **DDS frequency step** ($C4) / working accum ($AC)      | Resampler loop or DI handoff |
| R15     | $0F  | Working accumulator (interleaved with R14)              | Micro-ops + DI handoff       |
| SPH     | $FE  | Phase accumulator high (RL through carry)               | IRQ4 handler                 |
| $40-$7F | —    | **Waveform table** (64 entries, read-only by micro-ops) | LDEI chain or resampler      |

### 3.2 IRQ3 Registers (envelope loop, per ECLK ~200 Hz)

Written/read by the interpreter, finalize, envelope, synthesis_output:

| Pair              | Addr    | Role                                                     |
|-------------------|---------|----------------------------------------------------------|
| R8:R9             | $08:$09 | **Primary accumulator** → finalize_output 12×16 multiply |
| R10:R11           | $0A:$0B | Scratch / ext memory pointer / multiply result           |
| R12:R13           | $0C:$0D | Scratch / multiply operand / table index                 |
| reg[$14]:reg[$15] | $14:$15 | Secondary accumulator (NOT R14:R15!)                     |
| reg[$16]:reg[$17] | $16:$17 | Tertiary accumulator                                     |

Control registers:

| Reg     | Addr | Role                                                    |
|---------|------|---------------------------------------------------------|
| $10     | —    | Mode register (from slave_ram[$FA])                     |
| $12:$13 | —    | Frequency parameters (from slave_ram[$FC:$FD])          |
| $18     | —    | Primary loop counter                                    |
| $19     | —    | Secondary loop counter                                  |
| $1A     | —    | Pitch envelope (from co-processor R1, updated per ECLK) |
| $1B     | —    | Micro-program counter (indexes into $1D-$3C)            |
| $1C     | —    | Tertiary counter / coeff multiply toggle                |

### 3.3 Shared Registers (handoff between IRQ3 and IRQ4)

These are written by the IRQ3 envelope loop and read by IRQ4 micro-ops.
The DI section in synthesis_output ($0E78/$0E8D) updates them atomically
to prevent IRQ4 from seeing a half-updated state.

| Reg      | Addr | IRQ3 writes                                    | IRQ4 reads                                |
|----------|------|------------------------------------------------|-------------------------------------------|
| reg[$1D] | $1D  | Envelope-modulated pitch (from multiply)       | → R6 → T0 (timer period)                  |
| reg[$1E] | $1E  | Micro-op chain address (from ROM table byte 1) | micro_init reads → R5                     |
| R5       | $05  | DI path: next micro-op start                   | IRQ4 dispatch target                      |
| R6       | $06  | synthesis_output: initial timer value          | IRQ4: reloads T0                          |
| R14      | $0E  | DI path: waveform[phase] for $AC chains        | $AC chain working reg / $C4 DDS freq step |
| R15      | $0F  | DI path: waveform[phase] for $AC chains        | $AC/$B8 chain working reg                 |

ECLK synchronizes this handoff: the IRQ3 loop busy-waits at $0E2A until
ECLK fires, then runs synthesis_output which updates these shared registers
(with DI protection), then IRQ1 restarts the timer and IRQ4 resumes with
the new values. Between ECLK cycles, IRQ4 runs freely.

### 3.4 Outer Pipeline Registers (micro-program interpreter, between passes)

Three 16-bit accumulator pairs — the "programmable" operands:

| Pair              | Addr    | Role                                                     |
|-------------------|---------|----------------------------------------------------------|
| R8:R9             | $08:$09 | **Primary accumulator** → finalize_output 12×16 multiply |
| reg[$14]:reg[$15] | $14:$15 | Secondary accumulator (NOT R14:R15!)                     |
| reg[$16]:reg[$17] | $16:$17 | Tertiary accumulator                                     |

The 76 outer functions are constrained to operations between these 3 pairs:
LOAD (from micro-program immediate), COPY, ADD, SUB, MUL (8×16), NEG,
CMP + conditional branch, and LOOP control.

Control registers:

| Reg | Addr | Role                                                    |
|-----|------|---------------------------------------------------------|
| $1B | —    | **Micro-program counter** (indexes into $1D-$3C)        |
| $18 | —    | Primary loop counter                                    |
| $19 | —    | Secondary loop counter                                  |
| $1A | —    | Pitch envelope (from co-processor R1, updated per ECLK) |
| $1C | —    | Tertiary counter / coefficient multiply toggle          |
| $3B | —    | Global iteration counter (INC on each primary loop)     |

Micro-program storage and control registers:

| Range   | Role                                                                   |
|---------|------------------------------------------------------------------------|
| $1D     | Timer reload / frequency parameter (set by synthesis_output)           |
| $1E     | Micro-op chain address (set by synthesis_output from ROM table byte 1) |
| $1F-$3C | **Micro-program bytecodes** (30 bytes, loaded from slave RAM via LDEI) |

reg[$1D] and reg[$1E] are **control registers** that must not be overwritten
by the micro-program. The regpair_dispatch base offset `#$1F` (patched from
`#$1D`) ensures the micro-program can only self-modify its own bytecodes
at reg[$1F-$3C], not the control registers. reg[$1F] holds the first
bytecode; the interpreter reads them via `LD R11, @reg[$1B]` where
reg[$1B] is the program counter.

Note: the `#$1D` offset at $0882 (ADD $1B,#$1C + INC) and $0436 (ADD $1B,#$1D)
is **correct** — it converts the sram-relative start_value (stored in reg[$1D])
to an absolute register address. With start_value=2: counter = 2+$1D = $1F,
correctly pointing to the first bytecode. The regpair_dispatch `#$1D→#$1F` fix
is different: it maps 0-based operand offsets to absolute addresses, where
offset 0 must target the first bytecode (reg[$1F]), not a config byte.

### 3.2a Z8 $Ex Register Alias Encoding

The Z8 reserves addresses $E0-$EF as **working register aliases**. Any access
to $Ex resolves at runtime to `(RP & $F0) | (x & $0F)`, where RP is set by SRP.
With SRP=$00 (as in this firmware): $E0≡R0, $E1≡R1, ..., $EF≡R15.

The ROM encodes `LD r, R` instructions using the `x8 Ey` form (e.g., `08 EF` =
LD R0, R15), while modern assemblers (asz8) prefer the `y9 Ex` form (`F9 E0`).
Both are functionally identical 2-byte encodings. The choice is purely an
artifact of the original Wersi/Zilog assembler toolchain from the 1980s.

The firmware never changes SRP during synthesis, so the $Ex alias has no
special runtime significance — it is always equivalent to $0x.

### 3.3 Shared / Configuration Registers

| Reg | Addr | Role |
|-----|------|------|
| R2 | $02 | Voice state flags |
| R3 | $03 | Port 3 input (ECLK, EXSLA0, EXSLA1) |
| R10:R11 | $0A:$0B | Scratch / multiply result / ext memory pointer |
| R12:R13 | $0C:$0D | Scratch / multiply operand / table index |
| $10 | — | **Mode register** — bit 7: pitch env, bit 5/4: mode, bits 3:0: sub |
| $11 | — | LFSR / parameter |
| $12:$13 | — | Frequency params (from slave_ram[$FC:$FD]) |
| $1A | — | Pitch envelope parameter (from co-processor) |
| $1D-$3C | — | Micro-program bytecode (32 bytes from slave RAM) |
| $40-$7F | — | Waveform table (filled by LDEI chain for sub<9) |

#### Mode register ($10) — slave_ram[$FA]

Written by the master CPU during SETUP. Copied from slave_ram[$FA] to reg[$10].

```
  bit 7:   Pitch envelope enable (1 = load PITCH_ENV from COP, 0 = force PITCH_ENV=0 "fixed formant")
  bit 6:   Used by coeff multiply (TCM in $0AC3)
  bits 5:4: Synthesis mode:
              $20 = Mode A — Oversampled wavetable + direct DDS ($20/$82/$AC/$C4)
              $00 = Mode B — Variable-duty DDS ($CF)
              $10 = Mode C — Programmable-wrap DDS ($F2)
  bits 3:0: Sub-mode (0-15)
```

**Sub-mode** (bits 3:0) selects the micro-op chain for the inner pipeline.
It is computed by the master CPU from the note's octave/pitch range — higher
pitches get higher sub values. The sub-mode indexes into the synthesis output
parameter table at $0101, which determines:
- byte 1 → reg[$1E]: the micro-op chain address ($AC for sub≤7, $C4 for sub≥8)
- byte 2 → PRE0: timer prescaler

The sub=7→sub=8 boundary is where the inner micro-op chain switches from
the self-updating $AC chain (phase increments by 1, R14 self-managed) to
the $C4 DDS loop (phase increments by R14, requires pre-loaded constant).
This is the exact boundary where distortion begins if R14 is not initialized.

The sub-mode also controls the Mode A initialization path:
- sub < 9: LDEI chain fills waveform table ($0576), R14 NOT initialized
- sub = 9: accumulator init loop ($054C), R14 initialized
- sub > 9: accumulator init loop ($051E), R14 initialized

Examples from captures:
- Drawbar C5: $E7 (sub=7, Mode A) or $E8 (sub=8) depending on octave mapping
- Drawbar C#5: $E8 (sub=8) or $E9 (sub=9)
- Ensemble: $E8 (sub=8) at all pitches

### 3.4 Special Function Registers

| Reg | Address | Purpose |
|-----|---------|---------|
| TMR | $F1 | Timer mode: bit 0=LOAD_T0, bit 1=ENABLE_T0, bits 7:6=TOUT (01=T0). Set by voice_param_update from sram[$F9], then `OR #$03` by IRQ1 |
| T0 | $F4 | Timer 0 counter/reload (pitch) |
| PRE0 | $F5 | Timer 0 prescaler |
| SPH | $FE | **Repurposed as phase accumulator high byte** |

### 3.5 Two-Level Synthesis Pipeline

```
OUTER LEVEL (runs once per ECLK/RAUD cycle):
  ┌─────────────────────────────────────────────────────────┐
  │ synthesis_mode_a/b/default                              │
  │   → fills waveform table (LDEI, reg[$40-$7F])           │
  │   → initializes R14 from slave_ram[$F7]                 │
  │     (*** MISSING for Mode A sub<9 ***)                  │
  ├─────────────────────────────────────────────────────────┤
  │ micro_program_interpreter ($058E)                       │
  │   reads opcodes from reg[$1D-$3C]                       │
  │   operates on: R8:R9, reg[$14:$15], reg[$16:$17]        │
  │   does NOT touch: R0,R4-R7, R14,R15, reg[$40-$7F]       │
  ├─────────────────────────────────────────────────────────┤
  │ finalize_output ($0CA5)                                 │
  │   R10:R11 = reg[$12:$13] × R8:R9  (12×16 multiply)      │
  │   envelope modulation by reg[$1A]                       │
  ├─────────────────────────────────────────────────────────┤
  │ synthesis_output ($0E62)                                │
  │   reads param table (all ROM via LDC): T, micro_op, PRE0│
  │   sets reg[$1E]=micro_op, PRE0, reg[$1D]=R10, R6=R10    │
  │   T<$40 (all DDS: $C4/$CF/$F2): R14=T (phase increment)  │
  │   T≥$40 (Mode A chains only): R14 unchanged (self-mgd)  │
  └─────────────────────────────────────────────────────────┘

INNER LEVEL (runs at audio sample rate, IRQ4-driven):
  ┌──────────────────────────────────────────────────────┐
  │ IRQ4 handler ($000F) — fires on Timer T0 overflow    │
  │   RL SPH (phase accumulator high)                    │
  │   ADC R6, R4 (frequency counter)                     │
  │   LD T0, R6 (reload timer)                           │
  │   LD R6, reg[$1D] (frequency param)                  │
  │   JP @RR4 → micro-op at R4:R5                        │
  ├──────────────────────────────────────────────────────┤
  │ Micro-op (one of ~15 fixed operations)               │
  │   Reads: R7 (phase), R14/R15 (working), reg[$40+R7]  │
  │   Writes: R0 (output), R5 (next op), R7, R14, R15    │
  │   IRET → timer fires → next IRQ4 → next micro-op     │
  └──────────────────────────────────────────────────────┘
```

### 3.6 Mode A Waveform Table Loading (SETUP time)

Mode A ($050E) loads the waveform table reg[$40-$7F] during SETUP, with
three paths based on the initial sub value from the MODE byte:

| Path  | Sub | Waveform table           | Waveform source                   |
|-------|-----|--------------------------|-----------------------------------|
| $0576 | <9  | LDEI raw copy (64 bytes) | Bass/Tenor (64 samples from sram) |
| $054C | =9  | Resampled via $D7 writes | Alt (32→64 upsample)              |
| $051E | >9  | Resampled via $D7 writes | Sopran (16→64 upsample)           |

The sub>=9 paths use **opcode $D7** (`LD x(Rn), Rd`) to write computed
values into reg[$40-$7F]. This is a 2× (sub=9) or 4× (sub>9) upsample
of the shorter Alt/Sopran waveforms to fill all 64 entries. R12 serves
double duty as both the write offset (decremented by 4 per iteration:
$3C→$38→...→$00) and as a temporary for computed values.

The sub<9 path copies 64 raw bytes from slave RAM via the LDEI bulk copy.
This works for Bass/Tenor (64-sample waveforms). All paths fall through
to synthesis_loop_entry at $057B.

### 3.6a R14 Usage by Mode (runtime)

R14 has **incompatible meanings** depending on the current micro-op:

| Micro-op | Sub range | R14 role | Set by |
|----------|-----------|----------|--------|
| $20 (8×) | Mode A 0-5 | Working sample (read+write) | Chain steps |
| $82 (4×) | Mode A 6 | Working sample (read+write) | Chain steps |
| $AC (2×) | Mode A 7 | Working sample (read+write) | Chain steps |
| $C4 (1×) | Mode A 8-13 | Freq step constant (=T byte) | synthesis_output |
| $CF (duty) | Mode B all | Freq step constant | synthesis_output / SETUP |
| $F2 (wrap) | Mode C all | Freq step constant | synthesis_output / SETUP |
| $19 (init) | all 14-15 | Set to $40 (waveform base) | micro_init |

For oversampled chains (sub 0-7): R14 is a working register managed by
the chain itself. `.switch_to_oversampling` initializes R14=R15=
waveform[PHASE] when transitioning from 1x DDS to oversampled mode.

For 1x DDS ($C4, sub 8+): R14 = T byte = timer period = phase increment.
Set by synthesis_output on every ECLK (`.update_phase_increment` for
steady-state, `.switch_to_direct_sampling` for oversample→DDS transitions;
both write `LD R14, R11` where R11 = T byte from param table).

For Mode B/C: R14 is set from sram[$F7] during SETUP, then refreshed
by synthesis_output's `.update_phase_increment` path on each ECLK.

**WARNING**: Crossing the sub 7/8 boundary swaps R14 between "working
sample" and "freq step constant" — the two uses are incompatible.
See WIP_ocean_crash.md for the mode-bouncing issue.

### 3.7 Implementation Tricks

- **SPH as register**: Internal stack mode ignores SPH, freeing it for the phase accumulator
- **Analog amplitude**: DAC 0832 Vref = co-processor envelope → waveform × envelope in hardware
- **Register-file waveform table**: 64 bytes at $40-$7F — filled from slave RAM (sub<9) or computed in place (sub>=9). Micro-ops read via `LD Rd, 40h(Rs)` for single-cycle lookup
- **Indexed register writes**: opcode $D7 `LD x(Rs), Rd` writes to computed register addresses, enabling in-place waveform generation in reg[$40-$7F]
- **Timer-driven micro-ops**: IRQ4 tick rate = pitch frequency, coupling computation to output rate
- **LDEI chain**: 64 consecutive LDEI instructions at $025F — entry point computed as $DD - 2×N to copy exactly N bytes from slave RAM
- **Register partitioning**: Inner (R0,R5-R7,R14-R15) and outer (R8-R9,reg[$14-$17]) domains never conflict — enables the two-level pipeline without save/restore
- **R4 as zero register**: `synthesis_loop_entry` clears R4 once (`CLR R4`). It stays zero throughout synthesis and is used wherever a zero operand is needed: `CP reg, R4` (compare with 0), `ADC Rn, R4` (add carry only), `SBC Rn, R4` (subtract borrow only). Register-register ops are 2 bytes / 6 cycles vs. 3 bytes / 10 cycles for immediates — saves 1 byte and 4 cycles per use. Used ~8 times in the LFSR handler alone


## 4. Interrupt System

### 4.1 Boot Sequence

```
Reset → $023A: Init ports, stack, IPR=$27, IMR=$88 (IRQ3 + global), spin at $025D
```

### 4.2 IRQ3 — RAUD / Command Dispatch ($02E0)

Triggered by RAUD pulse on P3.0. Reads command byte from slave RAM $F8.

```
$02E0: SPL=$40, EI, SRP=0           ; Reset stack, enable nesting
$02E9: XOR R2,#$07                  ; Toggle voice state
$02EC: OR IRQ,#$02                  ; Flag IRQ1 pending
$02F6: LDE R13,@RR10               ; Read command from slave RAM[$F8]
$02F8: TCM R13,#$04 → VOICE_PARAM  ; bit 2 → voice_param_update ($0374)
       TCM FLAGS,#$02 → FIRST_TIME ; FLAGS check → first-time ($0302)
$0326: TCM R13,#$02 → STOP         ; bit 1 → stop ($0389)
$032B: TCM R13,#$01 → FULL_SETUP   ; bit 0 → full setup ($03A5)
$0330: (default) → SYNTH_UPDATE    ; synthesis parameter update path
```

### 4.3 IRQ1 — Timer Setup ($0EBE)

One-shot handler. Starts the synthesis timer chain.

```
$0EBE: LD T0, R6           ; Load timer with pitch
$0EC0: OR TMR, #$03        ; LOAD_T0 | ENABLE_T0 (preserves T_OUT bits set by voice_param_update)
$0EC3: LD R5, reg[$1E]     ; Next micro-op address
$0EC5: AND IMR, #$FD       ; Disable IRQ1 (self-disable)
$0EC8: OR IMR, #$10        ; Enable IRQ4
$0ECB: OR IRQ, #$10        ; Force-trigger IRQ4
$0ECE: IRET
```

### 4.4 IRQ4 — Phase Accumulator + Micro-Op Dispatch ($000F)

Fires on every T0 overflow. Runs one micro-op per tick.

```
$000F: RL  SPH              ; Rotate phase accumulator left
$0011: ADC R6, R4           ; Frequency counter += step + carry
$0013: LD  T0, R6           ; Reload timer (variable pitch)
$0015: LD  R6, reg[$1D]     ; Load frequency parameter
$0017: JP  @RR4             ; Dispatch to micro-op at R4:R5
```

### 4.5 Signal Flow Summary

The Z8 runs two interleaved execution contexts:

**IRQ3 context (heavy computation, per ECLK cycle ~200 Hz):**
The interpreter, finalize, envelope, and synthesis_output all run as
subroutine calls within IRQ3. The synthesis_loop_reentry at $0E2A
busy-waits for the next ECLK, polling IRQ1 pending. This context does
all waveform computation and parameter updates.

**IRQ4 context (lightweight, at audio sample rate ~10-30 kHz):**
Only the phase accumulator (5 instructions) + one micro-op dispatch
(~5-7 instructions + IRET). Writes the DAC sample via R0 → Port 0.
Total ~12 instructions per sample — minimal latency.

**DAC output:** The micro-op writes R0 = Port 0 on every IRQ4 tick.
The DAC 0832 ILE pin is on P3.6 (T_OUT). T_OUT is controlled by TMR
bits 7:6 — set to $40 (TOUT=T0) by the master via voice_param_update
before SETUP. The `OR TMR, #$03` in IRQ1 preserves these bits while
restarting T0. Each T0 overflow toggles P3.6, latching the DAC output
as a synchronous sample-and-hold at the synthesis sample rate.

```
RAUD (P3.0) ──→ IRQ3: claim bus, load params from slave RAM
                  │
                  ├─ VOICE PARAM UPDATE ($0374): sram[$F9-$FB]→TMR/T1/PRE1, release bus, JP spin (idle)
                  ├─ STOP ($0389): silence DAC, release bus, reset
                  ├─ FULL SETUP ($03A5): bulk copy, synthesis_mode_X, → interpreter
                  └─ SYNTHESIS UPDATE ($0330): read pitch params, → interpreter
                       │
                       ▼  (runs in IRQ3 context, IRQ4 interrupts this)
               Micro-program interpreter ($058E)
               reads opcodes from regs $1D-$3C
               dispatches to 76 outer functions
                       │
                       ▼ (reg[$1B] >= $3D)
               Finalize output ($0CA5)
               12×16 multiply + pitch envelope
                       │
                       ▼
               Synthesis output ($0E62)
               reads ROM parameter table (LDC)
               sets reg[$1E], PRE0, reg[$1D], R6
                       │
                  ┌────┴────┐
                  │         │
            carry set    carry clear
                  │         │
                  ▼         ▼
            JP $058E     Enable IRQ1 ($0EA6)
            (next pass)  Release bus (P2M=#$04)
                         JP synthesis_loop_reentry ($0E2A)
                              │
                              ▼
                         Busy-wait for ECLK (poll IRQ1 pending)
                         Updates routing from P3, pitch envelope from R1
                              │
                              ▼ (ECLK fires → IRQ1 pending)
                         IRQ1 ($0EBE): start/restart timer
                         LD T0,R6; OR TMR,#$03; enable IRQ4
                              │
                              ▼
                         IRQ4 ($000F): micro-op chain at audio rate
                         RL SPH; ADC R6,R4; LD T0,R6; JP @RR4
                         → micro-op writes R0 (DAC) + IRET
                         (repeats at timer rate until next ECLK)
```


## 5. Micro-Program VM

### 5.1 Opcode Encoding

Each byte in the micro-program (regs $1D-$3C) encodes one instruction:

| Format | Condition      | Selector               | Dispatch Table | Opcodes             |
|--------|----------------|------------------------|----------------|---------------------|
| A      | bits[1:0] = 00 | bits[7:2] → 64 entries | $01B2          | $00,$04,$08,...,$FC |
| B      | bits[1:0] ≠ 00 | bits[5:1] → 32 entries | $0192          | $01-$3F             |

4 type-B entries redirect to the type-A table (shared functions).
Multi-byte instructions consume additional bytes from the micro-program.

### 5.2 Instruction Set (76 instructions)

Register pair aliases used below: **ACC** = R8:R9 (primary accumulator),
**P14** = reg[$14:$15], **P16** = reg[$16:$17]. "micro[N]" = N bytes
consumed from the micro-program after the opcode. "sat" = saturating.

#### Data Movement (10)

| Opcode  | Bytes | Mnemonic    | Operation      |
|---------|-------|-------------|----------------|
| $02,$03 | 2     | LOAD ACC    | ACC ← micro[2] |
| $05     | 2     | LOAD P16    | P16 ← micro[2] |
| $06,$07 | 2     | LOAD P14    | P14 ← micro[2] |
| $44     | 1     | MOV P16→ACC | ACC ← P16      |
| $48     | 1     | MOV P14→ACC | ACC ← P14      |
| $54     | 1     | MOV ACC→P16 | P16 ← ACC      |
| $4C     | 1     | MOV P14→P16 | P16 ← P14      |
| $58     | 1     | MOV ACC→P14 | P14 ← ACC      |
| $5C     | 1     | MOV P16→P14 | P14 ← P16      |
| $60     | 1     | SWAP        | P14 ↔ P16      |

#### 16-bit Add (9)

| Opcode  | Bytes | Mnemonic     | Operation              |
|---------|-------|--------------|------------------------|
| $0A,$0B | 2     | ADD ACC,imm  | ACC += micro[2]        |
| $0D     | 2     | ADD P16,imm  | P16 += micro[2]        |
| $0E,$0F | 2     | ADD P14,imm  | P14 += micro[2]        |
| $64     | 1     | ADD ACC,P16  | ACC += P16             |
| $68     | 1     | ADDS ACC,P14 | ACC += P14 (sat $FFFF) |
| $74     | 1     | ADD P16,ACC  | P16 += ACC             |
| $6C     | 1     | ADD P16,P14  | P16 += P14             |
| $78     | 1     | ADD P14,ACC  | P14 += ACC             |
| $7C     | 1     | ADD P14,P16  | P14 += P16             |

#### 16-bit Subtract (6)

| Opcode | Bytes | Mnemonic     | Operation                |
|--------|-------|--------------|--------------------------|
| $84    | 1     | SUB ACC,P16  | ACC -= P16               |
| $88    | 1     | SUBS ACC,P14 | ACC -= P14 (floor $0000) |
| $94    | 1     | SUB P16,ACC  | P16 -= ACC               |
| $8C    | 1     | SUB P16,P14  | P16 -= P14               |
| $98    | 1     | SUB P14,ACC  | P14 -= ACC               |
| $9C    | 1     | SUB P14,P16  | P14 -= P16               |

#### Negate (2)

| Opcode | Bytes | Mnemonic | Operation                     |
|--------|-------|----------|-------------------------------|
| $40    | 1     | NEG P16  | P16 = -P16 (two's complement) |
| $50    | 1     | NEG P14  | P14 = -P14                    |

#### 8×16 Multiply (11)

8-bit scalar × 16-bit pair → 16-bit result. NMUL negates the scalar first.

| Opcode | Bytes | Mnemonic     | Operation               |
|--------|-------|--------------|-------------------------|
| $D0    | 1     | MUL ACC,$16  | ACC = reg[$16] × ACC    |
| $D4    | 1     | MUL ACC,$11  | ACC = reg[$11] × ACC    |
| $B4    | 2     | MUL ACC,imm  | ACC = micro[1] × ACC    |
| $F0    | 1     | NMUL ACC,$16 | ACC = (-reg[$16]) × ACC |
| $F4    | 1     | NMUL ACC,$14 | ACC = (-reg[$14]) × ACC |
| $D8    | 1     | MUL P16,$14  | P16 = reg[$14] × P16    |
| $B8    | 2     | MUL P16,imm  | P16 = micro[1] × P16    |
| $F8    | 1     | NMUL P16,$14 | P16 = (-reg[$14]) × P16 |
| $DC    | 1     | MUL P14,$14  | P14 = reg[$14] × P14    |
| $BC    | 2     | MUL P14,imm  | P14 = micro[1] × P14    |
| $FC    | 1     | NMUL P14,$16 | P14 = (-reg[$16]) × P14 |

#### Multiply-Accumulate (7)

Same multiply but result is added to the destination pair (MAC operation).

| Opcode | Bytes | Mnemonic    | Operation             |
|--------|-------|-------------|-----------------------|
| $E0    | 1     | MAC ACC,$16 | ACC += reg[$16] × ACC |
| $E4    | 1     | MAC ACC,$14 | ACC += reg[$14] × ACC |
| $C4    | 2     | MAC ACC,imm | ACC += micro[1] × ACC |
| $E8    | 1     | MAC P16,$14 | P16 += reg[$14] × P16 |
| $C8    | 2     | MAC P16,imm | P16 += micro[1] × P16 |
| $EC    | 1     | MAC P14,$16 | P14 += reg[$16] × P14 |
| $CC    | 2     | MAC P14,imm | P14 += micro[1] × P14 |

#### 16×8 Saturating Multiply (2)

16-bit pair × 8-bit immediate, overflow clamps to $FFFF.

| Opcode | Bytes | Mnemonic     | Operation                  |
|--------|-------|--------------|----------------------------|
| $B0    | 2     | MULS P16,imm | P16 = P16 × micro[1] (sat) |
| $C0    | 2     | MULS P14,imm | P14 = P14 × micro[1] (sat) |

#### Signed Compare + Branch (3)

Signed 16-bit comparison against 2-byte immediate. The 3rd micro-program
byte encodes branch conditions for GT/EQ/LT outcomes (bits 7:6).

| Opcode  | Bytes | Mnemonic     | Operation                       |
|---------|-------|--------------|---------------------------------|
| $12,$13 | 3     | CMPS ACC,imm | signed ACC vs micro[2] → branch |
| $15     | 3     | CMPS P16,imm | signed P16 vs micro[2] → branch |
| $16,$17 | 3     | CMPS P14,imm | signed P14 vs micro[2] → branch |

#### Unsigned Compare + Branch (9)

Unsigned 16-bit comparison. Immediate variants use 3 bytes, pair-vs-pair
variants use 2 bytes (1 opcode + 1 branch condition byte).

| Opcode          | Bytes | Mnemonic     | Operation                         |
|-----------------|-------|--------------|-----------------------------------|
| $1A,$1B         | 3     | CMPU ACC,imm | unsigned ACC vs micro[2] → branch |
| $1D             | 3     | CMPU P16,imm | unsigned P16 vs micro[2] → branch |
| $1E,$1F         | 3     | CMPU P14,imm | unsigned P14 vs micro[2] → branch |
| $24,$32,$33     | 2     | CMPU ACC,P16 | unsigned ACC vs P16 → branch      |
| $28,$35         | 2     | CMPU ACC,P14 | unsigned ACC vs P14 → branch      |
| $2C,$36,$37     | 2     | CMPU P16,P14 | unsigned P16 vs P14 → branch      |
| $34,$3A,$3B     | 2     | CMPU P16,ACC | unsigned P16 vs ACC → branch      |
| $3C             | 2     | CMPU P14,P16 | unsigned P14 vs P16 → branch      |
| $38,$3D,$3E,$3F | 2     | CMPU P14,ACC | unsigned P14 vs ACC → branch      |

Branch condition encoding (in the byte following the comparison operands):

| bits[7:6] | On GT       | On EQ       | On LT       |
|-----------|-------------|-------------|-------------|
| 00        | skip        | skip        | take branch |
| 01        | skip        | skip        | skip        |
| 10        | skip        | take branch | skip        |
| 11        | take branch | skip        | skip        |

"Take branch" reads the next byte and does a computed jump (see JMP).
"Skip" continues to the next micro-program instruction.

#### Flow Control (8)

| Opcode      | Bytes | Mnemonic | Operation                                              |
|-------------|-------|----------|--------------------------------------------------------|
| $70         | 1     | JMP      | computed jump: 6-bit offset or register-indirect       |
| $04,$22,$23 | 2+2   | LOOP1    | decrement reg[$18]; if 0, reload from micro[1]; branch |
| $14,$2A,$2B | 2+2   | LOOP2    | same with reg[$19] counter                             |
| $08,$25     | 1+2   | LOOP1R16 | reload reg[$18] from reg[$16], then LOOP1              |
| $18,$2D     | 1+2   | LOOP2R16 | reload reg[$19] from reg[$16], then LOOP2              |
| $0C,$26,$27 | 1+2   | LOOP1R14 | reload reg[$18] from reg[$14], then LOOP1              |
| $1C,$2E,$2F | 1+2   | LOOP2R14 | reload reg[$19] from reg[$14], then LOOP2              |
| $00,$21     | 2     | REPEAT   | reg[$1C] counter; micro[1]=$FF → voice end (reinit)    |

Byte counts: "N+2" means N explicit bytes + 2 implicit branch target bytes.
The branch target is always consumed (read by branch_dispatch if taken, skipped
by `ADD $1B, #$02` if not). Total micro-program footprint = N+2.

**BIT ERROR CANDIDATE at $09C9**: synth_loop_primary (LOOP1) has `INC $3B`
where it should be `INC $1B` (bit 5 stuck-HIGH, $3B vs $1B). Without the
fix, LOOP1 does not advance $1B past the opcode, causing it to read the
opcode byte itself as the reload value. LOOP2 at $09E3 has `INC $1B` — the
correct form. See WIP_church_organ_stuck.md.

JMP target encoding (6-bit field from current micro-program byte):
- offset < $30: direct — reg[$1B] = $1D + offset
- $30 ≤ offset < $38: indirect via reg[$16] — skip = f(offset-$2F, reg[$16])
- offset ≥ $38: indirect via reg[$14] — skip = f(offset-$37, reg[$14])

#### Register-Pair Operations (3)

Programmable register move/clear with 2nd byte selecting source, direction,
and chaining mode (bits 7:6 of operand byte).

| Opcode | Bytes | Mnemonic   | Operation                            |
|--------|-------|------------|--------------------------------------|
| $A4    | 2     | REGOP @ACC | move/clear using R8 as base register |
| $A8    | 2     | REGOP @P16 | move/clear using reg[$16] as base    |
| $AC    | 2     | REGOP @P14 | move/clear using reg[$14] as base    |

#### Waveform Accumulation (1)

| Opcode | Bytes | Mnemonic | Operation |
|--------|-------|----------|-----------|
| $A0 | var | WACC | stride-3 table: groups of {counter, coeff_hi, coeff_lo}. Accumulates into ACC. Advances through up to 6 groups (reg[$1C] stride). |

#### Coefficient Multiply — Pitch Envelope Scaling (2)

| Opcode | Bytes | Mnemonic | Operation                                            |
|--------|-------|----------|------------------------------------------------------|
| $80    | 4     | COEFF    | Coefficient multiply with counter/decay → R8:R9      |
| $90    | 4     | COEFF_V  | Coeff multiply variant with phase modulation → R8:R9 |

COEFF uses 4 bytes: {opcode, coeff1_offset, coeff2_offset, loop_count}.
COEFF_V uses 4 bytes: {opcode, saved_R9, phase_increment, freq_param}.
Phase init = $40 (quarter-wave). Per iteration: phase += increment,
sin(|phase|+$BF) via ROM[$0Fxx] lookup, multiply × freq, sign correct.
(Previously named FM1/FMPM — actually pitch envelope coefficients, not FM.)

#### LFSR Pitch Modulation (3)

16-bit Galois LFSR with polynomial $1D87 (taps at bits 0,1,2,7,8,10,11,12):
- State in reg[$11] (high) and micro-program byte @(R10+3) (low)
- `RLC @R10 → RLC reg[$11] → if carry: XOR $87, XOR $1D`
- Output is differenced (high-pass filtered) before accumulating into R8:R9
- Creates random pitch modulation for chorus/ensemble/vibrato effects
- On counter overflow: reads pitch envelope from Port 1 → reg[$1A],
  triggers IRQ1 for pitch reload, reads EXSLA from Port 3 → reg[$12]

| Opcode  | Bytes | Mnemonic | Operation                                                                      |
|---------|-------|----------|--------------------------------------------------------------------------------|
| $10,$29 | 4     | LFSR     | LFSR advance + differenced multiply → R8:R9. Envelope S&H on counter overflow. |
| $20,$31 | 2     | LMUL_P16 | 8-bit LFSR step + 8×8 multiply → reg[$16:$17]                                  |
| $30,$39 | 2     | LMUL_P14 | 8-bit LFSR step + 8×8 multiply → reg[$14:$15]                                  |

LFSR state: reg[$11] (high byte) + micro-program byte @(R10+3) (low byte).
Advance: `RLC low → RLC high; if carry: XOR low,$87; XOR high,$1D`.
Output is differenced (high-pass filtered) before accumulating into R8:R9.
Creates random pitch modulation for chorus/ensemble/vibrato effects.

FIXME **Coefficient / LFSR (5)**:
- COEFF: coefficient multiply with counter/decay, scales R8:R9 for pitch envelope
- COEFF_V: sine ROM lookup variant, phase accumulation, 4-byte setup
- LFSR: 16-bit PRNG noise via XOR $1D87 polynomial, includes pitch envelope S&H
- LMUL_P16/P14: LFSR step + 8×8 multiply → secondary/tertiary accumulator pair

### 5.3 Execution Model

The micro-program interpreter at $058E runs synchronously within a single
RAUD trigger (no timer interrupts during execution — DI at $03A5 in full setup).

1. Read opcode byte from `reg[reg[$1B]]`
2. Decode: type (bits 1:0) selects table, upper bits select entry
3. Read function address from dispatch table in ROM
4. `JP @RR12` — execute synthesis function
5. Function may consume additional bytes, advances reg[$1B]
6. Return to $058C (INC $1B) → $058E (check bounds)
7. When reg[$1B] ≥ $3D → finalize_output

After finalize, the synthesis output stage at $0E62 re-enables interrupts
(EI at $0E96), loads timer parameters, and either loops back to the
interpreter for another pass or enables IRQ1 for the micro-op chain.

### 5.4 Constraints

| Parameter                            | Value                                      |
|--------------------------------------|--------------------------------------------|
| Micro-program storage                | 32 bytes (regs $1D-$3C)                    |
| Instruction sizes                    | 1-4 bytes (most are 1-2)                   |
| Effective instructions per sample    | ~10-16                                     |
| Register pairs (16-bit accumulators) | 3: R8:R9, reg[$14:$15], reg[$16:$17]       |
| Loop nesting                         | 3 levels: reg[$18], reg[$19], reg[$1C]     |
| Wavetable                            | 64-entry single-cycle waveform in registers $40-$7F |
| Waveform accumulation (WACC)         | ~10 groups (3 bytes each)                  |
| Coeff/LFSR operators                 | ~8 max (4 bytes each)                      |
| LFSR polynomial                      | $87/$1D (16-bit)                           |


## 6. Synthesis Modes

Selected by reg[$10] bits 5:4 after full setup at $0404:

| bit 5 | bit 4 | Mode                                                  | Entry |
|-------|-------|-------------------------------------------------------|-------|
| 1     | x     | Mode A — Oversampled wavetable + direct DDS           | $050E |
| 0     | 0     | Mode B — Variable-duty DDS ($CF)                      | $04DD |
| 0     | 1     | Mode C — Programmable-wrap DDS ($F2) (fall-through)   | $0412 |

The mode determines which micro-op chain handles the inner (IRQ4) synthesis
pipeline. All modes share the same outer (IRQ3) envelope pipeline: the
micro-program interpreter, finalize_output multiply, normalize/overflow,
and synthesis_output parameter table.

### 6.1 Mode A ($050E) — Oversampled wavetable + direct DDS

Selected when MODE bit 5 is set (typically $E0-$EF). Uses a quality ladder
of oversampled scanning chains for low pitches, switching to direct DDS
for high pitches:

| Sub   | Micro-op | Algorithm                              | Timer | R14 role        |
|-------|----------|----------------------------------------|-------|-----------------|
| 0-5   | $20      | 8× oversampled scan (8 IRQ4s/phase)   | T=$80 | working sample  |
| 6     | $82      | 4× oversampled scan (4 IRQ4s/phase)   | T=$80 | working sample  |
| 7     | $AC      | 2× oversampled scan (2 IRQ4s/phase)   | T=$80 | working sample  |
| 8-13  | $C4      | 1× direct DDS (1 IRQ4/output)         | T=R14 | freq step const |
| 14-15 | $19      | Silence (micro_init)                   | T=$00 | N/A             |

The master pre-computes one cycle of the desired waveform and stores it
in slave RAM. The Z8 loads it into reg[$40-$7F] (64-sample wavetable).
Oversampled modes (sub 0-7) scan through this table with phase+=1 and
produce multiple interpolated output samples per waveform step.
Direct DDS (sub 8+) uses phase+=R14 to skip through the table.

PRE0 (prescaler) extends the pitch range: sub 0-5 use prescalers 32→1
(halving per sub), sub 6+ use prescaler=1. Combined with the T byte
phase increment for DDS modes, this covers the full audible range.

The sub 7/8 boundary separates two incompatible R14 usages: working
sample register (oversampled chains) vs constant frequency step (DDS).

### 6.2 Mode B ($04DD) — Variable-duty DDS ($CF)

Selected when MODE bits 5:4 = $00. Setup: synthesis_mode_b ($04DD).
Micro-op: $CF/$DA/$E9 (variable-duty cycle DDS).

Note: MODE=$00 reads param table section labeled "Mode C" in the listing
(offset arithmetic: R13=(sub+0)*3+1 indexes section 0 which contains $CF).
The naming follows the param table section labels, not the MODE register value.

#### Runtime ($CF/$DA/$E9 micro-ops)

Three micro-ops form an output→skip→skip cycle:
- **$CF**: outputs `@PHASE` (reg[PHASE]) to DAC. PHASE scans $40→$7F.
  When PHASE crosses $7F (MI): `SUB PHASE, R15` sets skip entry point,
  switches to $DA.
- **$DA**: `SUB PHASE, #$81` + `ADD PHASE, R14`. No DAC output.
  On carry (wrap past $FF): reset PHASE=$40, switch to $CF.
- **$E9**: same as $DA. On carry: reset to $CF.

R14 = freq step (added each IRQ4). R15 = skip entry offset — controls
where the skip zone starts after the output scan. Larger R15 = shorter
skip = higher duty cycle = brighter timbre.

#### Setup (synthesis_mode_b at $04DD)

Loads waveform samples from slave RAM into reg[$40+].

Inputs:
- sram[$F7] → R14 (temp): DC offset / formant value to subtract
- sram[$F6] → R11: waveform source address in slave RAM
- R15 (from sram[$F9]): sample count - 1

Two paths:
- **R14 ≠ 0** (formant mode): manual loop copies R15 samples backward
  from sram, subtracting R14 from each sample → DC-shifted waveform.
- **R14 = 0** (sampling mode): LDEI sled bulk-copies R15 samples directly.

### 6.3 Mode C ($0412) — Programmable-wrap DDS ($F2)

Selected when MODE bits 5:4 = $10 (fall-through). Setup: synthesis_mode_c ($0412).
Micro-op: $F2 (programmable-wrap DDS).

Note: MODE=$10 reads param table section labeled "Mode B" in the listing
(offset arithmetic: R13=(sub+$10)*3+1 indexes section 1 which contains $F2).

#### Runtime ($F2 micro-op at $00F2)

Single micro-op per IRQ4. Outputs `reg[$40+PHASE]` to DAC, then compares
PHASE to R15. If PHASE < R15: PHASE += R14, IRET. If PHASE >= R15:
PHASE = 0, reload micro-op, IRET. R14 = phase step (from param table T
byte, typically $01). Frequency = f_timer / R15.

#### Anti-aliasing gate ($0E19)

Before entering synthesis_loop_reentry, pitch_output checks if the new
sub's T byte satisfies `T*2 < R15`. If not (fewer than 2 samples per
cycle), the voice is forced to silence (R13=$2E → sub=15, op=$19).
This prevents aliased output when the phase step is too large.

#### Setup (synthesis_mode_c at $0412)

Computes a wrap-boundary value via 8-bit restoring division, then fills
the wavetable reg[$40-$7E] with delta-adjusted samples from slave RAM.
Stores complemented quotient at reg[$7F] as the wrap-boundary marker.

Inputs:
- sram[$F7] → R14 (temp): freq step (used in division, destroyed)
- sram[$F6] → R5 (temp): waveform source offset in slave RAM
- R15 (from sram[$F9]): used in dividend and divisor computation

Algorithm:
1. Read last waveform sample from sram[sram[$F6] + $3F]
2. 8-bit restoring division: ((last_sample - freq_step)/2 + R15 + 1) / 2
   divided by ($41 + R15) / 2
3. Complement quotient → store at reg[$7F]
4. Fill reg[$40-$7E] with (sample + delta) from slave RAM backward

#### COP pitch tables

Two pitch tables select per-note parameters (see WIP_mode_bc_synthesis.md):
- **Sampling mode** (ICB byte[4] bit 7 clear): table at COP ROM $C92E.
- **Formant mode** (ICB byte[4] bit 7 set): table at COP ROM $CA9A.

Each table entry = 4 bytes: {R15, sub_index, pitch_hi, pitch_lo}.
The sub_index encodes MODE bits 5:4 (selecting Mode B or C) AND the
sub-mode, so the synthesis mode can change per note within the same voice.

#### Formant synthesis

The WAVE block's FixFmt region (sram[$B1-$D3], 35 bytes) stores per-note
formant data. The COP reads a byte from this region using R15 as index
into `freq_slave_ptr_table` (COP ROM $AC88), then stores it at sram[$F7].
The setup code uses this formant value in its division and delta
computation, shaping the waveform timbre per-note.

The wersi-mk1-editor Formant UI edits 29 of these 35 bytes (the first 6
are header/metadata). The freq_slave_ptr_table maps 59 note indices to
35 bytes with logarithmic distribution (low notes get 1 byte each, high
notes share 3-4 notes per byte).

Waveform data in the WAVE block is Fourier coefficients, converted to
time-domain samples by the COP via inverse FFT before writing to slave RAM.

### 6.4 Synthesis Output Parameter Table ($0101-$0190)

ROM data table read by synthesis_output at $0E62 via three consecutive
LDC reads (all from ROM — the byte 0 read was patched from LDE to LDC
at $0E69 to fix a stuck-low bit error).

144 bytes: 3 modes × 16 sub-modes × 3 bytes per entry.

| Byte | Read at     | Stored to         | Purpose                                |
|------|-------------|-------------------|----------------------------------------|
| 0    | $0E69 (LDC) | compared with $40 | Path selection: ≥$40=upper, <$40=lower |
| 1    | $0E71 (LDC) | reg[$1E]          | Micro-op chain address                 |
| 2    | $0E98 (LDC) | PRE0              | Timer prescaler                        |

Micro-op addresses by mode:

| Mode                | Sub 0-5              | Sub 6-7       | Sub 8-10         | Sub 11+    |
|---------------------|----------------------|---------------|------------------|------------|
| Mode C ($10)        | $F2 (wrap DDS)       | $F2           | $F2              | $19 (init) |
| Mode B ($00)        | $CF (duty-cycle DDS) | $CF           | $CF              | $19 (init) |
| Mode A ($20) wvtbl  | $20 (8× oversample)  | $82/$AC (4×/2×) | $C4 (1× direct)| $19 (init) |

Mode $30 (bits 5:4 = $11) is invalid — table entries would overlap
with the dispatch tables at $0192+.

Mode C uses $F2 (programmable-wrap DDS: `CP R7, R15`) for all
sub-modes, allowing variable waveform lengths. Mode A uses $C4
(fixed 64-entry wrap: `AND R7, #$3F`) for sub 8+, relying on the
resampler loops to fill all 64 entries.

### 6.5 Coefficient Multiply — Pitch Envelope Scaling

Two coefficient multiply variants for pitch envelope computation:

**COEFF ($0AC3)**: Coefficient multiply with counter/decay, alternating
between two stored coefficients per iteration using reg[$1C] as toggle.
Scales R8:R9 as pitch modifier.

**COEFF_V ($0AF1)**: Variant with phase modulation:
1. Initialize: phase = $40 (quarter-wave), save current R8:R9
2. Per iteration: accumulate phase from reg[$18:$19]
3. |phase| → add $BF → ROM sine table lookup at $0Fxx
4. Multiply sine × reg[$16:$17] (frequency parameter)
5. Sign correction based on phase quadrant
6. Add to output accumulator

(Previously named FM1/FMPM — actually pitch envelope coefficients.)

#### Step-by-step: Note-on to audio output

**1. Master prepares voice data (68B09 main CPU)**

The master's voice program (from Voice ROM IC5) contains pre-computed
single-cycle waveforms at four resolutions packed into slave RAM $00-$B0:

| Range | Slave RAM | Samples | Sub-modes | Notes |
|-------|-----------|---------|-----------|-------|
| Bass/Tenor | $01-$40 | 64 | 0-7 | Full resolution |
| Tenor | $41-$80 | 64 | 6-8 | Same or different waveform |
| Alt | $81-$A0 | 32 | 8-9 | Downsampled 2× |
| Sopran | $A1-$B0 | 16 | 10-12 | Downsampled 4× |

The master selects a sub-mode based on pitch: higher notes use shorter
waveforms (fewer samples per cycle = higher Nyquist limit). It writes:
- slave_ram[$00-$B0]: waveform data (via voice_cmd_0_setup)
- slave_ram[$D4+]: micro-program bytecode + parameters
- slave_ram[$F6]: waveform base offset (e.g. $01, $41, $81, $A1)
- slave_ram[$F7]: DDS frequency step for Mode C/Mode B
- slave_ram[$FA]: mode register (e.g. $E7=sub7, $E8=sub8, $E9=sub9)
- slave_ram[$FC:$FD]: frequency parameters for timer rate
- slave_ram[$F8]: command byte with bit 0 set (SETUP)

Then triggers RAUD → Z8 IRQ3.

**2. Z8 reads parameters (IRQ3 → full_setup at $03A5)**

- Copies slave_ram[$F9-$FF] → reg[$0F-$15] (R15, mode, freq params)
- Copies slave_ram[$D4+] → reg[$1D-$3C] (micro-program + control regs)
- Sets R0=$80 (silence), R7=0 (phase reset)
- Checks reg[$10] bits 5:4 → Mode A → jumps to $050E

**3. Mode A waveform loading ($050E)**

Reads sram[$F6] into R11 (waveform base offset). Then branches on
sub-mode (reg[$10] bits 3:0):

**Sub < 9 → LDEI copy ($0576)**

Copies 64 bytes from slave RAM directly into the waveform table:
```
sram[F6+0..F6+63] → reg[$40-$7F]
```
This is a raw copy — whatever is in slave RAM at that offset goes into
the register file. For Bass/Tenor (64-sample waveform at F6=$01/$41),
all 64 entries are valid. For Alt (32-sample at F6=$81), entries 0-31
are the waveform and entries 32-63 overflow into the Sopran/config area.

R14 is NOT initialized on this path. Sub 0-7 don't need it (their
micro-op chains self-manage R14). Sub 8 needs it but doesn't get it —
this is a confirmed firmware issue fixed by ROM patches.

**Sub = 9 → Waveform generation loop ($054C)**

Computes the waveform table in place using indexed register writes:
```
LD 42h(R15), R12    ; opcode $D7 — writes reg[$42+R15]
LD 43h(R14), R12    ; writes reg[$43+R14]
LD 40h(R14), R12    ; writes reg[$40+R14]
LD 41h(R15), R12    ; writes reg[$41+R15]
```
Each iteration reads two slave RAM bytes (via LDE), uses them as indices
to compute scaled values, and writes results to 2 addresses in reg[$40-$7F].
R14 is initialized from sram[F6_offset] at loop entry.

**Sub > 9 → Waveform generation loop ($051E)**

Same principle as sub=9 but with 4 writes per iteration. Also initializes
R14 and writes computed values into reg[$40-$7F] via $D7 instructions.

All three paths fall through to synthesis_loop_entry at $057B.

**4. Micro-program interpreter ($058E)**

Reads opcodes from reg[$1D-$3C] and dispatches to 76 outer synthesis
functions. These operate on three 16-bit accumulator pairs (R8:R9,
reg[$14:$15], reg[$16:$17]) performing add/sub/multiply/compare/branch.
The interpreter does NOT touch R14 or reg[$40-$7F].

When reg[$1B] reaches $3D, falls through to finalize_output.

**5. Finalize + envelope ($0CA5 → $0D63)**

12×16 multiply: R10:R11 = reg[$12:$13] × R8:R9.
Pitch envelope modulation via reg[$1A].
Mode dispatch computes the parameter table index from reg[$10].

**6. Synthesis output ($0E62)**

Reads the synthesis output parameter table (3 bytes per sub-mode entry,
all from ROM via LDC):
- byte 0 (T): path selection. T≥$40 = multi-step chains, T<$40 = single-step DDS
- byte 1: micro-op address → reg[$1E] ($20/$82/$AC for chains, $C4/$CF/$F2 for DDS)
- byte 2: PRE0 (timer prescaler, bit 0 = modulo mode)

Also stores R10 → reg[$1D] and R6 (timer reload / frequency parameter).

**R14 initialization** depends on the path:

| T | reg[$1E] | R14 set to | Purpose |
|---|----------|-----------|---------|
| <$40 | any | T (byte 0) | **DDS phase increment**: `ADD R7, R14` per IRQ4 tick |
| ≥$40 | <$C4 | unchanged | Multi-step chains ($20/$82/$AC) self-manage R14 as interpolation sample |
| ≥$40 | ≥$C4 | waveform[R7] | Primes interpolation (DI protected, shouldn't occur in practice) |

R14 has two roles depending on the micro-op:
- **All single-step DDS** ($C4/$CF/$F2): R14 = phase increment (constant, set
  from T byte on every ECLK cycle). This is the common case — Mode C (all subs),
  Mode B (all subs), and Mode A sub 8+ all use T<$40 DDS micro-ops.
  T=$01→advance 1 sample/tick, T=$04→advance 4/tick (soprano), T=$20→advance 32/tick.
- **Multi-step chains** ($AC/$82/$20, Mode A sub 0-7 only): R14 = current waveform
  sample for linear interpolation. Updated by the chain micro-ops each tick.
  These use T=$80 (≥$40), so synthesis_output leaves R14 unchanged.

Note: Mode C/Mode B setup paths also write R14 from sram[$F7], but this is
overwritten by synthesis_output (R14=T) on the next ECLK cycle (~5ms later).
The sram[$F7] value only affects the first burst of audio samples.

**DI sections**: paths that set both R14 and R5 disable interrupts briefly to
prevent IRQ4 from reading an inconsistent R14/R5 pair mid-update.

**7. IRQ1 starts the timer ($0EBE)**

Loads T0 with R6, enables Timer 0 + prescaler + T_OUT.
Sets R5 = reg[$1E] (first micro-op). Enables IRQ4.

**8. IRQ4 micro-op chain (audio rate)**

Timer overflow fires IRQ4 → phase accumulator advance → dispatch to
the micro-op at R4:R5. For sub 0-7: the $AC/$B8 two-step chain scans
the waveform table with R7 incrementing by 1 per step. For sub 8+:
the $C4 DDS scans with R7 incrementing by R14 per step.

Each micro-op writes R0 (→ Port 0 → DAC) and sets R5 for the next op.
The T_OUT pin latches the DAC on each timer overflow.

#### Sub-mode to waveform range mapping

| Sub   | Micro-op        | Waveform table fill   | Expected range      | Prescaler |
|-------|-----------------|-----------------------|---------------------|-----------|
| 0-5   | $20 chain       | LDEI 64 bytes         | Bass (64 samples)   | 32→1      |
| 6     | $82 chain       | LDEI 64 bytes         | Tenor (64 samples)  | 1         |
| 7     | $AC chain       | LDEI 64 bytes         | Tenor (64 samples)  | 1         |
| 8     | $C4 DDS         | LDEI 64 bytes         | Tenor/Alt (64/32)   | 1         |
| 9     | $C4 DDS         | Computed ($D7 writes) | Alt (32 samples)    | 1         |
| 10-12 | $C4 DDS         | Computed ($D7 writes) | Sopran (16 samples) | 1         |
| 14-15 | $19 (init only) | —                     | —                   | 1         |

Sub 8 is the boundary case: it uses LDEI (64-byte copy) but the $C4 DDS.
If the master assigns F6=$81 (Alt, 32 samples), the LDEI copies 32 valid
samples + 32 overflow bytes. Sub 9+ compute the waveform in place via
indexed register writes ($D7), so the waveform length is determined by
the computation, not by a fixed 64-byte copy.

## 7. Arithmetic Routines

### 7.1 16-bit Restoring Division ($042D-$04B9)

8-iteration shift-and-subtract. Divides R10:R11 by R12:R13.
Quotient → R14 (complemented at end), remainder → R10:R11.
Used for pitch/frequency calculation in full setup path.

### 7.2 Multiply Variants

**8×16 Multiply ($074D)**: R10:R11 = R12 × R8:R9. 8-iteration shift-add.
Three variants for different source pairs (R8:R9, reg[$16:$17], reg[$14:$15]).
Accumulate variants add result to existing pair (MAC).

**12×16 Multiply ($0CCC)**: R10:R11 = (reg[$12]:reg[$13]) × (R8:R9).
8 iterations on reg[$13], then 4 on reg[$12]. Used in finalize_output.

**16×8 Saturating Multiply ($086D/$089D)**: pair = pair × micro-program-byte.
Left-shifting variant with overflow detection → clamp to $FFFF.

### 7.3 Pitch Envelope Processing ($0D63 / $0ECF)

8×16 multiply: R12:R13 = reg[$1A] × R10:R11.
Mode-dependent scaling: reg[$12] bit 6 → extra /2, bit 7 → extra /8 (SRA).
Direction: reg[$1A] bit 0 selects add or subtract.
Result modifies R10:R11 (the waveform accumulator from the 12×16 multiply).

Note: amplitude envelope is in hardware (co-processor → DAC Vref).
reg[$1A] is the pitch envelope only.


## 8. ROM Tables

### 8.1 Dispatch Tables

**Type B table** ($0192-$01D0): 32 entries × 2 bytes = 64 bytes.
Used when opcode bits[1:0] ≠ 0. Index = (opcode << 1) & $3E + $92.

**Type A table** ($01B2-$0232): 64 entries × 2 bytes = 128 bytes.
Used when opcode bits[1:0] = 0. Index = (opcode >> 1) & $7E.
Overlaps with type B table (type B starts $20 bytes earlier).

### 8.2 Micro-Program Data ($0101-$018D)

NOT executable code. This is the synthesis output parameter table, read
by synthesis_output ($0E62) to select the inner micro-op chain and timer
prescaler per sub-mode. See section 3.6 for the register layout. The
micro-program opcodes are loaded from the FREQ block via slave RAM.

### 8.3 Quarter-Wave Sine Table ($0FC0-$0FFF)

64 entries: `round(127 × sin(π/2 × (i+1)/64))` for i=0..63.
Used for Vibrato 2 op.

```
$0FC0:  3   6   9  12  16  19  22  25  28  31  34  37  40  43  46  49
$0FD0: 51  54  57  60  63  65  68  71  73  76  78  81  83  85  88  90
$0FE0: 92  94  96  98 100 102 104 106 107 109 111 112 113 115 116 117
$0FF0:118 120 121 122 122 123 124 125 125 126 126 126 127 127 127 127
```

Full wave: Q1=table[i], Q2=table[63-i], Q3=-table[i], Q4=-table[63-i].

### 8.4 Pitch Fine Table (master side, $AD09)

Maps 16-bit pitch_index (0-~1023) to fine pitch byte (64-127) written to
slave RAM $FF. Four segments aligned to EXSLA boundaries:

| EXSLA | Division | Index Range | Entries | Values          |
|-------|----------|-------------|---------|-----------------|
| $80   | ÷1       | 0x000-0x0D6 | 215     | $7F→$40         |
| $A0   | ÷2       | 0x0D7-0x15B | 133     | $7F→$40         |
| $C0   | ÷4       | 0x15C-0x1A9 | 78      | $7F→$40         |
| $E0   | ÷8       | 0x1AA-0x260 | 183     | $7E→$00+$81→$FF |

EXSLA is a 2-bit pitch exponent (÷1/÷2/÷4/÷8 scaler for the Z8's
envelope multiply), updated in real-time during vibrato/pitch bend.
NOT related to waveform selection — that is controlled separately
by $F6, set once at SETUP from the FREQ block's pitch table.
Entries per range ~halve (215, 133, 78) matching log₂ pitch scaling.

Frequency pointer table at $AC88 (overlaps select_slave_bank code — ROM
space trick). Entries 4-62 point to slave RAM $B1-$D3 (frequency envelope data).


## 9. ROM Bit Errors

### Confirmed (applied in `patched_roms/apply_patches.py`)

| Address | Original | Fixed | Bit | Cert | Description |
|---------|----------|-------|-----|------|-------------|
| $0646 | $1D | $1F | 1 | 44 | ADD #$1D→#$1F: regpair base must be $1F (first bytecode reg), not $1D (config). Operand offsets are bytecode-relative. $1D at $0882/$0436 is different — adds LDEI block base to start_value that already skips config. |
| $0663 | $7F | $77 | 3 | 4 | JP $067F→$0677: fix mid-instruction target, adds CP R12 check |
| $0E20 | $82 | $C2 | 6 | 0 | LDE→LDC: $F2 special path read from ROM, not slave RAM |
| $0E69 | $82 | $C2 | 6 | 12 | LDE→LDC: synth_output byte0 from ROM, not slave RAM |

### Under investigation

| Address | Original | Fixed | Bit | Cert | Description |
|---------|----------|-------|-----|------|-------------|
| $0F3D | $00 | $80 | 7 | 3 | TCM overflow path EXSLA attenuation |
| $0E8F | $EB | $FB | 4 | 46 | LD R14→R15: lower DI path (same pattern as $0E88) |
| $0338 | $1B | $1F | 2 | 0 | UPDATE path: counter writes to wrong register |
| $077B | $14 | $94 | 7 | 0 | Synth dispatch: multiply reads unmapped register |
| $0F52 | $8B | $AB | 5 | 0 | Overflow path: unconditional→conditional jump |

### Rejected (confirmed correct in original ROM)

| Address | Original | Was | Why rejected |
|---------|----------|-----|-------------|
| $03A5 | $8F (DI) | $9F (EI) | DI is intentional — prevents IRQ4 during setup |
| $03A8 | $08 | $88/$8A | IRQ1 enabled later at $0EA6; global enable via EI |
| $0E6E | $40 | $C0 | CP threshold $40 correctly separates upper/lower paths |
| $0EC2 | $03 | $03 | CONFIRMED CORRECT: `OR TMR,#$03` = LOAD_T0+ENABLE_T0. T_OUT bits set earlier by voice_param_update, preserved by OR |

See `patched_roms/apply_patches.py` for full patch list and
`WIP_low_certainty_bits.md` for the certainty=0 analysis.


## 10. Files

| File | Description |
|------|-------------|
| `wersi_firmware.bin` | Production firmware (4096 bytes) |
| `wersi_firmware_disasm.txt` | Full unidasm disassembly |
| `wersi_firmware_annotated.asm` | Annotated assembly for key ranges |
| `wersi_firmware_analysis.md` | Original analysis (superseded by this doc) |
| `z8_firmware_c/sr0106.c` | Complete C translation (~90% of ROM) |
| `build_callgraph.py` | Recursive-descent reachability analysis |
| `slm2_schematic_z8.md` | Z8611 pin connections from SLM-2 schematic |
| `patched_roms/` | Patch scripts, patched ROMs, verification |
| `test_voice_slave0.bin` | Captured voice RAM snapshot (256 bytes) |
| `WIP_z8_firmware_c_port.md` | C port progress tracking |

## Appendix: DDS Fractional Frequency via SPH Phase Accumulator

*(To be integrated into §6.4 Synthesis Output and §6.1 IRQ4 Handler)*

### Overview

The Z8 timer rate (pitch) has **11-bit effective resolution**: 8 integer bits
from reg[$1D] (timer reload) + 3 fractional bits from SPH ($FE, the phase
accumulator). This is a DDS fractional-N technique that distributes sub-integer
timer increments across audio-rate IRQ4 cycles.

### Mechanism

**IRQ4 handler ($000F)** runs on every Timer 0 overflow (~audio rate):
```asm
    RL   $FE            ; rotate SPH left — bit 7 goes to carry
    ADC  R6, R4         ; R6 += carry (R4=0, so R6 += 0 or 1)
    LD   T0, R6         ; reload timer with R6
    LD   R6, $1D        ; reload R6 from base frequency (reg[$1D])
    JP   @RR4           ; dispatch micro-op
```

Each IRQ4 tick: timer reload = reg[$1D] + carry_from_SPH_rotation.
Over 8 IRQ4 cycles, SPH's 8 bits rotate through, producing exactly
N extra timer ticks where N = number of set bits in SPH.

**dac_output_setup ($0DF3)** computes SPH from the envelope multiply result:
```asm
    LD   R10, #$02
    SWAP R11            ; swap nibbles of R11 (low byte of multiply result)
    RR   R11            ; rotate right
    AND  R11, #$07      ; mask to 3 bits (= bits 4:1 of original R11)
    ADD  R11, #$32      ; table index: $32 + (0..7)
    LDC  R11, @RR10     ; R11 = ROM[$0232 + index]
```

This extracts bits 4:1 of R11 (the finalize multiply low byte) as a 3-bit
fractional index (0-7) into the SPH pattern table.

**synthesis_output ($0E62)** writes the pattern to SPH:
```asm
    LD   $FE, R11       ; SPH = fractional bit pattern
```

**synthesis_pass_done ($0EB0)** preserves the rotation state:
```asm
    LD   R11, $FE       ; R11 = current SPH (rotated by IRQ4 since last write)
```

On the next ECLK, `LD $FE, R11` restores the rotation phase, maintaining
continuous fractional dither without phase discontinuities.

### Fractional Frequency Pattern Table ($0232)

8 entries at ROM address $0232, indexed by 3-bit fractional value:

| Index | Value | Binary | Set bits | Effective fraction |
|-------|-------|--------|----------|-------------------|
| 0 | $00 | 00000000 | 0 | 0/8 |
| 1 | $01 | 00000001 | 1 | 1/8 |
| 2 | $11 | 00010001 | 2 | 2/8 = 1/4 |
| 3 | $15 | 00010101 | 3 | 3/8 |
| 4 | $55 | 01010101 | 4 | 4/8 = 1/2 |
| 5 | $EA | 11101010 | 5 | 5/8 |
| 6 | $EE | 11101110 | 6 | 6/8 = 3/4 |
| 7 | $FE | 11111110 | 7 | 7/8 |

The bit patterns are designed for maximum temporal spacing of the set bits
(minimal jitter). For example, $55 = 01010101 distributes 4 carries evenly
across 8 cycles, rather than clustering them ($0F = 00001111 would produce
4 consecutive carries then 4 gaps).

### Combined Pitch Resolution

The effective timer period per audio cycle is:

    T = reg[$1D] + carry_from_SPH

Averaged over 8 IRQ4 cycles:

    T_avg = reg[$1D] + (set_bits_in_SPH / 8)

Where reg[$1D] = R10 (high byte of finalize multiply = integer part)
and SPH index = R11 bits 4:1 (3 bits from low byte = fractional part).

Total resolution: **8 + 3 = 11 bits** of pitch control, updated at ECLK
rate (~200 Hz, 5ms). The fractional part provides fine-grained pitch
within each ECLK period via audio-rate dithering.

### NOT Interpolation

This does NOT interpolate between old and new pitch values across ECLK
boundaries. Both R10 (integer) and R11 (fractional) are computed from
the same finalize multiply result in the same ECLK pass. The pitch still
changes in discrete 5ms steps, but each step has 11-bit resolution
instead of 8-bit, reducing the audible staircase effect.
