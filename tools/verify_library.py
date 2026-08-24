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
import re
import sys
from collections import defaultdict

# GoPro splits a long recording into chapters at roughly 4 GB, and registers
# each chapter as its own library item: GX019086.MP4, GX029086.mp4, ...
# The digits after the two-letter prefix are the chapter number; the last four
# identify the recording. The chapter-01 item's catalogued file_size covers the
# WHOLE recording, so comparing it against just that chapter's bytes reports a
# shortfall that does not exist.
CHAPTER_RE = re.compile(r"^(G[A-Z])(\d{2})(\d{4})\.(?:MP4|mp4)$")


def recording_key(filename):
    """Return (prefix, number) for a chaptered file, else None."""
    match = CHAPTER_RE.match(filename or "")
    return (match.group(1), match.group(3)) if match else None


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

    # Group chaptered recordings so a multi-chapter video is judged as a whole.
    groups = defaultdict(list)
    for item in catalog:
        key = recording_key(item.get("filename"))
        if key:
            groups[key].append(item)

    chaptered = {}       # media_id -> group key, for items in a real group
    for key, members in groups.items():
        if len(members) > 1:
            for item in members:
                chaptered[item["id"]] = key

    short, unverifiable, absent, ok = [], [], [], 0
    by_chapters = []
    disk_total = sum(on_disk.values())
    seen_groups = set()

    for item in catalog:
        media_id = item.get("id")
        expected = item.get("file_size")
        actual = on_disk.get(media_id, 0)

        key = chaptered.get(media_id)
        if key:
            # Judge the whole recording once, on its first chapter.
            if key in seen_groups:
                continue
            seen_groups.add(key)
            members = groups[key]
            first = min(members, key=lambda i: i["filename"])
            total_expected = first.get("file_size") or 0
            total_actual = sum(on_disk.get(m["id"], 0) for m in members)
            gone = [m for m in members if m["id"] not in on_disk]
            if gone:
                absent.extend(gone)
            if not total_expected:
                unverifiable.append((first, total_actual))
            elif total_actual < total_expected * args.tolerance:
                short.append((first, total_expected, total_actual))
            else:
                ok += 1
                if len(members) > 1 and total_actual > (first.get("file_size") or 0) * 0.99:
                    by_chapters.append((first, members, total_actual))
            continue

        if media_id not in on_disk:
            absent.append(item)
            continue
        if not expected:
            unverifiable.append((item, actual))
            continue
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
    if by_chapters:
        print(f"     (of which {len(by_chapters)} are multi-chapter recordings,")
        print(f"      complete once their chapters are summed)")
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
            key = recording_key(item.get("filename"))
            members = groups.get(key, [item]) if key else [item]
            print(f"  {item.get('filename')}  ({item.get('type')})")
            print(f"      catalogue {human(expected):>9}   on disk {human(actual):>9}"
                  f"   = {pct:.0f}%")
            if len(members) > 1:
                for member in sorted(members, key=lambda i: i["filename"]):
                    got = on_disk.get(member["id"], 0)
                    mark = "" if got else "   <-- NOT DOWNLOADED"
                    print(f"        {member['filename']:<18} {human(got):>10}{mark}")
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
