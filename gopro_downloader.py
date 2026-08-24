#!/usr/bin/env python3
"""
gopro_downloader.py - download your entire GoPro Cloud (Quik / media library)
back to your own disk.

Design goals, in order:
  1. Never lose a file silently. Every file is verified by byte count and only
     then recorded as done.
  2. Resume safely. Interrupt it whenever; re-run and it continues.
  3. No third-party packages. Python 3.9+ standard library only, so there is
     nothing to pip-install and no supply chain to trust.
  4. Never leak your credentials. The access token is redacted from all output.

Usage (typical):
    python gopro_downloader.py --har gopro.com.har --out ./GoProLibrary

See README.md for how to capture the .har file.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock

# Overridable only so the test suite can point at a local mock server.
API_ROOT = os.environ.get("GOPRO_API_ROOT", "https://api.gopro.com")

# The GoPro web app asks for a versioned media representation. If the API ever
# rejects it we fall back to plain JSON rather than dying.
ACCEPT_LADDER = [
    "application/vnd.gopro.jk.media+json; version=2.0.0",
    "application/json",
]

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Media that is still being processed cloud-side has no downloadable original.
PROCESSING_STATES = "registered,rendering,pretranscoding,transcoding,failure,ready"

# Preferred download variants, best first. "source" is the untouched original.
# Observed labels: source, high_res_proxy_mp4, edit_proxy, audio_proxy.
VARIANT_PREFERENCE = ["source", "baked_source", "concat", "high_res_proxy_mp4",
                      "edit_proxy", "mp4_low"]

# audio_proxy is an audio-only .m4a. Falling back to it would look like a
# successful download while losing the video entirely.
VARIANT_EXCLUDE = {"audio_proxy"}

LEDGER_NAME = "_download_ledger.jsonl"
CATALOG_NAME = "_library_catalog.json"

_print_lock = Lock()
_secrets: list[str] = []


# --------------------------------------------------------------------------
# Output helpers (all output passes through here so tokens can be redacted)
# --------------------------------------------------------------------------

def register_secret(value: str | None) -> None:
    if value and len(value) > 12:
        _secrets.append(value)


def redact(text: str) -> str:
    for secret in _secrets:
        text = text.replace(secret, "<redacted>")
    return text


def say(message: str = "") -> None:
    with _print_lock:
        # ASCII only: Windows consoles still default to cp1252 and will raise
        # UnicodeEncodeError on emoji, killing a long-running download.
        sys.stdout.write(redact(message) + "\n")
        sys.stdout.flush()


def human_bytes(count: float) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(count) < 1024.0:
            return f"{count:3.1f}{unit}"
        count /= 1024.0
    return f"{count:.1f}PB"


# --------------------------------------------------------------------------
# HAR handling
#
# A .har file is a full recording of your browser session. It contains your
# GoPro access token and cookies -- treat it exactly like a password. We read
# it in chunks rather than json.load()-ing it, because a HAR captured while
# scrolling a large library is routinely several hundred megabytes and a full
# parse would need several times that in RAM.
# --------------------------------------------------------------------------

# GoPro issues an encrypted JWE with FIVE dot-separated segments
# (header.key.iv.ciphertext.tag), not the familiar three-segment signed JWT.
# Matching only three segments silently truncates the token, so allow 3-5.
JWT_RE = re.compile(rb"eyJ[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]+){2,4}")

# The token also appears in the session cookie under a known name, which is a
# far more reliable place to find it than "any JWT-shaped string".
COOKIE_TOKEN_RE = re.compile(rb"gp_access_token=([A-Za-z0-9._-]{40,})")
# GoPro media IDs inside a HAR body. Response bodies are normally stored as an
# escaped JSON string ( \"id\":\"..\" ), but some exporters keep them
# unescaped, so accept either form.
MEDIA_ID_RE = re.compile(rb'\\?"id\\?"\s*:\s*\\?"([0-9a-f]{24})\\?"')
UA_RE = re.compile(rb'"user-agent"\s*,\s*"value"\s*:\s*"([^"]{10,300})"', re.I)


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def decode_jwt_claims(token: str) -> dict:
    """Best-effort decode of a JWT payload. Never raises."""
    try:
        payload = token.split(".")[1]
        return json.loads(_b64url_decode(payload))
    except Exception:
        return {}


def scan_har(path: str, want_ids: bool) -> tuple[list[str], list[str], str | None]:
    """Stream the HAR once, collecting tokens, (optionally) media IDs and a UA.

    Tokens found in the gp_access_token cookie are returned first, because that
    name identifies them unambiguously as GoPro's.
    """
    cookie_tokens: list[str] = []
    tokens: list[str] = []
    seen_tokens: set[str] = set()
    ids: list[str] = []
    seen_ids: set[str] = set()
    user_agent: str | None = None

    size = os.path.getsize(path)
    chunk_size = 8 * 1024 * 1024
    overlap = 4096  # so a match straddling a chunk boundary is not missed
    read_so_far = 0

    with open(path, "rb") as handle:
        tail = b""
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            read_so_far += len(chunk)
            window = tail + chunk

            for match in COOKIE_TOKEN_RE.finditer(window):
                token = match.group(1).decode("ascii", "ignore")
                if token not in seen_tokens:
                    seen_tokens.add(token)
                    cookie_tokens.append(token)

            for match in JWT_RE.finditer(window):
                token = match.group(0).decode("ascii", "ignore")
                if token not in seen_tokens:
                    seen_tokens.add(token)
                    tokens.append(token)

            if want_ids:
                for match in MEDIA_ID_RE.finditer(window):
                    media_id = match.group(1).decode("ascii", "ignore")
                    if media_id not in seen_ids:
                        seen_ids.add(media_id)
                        ids.append(media_id)

            if user_agent is None:
                match = UA_RE.search(window)
                if match:
                    user_agent = match.group(1).decode("utf-8", "ignore")

            tail = window[-overlap:]
            if size:
                pct = min(100, int(read_so_far * 100 / size))
                with _print_lock:
                    sys.stdout.write(f"\r    reading HAR... {pct}%")
                    sys.stdout.flush()

    with _print_lock:
        sys.stdout.write("\r" + " " * 40 + "\r")
        sys.stdout.flush()

    return cookie_tokens + tokens, ids, user_agent


def pick_access_token(tokens: list[str]) -> tuple[str | None, dict]:
    """Choose the most plausible GoPro access token.

    GoPro's token is encrypted (JWE), so its claims cannot be read and the
    expiry cannot be compared. scan_har puts cookie-sourced tokens first, so
    when one of those is present it wins outright.
    """
    if tokens and tokens[0].count(".") == 4:
        # A five-segment token from the gp_access_token cookie: unambiguous.
        return tokens[0], decode_jwt_claims(tokens[0])

    best_token, best_claims, best_exp = None, {}, -1.0
    for token in tokens:
        claims = decode_jwt_claims(token)
        if not claims:
            continue
        blob = json.dumps(claims).lower()
        # GoPro tokens carry a gopro issuer/audience; skip unrelated JWTs that
        # happen to be in the capture (analytics, third-party widgets).
        if "gopro" not in blob:
            continue
        exp = float(claims.get("exp") or 0)
        if exp > best_exp:
            best_token, best_claims, best_exp = token, claims, exp

    if best_token is None and tokens:
        # No token advertised GoPro in its claims; fall back to the longest one
        # and let the first API call be the judge.
        best_token = max(tokens, key=len)
        best_claims = decode_jwt_claims(best_token)

    return best_token, best_claims


def describe_token(token: str) -> str:
    """Human-readable sanity check on a token's shape."""
    dots = token.count(".")
    return f"{len(token)} characters, {dots} dot(s)"


def token_shape_warning(token: str) -> str | None:
    """Return a warning if the token does not look like a whole GoPro token.

    A truncated copy/paste is by far the most common cause of a rejected
    token, and it is indistinguishable from expiry unless we check the shape.
    GoPro issues a 5-segment JWE (4 dots), around 1,200-1,300 characters.
    """
    dots = token.count(".")
    if dots == 4 and len(token) > 800:
        return None
    if dots == 2 and len(token) < 300:
        return (
            "This looks like a short 3-segment JWT from another site, not "
            "GoPro's token. Check you copied gp_access_token, not another cookie."
        )
    if dots < 4:
        return (
            f"This token has {describe_token(token)}, but GoPro's tokens have "
            "4 dots and are ~1,200-1,300 characters. It looks TRUNCATED. Note "
            "that macOS terminals cut pasted input off at 1024 bytes, so a "
            "length near 1015 means the terminal truncated it, not your copy."
        )
    return (
        f"This token has {describe_token(token)}, which does not match the "
        "expected shape (4 dots, ~1,200-1,300 characters)."
    )


def report_token_expiry(claims: dict) -> None:
    exp = claims.get("exp")
    if not exp:
        say("    token expiry: not readable (GoPro's token is encrypted). These")
        say("    are typically valid for hours, so capture it shortly before use.")
        return
    expires_at = datetime.fromtimestamp(float(exp), tz=timezone.utc)
    remaining = expires_at - datetime.now(timezone.utc)
    minutes = remaining.total_seconds() / 60
    stamp = expires_at.astimezone().strftime("%Y-%m-%d %H:%M")
    if minutes <= 0:
        say(f"    token EXPIRED at {stamp}. Capture a fresh HAR file.")
    elif minutes < 60:
        say(f"    token valid for {minutes:.0f} more minutes (until {stamp}).")
    else:
        say(f"    token valid for {minutes / 60:.1f} more hours (until {stamp}).")


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

# Any token-shaped string: GoPro's 5-segment JWE, or a 3-segment JWT.
ANY_TOKEN_RE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]+){2,4}")

CLIPBOARD_COMMANDS = [
    ["pbpaste"],                                    # macOS
    ["wl-paste"],                                   # Linux/Wayland
    ["xclip", "-selection", "clipboard", "-o"],     # Linux/X11
    ["xsel", "--clipboard", "--output"],            # Linux/X11
    ["powershell.exe", "-NoProfile", "-Command", "Get-Clipboard"],  # Windows
]


def read_clipboard() -> str:
    """Return the clipboard's text, or "" if no reader is available."""
    for command in CLIPBOARD_COMMANDS:
        try:
            result = subprocess.run(
                command, capture_output=True, timeout=15, check=True
            )
        except (OSError, subprocess.SubprocessError):
            continue
        return result.stdout.decode("utf-8", "ignore")
    return ""


def extract_token_from_text(text: str) -> str | None:
    """Pull the most plausible access token out of arbitrary pasted text.

    Accepts the bare token, a cookie value, or a whole "Copy as cURL" command
    -- anything containing a token-shaped string. The longest match wins, since
    a truncated or unrelated JWT is always shorter than the real one.
    """
    matches = ANY_TOKEN_RE.findall(text or "")
    if not matches:
        return None
    return max(matches, key=len)


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class Client:
    def __init__(self, token: str, user_agent: str, timeout: int = 60):
        self.token = token
        self.user_agent = user_agent
        self.timeout = timeout
        self.accept = ACCEPT_LADDER[0]

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "User-Agent": self.user_agent,
            "Accept": self.accept,
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://gopro.com",
            "Referer": "https://gopro.com/",
            # Deliberately NOT sending Accept-Encoding: urllib does not
            # transparently decompress, so we ask for identity by omission.
        }
        if extra:
            headers.update(extra)
        return headers

    def open(self, url: str, extra_headers: dict | None = None, retries: int = 4):
        """GET a URL, retrying transient failures with exponential backoff."""
        delay = 2.0
        last_error: Exception | None = None

        for attempt in range(retries + 1):
            request = urllib.request.Request(
                url, headers=self._headers(extra_headers), method="GET"
            )
            try:
                return urllib.request.urlopen(request, timeout=self.timeout)
            except urllib.error.HTTPError as error:
                body = ""
                try:
                    body = error.read(600).decode("utf-8", "ignore")
                except Exception:
                    pass

                if error.code in (401, 403) and "amazon" not in body.lower():
                    # Auth problems will not fix themselves by retrying.
                    raise ApiError(error.code, body or error.reason)
                if error.code == 406 and self.accept != ACCEPT_LADDER[-1]:
                    self.accept = ACCEPT_LADDER[-1]
                    continue
                if error.code not in (403, 408, 425, 429, 500, 502, 503, 504):
                    raise ApiError(error.code, body or str(error.reason))

                last_error = ApiError(error.code, body or str(error.reason))
                retry_after = error.headers.get("Retry-After") if error.headers else None
                wait = float(retry_after) if (retry_after or "").isdigit() else delay
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last_error = error
                wait = delay

            if attempt < retries:
                say(f"    transient failure ({last_error}); retrying in {wait:.0f}s")
                time.sleep(wait)
                delay = min(delay * 2, 60)

        raise last_error if last_error else RuntimeError("request failed")

    def get_json(self, url: str) -> dict:
        with self.open(url) as response:
            raw = response.read()
        return json.loads(raw.decode("utf-8", "ignore"))


# --------------------------------------------------------------------------
# GoPro API
# --------------------------------------------------------------------------

def search_library(client: Client, page_size: int = 100) -> list[dict]:
    """Enumerate the whole library straight from the API, page by page.

    This is what removes the fragile 'scroll to the very bottom of the page'
    step: the browser only ever loaded the pages you scrolled past, but the
    API will hand over every page.
    """
    items: list[dict] = []
    page = 1
    total_pages = None

    while True:
        query = urllib.parse.urlencode(
            {
                "processing_states": PROCESSING_STATES,
                "fields": "id,filename,file_size,captured_at,created_at,type,file_extension,ready_to_view",
                "order_by": "captured_at",
                "per_page": page_size,
                "page": page,
            }
        )
        payload = client.get_json(f"{API_ROOT}/media/search?{query}")
        media = (payload.get("_embedded") or {}).get("media") or []
        items.extend(media)

        pages = payload.get("_pages") or {}
        total_pages = pages.get("total_pages") or total_pages
        total_items = pages.get("total_items")
        say(
            f"    page {page}"
            + (f"/{total_pages}" if total_pages else "")
            + f": {len(media)} items (running total {len(items)}"
            + (f" of {total_items}" if total_items else "")
            + ")"
        )

        if not media:
            break
        if total_pages and page >= int(total_pages):
            break
        if not total_pages and len(media) < page_size:
            break
        page += 1
        if page > 10000:  # hard stop; never loop forever on a odd API response
            say("    [!] stopping pagination at 10000 pages as a safety guard")
            break

    return items


def download_targets(client: Client, media_id: str, quality: str) -> list[dict]:
    """Return [{'url':..., 'part':int, 'suggested_ext':str}] for one media item."""
    payload = client.get_json(f"{API_ROOT}/media/{media_id}/download")
    embedded = payload.get("_embedded") or {}
    targets: list[dict] = []

    all_files = embedded.get("files") or []
    all_variations = embedded.get("variations") or []

    files = [f for f in all_files if f.get("available") is not False]
    if quality == "source" and files:
        # 'files' are the untouched originals. Chaptered videos have several.
        for index, entry in enumerate(files, start=1):
            url = entry.get("url")
            if url:
                targets.append(
                    {
                        "url": url,
                        "part": int(entry.get("item_number") or index),
                        "suggested_ext": guess_extension(url),
                    }
                )
        if targets:
            return targets

    variations = [
        v for v in all_variations
        if v.get("label") not in VARIANT_EXCLUDE and v.get("available") is not False
    ]
    ordered = sorted(
        variations,
        key=lambda v: VARIANT_PREFERENCE.index(str(v.get("label")))
        if str(v.get("label")) in VARIANT_PREFERENCE
        else len(VARIANT_PREFERENCE),
    )
    if quality == "proxy":
        ordered = [v for v in ordered if v.get("label") != "source"] or ordered
    for entry in ordered:
        url = entry.get("url")
        if url:
            return [{"url": url, "part": 1, "suggested_ext": guess_extension(url)}]

    # Last resort: entries GoPro flags as unavailable but which still carry a
    # URL. The flag is sometimes stale, and a request that 403s costs us one
    # round trip, whereas refusing to try loses the file for good.
    stale: list[dict] = []
    for index, entry in enumerate(all_files, start=1):
        if entry.get("url"):
            stale.append({
                "url": entry["url"],
                "part": int(entry.get("item_number") or index),
                "suggested_ext": guess_extension(entry["url"]),
            })
    if not stale:
        for entry in all_variations:
            if entry.get("url") and entry.get("label") not in VARIANT_EXCLUDE:
                stale.append({
                    "url": entry["url"],
                    "part": 1,
                    "suggested_ext": guess_extension(entry["url"]),
                })
                break
    if stale:
        say(f"    [!] GoPro flags this item as unavailable, but a URL is still "
            f"present. Trying it anyway ({len(stale)} file(s)).")
        return stale

    # Genuinely nothing to fetch. Say exactly what the API returned, so the
    # cause is visible without having to re-query by hand.
    labels = [str(v.get("label")) for v in all_variations] or ["none"]
    unavailable = sum(1 for v in all_files + all_variations
                      if v.get("available") is False)
    detail = (
        f"no downloadable URL for {media_id}: "
        f"{len(all_files)} source file(s), "
        f"{len(all_variations)} variation(s) [{', '.join(labels)}]"
    )
    if unavailable:
        detail += f", {unavailable} marked unavailable"
    excluded = [str(v.get("label")) for v in all_variations
                if v.get("label") in VARIANT_EXCLUDE]
    if not all_files and not all_variations:
        detail += (". GoPro is offering nothing at all for this item -- it is "
                   "most likely still processing in the cloud. Try again later")
    elif excluded and not [v for v in all_variations
                           if v.get("label") not in VARIANT_EXCLUDE]:
        detail += (f". Only audio-only variation(s) offered ({', '.join(excluded)}); "
                   "there is no video to download")
    else:
        detail += ". No entry carries a URL, so there is nothing to request"
    raise ApiError(0, detail)


def guess_extension(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    _, extension = os.path.splitext(path)
    return extension if 1 < len(extension) <= 6 else ""


# --------------------------------------------------------------------------
# Naming and the ledger
# --------------------------------------------------------------------------

UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{n}" for n in range(1, 10)),
    *(f"LPT{n}" for n in range(1, 10)),
}


def safe_name(name: str) -> str:
    """Make a string safe as a filename on Windows, macOS and Linux."""
    cleaned = UNSAFE.sub("_", name).strip(" .")
    if cleaned.split(".")[0].upper() in RESERVED:
        cleaned = "_" + cleaned
    return cleaned[:120] or "unnamed"


def target_path(out_dir: str, item: dict, target: dict, multipart: bool) -> str:
    media_id = item.get("id", "unknown")
    original = item.get("filename") or f"{media_id}"
    stem, extension = os.path.splitext(original)
    extension = extension or target.get("suggested_ext") or ""
    if item.get("file_extension") and not extension:
        extension = "." + str(item["file_extension"]).lstrip(".")

    captured = str(item.get("captured_at") or item.get("created_at") or "")[:10]
    prefix = f"{captured}_" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", captured) else ""
    part = f"_part{target['part']:02d}" if multipart else ""

    # The media ID keeps two files that share a name (GoPro reuses GX010001.MP4
    # across cards constantly) from colliding.
    name = safe_name(f"{prefix}{stem}{part}_{media_id}{extension}")

    year = captured[:4] if prefix else "undated"
    folder = os.path.join(out_dir, year)
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, name)


class Ledger:
    """Append-only record of what is verifiably on disk.

    One line per finished FILE, not per batch. The original tool recorded a
    whole batch as done the moment its archive extracted, so anything the
    server quietly omitted from that archive was marked complete and never
    retried. Recording per verified file is what prevents that.
    """

    def __init__(self, path: str):
        self.path = path
        self.done: dict[str, dict] = {}
        self._lock = Lock()
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # tolerate a torn final line after a hard kill
                    if record.get("key"):
                        self.done[record["key"]] = record

    def is_done(self, key: str, expected_path: str) -> bool:
        record = self.done.get(key)
        if not record:
            return False
        # Trust, but verify: if the file was moved or deleted, fetch it again.
        path = record.get("path", expected_path)
        if not os.path.exists(path):
            return False
        if record.get("size") and os.path.getsize(path) != record["size"]:
            return False
        return True

    def record(self, key: str, path: str, size: int) -> None:
        record = {
            "key": key,
            "path": path,
            "size": size,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        with self._lock:
            self.done[key] = record
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
                handle.flush()
                os.fsync(handle.fileno())


# --------------------------------------------------------------------------
# Downloading
# --------------------------------------------------------------------------

def _fetch_once(
    client: Client,
    url: str,
    partial: str,
    show_progress: bool,
) -> tuple[int, int]:
    """One transfer attempt. Returns (bytes_on_disk, expected_total)."""
    already = os.path.getsize(partial) if os.path.exists(partial) else 0
    headers = {"Range": f"bytes={already}-"} if already else {}

    try:
        response = client.open(url, extra_headers=headers)
    except ApiError as error:
        if error.status == 416 and already:
            # The server says that range is unsatisfiable, i.e. we already hold
            # the whole file. Treat the partial as complete.
            return already, already
        raise

    with response:
        status = getattr(response, "status", 200)
        content_length = int(response.headers.get("Content-Length") or 0)

        if already and status != 206:
            # The server ignored our Range header and is sending the whole file
            # again. Appending here would silently corrupt the result, so start
            # the file over instead.
            already = 0
            mode = "wb"
            expected = content_length
        else:
            mode = "ab" if already else "wb"
            expected = content_length + already

        written = already
        last_report = 0.0
        with open(partial, mode) as handle:
            while True:
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                handle.write(chunk)
                written += len(chunk)
                if show_progress and time.monotonic() - last_report > 0.25:
                    last_report = time.monotonic()
                    with _print_lock:
                        if expected:
                            pct = written * 100 / expected
                            bar = "#" * int(pct / 4)
                            sys.stdout.write(
                                f"\r      [{bar:<25}] {pct:5.1f}%  "
                                f"{human_bytes(written)}/{human_bytes(expected)}"
                            )
                        else:
                            sys.stdout.write(f"\r      {human_bytes(written)}")
                        sys.stdout.flush()
            handle.flush()
            os.fsync(handle.fileno())

    if show_progress:
        with _print_lock:
            sys.stdout.write("\r" + " " * 70 + "\r")
            sys.stdout.flush()

    return written, expected


def download_file(
    client: Client,
    url: str,
    destination: str,
    show_progress: bool,
    attempts: int = 5,
) -> int:
    """Stream one file to disk, resuming across dropped connections.

    Writes to <destination>.part and renames only once the byte count matches
    what the server promised, so a truncated transfer can never be mistaken for
    a finished file. A short read keeps the partial data and resumes from that
    offset next time, which matters when a single video is several gigabytes.
    """
    partial = destination + ".part"
    previous = -1

    for attempt in range(1, attempts + 1):
        written, expected = _fetch_once(client, url, partial, show_progress)

        if expected and written > expected:
            # More data than promised: something is wrong with this transfer.
            os.remove(partial)
            raise IOError(
                f"received {written} bytes but only {expected} were expected; "
                "discarded the file"
            )

        if not expected or written == expected:
            os.replace(partial, destination)
            return written

        if written <= previous:
            # An attempt that adds nothing means retrying is pointless.
            raise IOError(
                f"transfer stalled at {human_bytes(written)} of "
                f"{human_bytes(expected)} after {attempt} attempt(s)"
            )
        previous = written

        if attempt < attempts:
            say(
                f"    connection dropped at {human_bytes(written)}/"
                f"{human_bytes(expected)}; resuming (attempt {attempt + 1}/{attempts})"
            )
            time.sleep(min(2 * attempt, 10))

    raise IOError(
        f"gave up after {attempts} attempts; kept the partial file so the next "
        f"run can resume: {os.path.basename(partial)}"
    )


def process_item(
    client: Client,
    item: dict,
    out_dir: str,
    ledger: Ledger,
    quality: str,
    show_progress: bool,
) -> tuple[int, int, int]:
    """Returns (files_downloaded, files_skipped, bytes_downloaded)."""
    media_id = item.get("id")
    label = item.get("filename") or media_id

    targets = download_targets(client, media_id, quality)
    multipart = len(targets) > 1
    downloaded = skipped = total_bytes = 0
    errors: list[str] = []

    for target in targets:
        key = f"{media_id}:{target['part']}"
        destination = target_path(out_dir, item, target, multipart)

        if ledger.is_done(key, destination):
            skipped += 1
            continue

        say(f"    -> {os.path.basename(destination)}")
        try:
            size = download_file(client, target["url"], destination, show_progress)
        except Exception as error:  # noqa: BLE001
            # A chaptered video has several files; one bad chapter must not
            # cost us the others. Collect and report at the end of the item.
            say(f"    [!] {os.path.basename(destination)}: {error}")
            errors.append(str(error))
            continue
        ledger.record(key, destination, size)
        downloaded += 1
        total_bytes += size

    if errors:
        raise IOError(f"{len(errors)} of {len(targets)} file(s) failed: {errors[0]}")

    if skipped and not downloaded:
        say(f"    (already have {label})")
    return downloaded, skipped, total_bytes


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download your GoPro Cloud library to local disk.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--har", default="gopro.com.har",
                        help="HAR file exported from your browser (default: gopro.com.har)")
    parser.add_argument("--token", default=None,
                        help="Bearer token, if you would rather not use a HAR file. "
                             "Also read from the GOPRO_TOKEN environment variable.")
    parser.add_argument("--token-from-clipboard", action="store_true",
                        help="Take the token from your clipboard. Accepts the bare "
                             "token, a cookie value, or a whole 'Copy as cURL' "
                             "command - it finds the token inside. Avoids the "
                             "1024-byte limit on pasting into a terminal.")
    parser.add_argument("--out", default="GoProLibrary",
                        help="Output directory (default: GoProLibrary)")
    parser.add_argument("--quality", choices=["source", "proxy"], default="source",
                        help="'source' = untouched originals (default), "
                             "'proxy' = smaller transcodes")
    parser.add_argument("--workers", type=int, default=2,
                        help="Parallel downloads (default: 2, keep it modest)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Only process the first N items (useful for a test run)")
    parser.add_argument("--list-only", action="store_true",
                        help="Enumerate and write the catalog, download nothing")
    parser.add_argument("--from-har-ids", action="store_true",
                        help="Skip the search API and use IDs scraped from the HAR "
                             "(fallback if the API enumeration fails)")
    parser.add_argument("--yes", action="store_true",
                        help="Do not ask for confirmation before downloading")
    return parser


def resolve_token(args) -> tuple[str, str, list[str]]:
    """Returns (token, user_agent, media_ids_scraped_from_the_har)."""
    token = args.token or os.environ.get("GOPRO_TOKEN")
    user_agent = DEFAULT_UA
    har_ids: list[str] = []

    if getattr(args, "token_from_clipboard", False):
        clipboard = read_clipboard()
        if not clipboard.strip():
            say("[!] The clipboard is empty, or no clipboard reader is available.")
            say("    On Linux install xclip or wl-clipboard, or use --token.")
            sys.exit(1)
        found = extract_token_from_text(clipboard)
        if not found:
            say(f"[!] No token found in the clipboard ({len(clipboard)} characters).")
            say("    Copy either the gp_access_token value, or right-click a")
            say("    request to api.gopro.com in the Network tab and choose")
            say("    Copy -> Copy as cURL, then run this again.")
            sys.exit(1)
        token = found
        say(f"[1/4] Took the token from your clipboard ({describe_token(token)}).")

    if token:
        if not getattr(args, "token_from_clipboard", False):
            say("[1/4] Using the access token supplied on the command line.")
        token = token.strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
    else:
        if not os.path.exists(args.har):
            say(f"[!] Could not find HAR file '{args.har}'.")
            say("    Either put it next to this script, pass --har <path>,")
            say("    or pass --token <token>. See README.md.")
            sys.exit(1)

        size = os.path.getsize(args.har)
        say(f"[1/4] Reading {args.har} ({human_bytes(size)})...")
        tokens, har_ids, har_ua = scan_har(args.har, want_ids=args.from_har_ids)
        if har_ua:
            user_agent = har_ua
        if not tokens:
            say("[!] No access token found in that HAR file.")
            say("    Make sure you were logged in to GoPro when you recorded it,")
            say("    and that you exported the full HAR (not a filtered view).")
            sys.exit(1)

        token, claims = pick_access_token(tokens)
        say(f"    found an access token ({len(tokens)} JWT(s) in the capture).")
        report_token_expiry(claims)

    warning = token_shape_warning(token)
    if warning:
        say("")
        say("[!] " + warning)
        say("    Continuing anyway, but expect it to be rejected.")
    else:
        say(f"    token looks well-formed ({describe_token(token)}).")

    register_secret(token)
    return token, user_agent, har_ids


def main() -> int:
    args = build_parser().parse_args()

    say("=" * 62)
    say(" GoPro Cloud Downloader")
    say("=" * 62)

    token, user_agent, har_ids = resolve_token(args)
    client = Client(token, user_agent)
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)

    # ---- Enumerate ----
    say("\n[2/4] Asking the GoPro API for your full media list...")
    items: list[dict] = []
    if not args.from_har_ids:
        try:
            items = search_library(client)
        except ApiError as error:
            if error.status in (401, 403):
                say(f"[!] The API rejected the token ({error}).")
                warning = token_shape_warning(token)
                if warning:
                    say("")
                    say("    LIKELY CAUSE: " + warning)
                    say("")
                    say("    In DevTools on your GoPro media library, open the")
                    say("    Console tab, type 'allow pasting' + Enter if asked,")
                    say("    then run:")
                    say("")
                    say("      copy(document.cookie.match(/gp_access_token=([^;]+)/)[1])")
                    say("")
                    say("    That puts the complete token on your clipboard. Then,")
                    say("    on macOS, load it WITHOUT typing it (terminals cut")
                    say("    pasted input at 1024 bytes):")
                    say("")
                    say('      export GOPRO_TOKEN="$(pbpaste)"')
                else:
                    say(f"    The token is well-formed ({describe_token(token)}),")
                    say("    so it has most likely expired. GoPro tokens last hours.")
                    say("    Grab a fresh one and retry.")
                return 2
            say(f"[!] Library enumeration failed: {error}")
            say("    You can retry with --from-har-ids to use the IDs found in the HAR.")
            return 2

    if args.from_har_ids or not items:
        if not har_ids and os.path.exists(args.har):
            say("    falling back to IDs scraped from the HAR file...")
            _, har_ids, _ = scan_har(args.har, want_ids=True)
        if not har_ids:
            say("[!] No media found by either method. Nothing to do.")
            return 1
        say(f"    using {len(har_ids)} candidate IDs from the HAR capture.")
        say("    note: IDs scraped this way only cover what you actually scrolled")
        say("    past, and may include false positives that will simply 404.")
        items = [{"id": media_id} for media_id in har_ids]

    if args.limit:
        items = items[: args.limit]

    total_bytes_expected = sum(int(i.get("file_size") or 0) for i in items)
    say(f"\n    {len(items)} media items found.")
    if total_bytes_expected:
        say(f"    approximate total size: {human_bytes(total_bytes_expected)}")
        free = shutil.disk_usage(out_dir).free
        say(f"    free space on target disk: {human_bytes(free)}")
        if free < total_bytes_expected * 1.05:
            say("    [!] That may not be enough free space for the whole library.")

    catalog_path = os.path.join(out_dir, CATALOG_NAME)
    with open(catalog_path, "w", encoding="utf-8") as handle:
        json.dump(items, handle, indent=2)
    say(f"    catalog written to {catalog_path}")

    if args.list_only:
        say("\n--list-only was set; stopping here.")
        return 0

    ledger = Ledger(os.path.join(out_dir, LEDGER_NAME))
    if ledger.done:
        say(f"    ledger shows {len(ledger.done)} file(s) already downloaded.")

    if not args.yes:
        say("")
        answer = input(f"Download to {out_dir}? [y/N]: ").strip().lower()
        if answer != "y":
            say("Cancelled.")
            return 0

    # ---- Download ----
    say(f"\n[3/4] Downloading with {args.workers} worker(s)...")
    workers = max(1, args.workers)
    show_progress = workers == 1
    counters = {"ok": 0, "skip": 0, "bytes": 0}
    failures: list[tuple[str, str]] = []
    counter_lock = Lock()

    def worker(index_item):
        index, item = index_item
        media_id = item.get("id")
        label = item.get("filename") or media_id
        say(f"\n  [{index}/{len(items)}] {label}")
        try:
            downloaded, skipped, byte_count = process_item(
                client, item, out_dir, ledger, args.quality, show_progress
            )
            with counter_lock:
                counters["ok"] += downloaded
                counters["skip"] += skipped
                counters["bytes"] += byte_count
        except Exception as error:  # noqa: BLE001 - one bad item must not stop the run
            say(f"    [!] FAILED: {error}")
            with counter_lock:
                failures.append((str(media_id), str(error)))

    work = list(enumerate(items, start=1))
    if workers == 1:
        for entry in work:
            worker(entry)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(worker, work))

    # ---- Report ----
    say("\n" + "=" * 62)
    say(f"[4/4] Done. {counters['ok']} file(s) downloaded "
        f"({human_bytes(counters['bytes'])}), {counters['skip']} already present.")

    if failures:
        failure_path = os.path.join(out_dir, "_failed.json")
        with open(failure_path, "w", encoding="utf-8") as handle:
            json.dump([{"id": i, "error": redact(e)} for i, e in failures], handle, indent=2)
        say(f"[!] {len(failures)} item(s) failed. Details: {failure_path}")
        say("    Re-run the same command to retry only the failures.")
        return 1

    say("Every item in the catalog is present and size-verified.")
    say(f"Your library is in: {out_dir}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("\nInterrupted. Re-run the same command to resume where you left off.")
        sys.exit(130)
