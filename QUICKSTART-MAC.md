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

## 4. Get your access token

Recent Chrome versions only offer **"Export HAR (sanitized)"**, which strips the
`Authorization` header on purpose — a sanitized HAR can never work. Copying the
token straight out of the cookie jar is more reliable and quicker anyway.

Do this immediately before step 5; the token expires in hours.

1. In Chrome, sign in at **gopro.com** and open your **Media Library**
2. `Cmd + Option + I` opens DevTools (**not** F12 — that is a brightness key)
3. Click the **Application** tab (it may be hidden behind the **»** chevron)
4. Left sidebar → **Storage** → **Cookies** → **https://gopro.com**
5. Click the row named **`gp_access_token`**
6. The full value appears in the panel at the bottom — select it all and copy.
   (Or double-click the **Value** cell, then `Cmd + A`, `Cmd + C`.)

It starts `eyJhbGciOiJSU0EtT0FFUCI...`, runs to roughly 1,270 characters and
contains four dots. Copy **all** of it.

Then, in Terminal in the `goprodownloader` folder:

```bash
read -rs GOPRO_TOKEN && export GOPRO_TOKEN
```

Press Enter, paste the token, press Enter again. Nothing appears on screen —
that is deliberate, and keeps the token out of your shell history.

**This token is a password.** It only lives in that Terminal window, so closing
the window discards it. Every command below assumes that window.

### If you would rather use a HAR file

Still supported, and it means you never handle the token yourself — but only if
your Chrome can produce an unsanitized export. Right-click the request list and
look for **"Save all as HAR with content"** or **"Export HAR (with sensitive
data)"**. Save it as `gopro.com.har` in the `goprodownloader` folder and add
`--har gopro.com.har` to the commands below. If the only option you get is
*sanitized*, use the cookie method above instead.

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

If the token expires mid-run — likely on a long run — you get a clear message.
Re-do step 4 to get a fresh token and re-run. Nothing already downloaded is lost.

## 9. Finish

You want: `Every item in the catalog is present and size-verified.`

```bash
python3 -c "import json;print(len(json.load(open('GoProLibrary/_library_catalog.json'))))"
wc -l < GoProLibrary/_download_ledger.jsonl
```

The second number should be **at least** the first. Higher is normal —
chaptered videos become several files.

Then close that Terminal window, which discards the token. If you used a HAR
file, delete it — it holds the same credential:

```bash
rm -f gopro.com.har
```

---

## If something goes wrong

| Message | Cause | Fix |
|---|---|---|
| `No access token found in that HAR file` | Sanitized HAR export | Use the cookie method in step 4 |
| `The API rejected the token` | Token expired, or copied only part of it | Re-do step 4; check it has four dots |
| `Could not find HAR file` | No token set in this window | Re-run the `read -rs` command in step 4 |
| `Library enumeration failed` | GoPro changed their API | Scroll to the bottom of the library, export a HAR, then `--from-har-ids` |
| 403s partway through | Going too fast | `--workers 1` |
| Anything else | — | Run `python3 tools/probe_api.py` and share the output; it prints structure only, no private data |
