# Wersi EX-20 Micro-Program Analysis

Extracted from Voice ROM IC5 (mk1_ic5_s3.bin) FREQ blocks.
Cross-referenced with Z8 firmware (SR0106) micro-program interpreter at $058E.

## 1. FREQ Block = Micro-Program

Each FREQ block is 32 bytes (LEN_FREQ=$20), loaded directly into Z8
registers $1D-$3C by the full_setup LDEI chain at $03F8.

```
FREQ layout (32 bytes → reg[$1D-$3C]):
  [0]   → reg[$1D]: initial reg[$1B] value (micro-program counter)
  [1]   → reg[$1E]: initial value (overwritten by synthesis_output later)
  [2-31] → reg[$1F-$3C]: micro-program opcodes + pre-loaded values
```

The micro-program counter starts at `FREQ[0] + $1C + 1`:
- FREQ[0]=$02 → first opcode at reg[$1F] = FREQ[2] (most programs)
- FREQ[0]=$04 → first opcode at reg[$21] = FREQ[4] (DRAWB, PC-MED)
  In this case FREQ[2-3] are pre-loaded register values, not opcodes.

## 2. Instruction Set

76 opcodes dispatched via two ROM tables:
- Type A (bits 1:0 = 00): index (opcode >> 1) & $7E → table at $01B2
- Type B (bits 1:0 ≠ 00): index ((opcode << 1) & $3E) + $92 → table at $0192

The micro-program operates on three 16-bit accumulator pairs:
- **R8:R9** ($08:$09) — primary accumulator → feeds finalize_output
- **reg[$14]:reg[$15]** — secondary accumulator
- **reg[$16]:reg[$17]** — tertiary accumulator

These are in the OUTER pipeline register domain. The micro-program
cannot access the inner pipeline registers (R0, R4-R7, R14-R15).

### Instruction categories

| Category  | Count | Opcodes                            | Description                            |
|-----------|-------|------------------------------------|----------------------------------------|
| Load      | 3     | LOAD_R8R9, LOAD_1617, LOAD_1415    | Load pair from inline 2-byte immediate |
| Copy      | 6     | CP_16→89, CP_14→89, ST_89→16, etc. | Copy between accumulator pairs         |
| Add/Sub   | 12    | ADD_16→89, SUB_14←89, etc.         | 16-bit add/subtract between pairs      |
| Negate    | 2     | NEG_16, NEG_14                     | Two's complement negate                |
| Multiply  | 14    | MUL_16, MI+89, NEG16_MUL, etc.     | 8×16 multiply, with/without accumulate |
| Sat.Mul   | 2     | MUL16_I, MUL14_I                   | 16×8 multiply with saturation          |
| Compare   | 12    | CMP89v16, CMP16v14, etc.           | Signed/unsigned compare + 3-way branch |
| Loop      | 6     | LOOP1, LOOP2, RL18←16, RL19←14     | Counter-based loop/reload              |
| Repeat    | 1     | REPEAT                             | Conditional counter with sign check    |
| Branch    | 1     | BRANCH                             | Computed micro-program jump            |
| Regpair   | 6     | REGOP_08, REGOP_16, REGOP_14       | Programmable register move             |
| Waveform  | 1     | WACC                               | Waveform accumulation                  |
| Coeff Mul | 1     | COEFF ($0AC3)                      | Coefficient multiply with counter/decay|
| Coeff+PM  | 1     | COEFF_V ($0AF1)                    | Coeff multiply variant with phase mod  |
| LFSR      | 1     | LFSR ($0B94)                       | 16-bit PRNG noise + envelope S&H       |
| LFSR Mul  | 2     | LMUL_P16 ($0C41), LMUL_P14 ($0C73)| LFSR step + multiply to pair 16/14     |

## 3. DMS Voice1 Micro-Programs (20 instruments)

### 3.1 Simple wavetable (2-4 opcodes)

These instruments use the micro-program only to set a fixed gain in R8:R9.
The actual timbre comes entirely from the WAVE block (wavetable playback).

```
FREQ[0]  DRAWB    $04/$02  pre=[$00,$80]  LOAD_R8R9($37,$01) → NEG_16
FREQ[13] STRING   $02/$0E  LOAD_R8R9($40,$00)
FREQ[14] ENSEMB   $02/$16  LOAD_R8R9($7F,$15)
```

DRAWB: Master computes waveform from drawbar sliders → writes to slave RAM.
Z8 just plays it back with fixed gain $3701. No frequency envelope.

ENSEMB: Similar — fixed gain $7F15, wavetable playback. Uses sub=8 at all
pitches → hits the R14 initialization bug (R14=0 → silence).

STRING: Fixed gain $4000. Simple wavetable.

### 3.2 LFSR + simple operation (12 instruments)

Most instruments start with LFSR (noise/modulation generator) followed
by a single operation. The LFSR provides time-varying modulation.

```
FREQ[3]  US-STG   $02/$FF  LFSR → COEFF
FREQ[4]  STAGE    $02/$FF  LFSR → NEG_16
FREQ[7]  SLAPBS   $02/$FF  LFSR → CMP → LOAD → LOAD → LOAD_1617 → CMP → LOAD → COEFF
FREQ[8]  ACGUIT   $02/$FF  LFSR → CMP → LOAD → LOAD → CMP
FREQ[9]  LEAD G   $02/$FF  LFSR → ADD_16→89
FREQ[10] POLYSN   $02/$FF  LFSR → NEG_16
FREQ[11] BRASS    $02/$FF  LFSR → LOAD_1617 → CMP → ADD_I→89 → LOAD_R8R9
FREQ[12] DREAM    $02/$FF  LFSR → ADD_16→89
FREQ[17] BIGBEN   $02/$FF  LFSR → ADD_I→16 → CMP → LOAD_R8R9 → CMP
FREQ[18] MOOGLY   $02/$FF  LFSR → COEFF
FREQ[19] MT-ORG   $02/$FF  LFSR → LOAD_1617 → CMP → LOAD_R8R9
```

Common patterns:
- `LFSR → COEFF`: noise modulates coefficient scaling (US-STG, MOOGLY)
- `LFSR → NEG_16`: noise inverts waveform phase (STAGE, POLYSN)
- `LFSR → ADD_16→89`: noise adds to output (LEAD G, DREAM)

### 3.3 Medium complexity (5 instruments)

```
FREQ[1]  PIANO1   $02/$26  NEG_16 → LOOP1 → CMP → SUB → LOAD_R8R9 → COEFF
FREQ[2]  MARIMB   $02/$04  LOAD_R8R9 → COEFF (short, uses $1B=$02 directly)
FREQ[5]  CLAVI    $02/$2A  LOOP1 → CMP → NEG_14 → M14→16 → M14→16 → MI→16 → LOAD_R8R9 → LOOP2
FREQ[16] CHURCH   $02/$0E  RL18←14 → CMP → CMP_u → CMP → COEFF
FREQ[42] MOOGLY   $02/$14  LOAD_R8R9 → LOAD_R8R9 → RL18←14
```

PIANO1: Frequency envelope with decay — NEG inverts, LOOP iterates,
CMP checks threshold, COEFF scales output. Produces the characteristic
piano attack-decay.

CLAVI: Complex clavinet simulation with nested loops and multiply chains.

### 3.4 Complex (2 instruments)

```
FREQ[6]  FRETLS   30 opcodes — fills entire block
FREQ[15] HUMAN    30 opcodes — fills entire block
```

These use the full 32-byte capacity with nested loops (LOOP1/LOOP2),
multiple compare-branch paths, and iterative coefficient computation. They produce
the most complex timbral evolution over time.

## 4. Micro-Program → Synthesis Chain

```
OUTER (micro-program, runs once per ECLK cycle ~200 Hz):
  reg[$1B] indexes into opcodes at reg[$1D-$3C]
  Each opcode operates on R8:R9, reg[$14:$15], reg[$16:$17]
  When reg[$1B] ≥ $3D → finalize_output:
    R10:R11 = reg[$12:$13] × R8:R9  (12×16 multiply)
    envelope_apply with reg[$1A]
    → synthesis_output: set reg[$1E], PRE0, timer params

INNER (micro-op chain, runs at audio sample rate via IRQ4):
  Selected by sub-mode → parameter table at $0101 → reg[$1E]
  Each IRQ4: output DAC sample, compute next, set R5=next step, IRET
  Last step in chain reloads R5 from reg[$1E] to restart
```

The micro-program computes the OUTER pitch envelope (R8:R9).
The INNER playback loop is fixed per sub-mode — it is NOT
programmed by the FREQ block.

### 4.1 Inner Micro-Op Chains

Every micro-op follows the same pattern: **output first, compute next**.
`LD R0, Rxx` writes the DAC (Port 0), then the step computes the
next sample value into R14 or R15. On the next IRQ4, the computed
value gets output.

The parameter table at $0101 provides byte1 = micro-op address,
loaded into reg[$1E] by synthesis_output. The last step in each
chain reloads R5 from reg[$1E] to restart.

**$19 — Init (silence)**
```
$19: R0=#$80, R5=reg[$1E], R7=#$40 → IRET
```
Single step. Outputs silence ($80 = center), resets phase to waveform
table start. Used for unused sub-modes (sub=11-15).

**$20 — 8-step accumulation chain (Mode A sub 0-5)**
```
$20 → $2C → $37 → $44 → $51 → $5E → $6A → $76 → reg[$1E]
```
Complex waveform accumulation with forward/backward table lookups
and interpolation. Each step outputs one DAC sample.

| Step | Addr | DAC | Operation                                            |
|------|------|-----|------------------------------------------------------|
| 1    | $20  | R15 | OR R7,#$40 (force upper half), R15 += R14, RRC       |
| 2    | $2C  | R15 | R15 = R14, R15 += @R7 (indirect add), RRC            |
| 3    | $37  | R14 | INC R7, R14 = @R7 (forward lookup, wrap to reg[$40]) |
| 4    | $44  | R15 | DEC R7, R14 += @R7, RRC, R15 = R14                   |
| 5    | $51  | @R7 | R14 += @R7, RRC, R14 += @R7, RRC (double add)        |
| 6    | $5E  | R14 | R14 = @R7, R14 += R15, RRC, INC R7                   |
| 7    | $6A  | R14 | AND R7,#$3F (wrap), R14 += R15, RRC                  |
| 8    | $76  | R14 | R14 = wave[R7], R14 += R15, RRC. **R5=reg[$1E]**     |

8 DAC samples per cycle. R7 advances through the waveform table with
the step managing its own phase. R14/R15 alternate as interpolation
accumulators.

**$82 — 4-step interpolating DDS (Mode A sub 6)**
```
$82 → $8F → $9B → $A4 → reg[$1E]
```

| Step | Addr | DAC | Operation                                   |
|------|------|-----|---------------------------------------------|
| 1    | $82  | R15 | R7++ & $3F, R15 += R14, RRC                 |
| 2    | $8F  | R15 | R15 = wave[R7], R15 += R14                  |
| 3    | $9B  | R14 | (RRC R15 from step 2 tail), R14 += R15, RRC |
| 4    | $A4  | R14 | R14 = wave[R7]. **R5=reg[$1E]**             |

4 DAC samples per phase advance. Linear interpolation between
adjacent waveform samples using R14/R15 ping-pong.

**$AC — 2-step chain (Mode A sub 7)**
```
$AC → $B8 → reg[$1E]
```

| Step | Addr | DAC | Operation                                            |
|------|------|-----|------------------------------------------------------|
| 1    | $AC  | R15 | R7++ & $3F, R15 = wave[R7]                           |
| 2    | $B8  | R14 | R15 += R14, RRC R15, R14 = wave[R7]. **R5=reg[$1E]** |

2 DAC samples per phase advance. Simpler interpolation than $82.

**$C4 — Single-step DDS (Mode A sub 8-13)**
```
$C4: R0=wave[R7], R5=reg[$1E], R7 += R14, AND R7,#$3F → IRET
```
1 DAC sample per IRQ4. Direct waveform output with constant
frequency step R14. Wraps at 64 entries (AND #$3F).

**$CF — Multi-advance DDS (Mode B sub 0-10)**
```
$CF → possibly $DA → possibly $E6 → $E9
```
Variable-length chain with overflow detection at each step:

| Step | Addr | DAC | Operation                                                 |
|------|------|-----|-----------------------------------------------------------|
| 1    | $CF  | @R7 | R7 += R14. If overflow: R7 -= R15 (**soft sync**), R5=$DA |
| 2    | $DA  | —   | R7 -= $81, R7 += R14. Overflow → R7=$40, R5=$CF           |
| 3    | $E6  | —   | R5=$E9 (just chains forward)                              |
| 4    | $E9  | —   | R7 += R14. Overflow → R7=$40, R5=$CF                      |

Steps 2-4 only execute if no overflow in the previous step.
Step 1 uses **soft sync**: `R7 -= R15` subtracts the period instead
of hard-resetting to 0, preserving phase continuity. Compare with
$F2 which hard-resets (`R7 = #$00`). Can do up to 3 phase advances
per cycle for high-frequency notes.

**$F2 — Hard sync DDS (Default sub 0-10)**
```
$F2: R0=wave[R7], CP R7,R15. If R7 < R15: R7 += R14, IRET.
     If R7 ≥ R15: R7=#$00, R5=#$F2, IRET.
```
1 DAC sample per IRQ4. Hard-resets phase to 0 when R7 reaches R15,
forcing the waveform period to R15 regardless of table length.
This is classic **oscillator hard sync** — the master (R15 from
parameter table) controls the effective period.

### 4.2 Parameter Table Summary

Each sub-mode maps to one chain via the parameter table at $0101:

| Mode    | Sub 0-5         | Sub 6        | Sub 7        | Sub 8-13     | Sub 14-15 |
|---------|-----------------|--------------|--------------|--------------|-----------|
| Default | $F2 (hard sync) | $F2          | $F2          | $F2          | $19 (off) |
| Mode B  | $CF (soft sync) | $CF          | $CF          | $CF          | $19 (off) |
| Mode A  | $20 (8-step)    | $82 (4-step) | $AC (2-step) | $C4 (1-step) | $19 (off) |

Mode A uses progressively simpler chains for higher sub-modes.
The chain length determines how many DAC samples are produced per
phase advance — the phase only advances once per complete cycle:

| Chain | Steps | Samples/period (64-entry table) | Range           |
|-------|-------|---------------------------------|-----------------|
| $20   | 8     | 512                             | Bass (sub 0-5)  |
| $82   | 4     | 256                             | Tenor (sub 6)   |
| $AC   | 2     | 128                             | Alt (sub 7)     |
| $C4   | 1     | 64                              | Sopran (sub 8+) |

At the same timer rate, each doubling in chain length halves the
pitch (one octave lower). This avoids needing extremely slow timer
rates for bass notes.

### 4.3 Digital Oversampling via Interpolation

The multi-step chains are effectively **oversampling with linear
interpolation**. The intermediate steps compute midpoints between
adjacent waveform table entries using ADD+RRC (add then rotate right
through carry = average). R14 and R15 ping-pong as current/previous
sample accumulators.

For the 8-step chain, 8 interpolated values are produced between
each pair of waveform entries — a triangular (linear interpolation)
filter kernel applied in software. This smooths the staircase DAC
output without needing an analog reconstruction filter.

The analog path confirms this: the DAC 0832 with T_OUT (ILE) is a
simple zero-order hold — no dedicated reconstruction filter on the
PCB. The firmware does all the smoothing digitally. MAME emulation
should use the DAC output as-is without additional filtering.

## 5. LFSR Pitch Modulation Source ($0B94)

The `synth_lfsr_noise` function is a **16-bit Galois LFSR pseudo-random
generator** with a built-in envelope sample-and-hold mechanism. It serves as the
primary pitch modulation source for most instruments (12 of 20 Voice1 programs).

All micro-program functions (LFSR, COEFF, LMUL, comparisons, etc.) compute
the **pitch envelope** in R8:R9. The finalize_output stage then multiplies
reg[$12:$13] (base frequency from sram[$FC:$FD]) × R8:R9 to produce the
final pitch that sets the DDS timer. So R8:R9 is a pitch modifier, not an
amplitude control.

### LFSR State

- **reg[$11]**: LFSR high byte (persistent, seeded from sram[$FB] at setup)
- **@R10+3** (micro-program byte 3): LFSR low byte (stored in FREQ data)
- **reg[$1C]**: down-counter (reloaded from FREQ byte 3)

### Algorithm

Three paths based on counter state:

1. **Counter overflow (MI)** — full 16-bit LFSR step:
   ```
   RLC @R10      ; shift low byte left
   RLC $11       ; shift high byte left, carry chain
   if carry:
     XOR @R10, #$87   ; feedback polynomial low
     XOR $11, #$1D    ; feedback polynomial high
   ```
   Polynomial: **$1D87** (taps at bits 0,1,2,7,8,10,11,12).
   Also reads pitch envelope from Port 1 → reg[$1A] and triggers IRQ1.

2. **Counter positive, non-zero** — 8-bit LFSR step:
   ```
   RLC $11       ; shift high byte left
   if carry:
     XOR $11, #$1D   ; 8-bit feedback
   ```
   Polynomial: **$1D** (taps at bits 0,2,3,4).

3. **Counter zero** — advance past 4-byte operand, return to interpreter.

### Dual Purpose: Pitch Envelope S&H

On counter overflow, the function also performs **envelope S&H**:
- `OR IRQ, #$02` — triggers IRQ1 (pitch reload via T0/IRQ4 setup)
- Reads Port 1 → reg[$1A] (pitch envelope value)
- Reads Port 3 → extracts EXSLA routing bits into reg[$12]
- Double-checks RARC bus state before reading (avoids bus conflict)

This means the LFSR counter simultaneously controls:
- How often the PRNG does a full 16-bit step
- How often the pitch envelope is sampled from the bus

### Output — Differenced Noise

After the LFSR step, the output is coefficient-multiplied and **differenced**:
```
product = (reg[$11] × FREQ[2]) >> 8    ; 8×8 multiply
product -= FREQ[2]/2                    ; center around zero (bipolar)
delta = product - running_state         ; first-order difference (high-pass)
running_state = product                 ; update stored state (FREQ[1])
R8:R9 += sign_extend(delta) << 4       ; accumulate pitch modulation
```

The first-order difference acts as a **high-pass filter** on the noise,
suppressing DC and emphasizing transients. This creates pitch variation
that changes rapidly but doesn't drift.

### Musical Purpose

At ~200 Hz ECLK rate, the LFSR creates **random pitch modulation** —
the same effect as detuning in a string ensemble or synthesizer chorus.
Each ECLK cycle slightly shifts the DDS frequency, creating sidebands
around each harmonic. This is what gives string patches their chorused,
ensemble character.

The coefficient (FREQ[2]) controls modulation depth:
- Small coefficient → subtle detuning (strings, ensemble)
- Large coefficient → dramatic pitch variation (synth effects)

### Related LFSR Functions

- **LMUL_P16 ($0C41)**: 8-bit LFSR step + multiply → reg[$16:$17]
- **LMUL_P14 ($0C73)**: 8-bit LFSR step + multiply → reg[$14:$15]

These are simpler: always do one 8-bit LFSR step, no counter, no envelope S&H.
They target secondary/tertiary accumulator pairs for multi-stage pitch
computation. They share the same reg[$11] LFSR state register.

## 6. Known Issues

**R14 initialization gap (Mode A sub<9):**
Instruments using sub=8 with simple micro-programs (ENSEMB, and DRAWB
at C#5+) produce silence because R14 (the DDS frequency step) is never
initialized. The micro-program cannot set R14 — it's in the inner
pipeline register domain, inaccessible to outer functions.

R14 should be loaded from slave_ram[$F7] (as Default mode and Mode B do),
but Mode A sub<9 skips this initialization at $0576.

See `WIP_distortion_investigation.md` for full analysis.

**Waveform length vs DDS wrap (LIKELY BUG):**
WAVE blocks contain 4 sub-waveforms: Bass=64, Tenor=64, Alt=32, Sopran=16
samples. The $C4 DDS wraps at 64 (`AND R7, #$3F`), which is correct for
Bass/Tenor but wrong for Alt/Sopran. The LDEI chain always loads 64 bytes
from sram[$F6], so for Alt/Sopran the DDS reads overflow data from the
next WAVE section (Sopran data + FixFmt garbage).

Shorter waveforms at higher octaves are a timer requirement: the Z8 must
execute the DDS micro-op before the next timer interrupt. Fewer samples
per period = slower timer = feasible for the MCU.

The $F2 micro-op (`CP R7, R15` = programmable wrap) and $CF (`SUB R7, R15`)
can wrap at 32 or 16. The synthesis output parameter table should use these
for Alt/Sopran sub-modes. Currently all sub≥8 use $C4 (AND #$3F = always
64) — likely a bug or bit errors in the table.

## 7. Voice2 Micro-Programs

Multi-voice instruments chain ICBs. Voice2 FREQ blocks are at indices
20-43. Common pattern: Voice2 uses a variant of Voice1's program with
different gain constants or additional modulation:

| V2 blk | Name   | Based on   | Difference                            |
|--------|--------|------------|---------------------------------------|
| 20     | PC-MED | DRAWB(0)   | Different R8:R9 gain ($2B01 vs $3701) |
| 21     | PIANO2 | PIANO1(1)  | Different threshold ($3F00 vs $4000)  |
| 34     | BRASS* | HUMAN(15)  | Same complex program (shared)         |
| 38     | ENSEM1 | ENSEMB(14) | Longer: LOOP1 → COEFF → LOOP2 chain   |
| 39     | HUMAN" | HUMAN(15)  | Identical to HUMAN                    |

## Appendix A: synthesis_output Path Analysis

How reg[$1E] (the micro-op chain address) gets set, and how the
parameter table is read. This is the critical handoff between the
OUTER pitch envelope computation and the INNER DDS chain.

### Parameter table read via LDC @RR12

`synthesis_output` at $0E62 reads 3 bytes from the ROM parameter
table at $0101 using `LDC R11, @RR12` with `INC R13` between reads:

```
R12 = $01 (fixed, set at $0E65)
R13 = sub*3 + mode_offset*3 + 1 (computed by dac_output_setup)

1st LDC: ROM[$01:R13]   → byte0 (timer param, compared with $40)
         INC R13
2nd LDC: ROM[$01:R13]   → byte1 (micro-op chain address)
         ... (stored to reg[$1E] at .synthesis_output_common)
         INC R13
3rd LDC: ROM[$01:R13]   → byte2 (PRE0, stored to $F5)
```

Everything depends on R13 being computed correctly by `dac_output_setup`.
If R13 is wrong, all three bytes come from the wrong table entry.

### R13 computation (dac_output_setup at $0DF8)

```
R13 = reg[$10] & $0F              ; sub-mode (0-15)
R12 = reg[$10] & $30              ; mode bits (0/$10/$20)
R13 += R12                        ; R13 = sub + mode_offset
R12 = 1 + R13                     ; save R13+1
R13 = R13*2                       ; RL R13
R13 += R12                        ; R13 = R13*2 + R13 + 1 = R13*3 + 1
```

This gives R13 = entry_index × 3 + 1, pointing to byte0 of the entry.
The +1 accounts for $0100 being the IRET of micro_dds_f2, not table data.

### Four paths through synthesis_output

After byte0 is read, the code splits based on two conditions:
- byte0 vs $40: **upper** (≥$40) or **lower** (<$40)
- reg[$1E] vs $C4: whether to do **DI** (atomic IRQ4 handoff) or not

```
                    byte0 ≥ $40          byte0 < $40
                   (upper path)         (lower path)
                 ┌──────────────┐    ┌──────────────┐
reg[$1E] < $C4   │ Path 1       │    │ Path 3b (DI) │
                 │ R11=byte1    │    │ R14=byte0    │
                 │ no DI, no R5 │    │ R11=byte1    │
                 │              │    │ R5=byte1     │
                 └──────┬───────┘    └──────┬───────┘
reg[$1E] ≥ $C4   │ Path 2 (DI)  │    │ Path 3a      │
                 │ R11=byte1    │    │ R14=byte0    │
                 │ R5=byte1     │    │ R11=byte1    │
                 │ R14=R15=wave │    │ no DI, no R5 │
                 └──────┬───────┘    └──────┬───────┘
                        │                   │
                        └───────┬───────────┘
                                ▼
                  .synthesis_output_common:
                    LD reg[$1E], R11     ; micro-op chain addr
                    EI
                    LD PRE0, byte2       ; timer prescaler
                    LD reg[$1D], R10     ; timer freq
                    LD R6, R10           ; timer reload
```

**All paths**: R11 = byte1 from the parameter table → reg[$1E].

**DI paths** (reg[$1E] ≥ $C4): also set R5 = byte1 immediately and
preload R14 (lower) or R14+R15 (upper). This is an atomic handoff —
DI prevents IRQ4 from firing while the chain address AND the waveform
registers are being updated together.

**Non-DI paths** (reg[$1E] < $C4): only update reg[$1E]. The running
micro-op chain reads reg[$1E] at its last step (`LD R5, 1Eh`) to
pick up the new value naturally. No atomicity needed because the
multi-step chains ($20/$82/$AC) manage their own R14/R15.

### What determines R13 (table index)

R13 can arrive at synthesis_output from two sources:

**Normal path — from reg[$10] (= sram[$FA], the mode register):**
```
dac_output_setup ($0DF8):
  R13 = reg[$10] & $0F              ; sub-mode (0-15)
  R12 = reg[$10] & $30              ; mode bits ($00/$10/$20)
  R13 += R12
  R13 = R13*3 + 1                   ; table byte0 offset
```
The master writes sram[$FA] during voice setup. Bits 3:0 = sub-mode,
bits 5:4 = synthesis mode (Default/B/A). This is the steady-state path.

**$F2 override — hardcoded $2E:**
```
.f2_special_check ($0E19):
  if reg[$1E] == $F2 AND ROM[$01:$01] × 2 ≥ R15:
    R13 = #$2E      ; → Mode B sub=15 entry
```
When the $F2 (hard sync) DDS detects a wrap condition, it forces
R13 = $2E, overriding the normal table index. This points to
Mode B sub=15 (the entry we fixed from $99→$19). This may be how
the $F2 DDS transitions to a different chain on wrap events.

Note: `synthesis_loop_reentry` between dac_output_setup and
synthesis_output only modifies R12 (bus reads), never R13.
So R13 is preserved across the ECLK polling loop.

### Why the $0E69 LDE→LDC patch was critical

With `LDE` (slave RAM read) instead of `LDC` (ROM read) at $0E69,
byte0 came from whatever was on the data bus — not from the ROM
parameter table. This made the upper/lower path split random, and
byte1/byte2 reads were offset by one position (since R13 advances
via INC). The entire chain selection, timer, and prescaler were
corrupted. This was the root cause of the C#5 distortion.
