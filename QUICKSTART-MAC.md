# Quickstart — macOS

The short path from a fresh Mac to a downloaded library. Full detail is in
[README.md](README.md).

Have ready: the Mac on **mains power**, Chrome, your GoPro login, and enough
free space for your whole library.

---

## 1. Check space

Open Terminal (`Cmd + Space`, type `Terminal`, Enter):

```bash
df -h /
```

Read the **Avail** column. Not enough? Use an external drive, and check it is
not FAT32 — that format cannot hold a file over 4 GB:

```bash
diskutil info /Volumes/YourDrive | grep "File System Personality"
```

APFS, Mac OS Extended and ExFAT are all fine. MS-DOS (FAT32) is not.

## 2. Check Python

```bash
python3 --version
```

Need 3.8 or newer. If a dialog offers to install developer tools, click
**Install**, wait, then re-run. Nothing else to install.

## 3. Get the code

```bash
cd ~/Desktop
git clone https://github.com/f1d0/goprodownloader.git
cd goprodownloader
```

Keep this Terminal window open — every later command runs here.

## 4. Capture the HAR

Do this immediately before step 5. The token inside expires in hours.

1. In Chrome, sign in at **gopro.com** and open your **Media Library**
2. `Cmd + Option + I` opens DevTools (**not** F12 — that is a brightness key)
3. Click the **Network** tab
4. Tick **Preserve log**
5. `Cmd + R` to reload; wait for thumbnails to appear
6. Click **⬇ Export HAR** in the Network toolbar

   > If offered a choice, pick **"Export HAR (with sensitive data)"**. The
   > *sanitized* option strips the `Authorization` header on purpose, and the
   > tool cannot work without it.

7. Save as `gopro.com.har` and move it into the `goprodownloader` folder

**This file is a password.** It holds your access token and cookies. Do not
sync, email or commit it. Delete it when you are done (step 9).

## 5. Look before you leap

```bash
python3 gopro_downloader.py --list-only
```

Prints your item count, total size and a free-space check. Downloads nothing.

## 6. Test five files

```bash
python3 gopro_downloader.py --limit 5
```

Answer `y`. Open one of the results in QuickTime to confirm it plays.

## 7. Download everything

Charger connected, lid open:

```bash
caffeinate -s python3 gopro_downloader.py --out /Volumes/YourDrive/GoPro --workers 3
```

Omit `--out` to use a `GoProLibrary` folder here instead.

`caffeinate -s` stops the Mac sleeping mid-run and exits when the download
does. It needs mains power, and closing the lid sleeps the machine regardless.

## 8. While it runs

Stop it any time with `Ctrl + C`. Re-run the **same command** to resume —
finished files are skipped and a half-transferred file continues from the byte
it stopped at.

Progress, from a second Terminal window:

```bash
cd ~/Desktop/goprodownloader
wc -l < GoProLibrary/_download_ledger.jsonl
```

If the token expires mid-run, you get a clear message: capture a fresh HAR
(step 4) and re-run. Nothing already downloaded is lost.

## 9. Finish

You want: `Every item in the catalog is present and size-verified.`

```bash
python3 -c "import json;print(len(json.load(open('GoProLibrary/_library_catalog.json'))))"
wc -l < GoProLibrary/_download_ledger.jsonl
```

The second number should be **at least** the first. Higher is normal —
chaptered videos become several files.

Then delete the credential:

```bash
rm gopro.com.har
```

---

## If something goes wrong

| Message | Cause | Fix |
|---|---|---|
| `No access token found in that HAR file` | Sanitized HAR export | Re-export with *"with sensitive data"* |
| `The API rejected the token` | Token expired | Capture a fresh HAR, re-run |
| `Library enumeration failed` | GoPro changed their API | Scroll to the bottom of the library before exporting, then `--from-har-ids` |
| 403s partway through | Going too fast | `--workers 1` |
| Anything else | — | Run `python3 tools/probe_api.py --har gopro.com.har` and share the output; it prints structure only, no private data |
