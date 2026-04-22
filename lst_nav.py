#!/usr/bin/env python3
"""
Navigate the Wersi SLM-2 firmware.lst file by address or label.

Usage:
    python3 lst_nav.py 0x0F92          # show function at address
    python3 lst_nav.py $0F92           # same, dollar-sign prefix
    python3 lst_nav.py normalize       # search labels for 'normalize'
    python3 lst_nav.py --list          # list all labels with addresses
    python3 lst_nav.py --list norm     # list matching labels
    python3 lst_nav.py 0x0F92 -n 20   # show 20 lines of context
    python3 lst_nav.py 0x0F92 --full  # show until next section boundary
"""

import re
import sys
import os

DEFAULT_LST = os.path.join(os.path.dirname(__file__), "firmware.lst")

# Regex for lst body lines:  <lineno>/ <addr> : <bytes>  <content>
LINE_RE = re.compile(r'^\s*(\d+)/\s*([0-9A-Fa-f]+)\s*:\s*(.*)')

# ORG directive marks a new section
ORG_RE = re.compile(r'\bORG\b')

# Section header banners
SECTION_RE = re.compile(r'; ={3,}')


def parse_lst(path):
    """
    Parse firmware.lst into:
      lines: list of (lineno, addr_or_None, raw_text)
      labels: dict {name_lower: line_index}
      addr_to_line: dict {addr: first_line_index}
    """
    lines = []
    labels = {}
    addr_to_line = {}

    with open(path, encoding="latin-1") as f:
        for raw in f:
            raw = raw.rstrip("\n")
            m = LINE_RE.match(raw)
            if m:
                lineno = int(m.group(1))
                addr = int(m.group(2), 16)
                content = m.group(3)
                lines.append((lineno, addr, raw))
                if addr not in addr_to_line:
                    addr_to_line[addr] = len(lines) - 1
                # Detect label lines: content ends with ":" (possibly with comment)
                label_m = re.match(r'^(\S[\w.\-_/]*):(\s*;.*)?$', content.strip())
                if label_m:
                    label = label_m.group(1).lower()
                    labels[label] = len(lines) - 1
            else:
                lines.append((None, None, raw))

    return lines, labels, addr_to_line


def find_section_end(lines, start_idx):
    """
    Return the line index of the next section boundary after start_idx.
    A section boundary is an ORG directive or section banner ('; ===') that
    is at a different address than the starting line.
    Returns len(lines) if none found.
    """
    _, start_addr, _ = lines[start_idx]
    seen_new_addr = False
    for i in range(start_idx + 1, len(lines)):
        _, addr, text = lines[i]
        if addr is not None and addr != start_addr:
            seen_new_addr = True
        if seen_new_addr and (ORG_RE.search(text) or SECTION_RE.search(text)):
            return i
    return len(lines)


def print_lines(lines, start_idx, count=None, full=False):
    """Print lines from start_idx. If full, print until section end."""
    if full:
        end_idx = find_section_end(lines, start_idx)
        # For very large sections, cap at 120 lines and warn
        if end_idx - start_idx > 120:
            end_idx = start_idx + 120
            truncated = True
        else:
            truncated = False
        for i in range(start_idx, end_idx):
            print(lines[i][2])
        if truncated:
            print(f"  [...truncated — use -n <count> for more]")
    else:
        n = count or 30
        for i in range(start_idx, min(start_idx + n, len(lines))):
            print(lines[i][2])


def search_labels(labels, query):
    """Return list of (name, line_idx) for labels matching query (substring)."""
    q = query.lower()
    return [(name, idx) for name, idx in sorted(labels.items()) if q in name]


def parse_address(arg):
    """Parse an address argument: 0xNNNN, $NNNN, NNNN (hex). Returns int or None."""
    arg = arg.strip()
    if arg.startswith('$'):
        arg = arg[1:]
    if arg.startswith('0x') or arg.startswith('0X'):
        arg = arg[2:]
    try:
        val = int(arg, 16)
        if 0 <= val <= 0xFFFF:
            return val
    except ValueError:
        pass
    return None


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Navigate firmware.lst")
    parser.add_argument("query", nargs="?", help="Address (0xNNNN / $NNNN) or label substring")
    parser.add_argument("-n", "--lines", type=int, default=None, help="Number of lines to show (default: 30)")
    parser.add_argument("--full", action="store_true", help="Show until next section boundary")
    parser.add_argument("--list", "-l", action="store_true", help="List labels (optionally filtered)")
    parser.add_argument("--refs", "-r", action="store_true", help="Show all references to a label with context")
    parser.add_argument("--lst", default=DEFAULT_LST, help=f"Path to firmware.lst (default: {DEFAULT_LST})")
    args = parser.parse_args()

    if not os.path.exists(args.lst):
        print(f"ERROR: {args.lst} not found. Run: cd wersi-slm2-51173 && make firmware.lst", file=sys.stderr)
        sys.exit(1)

    lines, labels, addr_to_line = parse_lst(args.lst)

    if args.list:
        query = args.query or ""
        matches = search_labels(labels, query)
        if not matches:
            print(f"No labels matching '{query}'")
        else:
            for name, idx in matches:
                _, addr, _ = lines[idx]
                addr_str = f"${addr:04X}" if addr is not None else "     "
                print(f"  {addr_str}  {name}")
        return

    if args.refs:
        if not args.query:
            print("Usage: lst_nav.py --refs <label>")
            return
        # Find the label
        matches = search_labels(labels, args.query)
        if not matches:
            print(f"No labels matching '{args.query}'")
            return
        # Use first match (or exact match if available)
        target_name = matches[0][0]
        for name, idx in matches:
            if name == args.query.lower():
                target_name = name
                break
        # Search all lines for references to this label
        ctx = args.lines or 5
        found = 0
        for i, (lineno, addr, text) in enumerate(lines):
            if target_name in text.lower() and i != labels.get(target_name):
                # Check if this is a jump/call TO the label (not the label definition itself)
                if any(kw in text for kw in ['JP ', 'JR ', 'CALL ', 'DJNZ ']):
                    found += 1
                    print(f"--- ref #{found} at ${addr:04X} ---" if addr else f"--- ref #{found} ---")
                    start = max(0, i - ctx)
                    end = min(len(lines), i + ctx + 1)
                    for j in range(start, end):
                        marker = ">>>" if j == i else "   "
                        print(f"{marker} {lines[j][2]}")
                    print()
        if found == 0:
            print(f"No references to '{target_name}' found")
        return

    if not args.query:
        parser.print_help()
        return

    # Try to parse as address first
    addr = parse_address(args.query)
    if addr is not None:
        # Find exact or nearest address
        if addr in addr_to_line:
            start_idx = addr_to_line[addr]
            # Back up to include any preceding label or comment on same address
            while start_idx > 0:
                _, prev_addr, _ = lines[start_idx - 1]
                if prev_addr == addr:
                    start_idx -= 1
                else:
                    break
            print_lines(lines, start_idx, count=args.lines, full=args.full)
        else:
            # Find nearest address in range
            all_addrs = sorted(addr_to_line.keys())
            near = [a for a in all_addrs if a <= addr]
            if near:
                best = near[-1]
                start_idx = addr_to_line[best]
                print(f"[no exact match for ${addr:04X}, showing ${best:04X}]")
                while start_idx > 0 and lines[start_idx - 1][1] == best:
                    start_idx -= 1
                print_lines(lines, start_idx, count=args.lines, full=args.full)
            else:
                print(f"Address ${addr:04X} not found in {args.lst}")
        return

    # Try as label search
    matches = search_labels(labels, args.query)
    if not matches:
        print(f"No labels matching '{args.query}'")
        return

    if len(matches) == 1:
        name, start_idx = matches[0]
        while start_idx > 0 and lines[start_idx - 1][1] == lines[start_idx][1]:
            start_idx -= 1
        print_lines(lines, start_idx, count=args.lines, full=args.full)
    else:
        # Multiple matches: show a menu
        print(f"Multiple matches for '{args.query}':")
        for i, (name, idx) in enumerate(matches[:20]):
            _, addr, _ = lines[idx]
            addr_str = f"${addr:04X}" if addr is not None else "     "
            print(f"  {i+1:2d}.  {addr_str}  {name}")
        if len(matches) > 20:
            print(f"  ... and {len(matches) - 20} more")
        print()
        try:
            choice = input("Choose [1]: ").strip()
            n = int(choice) - 1 if choice else 0
            if 0 <= n < len(matches):
                name, start_idx = matches[n]
                while start_idx > 0 and lines[start_idx - 1][1] == lines[start_idx][1]:
                    start_idx -= 1
                print_lines(lines, start_idx, count=args.lines, full=args.full)
        except (EOFError, ValueError, KeyboardInterrupt):
            pass


if __name__ == "__main__":
    main()
