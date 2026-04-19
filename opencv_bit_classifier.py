#!/usr/bin/env python3
"""
OpenCV-based bit classifier for mask ROM error detection.

Replicates maskromtool's Wide sampler with correct Z86x1 left/right row
split (D0-D3 use left-half row lines, D4-D7 use right-half row lines).
Uses local template matching to identify bits where the image disagrees
with the current extraction.

Usage:
    python3 opencv_bit_classifier.py [--top N] [--k-neighbors K]
"""

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np
from scipy.spatial import cKDTree


# === Z86x1 Decoder Logic ===

def bitswap8(val, b7, b6, b5, b4, b3, b2, b1, b0):
    return (
        (((val >> b7) & 1) << 7) | (((val >> b6) & 1) << 6) |
        (((val >> b5) & 1) << 5) | (((val >> b4) & 1) << 4) |
        (((val >> b3) & 1) << 3) | (((val >> b2) & 1) << 2) |
        (((val >> b1) & 1) << 1) | (((val >> b0) & 1) << 0)
    )


def intersect_lines(c, r):
    """Compute (x, y) at intersection of column and row lines."""
    rdx, rdy = r['x2'] - r['x1'], r['y2'] - r['y1']
    cdx, cdy = c['x2'] - c['x1'], c['y2'] - c['y1']
    denom = rdx * cdy - rdy * cdx
    if abs(denom) < 1e-6:
        return (c['x1'] + c['x2']) / 2, (r['y1'] + r['y2']) / 2
    dx, dy = c['x1'] - r['x1'], c['y1'] - r['y1']
    t = (dx * cdy - dy * cdx) / denom
    return r['x1'] + t * rdx, r['y1'] + t * rdy


def sample_wide(red_ch, x, y, size=12):
    """Wide sampler: darkest pixel in horizontal strip."""
    ix, iy = int(round(x)), int(round(y))
    h, w = red_ch.shape
    x0 = max(0, ix - size // 2)
    x1 = min(w, ix + size // 2)
    if iy < 0 or iy >= h or x0 >= x1:
        return 255, None
    strip = red_ch[iy, x0:x1].astype(np.float32)
    return float(strip.min()), strip


def ncc(a, b):
    """Normalized cross-correlation."""
    if a.shape != b.shape or a.size == 0:
        return 0.0
    a_f = a.flatten().astype(np.float64)
    b_f = b.flatten().astype(np.float64)
    a_n = a_f - a_f.mean()
    b_n = b_f - b_f.mean()
    denom = np.sqrt(np.sum(a_n ** 2) * np.sum(b_n ** 2))
    if denom < 1e-10:
        return 0.0
    return float(np.sum(a_n * b_n) / denom)


def extract_patch(red_ch, x, y, half_w=8, half_h=6):
    """Extract a patch for template matching (wider than sampler to capture arch context)."""
    ix, iy = int(round(x)), int(round(y))
    h, w = red_ch.shape
    y0, y1 = iy - half_h, iy + half_h
    x0, x1 = ix - half_w, ix + half_w
    if y0 < 0 or x0 < 0 or y1 >= h or x1 >= w:
        return None
    return red_ch[y0:y1, x0:x1].astype(np.float32)


# === Main ===

def main():
    parser = argparse.ArgumentParser(description='OpenCV bit classifier for mask ROM')
    parser.add_argument('--image', default='rom10x.bmp')
    parser.add_argument('--project', default='rom10x.bmp.json')
    parser.add_argument('--certainty', default=None)
    parser.add_argument('--rom', default='rom.bin')
    parser.add_argument('--top', type=int, default=50)
    parser.add_argument('--cert-threshold', type=int, default=15)
    parser.add_argument('--use-agreement', action='store_true',
                        help='Use image-ROM agreement for training instead of certainty threshold')
    parser.add_argument('--k-neighbors', type=int, default=15)
    parser.add_argument('--sampler-size', type=int, default=12)
    parser.add_argument('--patch-half-w', type=int, default=8)
    parser.add_argument('--patch-half-h', type=int, default=6)
    parser.add_argument('--red-threshold', type=float, default=203.0)
    parser.add_argument('--coord-scale', type=float, default=1.0,
                        help='Scale factor for grid coords (2.0 for 2x upscaled image)')
    parser.add_argument('--test-rom-offset', type=int, default=64)
    args = parser.parse_args()

    cert_path = args.certainty
    if cert_path is None:
        for p in ['bit_certainty.bin', '../maskromtool/build/bit_certainty.bin',
                   str(Path.home() / 'bastel/wersi-ex20/maskromtool/build/bit_certainty.bin')]:
            if Path(p).exists():
                cert_path = p
                break

    print(f"Loading image: {args.image}")
    img_bgr = cv2.imread(args.image, cv2.IMREAD_COLOR)
    if img_bgr is None:
        sys.exit(f"ERROR: Cannot load {args.image}")
    red_ch = img_bgr[:, :, 2]
    print(f"  Image: {img_bgr.shape[1]}×{img_bgr.shape[0]}")

    print(f"Loading project: {args.project}")
    with open(args.project) as f:
        project = json.load(f)

    cert_data = open(cert_path, 'rb').read() if cert_path else None
    rom_data = open(args.rom, 'rb').read()

    # Parse grid: separate left/right row lines
    all_rows = sorted(project['rows'], key=lambda r: (r['y1'] + r['y2']) / 2)
    cols = sorted(project['cols'], key=lambda c: (c['x1'] + c['x2']) / 2)

    left_rows = sorted(
        [r for r in all_rows if (r['x1'] + r['x2']) / 2 < 2000],
        key=lambda r: (r['y1'] + r['y2']) / 2)
    right_rows = sorted(
        [r for r in all_rows if (r['x1'] + r['x2']) / 2 >= 2000],
        key=lambda r: (r['y1'] + r['y2']) / 2)

    print(f"  Grid: {len(cols)} cols, {len(left_rows)} left rows, {len(right_rows)} right rows")
    n_output_rows = min(len(left_rows), len(right_rows))

    # Z86x1 word order
    colcount = 32
    wordorder = [0] * colcount
    for i in range(colcount):
        wordorder[bitswap8(i ^ 0x1e, 7, 6, 5, 0, 1, 2, 3, 4)] = i

    # Build all bit data with correct positions
    print("Sampling all bits...")
    bit_data = []

    for row_idx in range(n_output_rows):
        rowi = row_idx
        if not (row_idx & 2):
            rowi ^= 1
        if rowi >= n_output_rows:
            rowi = row_idx

        lr = left_rows[rowi]
        rr = right_rows[rowi]

        for word in range(colcount):
            adr = row_idx * colcount + word

            for bit in range(4):
                wordi = wordorder[word]
                coli = bit * 32 + wordi
                if coli >= len(cols):
                    continue
                x, y = intersect_lines(cols[coli], lr)
                x *= args.coord_scale
                y *= args.coord_scale
                min_red, strip = sample_wide(red_ch, x, y, args.sampler_size)
                img_bit = 1 if min_red < args.red_threshold else 0
                rom_bit = ((rom_data[adr] >> bit) & 1) if adr < len(rom_data) else 0
                patch = extract_patch(red_ch, x, y, args.patch_half_w, args.patch_half_h)

                cert = 0
                if cert_data:
                    co = adr * 8 + bit
                    if co < len(cert_data):
                        cert = cert_data[co]

                bit_data.append({
                    'adr': adr, 'bit_num': bit, 'mask': 1 << bit,
                    'x': x, 'y': y,
                    'rom_bit': rom_bit, 'img_bit': img_bit,
                    'min_red': min_red, 'cert': cert, 'patch': patch,
                    'row_idx': row_idx, 'rowi': rowi, 'coli': coli,
                })

            for bit in range(4, 8):
                wordi = wordorder[(colcount - 1) - word]
                coli = bit * 32 + wordi
                if coli >= len(cols):
                    continue
                x, y = intersect_lines(cols[coli], rr)
                x *= args.coord_scale
                y *= args.coord_scale
                min_red, strip = sample_wide(red_ch, x, y, args.sampler_size)
                img_bit = 1 if min_red < args.red_threshold else 0
                rom_bit = ((rom_data[adr] >> bit) & 1) if adr < len(rom_data) else 0
                patch = extract_patch(red_ch, x, y, args.patch_half_w, args.patch_half_h)

                cert = 0
                if cert_data:
                    co = adr * 8 + bit
                    if co < len(cert_data):
                        cert = cert_data[co]

                bit_data.append({
                    'adr': adr, 'bit_num': bit, 'mask': 1 << bit,
                    'x': x, 'y': y,
                    'rom_bit': rom_bit, 'img_bit': img_bit,
                    'min_red': min_red, 'cert': cert, 'patch': patch,
                    'row_idx': row_idx, 'rowi': rowi, 'coli': coli,
                })

    print(f"  Sampled: {len(bit_data)} bits")
    agree = sum(1 for b in bit_data if b['rom_bit'] == b['img_bit'])
    print(f"  Image vs ROM: {agree}/{len(bit_data)} match ({100*agree/len(bit_data):.1f}%)")

    # Build KD-tree
    print("Building spatial index...")
    xy = np.array([(b['x'], b['y']) for b in bit_data])
    tree = cKDTree(xy)
    valid_patches = sum(1 for b in bit_data if b['patch'] is not None)
    print(f"  Valid patches: {valid_patches}/{len(bit_data)}")

    # Local template matching
    if args.use_agreement:
        train_label = "img==rom agreement"
        n_train = sum(1 for b in bit_data if b['img_bit'] == b['rom_bit'])
        print(f"Training on image-ROM agreement: {n_train}/{len(bit_data)} bits")
    else:
        train_label = f"cert>={args.cert_threshold}"
    print(f"Running local template matching (k={args.k_neighbors}, training={train_label})...")
    results = []

    for i, bd in enumerate(bit_data):
        if bd['patch'] is None:
            continue

        _, indices = tree.query([bd['x'], bd['y']], k=args.k_neighbors * 8)

        n0, n1 = [], []
        for idx in indices:
            if idx == i:
                continue
            nb = bit_data[idx]
            if nb['patch'] is None:
                continue
            # Training filter: use agreement or certainty
            if args.use_agreement:
                if nb['img_bit'] != nb['rom_bit']:
                    continue
            else:
                if nb['cert'] < args.cert_threshold:
                    continue
            if nb['patch'].shape != bd['patch'].shape:
                continue
            if nb['rom_bit'] == 0 and len(n0) < args.k_neighbors:
                n0.append(idx)
            elif nb['rom_bit'] == 1 and len(n1) < args.k_neighbors:
                n1.append(idx)
            if len(n0) >= args.k_neighbors and len(n1) >= args.k_neighbors:
                break

        if len(n0) < 3 or len(n1) < 3:
            continue

        tmpl_0 = np.mean([bit_data[j]['patch'] for j in n0], axis=0)
        tmpl_1 = np.mean([bit_data[j]['patch'] for j in n1], axis=0)

        s0 = ncc(bd['patch'], tmpl_0)
        s1 = ncc(bd['patch'], tmpl_1)
        predicted = 1 if s1 > s0 else 0
        confidence = abs(s1 - s0)
        disagree = (predicted != bd['rom_bit'])

        display_adr = bd['adr'] - args.test_rom_offset
        score = confidence * (1.0 - bd['cert'] / 255.0) if disagree else 0.0

        results.append({
            'idx': i, 'adr': bd['adr'], 'display_adr': display_adr,
            'bit_num': bd['bit_num'], 'current': bd['rom_bit'],
            'predicted': predicted, 'confidence': confidence,
            'ncc_0': s0, 'ncc_1': s1, 'cert': bd['cert'],
            'min_red': bd['min_red'], 'disagree': disagree, 'score': score,
            'row_idx': bd['row_idx'], 'coli': bd['coli'],
            'x': bd['x'], 'y': bd['y'],
        })

        if (i + 1) % 10000 == 0:
            print(f"  {i+1}/{len(bit_data)}...")

    n_disagree = sum(1 for r in results if r['disagree'])
    print(f"\nResults: {len(results)} classified, {n_disagree} disagreements")

    high_cert = [r for r in results if r['cert'] >= args.cert_threshold]
    if high_cert:
        correct = sum(1 for r in high_cert if not r['disagree'])
        print(f"Sanity: {correct}/{len(high_cert)} high-cert bits agree "
              f"({100*correct/len(high_cert):.2f}%)")

    disagrees = sorted([r for r in results if r['disagree']],
                       key=lambda r: r['score'], reverse=True)

    print(f"\n{'='*105}")
    print(f"Top {args.top} likely bit errors:")
    print(f"{'='*105}")
    print(f"{'#':>3} {'Addr':>8} {'Bit':>3} {'Cur':>3}{'→':>1}{'Pred':>3} "
          f"{'Conf':>6} {'Cert':>4} {'Score':>6} {'NCC0':>6} {'NCC1':>6} "
          f"{'MinR':>5} {'Row':>4} {'Col':>4}")
    print(f"{'-'*105}")

    for rank, r in enumerate(disagrees[:args.top], 1):
        adr_str = f"${r['display_adr']:04X}" if r['display_adr'] >= 0 else f"T${r['adr']:04X}"
        print(f"{rank:3d} {adr_str:>8}.{r['bit_num']} {r['current']:>3d} → {r['predicted']:<3d}"
              f" {r['confidence']:6.3f} {r['cert']:4d} {r['score']:6.3f}"
              f" {r['ncc_0']:6.3f} {r['ncc_1']:6.3f}"
              f" {r['min_red']:5.0f} {r['row_idx']:4d} c{r['coli']:03d}")

    h2l = sum(1 for r in disagrees if r['current'] == 1)
    l2h = sum(1 for r in disagrees if r['current'] == 0)
    print(f"\n--- Statistics ---")
    print(f"Disagreements: {len(disagrees)} ({h2l} stuck-HIGH, {l2h} stuck-LOW)")

    patches_to_check = [
        (0x015F, 7, "bit 7 stuck-HIGH (param table)"),
        (0x09C9, 5, "bit 5 stuck-HIGH (LOOP1 INC)"),
        (0x05A0, 5, "bit 5 stuck-HIGH (Type-B dispatch)"),
        (0x057F, 3, "bit 3 stuck-HIGH (CLR $18)"),
        (0x0F3D, 7, "bit 7 stuck-LOW  TCM $12,#$00→#$80: fix overflow path EXSLA attenuation"),
        (0x0A97, 5, "bit 5 stuck-HIGH synth_wavetable_acc: JP MI,$0CEC→$0CCC, use full multiply not phase2"),
        # (0x0E69, 6, "bit 6 stuck-LOW (LDE→LDC)"),
    ]
    print(f"\n--- Known Patch Validation ---")
    for patch_adr, patch_bit, desc in patches_to_check:
        rom_adr = patch_adr + args.test_rom_offset
        found = [r for r in results if r['adr'] == rom_adr and r['bit_num'] == patch_bit]
        if found:
            r = found[0]
            rank_pos = next((i + 1 for i, d in enumerate(disagrees)
                           if d['adr'] == rom_adr and d['bit_num'] == patch_bit), None)
            if r['disagree']:
                print(f"  ${patch_adr:04X}.{patch_bit}: FLAGGED rank #{rank_pos} "
                      f"(score={r['score']:.3f}, cert={r['cert']}) — {desc}")
            else:
                print(f"  ${patch_adr:04X}.{patch_bit}: agrees "
                      f"(cert={r['cert']}, minR={r['min_red']:.0f}, "
                      f"ncc0={r['ncc_0']:.3f}, ncc1={r['ncc_1']:.3f}) — {desc}")
        else:
            print(f"  ${patch_adr:04X}.{patch_bit}: not found — {desc}")

    csv_path = 'bit_classifier_results.csv'
    with open(csv_path, 'w') as f:
        f.write('rom_addr,bit,current,predicted,confidence,certainty,score,'
                'ncc_0,ncc_1,min_red,disagree,row,col,x,y\n')
        for r in sorted(results, key=lambda r: r.get('score', 0), reverse=True):
            f.write(f"0x{r['display_adr']:04X},{r['bit_num']},{r['current']},"
                    f"{r['predicted']},{r['confidence']:.4f},{r['cert']},"
                    f"{r.get('score', 0):.4f},{r['ncc_0']:.4f},{r['ncc_1']:.4f},"
                    f"{r['min_red']:.0f},{int(r['disagree'])},"
                    f"{r['row_idx']},{r['coli']},"
                    f"{r['x']:.1f},{r['y']:.1f}\n")
    print(f"\nFull results written to {csv_path}")

    # Write maskromtool-compatible JSON bitfixes for disagreements
    import json as json_mod
    disagree_fixes = []
    for r in disagrees:
        disagree_fixes.append({
            "ambiguous": True,
            "value": bool(r['predicted']),
            "x": bit_data[r['idx']]['x'],
            "y": bit_data[r['idx']]['y'],
        })
    fixes_path = 'classifier_bitfixes.json'
    with open(fixes_path, 'w') as f:
        json_mod.dump(disagree_fixes, f, indent=2)
        f.write('\n')
    print(f"Maskromtool bitfixes written to {fixes_path}")

    # Write ASCII candidate list with pixel coordinates
    rom_patched = open('patched_roms/sr0106_patched.bin', 'rb').read() if Path('patched_roms/sr0106_patched.bin').exists() else rom_data
    known_patches = {
        (0x015F, 7): "PATCHED: param table",
        (0x09C9, 5): "PATCHED: LOOP1 INC",
        (0x05A0, 5): "PATCHED: Type-B dispatch",
        (0x057F, 3): "PATCHED: CLR $18",
        #(0x0E69, 6): "PATCHED: LDE→LDC",
        (0x0A97, 5): "PATCHED: wavetable_acc JP",
        (0x0F3D, 7): "PATCHED: overflow EXSLA",
    }
    cand_path = 'classifier_candidates.txt'
    with open(cand_path, 'w') as f:
        f.write("OpenCV Bit Classifier — Candidates for Maskromtool Inspection\n")
        f.write("Generated: 2026-04-12\n")
        f.write("Method: local template matching (k=15, cert>=15)\n")
        f.write(f"Image vs ROM: {100*agree/len(bit_data):.1f}% | "
                f"Sanity: {100*sum(1 for r in high_cert if not r['disagree'])/len(high_cert):.2f}%\n")
        f.write("\n")
        f.write("=" * 90 + "\n")
        f.write("CLASSIFIER DISAGREEMENTS — RANKED BY SCORE\n")
        f.write("=" * 90 + "\n")
        f.write(f"{'Rank':>5}  {'Addr':>6}  Bit  Cert  {'ROM':>4}  {'Flip':>5}  "
                f"{'Score':>6}  {'x':>7}  {'y':>7}  Note\n")
        f.write("-" * 90 + "\n")
        for rank, r in enumerate(disagrees, 1):
            addr = r['display_adr']
            bit_n = r['bit_num']
            cert = r['cert']
            bd = bit_data[r['idx']]
            rom_byte = rom_patched[addr] if 0 <= addr < len(rom_patched) else 0
            flip_byte = rom_byte ^ (1 << bit_n)
            direction = "H→L" if r['current'] == 1 else "L→H"
            note = known_patches.get((addr, bit_n), "")
            f.write(f"{rank:5d}  ${addr:04X}  b{bit_n}   {cert:3d}  "
                    f"${rom_byte:02X}   ${flip_byte:02X}  "
                    f"{r['score']:6.3f}  {bd['x']:7.1f}  {bd['y']:7.1f}  "
                    f"{direction} {note}\n")
        f.write(f"\nTotal: {len(disagrees)} disagreements\n")
    print(f"Candidate list written to {cand_path}")


if __name__ == '__main__':
    main()
