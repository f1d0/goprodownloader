import json, os, shutil, sys, time, importlib
sys.path.insert(0, "/home/user/goprodownloader")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mock_gopro as mock
import gopro_downloader as gd

FAIL = []
def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else "  <- " + str(extra)))
    if not cond: FAIL.append(name)

srv = mock.serve()
base = f"http://127.0.0.1:{srv.server_port}"
gd.API_ROOT = base

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
shutil.rmtree(OUT, ignore_errors=True); os.makedirs(OUT)

print("\n=== 1. HAR parsing ===")
good = mock.make_jwe("gopro-user")   # 5-segment JWE, as GoPro really issues
stale = mock.make_jwt("gopro-old", int(time.time()) + 60)
other = mock.make_jwt("analytics", int(time.time()) + 99999, issuer="https://segment.io")
har = {"log": {"entries": [
    {"request": {"url": "https://api.gopro.com/media/search",
                 "headers": [{"name": "authorization", "value": f"Bearer {stale}"},
                             {"name": "user-agent", "value": "Mozilla/5.0 (TestBrowser) Chrome/131"}]},
     "response": {"content": {"text": json.dumps({"_embedded": {"media": [{"id": "6a873521c46e36abb12cbbf4"}]}})}}},
    {"request": {"url": "https://sdk.segment.io/x", "headers": [{"name": "authorization", "value": f"Bearer {other}"}]},
     "response": {"content": {"text": ""}}},
    {"request": {"url": "https://api.gopro.com/media/search?page=2",
                 "headers": [{"name": "cookie", "value": f"gp_user_id=x; gp_access_token={good}; gp_location=CZ"}]},
     "response": {"content": {"text": json.dumps({"_embedded": {"media": [{"id": "6a87353ba68ed51ff0547d52"}]}})}}},
]}}
harpath = os.path.join(OUT, "test.har")
open(harpath, "w").write(json.dumps(har) + " " * 200000)  # pad so it spans chunks

tokens, ids, ua = gd.scan_har(harpath, want_ids=True)
check("finds all 3 tokens in HAR", len(tokens) == 3, tokens)
picked, claims = gd.pick_access_token(tokens)
check("picks the gp_access_token cookie over other JWTs", picked == good, picked[:20])
check("the 5-segment JWE is matched in full, not truncated", picked.count(".") == 4, picked.count("."))
check("ignores segment.io JWT despite later expiry", picked != other)
check("extracts real user-agent from HAR", ua and "TestBrowser" in ua, ua)
check("scrapes 24-hex media IDs from escaped HAR bodies", set(ids) == {"6a873521c46e36abb12cbbf4", "6a87353ba68ed51ff0547d52"}, ids)
raw = os.path.join(OUT, "raw.har")
open(raw, "w").write('{"log":{"entries":[{"response":{"content":{"text":{"_embedded":{"media":[{"id":"aabbccddeeff001122334455"}]}}}}}]}}')
_, ids2, _ = gd.scan_har(raw, want_ids=True)
check("also scrapes unescaped HAR bodies", ids2 == ["aabbccddeeff001122334455"], ids2)
big = os.path.join(OUT, "big.har")
with open(big, "wb") as f:
    f.write(b"x" * (8*1024*1024 - 10)); f.write(b'\\"id\\":\\"ffeeddccbbaa998877665544\\"'); f.write(b"y" * 1000)
_, ids3, _ = gd.scan_har(big, want_ids=True)
check("finds a match straddling an 8MB chunk boundary", ids3 == ["ffeeddccbbaa998877665544"], ids3)
gd.report_token_expiry(claims)

print("\n=== 1b. Clipboard token extraction ===")
_real = "eyJ" + "a"*900 + "." + "b"*100 + "." + "c"*16 + "." + "d"*200 + "." + "e"*22
check("bare token", gd.extract_token_from_text(_real) == _real)
check("finds it in a cookie string among other JWTs",
      gd.extract_token_from_text("_ga=GA1.1; gp_access_token=" + _real + "; x=eyJhbGc.eyJzdWI.sig") == _real)
check("finds it in a 'Copy as cURL' authorization header",
      gd.extract_token_from_text("curl 'https://api.gopro.com/media/search' -H 'authorization: Bearer "
                                 + _real + "' --compressed") == _real)
check("finds it in a 'Copy as cURL' cookie header",
      gd.extract_token_from_text("curl 'https://x' -H 'cookie: a=1; gp_access_token=" + _real + "; b=2'") == _real)
check("prefers the full token over a truncated one in the same text",
      gd.extract_token_from_text(_real[:1015] + " ... " + _real) == _real)
check("returns None when there is no token", gd.extract_token_from_text("hello world") is None)
check("returns None on empty input", gd.extract_token_from_text("") is None)
check("shape check accepts the real thing", gd.token_shape_warning(_real) is None)
check("shape check flags the 1015-char macOS truncation",
      "TRUNCATED" in (gd.token_shape_warning(_real[:1015]) or ""))

print("\n=== 2. Secret redaction ===")
gd.register_secret(picked)
check("token never appears in output", "<redacted>" in gd.redact(f"Bearer {picked} oops") and picked not in gd.redact(picked))

print("\n=== 3. Filename safety ===")
check("strips illegal Windows chars", gd.safe_name('a<b>c:d"e/f\\g|h?i*j') == "a_b_c_d_e_f_g_h_i_j", gd.safe_name('a<b>c:d"e/f\\g|h?i*j'))
check("handles reserved device name CON", gd.safe_name("CON.MP4").startswith("_"), gd.safe_name("CON.MP4"))
check("no path traversal survives", "/" not in gd.safe_name("../../etc/passwd") and "\\" not in gd.safe_name("..\\..\\win"))
check("never returns empty", gd.safe_name("...") != "")

print("\n=== 4. Full download run ===")
client = gd.Client(picked, "test-agent")
items = gd.search_library(client, page_size=10)
check("paginates the whole library", len(items) == 23, len(items))
check("preserved order/ids", items[0]["id"] == mock.MEDIA[0]["id"])

ledger = gd.Ledger(os.path.join(OUT, "ledger.jsonl"))
ok = skip = total = 0
failed = []
for it in items:
    try:
        d, s, b = gd.process_item(client, it, OUT, ledger, "source", False)
        ok += d; skip += s; total += b
    except Exception as e:
        failed.append((it["id"], str(e)))
print(f"    downloaded={ok} skipped={skip} bytes={total} failed={len(failed)}")
check("mid-stream truncation auto-resumed instead of failing", len(failed) == 0, failed)
check("chaptered video produced 3 part files", len([p for p in os.listdir(os.path.join(OUT,"2026")) if "part0" in p]) == 3,
      [p for p in os.listdir(os.path.join(OUT,"2026")) if "part0" in p])
check("undated item filed under 'undated'", os.path.isdir(os.path.join(OUT, "undated")))
allfiles = [os.path.join(r,f) for r,_,fs in os.walk(OUT) for f in fs]
check("no .part files left behind", not [f for f in allfiles if f.endswith(".part")], [f for f in allfiles if f.endswith(".part")])
check("duplicate filenames did not collide", len([f for f in allfiles if "GX010005" in f]) == 2, [f for f in allfiles if "GX010005" in f])
mp4s = [f for f in allfiles if f.endswith(".MP4")]
check("every downloaded file is the full 300000 bytes", all(os.path.getsize(f) == 300000 for f in mp4s), [(f, os.path.getsize(f)) for f in mp4s if os.path.getsize(f) != 300000])

print("\n=== 5. Idempotent re-run ===")
ok2 = 0
for it in items:
    d, s2, b = gd.process_item(client, it, OUT, ledger, "source", False)
    ok2 += d
check("a second full run re-downloads nothing", ok2 == 0, ok2)

print("\n=== 6. Resume from a partial file ===")
victim = sorted([f for f in allfiles if f.endswith(".MP4")])[0]
real = open(victim, "rb").read()
os.remove(victim)
open(victim + ".part", "wb").write(real[:120000])   # simulate a kill mid-download
key = None
for k, rec in ledger.done.items():
    if rec["path"] == victim: key = k
del ledger.done[key]
before = mock.STATE["range_requests"]
item = next(i for i in items if i["id"] == key.split(":")[0])
gd.process_item(client, item, OUT, ledger, "source", False)
check("issued an HTTP Range request", mock.STATE["range_requests"] == before + 1)
check("resumed file is byte-identical to the original", open(victim,"rb").read() == real)

print("\n=== 7. Server that ignores Range ===")
os.remove(victim); open(victim + ".part", "wb").write(real[:90000])
del ledger.done[key]
mock.STATE["no_range_for"] = os.path.basename(mock.blob_for.__name__) and f"{item['id']}_1.MP4"
gd.process_item(client, item, OUT, ledger, "source", False)
check("restarted cleanly instead of corrupting by appending", open(victim,"rb").read() == real, os.path.getsize(victim))
mock.STATE["no_range_for"] = None

print("\n=== 8. Ledger integrity ===")
l2 = gd.Ledger(os.path.join(OUT, "ledger.jsonl"))
check("ledger reloads from disk", len(l2.done) >= 23, len(l2.done))
with open(os.path.join(OUT, "ledger.jsonl"), "a") as f: f.write('{"key": "torn')  # simulate hard kill
l3 = gd.Ledger(os.path.join(OUT, "ledger.jsonl"))
check("tolerates a torn final line", len(l3.done) == len(l2.done), (len(l3.done), len(l2.done)))
p = list(l3.done.values())[0]["path"]; sz = os.path.getsize(p)
os.truncate(p, sz - 10)
check("detects a file that changed size on disk", not l3.is_done(list(l3.done.keys())[0], p))
os.remove(p)
check("detects a file deleted from disk", not l3.is_done(list(l3.done.keys())[0], p))

print("\n=== 9. Expired / bad token ===")
bad = gd.Client("EXPIRED-token-value-here", "t")
try:
    gd.search_library(bad); check("401 raises ApiError", False, "no raise")
except gd.ApiError as e:
    check("401 raises immediately without retry storm", e.status == 401, e)
t0 = time.time()
try: gd.search_library(gd.Client("EXPIRED-x", "t"))
except gd.ApiError: pass
check("auth failure does not sit in a backoff loop", time.time() - t0 < 2, time.time()-t0)

print("\n=== 10. Command line interface ===")
import subprocess
env = dict(os.environ, GOPRO_API_ROOT=base, GOPRO_TOKEN=picked)
cli_out = os.path.join(OUT, "cli")
r = subprocess.run([sys.executable, os.path.join(os.path.dirname(gd.__file__), "gopro_downloader.py"),
                    "--token", picked, "--out", cli_out, "--list-only"],
                   capture_output=True, text=True, env=env, timeout=120)
check("--list-only exits 0", r.returncode == 0, r.stdout[-800:] + r.stderr[-800:])
check("--list-only writes a catalog", os.path.exists(os.path.join(cli_out, "_library_catalog.json")))
check("--list-only downloads nothing", not [f for _,_,fs in os.walk(cli_out) for f in fs if f.endswith(".MP4")])
check("token is never printed", picked not in (r.stdout + r.stderr), "TOKEN LEAKED TO STDOUT")

r = subprocess.run([sys.executable, os.path.join(os.path.dirname(gd.__file__), "gopro_downloader.py"),
                    "--token", picked, "--out", cli_out, "--limit", "3", "--workers", "3", "--yes"],
                   capture_output=True, text=True, env=env, timeout=180)
got = [f for _,_,fs in os.walk(cli_out) for f in fs if f.endswith(".MP4")]
check("--limit 3 --workers 3 downloads 3 files", r.returncode == 0 and len(got) == 3, (r.returncode, got, r.stdout[-600:]))

r = subprocess.run([sys.executable, os.path.join(os.path.dirname(gd.__file__), "gopro_downloader.py"),
                    "--token", "EXPIRED-abc", "--out", cli_out, "--yes"],
                   capture_output=True, text=True, env=env, timeout=60)
check("expired token exits non-zero with a clear message",
      r.returncode == 2 and "expired" in r.stdout.lower(), (r.returncode, r.stdout[-400:]))

print("\n" + "="*50)
print(("ALL PASSED" if not FAIL else f"{len(FAIL)} FAILURES: {FAIL}"))
sys.exit(1 if FAIL else 0)
