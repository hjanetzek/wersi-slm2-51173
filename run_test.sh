#!/bin/bash
# Run slm2test verification tests end-to-end.
#
# Generates synthetic capture or replays EX-20 captures, runs MAME slm2test,
# converts DAC trace to VCD.
#
# Usage:
#   ./run_test.sh c4_dds                  # single synthetic test
#   ./run_test.sh c4_dds interp_82        # multiple synthetic tests
#   ./run_test.sh all                     # all synthetic tests
#   ./run_test.sh --list                  # list available synthetic tests
#   ./run_test.sh c4_dds --gtkwave        # open result in GTKWave
#   ./run_test.sh c4_dds --no-run         # generate capture only (no MAME)
#   ./run_test.sh --capture path/to.bin   # replay EX-20 capture file
#   ./run_test.sh --capture path/to.bin --slot 3   # filter to slot 3
#
# Output goes to tmp/test_<name>/:
#   capture.bin   — slave RAM events (synthetic or copied)
#   output.wav    — audio output
#   dac.bin       — binary DAC trace
#   dac.vcd       — VCD for GTKWave

set -e
cd "$(dirname "$0")"

MAME=~/bastel/mame/mame/mamemuse
TMPDIR=tmp
OPEN_GTKWAVE=0
NO_RUN=0
CAPTURE_FILE=""
SLOT_FILTER=""
TESTS=()

# Parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --list|-l)
            python3 generate_test_capture.py --list
            exit 0
            ;;
        --gtkwave|-g)
            OPEN_GTKWAVE=1
            shift
            ;;
        --no-run|-n)
            NO_RUN=1
            shift
            ;;
        --capture|-c)
            CAPTURE_FILE="$2"
            shift 2
            ;;
        --slot|-s)
            SLOT_FILTER="$2"
            shift 2
            ;;
        --help|-h)
            echo "Usage: $0 [options] <test> [test2 ...]"
            echo "       $0 --capture <file.bin> [--slot N] [options]"
            echo ""
            echo "Options:"
            echo "  --list, -l            List available synthetic tests"
            echo "  --capture, -c FILE    Replay an EX-20 capture file"
            echo "  --slot, -s N          Filter capture to voice slot N"
            echo "  --gtkwave, -g         Open VCD in GTKWave after run"
            echo "  --no-run, -n          Generate/copy capture only, skip MAME"
            echo ""
            echo "Examples:"
            echo "  $0 c4_dds              Run single synthetic test"
            echo "  $0 all                 Run all synthetic tests"
            echo "  $0 c4_dds -g           Run and open in GTKWave"
            echo "  $0 -c capture_church.bin -g          Replay EX-20 capture"
            echo "  $0 -c capture_church.bin -s 3 -g     Replay slot 3 only"
            exit 0
            ;;
        *)
            TESTS+=("$1")
            shift
            ;;
    esac
done

# Mode 1: Replay existing capture file
if [ -n "$CAPTURE_FILE" ]; then
    if [ ! -f "$CAPTURE_FILE" ]; then
        echo "ERROR: Capture file not found: $CAPTURE_FILE"
        exit 1
    fi
    BASENAME=$(basename "$CAPTURE_FILE" .bin)
    if [ -n "$SLOT_FILTER" ]; then
        OUTDIR="$TMPDIR/replay_${BASENAME}_s${SLOT_FILTER}"
    else
        OUTDIR="$TMPDIR/replay_${BASENAME}"
    fi
    mkdir -p "$OUTDIR"
    cp "$CAPTURE_FILE" "$OUTDIR/capture.bin"

    RECORD_COUNT=$(( $(wc -c < "$OUTDIR/capture.bin") / 272 ))
    echo "=== Replaying EX-20 capture ==="
    echo "  Source: $CAPTURE_FILE ($RECORD_COUNT records)"
    [ -n "$SLOT_FILTER" ] && echo "  Slot filter: $SLOT_FILTER"

    # Show capture summary
    python3 -c "
import struct
with open('$OUTDIR/capture.bin','rb') as f:
    slots = set(); cmds = {}; n = 0
    while True:
        buf = f.read(272)
        if len(buf) < 272: break
        if buf[0:2] != b'RA': break
        slot, cmd = buf[2], buf[6]
        slots.add(slot)
        cs = 'STOP' if cmd & 0x02 else 'SETUP' if cmd & 0x01 else 'PARAM_UPDATE' if cmd & 0x04 else 'UPDATE'
        cmds[cs] = cmds.get(cs, 0) + 1
        n += 1
print(f'  Slots: {sorted(slots)}')
print(f'  Commands: {dict(sorted(cmds.items()))}')
print(f'  Total: {n} records')
"
    if [ "$NO_RUN" -eq 1 ]; then
        echo ""
        echo "Capture ready: $OUTDIR/capture.bin"
        exit 0
    fi
else
    # Mode 2: Generate synthetic capture
    if [ ${#TESTS[@]} -eq 0 ]; then
        echo "No tests specified. Use --list to see available tests, or --capture for EX-20 replays."
        exit 1
    fi

    if [ "${TESTS[0]}" = "all" ]; then
        TESTNAME="all"
    else
        TESTNAME=$(IFS=_; echo "${TESTS[*]}")
    fi
    OUTDIR="$TMPDIR/test_${TESTNAME}"
    mkdir -p "$OUTDIR"

    echo "=== Generating test capture ==="
    python3 generate_test_capture.py --test "${TESTS[@]}" -o "$OUTDIR/capture.bin"

    if [ "$NO_RUN" -eq 1 ]; then
        echo ""
        echo "Capture ready: $OUTDIR/capture.bin"
        echo "Run manually:"
        echo "  WERSI_CAPTURE=$OUTDIR/capture.bin WERSI_DAC_TRACE=$OUTDIR/dac.bin \\"
        echo "    $MAME slm2test -seconds_to_run 30 -wavwrite $OUTDIR/output.wav"
        exit 0
    fi
fi

# Ensure ROM is installed
if [ ! -f ~/bastel/mame/mame/roms/slm2test/sr0106.bin ]; then
    echo ""
    echo "=== Installing patched ROM ==="
    ./build_firmware.sh install
fi

# Run MAME slm2test
# Extract last record's cycle timestamp to compute duration
# Each record: 16-byte header (cycles at offset 8, 8 bytes LE) + 256 data
# Master CPU @ 2 MHz → duration = cycles / 2,000,000
RECORD_COUNT=$(( $(wc -c < "$OUTDIR/capture.bin") / 272 ))
LAST_CYCLES=$(python3 -c "
import struct, sys
with open('$OUTDIR/capture.bin','rb') as f:
    f.seek(-272+8, 2)
    print(struct.unpack('<Q', f.read(8))[0])
")
DURATION=$(( LAST_CYCLES / 2000000 + 2 ))
[ "$DURATION" -lt 3 ] && DURATION=3
echo ""
echo "=== Running slm2test ($RECORD_COUNT records, ${DURATION}s) ==="
SLOT_ENV=""
[ -n "$SLOT_FILTER" ] && SLOT_ENV="WERSI_SLOT=$SLOT_FILTER"
env $SLOT_ENV \
    WERSI_CAPTURE="$OUTDIR/capture.bin" \
    WERSI_DAC_TRACE="$OUTDIR/dac.bin" \
    $MAME slm2test \
    -seconds_to_run "$DURATION" \
    -wavwrite "$OUTDIR/output.wav" \
    -video none -sound none -oslog 2>&1
# | tail -20

# Step 4: Convert DAC trace to VCD
if [ -f "$OUTDIR/dac.bin" ]; then
    echo ""
    echo "=== Converting DAC trace to VCD ==="
    python3 dac_trace_to_vcd.py "$OUTDIR/dac.bin" -o "$OUTDIR/dac.vcd"
else
    echo "WARNING: No DAC trace produced"
fi

# Summary
echo ""
echo "=== Results ==="
echo "  Capture:  $OUTDIR/capture.bin"
[ -f "$OUTDIR/output.wav" ] && echo "  Audio:    $OUTDIR/output.wav"
[ -f "$OUTDIR/dac.bin" ]    && echo "  Trace:    $OUTDIR/dac.bin"
[ -f "$OUTDIR/dac.vcd" ]    && echo "  VCD:      $OUTDIR/dac.vcd"

# Step 5: Optionally open GTKWave
if [ "$OPEN_GTKWAVE" -eq 1 ] && [ -f "$OUTDIR/dac.vcd" ]; then
    echo ""
    echo "Opening GTKWave..."
    gtkwave "$OUTDIR/dac.vcd" &
fi
