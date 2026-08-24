#!/usr/bin/env python3
"""
refetch.py - mark items for re-download.

Removes their ledger entries (and optionally their files) so the next
downloader run fetches them again. Use after a fix that changes which assets
get downloaded.

    python3 tools/refetch.py --out GoProLibrary --short          # dry run
    python3 tools/refetch.py --out GoProLibrary --short --yes    # do it

--short selects every item the audit reports as smaller than catalogued.
--ids takes a comma-separated list of media IDs instead.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.verify_library import human, recording_key  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="GoProLibrary")
    parser.add_argument("--short", action="store_true",
                        help="select items smaller than their catalogued size")
    parser.add_argument("--ids", default="", help="comma-separated media IDs")
    parser.add_argument("--keep-files", action="store_true",
                        help="clear the ledger but leave the old files on disk")
    parser.add_argument("--yes", action="store_true", help="actually do it")
    args = parser.parse_args()

    catalog_path = os.path.join(args.out, "_library_catalog.json")
    ledger_path = os.path.join(args.out, "_download_ledger.jsonl")
    for path in (catalog_path, ledger_path):
        if not os.path.exists(path):
            print(f"[!] Not found: {path}")
            return 2

    catalog = json.load(open(catalog_path, encoding="utf-8"))
    records = [json.loads(l) for l in open(ledger_path, encoding="utf-8") if l.strip()]

    on_disk = {}
    for record in records:
        media_id = record["key"].split(":")[0]
        if os.path.exists(record["path"]):
            on_disk[media_id] = on_disk.get(media_id, 0) + os.path.getsize(record["path"])

    wanted = {i.strip() for i in args.ids.split(",") if i.strip()}
    if args.short:
        groups = {}
        for item in catalog:
            key = recording_key(item.get("filename"))
            if key:
                groups.setdefault(key, []).append(item)
        for item in catalog:
            expected = item.get("file_size")
            if not expected or item["id"] not in on_disk:
                continue
            key = recording_key(item.get("filename"))
            members = groups.get(key, [item]) if key else [item]
            if len(members) > 1:
                first = min(members, key=lambda i: i["filename"])
                if item["id"] != first["id"]:
                    continue
                expected = first.get("file_size") or 0
                actual = sum(on_disk.get(m["id"], 0) for m in members)
                if expected and actual < expected * 0.95:
                    wanted.update(m["id"] for m in members)
            elif on_disk[item["id"]] < expected * 0.95:
                wanted.add(item["id"])

    if not wanted:
        print("Nothing selected.")
        return 0

    by_id = {i["id"]: i for i in catalog}
    doomed = [r for r in records if r["key"].split(":")[0] in wanted]
    freed = sum(os.path.getsize(r["path"]) for r in doomed if os.path.exists(r["path"]))

    print(f"{len(wanted)} item(s), {len(doomed)} file(s), {human(freed)} on disk:\n")
    for media_id in sorted(wanted):
        item = by_id.get(media_id, {})
        print(f"  {item.get('filename', media_id):<20} {item.get('type', '?'):<13}"
              f" have {human(on_disk.get(media_id, 0)):>9}"
              f"  catalogue {human(item.get('file_size') or 0):>9}")

    if not args.yes:
        print("\nDry run. Re-run with --yes to clear these from the ledger"
              + ("" if args.keep_files else " and delete their files") + ".")
        return 0

    if not args.keep_files:
        for record in doomed:
            if os.path.exists(record["path"]):
                os.remove(record["path"])

    keep = [r for r in records if r["key"].split(":")[0] not in wanted]
    temporary = ledger_path + ".new"
    with open(temporary, "w", encoding="utf-8") as handle:
        for record in keep:
            handle.write(json.dumps(record) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, ledger_path)

    print(f"\nCleared. Ledger now holds {len(keep)} file(s).")
    print("Re-run the downloader to fetch these again.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
