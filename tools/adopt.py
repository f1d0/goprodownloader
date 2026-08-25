#!/usr/bin/env python3
"""
adopt.py - add a file you obtained elsewhere into the library.

For media the API will not serve but the GoPro website will: download it in
the browser, then adopt it here so it is named consistently, recorded in the
ledger, and counted by the audit.

    python3 tools/adopt.py --out GoProLibrary --file ~/Downloads/GH016224.MP4

The item is matched by --id, or by the file's own name against the catalogue.
Nothing is moved until you pass --yes.
"""

import argparse
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gopro_downloader as gd  # noqa: E402


def looks_like_media(path):
    """Cheap sanity check: is this actually a video or image?"""
    with open(path, "rb") as handle:
        head = handle.read(12)
    if head[4:8] == b"ftyp":
        return "MP4/MOV container"
    if head[:2] == b"\xff\xd8":
        return "JPEG image"
    if head[:4] == b"\x89PNG":
        return "PNG image"
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="GoProLibrary")
    parser.add_argument("--file", required=True, help="the file to adopt")
    parser.add_argument("--id", default=None, help="media ID, if the name is ambiguous")
    parser.add_argument("--copy", action="store_true",
                        help="copy instead of moving (keeps your original)")
    parser.add_argument("--yes", action="store_true", help="actually do it")
    args = parser.parse_args()

    source = os.path.expanduser(args.file)
    if not os.path.exists(source):
        print(f"[!] Not found: {source}")
        return 2

    catalog_path = os.path.join(args.out, "_library_catalog.json")
    if not os.path.exists(catalog_path):
        print(f"[!] Not found: {catalog_path}")
        return 2
    catalog = json.load(open(catalog_path, encoding="utf-8"))

    if args.id:
        matches = [i for i in catalog if i.get("id") == args.id]
    else:
        base = os.path.basename(source)
        matches = [i for i in catalog if (i.get("filename") or "") == base]

    if not matches:
        print(f"[!] No catalogue entry matches. Pass --id explicitly.")
        return 2
    if len(matches) > 1:
        print(f"[!] {len(matches)} catalogue entries share that filename. Pass --id:")
        for item in matches:
            print(f"      {item['id']}  {item.get('filename')}  "
                  f"{item.get('file_size')}  {item.get('captured_at')}")
        return 2

    item = matches[0]
    size = os.path.getsize(source)
    kind = looks_like_media(source)
    expected = item.get("file_size")

    target = {"part": 1, "suggested_ext": os.path.splitext(source)[1]}
    destination = gd.target_path(os.path.abspath(args.out), item, target, False)

    print(f"  item        : {item.get('filename')}  ({item.get('type')})")
    print(f"  media id    : {item['id']}")
    print(f"  captured    : {item.get('captured_at')}")
    print(f"  your file   : {source}")
    print(f"  size        : {size:,} bytes"
          + (f"   catalogue says {expected:,}" if expected else "   (catalogue has no size)"))
    print(f"  detected as : {kind or 'UNRECOGNISED -- this may not be a media file'}")
    print(f"  destination : {destination}")

    if kind is None:
        print("\n[!] That does not look like a video or image. Not adopting it.")
        return 1
    if expected and size < expected * 0.95:
        print(f"\n[!] Smaller than the catalogue says ({size * 100 // expected}%). "
              "Adopt it anyway only if you know it is complete.")
    if os.path.exists(destination):
        print("\n[!] A file already exists at the destination. Remove it first.")
        return 1

    ledger = gd.Ledger(os.path.join(os.path.abspath(args.out), gd.LEDGER_NAME))
    key = f"{item['id']}:1"
    if key in ledger.done:
        print("\n[!] The ledger already records this item. Clear it with "
              "tools/refetch.py first.")
        return 1

    if not args.yes:
        print("\nDry run. Re-run with --yes to "
              + ("copy" if args.copy else "move") + " it into place.")
        return 0

    os.makedirs(os.path.dirname(destination), exist_ok=True)
    if args.copy:
        shutil.copy2(source, destination)
    else:
        shutil.move(source, destination)
    ledger.record(key, destination, os.path.getsize(destination))

    print(f"\nAdopted. Run tools/verify_library.py to confirm.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
