#!/usr/bin/env python3
"""
probe_api.py - check that this machine can talk to the GoPro API, and report
what SHAPE the responses have, so the downloader can be adjusted if GoPro has
changed anything.

It prints structure only: key names, types, and list lengths. Every value is
replaced with its type. No filenames, no IDs, no dates, no token -- nothing
identifying comes out, so the output is safe to paste into a chat.

    python probe_api.py --har gopro.com.har
    python probe_api.py --token "eyJhbGciOi..."

Downloads nothing.
"""

import argparse
import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gopro_downloader as gd


def shape(value, depth=0, max_depth=6):
    """Describe a JSON value's structure without revealing its contents."""
    pad = "  " * depth
    if depth > max_depth:
        return "..."
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = ["{"]
        for key in list(value)[:40]:
            lines.append(f"{pad}  {key}: {shape(value[key], depth + 1, max_depth)}")
        if len(value) > 40:
            lines.append(f"{pad}  ... {len(value) - 40} more keys")
        lines.append(pad + "}")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return "[] (empty)"
        return (
            f"[{len(value)} items, first one:]\n"
            f"{pad}  {shape(value[0], depth + 1, max_depth)}"
        )
    if isinstance(value, bool):
        return "<bool>"
    if isinstance(value, (int, float)):
        return "<number>"
    if value is None:
        return "<null>"
    return f"<string, {len(str(value))} chars>"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--har", default="gopro.com.har")
    parser.add_argument("--token", default=None)
    args = parser.parse_args()

    token, user_agent, _ = gd.resolve_token(args)
    client = gd.Client(token, user_agent)

    print("\n" + "=" * 60)
    print("PROBE 1: /media/search  (does library enumeration work?)")
    print("=" * 60)
    query = urllib.parse.urlencode({
        "processing_states": gd.PROCESSING_STATES,
        "fields": "id,filename,file_size,captured_at,created_at,type,file_extension,ready_to_view",
        "order_by": "captured_at", "per_page": 3, "page": 1,
    })
    try:
        payload = client.get_json(f"{gd.API_ROOT}/media/search?{query}")
    except Exception as error:
        print(f"FAILED: {gd.redact(str(error))}")
        return 2

    print(shape(payload))

    media = (payload.get("_embedded") or {}).get("media") or []
    pages = payload.get("_pages") or {}
    print("\nHeadline numbers (safe to share):")
    print(f"  items returned on page 1 : {len(media)}")
    print(f"  total_items reported     : {pages.get('total_items', 'MISSING')}")
    print(f"  total_pages reported     : {pages.get('total_pages', 'MISSING')}")

    if not media:
        print("\nNo media came back -- nothing further to probe.")
        return 1

    print("\n" + "=" * 60)
    print("PROBE 2: /media/{id}/download  (can we get a real file URL?)")
    print("=" * 60)
    try:
        detail = client.get_json(f"{gd.API_ROOT}/media/{media[0]['id']}/download")
    except Exception as error:
        print(f"FAILED: {gd.redact(str(error))}")
        return 2

    print(shape(detail))

    embedded = detail.get("_embedded") or {}
    files = embedded.get("files") or []
    variations = embedded.get("variations") or []
    print("\nHeadline numbers (safe to share):")
    print(f"  _embedded.files count      : {len(files)}")
    print(f"  _embedded.variations count : {len(variations)}")
    print(f"  variation labels           : {[v.get('label') for v in variations]}")

    print("\n" + "=" * 60)
    print("PROBE 3: is the file URL actually fetchable? (first 1 KB only)")
    print("=" * 60)
    try:
        targets = gd.download_targets(client, media[0]["id"], "source")
        with client.open(targets[0]["url"], extra_headers={"Range": "bytes=0-1023"}) as response:
            data = response.read(1024)
        is_mp4 = data[4:8] == b"ftyp"
        is_jpeg = data[:2] == b"\xff\xd8"
        print(f"  HTTP {getattr(response, 'status', '?')}, got {len(data)} bytes")
        print(f"  looks like a real media file: {is_mp4 or is_jpeg}")
        print("\nAll three probes succeeded. The downloader should work as-is.")
    except Exception as error:
        print(f"FAILED: {gd.redact(str(error))}")
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
