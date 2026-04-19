#!/usr/bin/env python3
"""
Convert WERSI_DAC_TRACE binary to VCD (Value Change Dump) for GTKWave.

Reads the binary trace from wersi_slm2 and produces a VCD file with:
  - dac_write[7:0]   : R0 value on every DAC write (Port 0 store)
  - dac_latch[7:0]   : latched DAC value on T_OUT edge (actual output sample)
  - pc[11:0]         : Z8 program counter at each event
  - raud             : RAUD pulse (voice command trigger)
  - port2[7:0]       : Port 2 state (RARC, routing)
  - bank[7:0]        : slave RAM bank
  - exsla[1:0]       : EXSLA pitch bank select
  - eclk             : envelope clock

Usage:
    python3 dac_trace_to_vcd.py dac.bin -o dac.vcd
    gtkwave dac.vcd

The timescale is 1 Z8 machine cycle = 1/(clock/2) = 1/6MHz ≈ 166.7ns.
With 12 MHz crystal: 1 cycle = 166ns, so timescale 1ns gives ~6 cycles/μs.
"""
import argparse
import struct
import sys


def read_trace(path):
    """Read binary trace file, yield tuples.

    16-byte records (D/L/R/U): (cycles, pc, event, value, port0, port2, bank, flags, 0,0,0,0, 0,0,0,0)
    24-byte records (S):       (cycles, pc, event, value, port0, port2, bank, flags, r13,r14,r10,opc, r5,r7,mode,r15)
    """
    with open(path, 'rb') as f:
        hdr = f.read(8)
        if hdr[:4] != b'WDAC':
            print(f"ERROR: not a WDAC trace file (got {hdr[:4]})", file=sys.stderr)
            sys.exit(1)

        while True:
            rec = f.read(16)
            if len(rec) < 16:
                break
            cycles = struct.unpack_from('<Q', rec, 0)[0]
            pc = struct.unpack_from('<H', rec, 8)[0]
            event = chr(rec[10])
            value = rec[11]
            port0 = rec[12]
            port2 = rec[13]
            bank = rec[14]
            flags = rec[15]
            ext = (0, 0, 0, 0)
            ext2 = (0, 0, 0, 0)
            ext3 = (0, 0, 0, 0)
            if event == 'S':
                ext_bytes = f.read(12)
                if len(ext_bytes) < 12:
                    break
                ext = (ext_bytes[0], ext_bytes[1], ext_bytes[2], ext_bytes[3])
                ext2 = (ext_bytes[4], ext_bytes[5], ext_bytes[6], ext_bytes[7])
                ext3 = (ext_bytes[8], ext_bytes[9], ext_bytes[10], ext_bytes[11])
            elif event == 'E':
                ext_bytes = f.read(8)
                if len(ext_bytes) < 8:
                    break
                ext = (ext_bytes[0], ext_bytes[1], ext_bytes[2], ext_bytes[3])
                ext2 = (ext_bytes[4], ext_bytes[5], ext_bytes[6], ext_bytes[7])
            yield cycles, pc, event, value, port0, port2, bank, flags, *ext, *ext2, *ext3


def write_vcd(trace_path, output_path, clock_mhz=12.0):
    """Convert trace to VCD."""
    # Collect all events first to get time range
    events = list(read_trace(trace_path))
    if not events:
        print("No events in trace", file=sys.stderr)
        return

    cycle_ns = 1000.0 / (clock_mhz / 2)  # ns per machine cycle

    with open(output_path, 'w') as f:
        # VCD header
        f.write("$timescale 1ns $end\n")
        f.write("$scope module slm2 $end\n")
        f.write("$var wire 8 d dac_write [7:0] $end\n")
        f.write("$var wire 8 l dac_latch [7:0] $end\n")
        f.write("$var wire 12 p pc [11:0] $end\n")
        f.write("$var wire 1 r raud $end\n")
        f.write("$var wire 8 2 port2 [7:0] $end\n")
        f.write("$var wire 8 b bank [7:0] $end\n")
        f.write("$var wire 2 x exsla [1:0] $end\n")
        f.write("$var wire 1 e eclk $end\n")
        f.write("$upscope $end\n")
        f.write("$scope module synth $end\n")
        f.write("$var wire 8 P pitch [7:0] $end\n")       # reg[$1D] timer reload
        f.write("$var wire 8 B prog_ctr [7:0] $end\n")    # reg[$1B] micro-program counter
        f.write("$var wire 8 O micro_op [7:0] $end\n")    # reg[$1E] micro-op chain address
        f.write("$var wire 8 A acc_hi [7:0] $end\n")      # R8 = ACC high byte
        f.write("$var wire 8 a acc_lo [7:0] $end\n")      # R9 = ACC low byte
        f.write("$var wire 16 W acc_16 [15:0] $end\n")      # R8:R9 as 16-bit (view as signed in GTKWave)
        f.write("$var wire 8 T r13_ptr [7:0] $end\n")     # R13 = table pointer
        f.write("$var wire 8 S phase_step [7:0] $end\n")  # R14 = phase step
        f.write("$var wire 8 F r10 [7:0] $end\n")         # R10 = finalize result
        f.write("$var wire 8 Q opcode [7:0] $end\n")      # current opcode at $1B
        f.write("$var wire 8 5 r5 [7:0] $end\n")          # R5 = micro-op dispatch target
        f.write("$var wire 8 7 r7 [7:0] $end\n")          # R7 = phase / waveform index
        f.write("$var wire 8 m mode [7:0] $end\n")         # reg[$10] = mode byte
        f.write("$var wire 32 Z eff_pitch [31:0] $end\n")  # effective pitch period (ext clocks per waveform cycle)
        f.write("$upscope $end\n")
        f.write("$scope module errors $end\n")
        f.write("$var wire 1 u unmap_hit $end\n")          # 1 when unmapped reg read
        f.write("$var wire 8 U unmap_addr [7:0] $end\n")   # unmapped address (0x80+)
        f.write("$upscope $end\n")
        f.write("$scope module reg1e $end\n")
        f.write("$var wire 1 c r1e_chg $end\n")            # 1-pulse when reg[$1E] changes
        f.write("$var wire 8 n r1e_new [7:0] $end\n")      # new value
        f.write("$var wire 8 o r1e_old [7:0] $end\n")      # old value
        f.write("$var wire 12 w r1e_writer [11:0] $end\n")  # PC that wrote it (pc-0)
        f.write("$var wire 12 v pc_m1 [11:0] $end\n")      # PC trace: age 1 (previous)
        f.write("$var wire 12 y pc_m2 [11:0] $end\n")      # PC trace: age 2
        f.write("$var wire 12 z pc_m3 [11:0] $end\n")      # PC trace: age 3
        f.write("$upscope $end\n")
        f.write("$enddefinitions $end\n")

        # Initial values
        f.write("#0\n")
        f.write("b10000000 d\n")  # DAC = $80 (silence)
        f.write("b10000000 l\n")
        f.write("b000000000000 p\n")
        f.write("0r\n")
        f.write("b00000000 2\n")
        f.write("b00000000 b\n")
        f.write("b00 x\n")
        f.write("0e\n")
        f.write("b00000000 5\n")
        f.write("b00000000 7\n")
        f.write("b00000000 m\n")
        f.write("0u\n")
        f.write("b00000000 U\n")

        f.write("0c\n")
        f.write("b00000000 n\n")
        f.write("b00000000 o\n")
        f.write("b000000000000 w\n")

        prev_time_ns = -1
        unmap_clear_time = -1
        r1e_clear_time = -1
        raud_clear_time = -1

        for cycles, pc, event, value, port0, port2, bank, flags, ext0, ext1, ext2, ext3, ext4, ext5, ext6, ext7, ext8, ext9, ext10, ext11 in events:
            time_ns = int(cycles * cycle_ns)

            # Clear reg[$1E] change pulse after 200ns
            if r1e_clear_time >= 0 and time_ns >= r1e_clear_time:
                if time_ns != prev_time_ns:
                    f.write(f"#{time_ns}\n")
                    prev_time_ns = time_ns
                f.write("0c\n")
                r1e_clear_time = -1

            # Clear unmapped-read pulse after 200ns
            if unmap_clear_time >= 0 and time_ns >= unmap_clear_time:
                if time_ns != prev_time_ns:
                    f.write(f"#{time_ns}\n")
                    prev_time_ns = time_ns
                f.write("0u\n")
                unmap_clear_time = -1

            # Clear RAUD pulse after 500ns
            if raud_clear_time >= 0 and time_ns >= raud_clear_time:
                if time_ns != prev_time_ns:
                    f.write(f"#{time_ns}\n")
                    prev_time_ns = time_ns
                f.write("0r\n")
                raud_clear_time = -1

            if time_ns != prev_time_ns:
                f.write(f"#{time_ns}\n")
                prev_time_ns = time_ns

            exsla = flags & 0x03
            eclk = (flags >> 2) & 1

            # Always update PC and port state
            f.write(f"b{pc:012b} p\n")
            f.write(f"b{port2:08b} 2\n")
            f.write(f"b{bank:08b} b\n")
            f.write(f"b{exsla:02b} x\n")
            f.write(f"{eclk}e\n")

            if event == 'D':
                f.write(f"b{value:08b} d\n")
            elif event == 'L':
                f.write(f"b{value:08b} l\n")
            elif event == 'R':
                f.write("1r\n")
                raud_clear_time = time_ns + 500  # pulse width
            elif event == 'S':
                # 28-byte synthesis event
                f.write(f"b{value:08b} P\n")   # reg[$1D] (TIMER_VAL)
                f.write(f"b{port0:08b} B\n")   # reg[$1B] (prog counter)
                f.write(f"b{port2:08b} O\n")   # reg[$1E] (micro-op)
                f.write(f"b{bank:08b} A\n")    # R8 (acc_hi)
                f.write(f"b{flags:08b} a\n")   # R9 (acc_lo)
                acc16 = (bank << 8) | flags
                f.write(f"b{acc16:016b} W\n")  # R8:R9 as signed 16-bit
                f.write(f"b{ext0:08b} T\n")    # R13 (table pointer)
                f.write(f"b{ext1:08b} S\n")    # R14 (phase step)
                f.write(f"b{ext2:08b} F\n")    # R10 (finalize result)
                f.write(f"b{ext3:08b} Q\n")    # opcode at $1B
                f.write(f"b{ext4:08b} 5\n")    # R5 (micro-op dispatch)
                f.write(f"b{ext5:08b} 7\n")    # R7 (phase / waveform idx)
                f.write(f"b{ext6:08b} m\n")    # reg[$10] (mode byte)
                # ext8=SPH(dither), ext9=PRE0, ext10=R6(TIMER_LOAD)
                sph = ext8
                pre0 = ext9
                timer_val = value  # reg[$1D]
                micro_op = port2   # reg[$1E]
                phase_step = ext1  # R14
                # Compute effective pitch period (ext clocks per waveform cycle)
                prescaler = (pre0 >> 2) if (pre0 >> 2) else 64
                dither_bits = bin(sph).count('1')
                # Timer with dither: (timer_val * 8 + dither_bits) / 8
                timer_eff_x8 = timer_val * 8 + dither_bits
                # Chain length: IRQ4s per phase step
                chain_map = {0x20: 8, 0x82: 4, 0xAC: 2, 0xC4: 1, 0xCF: 1, 0xF2: 1, 0x19: 1}
                chain_len = chain_map.get(micro_op, 1)
                # Phase increment per cycle
                phase_inc = 1 if micro_op < 0xC4 else (phase_step if phase_step > 0 else 1)
                # Period per waveform cycle (64 phase steps):
                # = timer_eff × prescaler × 4 × chain_len × 64 / phase_inc / 8
                # Simplify: × prescaler × 4 × chain_len × 8 / phase_inc (keeping ×8 from dither)
                eff_pitch = timer_eff_x8 * prescaler * 4 * chain_len * 64 // (phase_inc * 8)
                eff_pitch = min(eff_pitch, 0xFFFFFFFF)  # clamp to 32-bit
                f.write(f"b{eff_pitch:032b} Z\n")
            elif event == 'U':
                # Unmapped register read: value=addr, port0=R5, port2=reg1E, bank=R7, flags=mode
                f.write("1u\n")
                f.write(f"b{value:08b} U\n")   # unmapped address
                f.write(f"b{port0:08b} 5\n")   # R5 at crash
                f.write(f"b{bank:08b} 7\n")    # R7 at crash
                unmap_clear_time = time_ns + 200  # pulse width
            elif event == 'E':
                # reg[$1E] change: value=new, port0=old, port2=R5, bank=R7, flags=mode
                # ext0-ext3 = prev PC age1 (lo), age1 (hi), age2 (lo), age2 (hi)
                # ext4-ext7 = prev PC age3 (lo), age3 (hi), age4 (lo), age4 (hi)
                pc_m1 = ext0 | (ext1 << 8)
                pc_m2 = ext2 | (ext3 << 8)
                pc_m3 = ext4 | (ext5 << 8)
                f.write("1c\n")
                f.write(f"b{value:08b} n\n")      # new reg[$1E]
                f.write(f"b{port0:08b} o\n")      # old reg[$1E]
                f.write(f"b{pc:012b} w\n")         # PC that wrote it (age 0)
                f.write(f"b{pc_m1:012b} v\n")      # age 1
                f.write(f"b{pc_m2:012b} y\n")      # age 2
                f.write(f"b{pc_m3:012b} z\n")      # age 3
                f.write(f"b{port2:08b} 5\n")       # R5 at time of write
                f.write(f"b{bank:08b} 7\n")        # R7 at time of write
                r1e_clear_time = time_ns + 200     # pulse width

    print(f"Wrote {len(events)} events to {output_path}")
    print(f"Time range: {events[0][0]}-{events[-1][0]} cycles "
          f"({events[-1][0] * cycle_ns / 1e6:.1f} ms)")
    print(f"Open: gtkwave {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description='Convert WERSI DAC trace to VCD for GTKWave')
    parser.add_argument('trace', help='Binary trace file (from WERSI_DAC_TRACE)')
    parser.add_argument('-o', '--output', default=None,
                        help='Output VCD file (default: <trace>.vcd)')
    parser.add_argument('--clock', type=float, default=12.0,
                        help='Z8 crystal frequency in MHz (default: 12)')
    args = parser.parse_args()

    output = args.output or args.trace.rsplit('.', 1)[0] + '.vcd'
    write_vcd(args.trace, output, args.clock)


if __name__ == '__main__':
    main()
