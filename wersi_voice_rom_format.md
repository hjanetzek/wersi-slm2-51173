# Wersi EX-20 / MK1 Voice ROM Format

References: Wersi documentation "Anhang 0 — Interne Blockverwaltung des MK1/EX20",
mk1utils-3.9.1 (mk1defs.h, mk1imglib.c), Ghidra analysis of mk1_ic3 + IC5 ROM.

## 1. Overview

Each voice bank page is an 8KB region at $4000-$5FFF selected by the VRAMB
register. The DMS ROM (IC5, 16KB) maps as a single page when VRAMB=$30.
CV RAM (IC1, 8KB per page) uses VRAMB=$00 or $50 for different pages.

A voice bank page contains 5 types of data blocks, each indexed by its own
address table. Block addresses 0-63 are valid per table; address 0 = NIL.

## 2. Page Header

```
$4000  FF FF              Magic bytes
$4002  ushort ICB_AA      Offset to ICB address table
$4004  ushort VCF_AA      Offset to VCF address table
$4006  ushort AMPL_AA     Offset to AMPL address table
$4008  ushort FREQ_AA     Offset to FREQ address table
$400A  ushort WAVE_AA     Offset to WAVE address table
```

Each `XX_AA` field is a 16-bit big-endian offset from the page base ($4000).
It points to an array of up to 64 16-bit offsets (one per block).
The address table entries themselves point to the actual block data.

Checksum word at $3FFE (offset from page base): should sum to $0000.

## 3. Address Tables

Each address table is a contiguous array of 16-bit big-endian offsets:

```
table[0]  →  offset of block 0 data
table[1]  →  offset of block 1 data
...
table[N]  →  offset of block N data
```

The number of entries per table varies (up to `ROM_DMS_BANUM` = 64).
The tables are packed sequentially in the page; the firmware determines
the entry count from the gap to the next table or to the first data block.

Example for IC5 DMS ROM (preset bank, VRAMB=$30):

| Table | Offset | Entries | Block size | Data range |
|-------|--------|---------|------------|------------|
| ICB   | $000C  | 44      | 16 bytes   | $41C4-$4483 |
| VCF   | $0194  | 25      | 10 bytes   | $73BE-$74AF |
| AMPL  | $0064  | 64      | 44 bytes   | $4484-$4F83 |
| FREQ  | $00E4  | 44      | 32 bytes   | $4F84-$5503 |
| WAVE  | $013C  | 44      | 177 bytes  | $5504-$73BD |

## 4. Block Types

### 4.1 ICB — Instrument Control Block (16 bytes)

```
Offset  Size  Field
 0      1     next    Next ICB block address (voice chaining, 0=end)
 1      1     VCF     VCF block address (0=NIL)
 2      1     AMPL    AMPL block address (0=NIL)
 3      1     FREQ    FREQ block address (0=NIL)
 4      1     WAVE    WAVE block address (0=NIL)
 5      5     params  Instrument parameters
10      6     name    ASCII instrument name (space-padded)
```

The `next` field chains voices for multi-voice instruments (up to 4 voices).
Block addresses in bytes 1-4 reference blocks within the SAME page or in
another page via the block address mapping (see section 5).

BAOFF_ICB = 1: the first DMS instrument uses ICB block address $01
(block address $00 = ICB list end marker).

### 4.2 VCF — Filter Parameters (10 bytes)

VCF envelope parameters: cutoff frequency, resonance (Güte), envelope shape.
Not used by the Z8 slave — processed by the co-processor (MM1/SLM-50).

### 4.3 AMPL — Amplitude Envelope (44 bytes)

Amplitude envelope parameters. Controls the co-processor's DAC reference
voltage, which modulates the waveform amplitude via the DAC 0832 analog
multiplier. Not directly processed by the Z8.

### 4.4 FREQ — Frequency Envelope + Micro-Program (32 bytes)

Contains the micro-program bytecode and pitch parameters that the master
CPU writes to slave RAM $D4+ during voice setup. The Z8's micro-program
interpreter reads these from the register file at reg[$1D-$3C].

```
Offset  Content
 0      Micro-program bytecode (variable length, zero-padded)
        Interpreted by the Z8 micro-program interpreter at $058E.
        76 possible opcodes dispatched via ROM tables at $01B2/$0192.
```

The FREQ block is loaded to slave RAM by the master during voice setup.
The Z8's `irq3_first_time` handler ($0302) copies from slave RAM at the
offset stored in $F5 into registers $1B+ (micro-program counter area).

When FREQ = NIL ($00), the voice has no frequency envelope and no
micro-program from the FREQ block. The master uses a default/fixed
mode byte. This is the case for DRAWB, CLAVI, FRETLS, ENSEMB, and others.

### 4.5 WAVE — Waveform Data (177 or 212 bytes)

Single-cycle waveform data for wavetable synthesis. Contains 4 sub-waveforms
optimized for different octave registers:

```
Offset  Size  Field
 0      1     flag      bit 7: FixFmt flag, bits 6:1: level
 1      64    bass      Bass waveform (64 samples, 8-bit unsigned)
65      64    tenor     Tenor waveform (64 samples)
129     32    alt       Alt waveform (32 samples)
161     16    sopran    Sopran waveform (16 samples)
177     35    fixfmt    FixFmt data (only if flag bit 7 = 1)
```

Total: 177 bytes (LEN_WAVEREL) for relative formant,
       212 bytes (LEN_WAVEFIX) for fixed formant.

Higher-pitched waveforms are progressively shorter. This is a timer
requirement: at higher pitches the Z8's timer fires faster, and the MCU
must execute the DDS micro-op before the next interrupt. Fewer samples
per waveform period = slower timer rate = feasible computation.

The master selects the waveform variant by
writing slave_ram[$F6] to the appropriate offset at SETUP time, based
on the FREQ block pitch table — NOT based on EXSLA bits.

| Name     | Samples | $F6 offset | WAVE offset |
|----------|---------|------------|-------------|
| Bass     | 64      | $01        | WAVE_BASS_A |
| Tenor    | 64      | $41        | WAVE_TENOR_A |
| Alt      | 32      | $81        | WAVE_ALT_A |
| Sopran   | 16      | $91        | WAVE_SOPRAN_A |

NOTE: EXSLA bits ($80/$A0/$C0/$E0 in SLRAMB) are a separate system —
a 2-bit pitch exponent (÷1/÷2/÷4/÷8 scaler) for the Z8's envelope
multiply. EXSLA changes in real-time during vibrato/pitch bend via
`voice_write_pitch`. Waveform selection ($F6) is set once at SETUP
and does not change. These are independent systems despite the
"Bass/Tenor/Alt/Sopran" naming overlap with the WAVE sub-waveforms.

## 5. Block Address Mapping

Block addresses in ICB fields are 8-bit values with the following ranges:

| Range     | Source | VRAMB | Description |
|-----------|--------|-------|-------------|
| $00       | —      | —     | NIL (no block) / ICB list end |
| $01-$3F   | DMS ROM | $30  | Internal DMS blocks |
| $40       | CV RAM | $00/$50 | Drawbar Voice1 |
| $41-$4A   | CV RAM | $00/$50 | CV Voice1 (10 instruments) |
| $4B-$54   | CV RAM | $00/$50 | Bank CV Voice1 (MK1) |
| $55       | CV RAM | $00/$50 | Drawbar Voice2 (MK1) |
| $56-$5F   | CV RAM | $00/$50 | CV Voice2 (MK1) |
| $60-$69   | CV RAM | $00/$50 | Bank CV Voice2 (MK1) |
| $80-$BF   | Cart ROM | varies | Cartridge DMS blocks |
| $C0-$FF   | Cart ROM | varies | Cartridge CV/Drawbar blocks |

The master firmware's `voice_bank_select` ($A0CF) maps block addresses
to VRAMB settings and data pointers:

- $00-$3F → VRAMB=$30, index via page tables at $4000 base
- $40-$7F → VRAMB=$00 or $50, index via CV table at $FE4A (master ROM)
- $80-$FF → cartridge ROM (same layout, different VRAMB page)

## 6. Waveform Playback Chain

The Z8 slave processor performs wavetable synthesis:

Note: `copy_to_slave_ram` ($A0C3) does WORD copies (PULU D / STD ,X++),
so the count parameter A = number of 16-bit words, not bytes.

```
Master side (voice_cmd_0_setup + voice_cmd_1_setup_finish):
  1. voice_write_pitch: select SLRAMB, write pitch exponent to sram[$FF]
  2. write_wave_block_to_slave: A=$6A → 212 bytes from WAVE block → sram[$00-$D3]
     (flag + bass64 + tenor64 + alt32 + sopran16 + fixfmt35 = all octave waveforms)
  3. write_freq_block_to_slave: A=$10 → 32 bytes from FREQ block → sram[$D4-$F3]
     (micro-program bytecode + parameters)
  4. Set sram[$F4] = $1F (LDEI chain length: 32 copies)
  --- cmd_0 done, chains to cmd_1 ---
  5. voice_cmd_1_setup_finish: write sram[$F5-$FE] (pitch/mode/freq/envelope)
  6. Trigger RAUD → Z8 IRQ3

Z8 side (synthesis chain):
  5. irq3_full_setup: load params from slave RAM $F9-$FF → registers
  6. synthesis_mode_a sub<9 ($0576):
     LDEI chain copies 64 bytes from sram[$F6] → reg[$40-$7F]
     → Loads the selected octave waveform into the register file
  7. micro_program_interpreter ($058E):
     Runs FREQ block micro-program (outer synthesis computation)
  8. synthesis_output ($0E62):
     Sets reg[$1E] = micro-op chain address, PRE0, timer params
  9. IRQ4 micro-op chain (inner loop at audio sample rate):
     $C4 DDS: LD R0, 40h(R7); ADD R7, R14; AND R7, #$3F
     → Scans through the 64-entry waveform table cyclically
     R14 = frequency step (from slave_ram[$F7])
     Timer rate = pitch (from $FC:$FD via 12×16 multiply)
```

## 7. DMS Instrument Inventory (IC5 Preset Bank)

20 DMS instruments, each with 2-4 voice ICBs chained:

| # | Name   | V1 FREQ | V1 WAVE | V2 name | Notes |
|---|--------|---------|---------|---------|-------|
| 1 | DRAWB  | NIL     | $40(DB) | PC-MED  | Drawbar organ, no FREQ block |
| 2 | PIANO1 | $01     | $01     | PIANO2  | |
| 3 | MARIMB | $02     | $02     | MARIMB  | |
| 4 | US-STG | $03     | $03     | STAGE%  | 3 voices |
| 5 | STAGE  | $04     | $04     | STAGE   | 3 voices |
| 6 | CLAVI  | $05     | $05     | CLAVIN  | |
| 7 | FRETLS | $06     | $06     | FRETL/  | |
| 8 | SLAPBS | $07     | $07     | SLAPBS  | |
| 9 | ACGUIT | $08     | $08     | ACGUIT  | |
|10 | LEAD G | $09     | $09     | LEAD'G  | 3 voices |
|11 | POLYSN | $0A     | $0A     | POLYS+  | |
|12 | BRASS  | $0B     | $0B     | BRASS*  | |
|13 | DREAM  | $0C     | $0C     | STRING  | 3 voices |
|14 | STRING | $0D     | $0D     | CELLO   | |
|15 | ENSEMB | $0E     | $0E     | ENSEM1  | |
|16 | HUMAN  | $0F     | $0F     | HUMAN"  | |
|17 | CHURCH | $10     | $10     | FULL    | |
|18 | BIGBEN | $11     | $11     | BELLS   | |
|19 | MOOGLY | $12     | $12     | MOOGLY  | |
|20 | MT-ORG | $13     | $13     | MT-ORG  | |

All Voice1 block numbers are $01-$14 (internal DMS, no cartridge).
VCF = AMPL = FREQ = WAVE for each voice (same block number).

## 8. Tools

- `mk1utils-3.9.1/mk1parse` — parses DMS ROM, exports WAV/SysEx/image
  Usage: `mk1parse <rom.bin> int [wavout] [hexdump] [imgout: <file>]`
- `firmware_py/snd_programs.ipynb` — Jupyter notebook with voice ROM analysis
- `disasm_microprog.py` — micro-program bytecode disassembler
