# GoPro Cloud Downloader

Pulls your entire GoPro Cloud media library down to your own disk, when the
GoPro website will not.

Standard library only — **no `pip install` needed**. Python 3.9 or newer, and
nothing else.

---

## Why this exists

GoPro's web "Download All" quietly caps how much it will actually give you, and
share links for older media often fail outright. This tool takes your logged-in
session's access token, asks the GoPro API for the *complete* list of your
media, and downloads each original file directly.

It is a from-scratch rewrite inspired by
[gopro-cloud-rescue](https://github.com/josefkeup741/gopro-cloud-rescue) (MIT).
That project had the right idea; see [REVIEW.md](REVIEW.md) for a full review
of it and why none of its code is reused here.

---

## ⚠️ Read this first: your `.har` file is a password

The `.har` file you are about to create is a recording of your browser session.
**It contains your GoPro access token and session cookies.** Anyone who gets
that file can access your GoPro account until the token expires.

* Do not commit it, email it, upload it, or put it in a synced folder.
* Delete it when you are done.
* `.gitignore` in this repo already blocks `*.har`, but do not rely on that
  alone.

This tool never transmits the token anywhere except `api.gopro.com`, and
redacts it from all of its own output.

---

## Step 1 — capture your session

1. Log in to <https://gopro.com/media-library> in Chrome or Edge.
2. Press `F12` to open DevTools, and click the **Network** tab.
3. Tick **Preserve log**.
4. Reload the page and wait for your media thumbnails to appear.
   You do **not** need to scroll to the bottom — this tool asks the API for the
   full list itself. Scrolling is only needed for the `--from-har-ids` fallback.
5. Click the **Export HAR** (download arrow) button in the Network tab.
6. Save it as `gopro.com.har` next to `gopro_downloader.py`.

Tokens are short-lived — typically hours. Capture the HAR shortly before you
run this, not the day before.

### Alternative without a HAR file

If you would rather not create a HAR at all: in the Network tab, click any
request to `api.gopro.com`, find the `Authorization: Bearer …` request header,
and pass just the token:

```bash
python gopro_downloader.py --token "eyJhbGciOi..." --out ./GoProLibrary
```

Or set `GOPRO_TOKEN` in your environment, which keeps it out of your shell
history.

---

## Step 2 — look before you leap

```bash
python gopro_downloader.py --list-only
```

This authenticates, enumerates your whole library, reports how many items and
roughly how many gigabytes there are, checks that against your free disk space,
and writes `GoProLibrary/_library_catalog.json`. It downloads nothing.

Then try a handful for real:

```bash
python gopro_downloader.py --limit 5
```

If those five land correctly, run the whole thing.

---

## Step 3 — download everything

```bash
python gopro_downloader.py --out D:/GoProLibrary --workers 3
```

On Windows you can also just double-click `run.bat`; on macOS/Linux, `./run.sh`.

Interrupt it whenever you like. Re-running the identical command resumes:
finished files are skipped, and a file cut off mid-transfer continues from the
byte it stopped at rather than starting over.

### Options

| Flag | Meaning |
|---|---|
| `--har PATH` | HAR file to read (default `gopro.com.har`) |
| `--token TOKEN` | Use a bearer token directly instead of a HAR |
| `--out DIR` | Output directory (default `GoProLibrary`) |
| `--quality source\|proxy` | `source` = untouched originals (default); `proxy` = smaller transcodes |
| `--workers N` | Parallel downloads (default 2; 3–4 is a sensible ceiling) |
| `--limit N` | Only process the first N items |
| `--list-only` | Enumerate and write the catalog, download nothing |
| `--from-har-ids` | Skip the search API; use IDs scraped from the HAR |
| `--yes` | Skip the confirmation prompt |

---

## What you end up with

```
GoProLibrary/
  2026/
    2026-03-14_GX010042_a1b2c3d4e5f6g.MP4
    2026-03-14_GX010043_part01_h7i8j9k0l1m2n.MP4
    2026-03-14_GX010043_part02_h7i8j9k0l1m2n.MP4
  2025/
    ...
  undated/
  _library_catalog.json     full metadata for everything found
  _download_ledger.jsonl    one line per verified file (this is the resume state)
  _failed.json              written only if something failed
```

Files are sorted into folders by capture year and prefixed with the capture
date. The media ID suffix is deliberate: GoPro reuses filenames like
`GX010001.MP4` across cards constantly, and without it those would overwrite
each other. Chaptered videos arrive as `_part01`, `_part02`, and so on.

---

## How it avoids losing files

The failure mode that matters is not crashing — it is finishing with a cheerful
"all done" while some videos are missing. Three things guard against that:

1. **Verified per file.** Every download is compared against the byte count the
   server promised. It is written to `name.part` and only renamed to its real
   name once that matches, so a truncated file can never be mistaken for a
   complete one.
2. **Recorded per file, not per batch.** The ledger gets one line per file that
   actually landed and verified. Nothing is marked done on the strength of
   something else succeeding.
3. **Re-checked on resume.** On each run the ledger is validated against the
   disk: if a recorded file is missing or has changed size, it is downloaded
   again.

If anything fails, the exit code is non-zero and the details land in
`_failed.json`. Re-running retries exactly those.

---

## Verifying it worked

```bash
# how many files did the catalog say you have?
python -c "import json;print(len(json.load(open('GoProLibrary/_library_catalog.json'))))"

# how many verified files are on disk?
wc -l < GoProLibrary/_download_ledger.jsonl
```

The second should be at least the first (more if you have chaptered videos).

---

## Tests

The tool is tested end to end against a mock GoPro API — pagination, resume
from a partial file, a server that ignores `Range`, mid-transfer truncation,
a torn ledger, expired tokens, unsafe filenames, and the CLI itself:

```bash
cd tests && python test_downloader.py
```

36 checks, no network access and no GoPro account required.

---

## Checking it against the live API without downloading anything

If you want to confirm GoPro hasn't changed their API before committing to a
long run:

```bash
python tools/probe_api.py --har gopro.com.har
```

It calls the two endpoints the downloader depends on, fetches the first 1 KB of
one real file, and prints the **structure** of each response — key names, types
and list lengths, with every value replaced by its type. No filenames, IDs,
dates or tokens appear in the output, so it is safe to paste into a chat if you
want someone to look at it. It downloads nothing.

---

## Troubleshooting

**"No access token found in that HAR file"** — you were not logged in when you
recorded, or DevTools exported a filtered view. Clear the Network filter box,
tick Preserve log, reload, then export.

**"The API rejected the token" / 401** — the token expired. Capture a fresh
HAR. The tool prints the remaining validity when it starts, so you can see this
coming before a long run.

**Library enumeration fails but you have a good token** — GoPro changed their
API response shape. Fall back to the old approach: scroll to the very bottom of
your library before exporting the HAR, then run with `--from-har-ids`.

**403 responses part way through** — you are going too fast. Drop to
`--workers 1`. The tool already backs off exponentially and honours
`Retry-After`.

**Very large HAR file** — fine. It is read in 8 MB chunks, not loaded into
memory.

---

## A caveat worth stating plainly

The GoPro API endpoints and response shapes this uses are the ones their web
app uses; they are not a published, supported API and GoPro can change them
without warning. The code is written defensively around that (it degrades to
plain JSON if the versioned media type is refused, falls back from original
files to transcoded variants, and falls back again to HAR-scraped IDs), and the
full flow is verified against a mock — but it has not been run against a live
GoPro account from this machine. Run `--list-only` first. If the shapes have
drifted, that is where you will see it, before anything downloads.

## Licence

MIT. See [LICENSE](LICENSE).
