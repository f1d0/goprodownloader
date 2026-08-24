#!/usr/bin/env python3
"""
verify_library.py - audit a completed download against the catalog.

Every file is already checked against the byte count the server promised
during download. This is the separate, larger question: does what GoPro's
catalog SAYS each item weighs match what actually landed on disk?

A large shortfall means a lower-quality variant was served instead of the
original, or the item is chaptered and parts are missing.

    python3 tools/verify_library.py --out GoProLibrary

Reads only local files. No token, no network.
"""

import argparse
import json
import os
import sys
from collections import defaultdict


def human(count):
    count = float(count)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(count) < 1024.0:
            return f"{count:.1f}{unit}"
        count /= 1024.0
    return f"{count:.1f}PB"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="GoProLibrary")
    parser.add_argument("--tolerance", type=float, default=0.95,
                        help="Flag an item when on-disk bytes fall below this "
                             "fraction of the catalogued size (default 0.95)")
    args = parser.parse_args()

    catalog_path = os.path.join(args.out, "_library_catalog.json")
    ledger_path = os.path.join(args.out, "_download_ledger.jsonl")
    for path in (catalog_path, ledger_path):
        if not os.path.exists(path):
            print(f"[!] Not found: {path}")
            return 2

    catalog = json.load(open(catalog_path, encoding="utf-8"))

    on_disk = defaultdict(int)
    parts = defaultdict(list)
    missing_files = []
    with open(ledger_path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            media_id = str(record.get("key", "")).split(":")[0]
            path = record.get("path", "")
            if not os.path.exists(path):
                missing_files.append((media_id, path))
                continue
            actual = os.path.getsize(path)
            on_disk[media_id] += actual
            parts[media_id].append((os.path.basename(path), actual))

    short, unverifiable, absent, ok = [], [], [], 0
    catalogued_total = 0
    disk_total = 0

    for item in catalog:
        media_id = item.get("id")
        expected = item.get("file_size")
        actual = on_disk.get(media_id, 0)
        disk_total += actual

        if media_id not in on_disk:
            absent.append(item)
            continue
        if not expected:
            unverifiable.append((item, actual))
            continue
        catalogued_total += expected
        if actual < expected * args.tolerance:
            short.append((item, expected, actual))
        else:
            ok += 1

    print("=" * 66)
    print(f"  Catalogue      : {len(catalog)} items")
    print(f"  Files on disk  : {sum(len(v) for v in parts.values())}")
    print(f"  Bytes on disk  : {human(disk_total)}")
    print("=" * 66)
    print(f"  size matches catalogue    : {ok}")
    print(f"  SMALLER than catalogue    : {len(short)}")
    print(f"  no size in catalogue      : {len(unverifiable)}")
    print(f"  not downloaded at all     : {len(absent)}")
    print(f"  in ledger but gone now    : {len(missing_files)}")

    if short:
        print("\n" + "-" * 66)
        print("ITEMS SMALLER THAN THE CATALOGUE SAYS")
        print("-" * 66)
        for item, expected, actual in sorted(
            short, key=lambda s: s[2] / s[1]
        )[:40]:
            pct = actual * 100 / expected
            print(f"  {item.get('filename')}  ({item.get('type')})")
            print(f"      catalogue {human(expected):>9}   on disk {human(actual):>9}"
                  f"   = {pct:.0f}%   [{len(parts[item['id']])} file(s)]")
        if len(short) > 40:
            print(f"  ... and {len(short) - 40} more")

    if absent:
        print("\n" + "-" * 66)
        print("NOT DOWNLOADED")
        print("-" * 66)
        for item in absent[:20]:
            print(f"  {item.get('filename')}  ({item.get('type')})  id={item.get('id')}")

    if missing_files:
        print("\n" + "-" * 66)
        print("RECORDED AS DOWNLOADED BUT NO LONGER ON DISK")
        print("-" * 66)
        for media_id, path in missing_files[:20]:
            print(f"  {os.path.basename(path)}")

    print()
    if not short and not absent and not missing_files:
        print("Everything matches. Your library is complete.")
        return 0
    print("Re-running the downloader retries anything not downloaded.")
    print("Items merely 'smaller' may be fine -- see the note in the README.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
