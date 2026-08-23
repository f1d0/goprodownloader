# Source review: `josefkeup741/gopro-cloud-rescue` @ v1.1

Reviewed commit `4755394` (tag `v1.1`, 16 Mar 2026), plus `v1.0.0` (`9ab1e51`)
for comparison. The project is three files; the whole tool is 148 lines of
Python. Every line was read.

## Is it malicious?

**No — the source code is benign.** Specifically, the script:

* contacts exactly one host, `api.gopro.com`, and no other;
* has no `eval`, `exec`, `subprocess`, `socket`, or dynamic import;
* contains no obfuscated, encoded, or minified content;
* writes only to its own working directory;
* sends nothing anywhere — there is no telemetry, analytics, or upload path.

The MIT licence is intact and the author's identity is consistent across the
git history. I found nothing resembling a backdoor or data exfiltration.

**However, "the source is benign" is not the same as "the released binary is
safe", and here the two cannot be connected.** See finding 1.

## Findings

### 1. The v1.1 source code does not run at all — CRITICAL

`gopro_rescue.py` at tag `v1.1` is not valid Python:

```
$ python3 -m py_compile gopro_rescue.py
IndentationError: expected an indented block after 'with' statement on line 80
```

The final commit ("403 error mitigation") added a new `with requests.get(...)`
line but left the old one directly beneath it, so two `with` statements sit
back to back with no body between them:

```python
with requests.get(url, headers=headers, stream=True) as response:
with requests.get(url, stream=True) as response:      # <- old line, never removed
```

`v1.0.0` does compile; `v1.1` does not.

The consequence matters more than the typo: **the `gopro_rescue.exe` published
in the v1.1 release cannot have been built from the v1.1 source, because that
source cannot be compiled or frozen.** The binary was built from something
else that is not in the repository. There is therefore no way to review what
the `.exe` actually does, and the README's instruction to click past the
Windows Defender warning asks you to trust an artifact nobody can inspect.

This is not evidence of bad intent — it is much more likely a careless commit.
But it is a complete blocker for "download the exe and run it", and it is the
single strongest argument for rebuilding from source, which is what you asked
for.

### 2. The script never authenticates — CRITICAL

Neither `v1.0.0` nor `v1.1` ever sends an `Authorization` header or a cookie.
The only request it makes is:

```python
url = f"https://api.gopro.com/media/x/zip/source?ids={batch_str}"
with requests.get(url, stream=True) as response:
```

That endpoint serves media belonging to a specific account and requires that
account's bearer token. Without one the request is rejected before it ever
reaches your files.

This explains the v1.1 commit message. The author interpreted the failure as
"CloudFront detected a Python bot" and tried to fix it by spoofing a Chrome
`User-Agent`. A missing-credentials rejection does not become an
authenticated request because the User-Agent string changed, so that fix could
not have worked even if the syntax error had not made it moot.

The access token is sitting right there in the `.har` file the tool already
reads — the tool just never looks for it.

### 3. A whole batch is marked "done" if any of it arrives — HIGH

```python
zip_ref.extractall(OUTPUT_FOLDER)
os.remove(TEMP_ZIP)
log_completed_ids(batch)          # all 5 IDs recorded as complete
```

Completion is recorded per *batch of five*, immediately after the archive
extracts, without checking which media IDs are actually inside it. If GoPro
returns an archive containing three of the five requested videos, all five are
written to the ledger and the two missing ones are never retried.

This is the same silent-drop behaviour the README says the tool exists to work
around, reproduced inside the tool. On a large library it would lose files
without ever reporting an error.

### 4. The retry loop can never give up — MEDIUM

```python
while pending_batches:
    ...
    pending_batches = failed_batches
```

There is no attempt ceiling. Given a permanent failure — an expired token, a
revoked account, a 404 — every batch fails, every batch is requeued, and the
loop spins forever, printing errors and sleeping five seconds per pass. Which
is precisely the state finding 2 guarantees.

### 5. Media IDs are scraped by regex from whatever you scrolled past — MEDIUM

```python
pattern = r'\\"id\\":\\"([a-zA-Z0-9]{13})\\"'
found_ids = list(set(re.findall(pattern, content)))
```

Three problems:

* **Coverage.** It only sees media whose data the browser actually loaded,
  hence the README's "slowly scroll all the way to the absolute bottom". Miss
  a lazy-loaded page and those videos are simply absent, with nothing to tell
  you they were missed.
* **Precision.** It matches *any* JSON field named `id` with a 13-character
  alphanumeric value, anywhere in the capture — including analytics, session,
  and third-party widget identifiers. Those get sent to GoPro as if they were
  media.
* **Ordering.** `list(set(...))` discards ordering non-deterministically, so
  batches differ between runs on the same input.

### 6. The entire HAR is loaded into memory — MEDIUM

```python
content = file.read()
```

A HAR recorded while scrolling a large library is routinely hundreds of
megabytes to several gigabytes, and decoding it to a `str` costs several times
the file size in RAM. On an 8 GB laptop this is a plausible `MemoryError`
before the download even starts.

### 7. Emoji in console output can kill a long run — LOW

Every status line uses emoji (`✅`, `📥`, `🎉`). Windows consoles still
default to cp1252 in many configurations, where printing these raises
`UnicodeEncodeError`. That exception is raised from the `print` inside the
`try`, so a run that is otherwise working can die on a status message.

### 8. Over-broad exception handling — LOW

```python
except (zipfile.BadZipFile, Exception):
```

`Exception` already covers `BadZipFile`, so this reads as intent that the code
does not carry out — it swallows *everything*, including `KeyboardInterrupt`'s
siblings and genuine bugs. The `os.remove(TEMP_ZIP)` inside the handler can
itself raise if the file is gone, replacing the original error with a
confusing one.

### 9. No dependency pinning — LOW

`pip install requests tqdm` with no `requirements.txt` and no versions. Minor
here, but it means "works on my machine" is the only compatibility statement.

### 10. The `.har` file is a credential, and nothing says so — MEDIUM (privacy)

Not a code defect, but the most important practical risk. A HAR export is a
complete recording of your browser session: it contains your GoPro access
token, your session cookies, and every request and response body from that
page load. Anyone who obtains that file can act as you on your GoPro account
until the token expires.

The README instructs you to create this file and never mentions that. If you
put it in a folder you later sync, zip up for support, or commit to a
repository, you have published your account access.

### Checked and *not* a problem

* **`zip_ref.extractall()` and Zip Slip.** CPython's `ZipFile.extractall`
  normalises member paths — it strips leading separators and drops `..`
  components — so a hostile archive cannot escape the target directory. Worth
  stating because `extractall` is often flagged automatically; here it is fine.
* **Streaming to disk.** The 8 KB chunked write genuinely does keep memory flat
  during downloads, as the README claims. That part is well done.
* **The overall approach.** Reading IDs from the site's own network traffic and
  going straight to the API is the right idea. It is the execution that fails.

## Verdict

Do not run the released `.exe`: it cannot be reproduced from the published
source, so it cannot be reviewed. Do not run the v1.1 script either — it does
not compile, and if it did, it would not authenticate.

The underlying idea is sound and worth keeping. The rewrite in this repository
keeps the idea and fixes findings 2–10.
