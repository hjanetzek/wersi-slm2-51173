#!/usr/bin/env python3
"""
Read voice_capture.bin produced by ex20.cpp RAUD capture.

File format: repeating records of 8-byte header + 256-byte slave RAM snapshot.
Header: 'R', slot, bank, exsla0, exsla1, cmd($F8), mode($FA), f4

Usage:
    python3 read_voice_capture.py voice_capture.bin [--slot N] [--dump N]

Options:
    --slot N    Filter to a specific voice slot
    --dump N    Dump full 256 bytes of capture #N (1-based)
    --extract N Save capture #N as voice_NNNN.bin for disasm_microprog.py
"""
import sys
import os

RECORD_V1 = 8 + 256   # old format: 'R', slot, bank, exsla0, exsla1, cmd, mode, f4, data[256]
RECORD_V2 = 16 + 256  # new format: 'R','A', slot, bank, exsla0, exsla1, cmd, mode, cycles[8], data[256]


def read_captures(filename):
    data = open(filename, "rb").read()
    records = []
    pos = 0

    # Detect format from first 2 bytes
    if len(data) >= 2 and data[0] == ord('R') and data[1] == ord('A'):
        record_size = RECORD_V2
        version = 2
    else:
        record_size = RECORD_V1
        version = 1

    while pos + record_size <= len(data):
        hdr = data[pos:pos+record_size-256]
        snapshot = bytearray(data[pos+record_size-256:pos+record_size])

        if hdr[0] != ord('R'):
            pos += 1
            continue

        if version == 2:
            if hdr[1] != ord('A'):
                pos += 1
                continue
            cycles = int.from_bytes(hdr[8:16], 'little')
            records.append({
                "slot": hdr[2],
                "bank": hdr[3],
                "exsla0": hdr[4],
                "exsla1": hdr[5],
                "cmd": hdr[6],
                "mode": hdr[7],
                "f4": snapshot[0xF4],
                "cycles": cycles,
                "data": snapshot,
            })
        else:
            records.append({
                "slot": hdr[1],
                "bank": hdr[2],
                "exsla0": hdr[3],
                "exsla1": hdr[4],
                "cmd": hdr[5],
                "mode": hdr[6],
                "f4": hdr[7],
                "cycles": 0,
                "data": snapshot,
            })
        pos += record_size
    return records


def cmd_str(cmd):
    if cmd & 0x02: return "STOP"
    if cmd & 0x01: return "SETUP"
    if cmd & 0x04: return "PARAM_UPDATE"
    return "UPDATE"


def mode_str(mode):
    if mode & 0x20: return "ModeA"
    if not (mode & 0x10): return "ModeB"
    return "Default"


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} voice_capture.bin [--slot N] [--dump N] [--extract N]")
        sys.exit(1)

    filename = sys.argv[1]
    filter_slot = None
    dump_num = None
    extract_num = None

    i = 2
    while i < len(sys.argv):
        if sys.argv[i] == "--slot" and i + 1 < len(sys.argv):
            filter_slot = int(sys.argv[i+1]); i += 2
        elif sys.argv[i] == "--dump" and i + 1 < len(sys.argv):
            dump_num = int(sys.argv[i+1]); i += 2
        elif sys.argv[i] == "--extract" and i + 1 < len(sys.argv):
            extract_num = int(sys.argv[i+1]); i += 2
        else:
            i += 1

    records = read_captures(filename)
    print(f"Read {len(records)} RAUD captures from {filename}")
    print()

    # Filter
    if filter_slot is not None:
        records = [r for r in records if r["slot"] == filter_slot]
        print(f"Filtered to slot {filter_slot}: {len(records)} records")
        print()

    # Summary
    has_cycles = any(r.get("cycles", 0) != 0 for r in records)
    base_cycles = records[0].get("cycles", 0) if records else 0

    hdr = f"{'#':>4s} {'Slot':>4s} {'Bank':>4s} {'EX':>3s} {'Cmd':>8s} {'Mode':>8s} {'$F4':>4s} {'$FE':>4s} {'$FF':>4s}"
    if has_cycles:
        hdr += f" {'delta_ms':>10s}"
    print(hdr)
    print("-" * (65 if has_cycles else 55))

    prev_cycles = base_cycles
    for i, r in enumerate(records):
        marker = ""
        if cmd_str(r["cmd"]) == "SETUP": marker = " ←"
        elif cmd_str(r["cmd"]) == "STOP": marker = " ×"

        line = (f"{i+1:4d} {r['slot']:4d} {r['bank']:4d}  {r['exsla1']}{r['exsla0']} "
                f"${r['cmd']:02X}({cmd_str(r['cmd']):>6s}) {mode_str(r['mode']):>8s} "
                f"  ${r['f4']:02X}   ${r['data'][0xFE]:02X}   ${r['data'][0xFF]:02X}{marker}")

        if has_cycles:
            cyc = r.get("cycles", 0)
            delta_us = (cyc - prev_cycles) * 0.5  # 2 MHz master = 500ns/cycle
            line += f" {delta_us/1000:10.2f}"
            prev_cycles = cyc

        print(line)

    # Dump specific capture
    if dump_num is not None and 1 <= dump_num <= len(records):
        r = records[dump_num - 1]
        print(f"\n=== Dump of capture #{dump_num} (slot {r['slot']}, {cmd_str(r['cmd'])}) ===")
        for row in range(0, 256, 16):
            hex_str = ' '.join(f'{r["data"][row+j]:02X}' for j in range(16))
            ascii_str = ''.join(chr(b) if 0x20 <= b < 0x7F else '.' for b in r["data"][row:row+16])
            nz = any(r["data"][row+j] != 0 for j in range(16))
            if nz:
                print(f"  ${row:02X}: {hex_str}  |{ascii_str}|")

    # Extract to file
    if extract_num is not None and 1 <= extract_num <= len(records):
        r = records[extract_num - 1]
        outname = f"voice_{r['slot']:02d}_{extract_num:04d}.bin"
        open(outname, "wb").write(bytes(r["data"]))
        print(f"\nExtracted capture #{extract_num} → {outname}")
        print(f"Disassemble: python3 disasm_microprog.py {outname}")


if __name__ == "__main__":
    main()
