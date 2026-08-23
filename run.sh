#!/usr/bin/env sh
# Convenience launcher. Edit the options below to taste.
cd "$(dirname "$0")" || exit 1
python3 gopro_downloader.py --har gopro.com.har --out GoProLibrary --workers 2 "$@"
