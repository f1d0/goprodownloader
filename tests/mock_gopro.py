"""Mock GoPro API to exercise gopro_downloader end to end, offline."""
import base64, hashlib, json, os, re, threading, time
from http.server import HTTPServer, BaseHTTPRequestHandler

MEDIA = []
for n in range(1, 24):
    MEDIA.append({
        "id": f"ID{n:011d}"[:13],
        "filename": f"GX01{n:04d}.MP4",
        "file_size": 100000 + n * 1000,
        "captured_at": f"2026-0{(n % 9) + 1}-1{n % 9}T10:00:00Z",
        "type": "Video",
        "file_extension": "MP4",
    })
# Deliberate nastiness: duplicate filename, illegal chars, no date, chaptered.
MEDIA[5]["filename"] = MEDIA[4]["filename"]
MEDIA[6]["filename"] = 'we|rd:name?<>.MP4'
MEDIA[7]["captured_at"] = None
MEDIA[8]["_chapters"] = 3

BLOBS = {}
def blob_for(key, size):
    if key not in BLOBS:
        BLOBS[key] = hashlib.sha256(key.encode()).digest() * (size // 32 + 1)
        BLOBS[key] = BLOBS[key][:size]
    return BLOBS[key]

STATE = {"range_requests": 0, "flaked": set(), "no_range_for": None}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            self._json({"error": "no auth"}, 401); return
        if "EXPIRED" in auth:
            self._json({"error": "token expired"}, 401); return

        path, _, query = self.path.partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)

        if path == "/media/search":
            per = int(params.get("per_page", 100)); page = int(params.get("page", 1))
            chunk = MEDIA[(page - 1) * per: page * per]
            total_pages = (len(MEDIA) + per - 1) // per
            self._json({"_embedded": {"media": chunk},
                        "_pages": {"current_page": page, "per_page": per,
                                   "total_items": len(MEDIA), "total_pages": total_pages}})
            return

        m = re.match(r"^/media/([A-Za-z0-9]+)/download$", path)
        if m:
            mid = m.group(1)
            item = next((i for i in MEDIA if i["id"] == mid), None)
            if not item:
                self._json({"error": "not found"}, 404); return
            n = item.get("_chapters", 1)
            files = [{"item_number": k, "url": f"http://{self.headers['Host']}/blob/{mid}_{k}.MP4",
                      "width": 1920, "height": 1080} for k in range(1, n + 1)]
            self._json({"filename": item["filename"], "_embedded": {
                "files": files,
                "variations": [{"label": "high_res_proxy_mp4", "type": "mp4",
                                "url": f"http://{self.headers['Host']}/blob/{mid}_proxy.MP4"}]}})
            return

        m = re.match(r"^/blob/(.+)$", path)
        if m:
            key = m.group(1)
            data = blob_for(key, 300000)
            rng = self.headers.get("Range")

            # One file dies mid-stream the first time it is requested.
            if key == "ID00000000009_1.MP4" and key not in STATE["flaked"]:
                STATE["flaked"].add(key)
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data[:50000])   # truncated body
                self.wfile.flush()
                return

            if rng:
                STATE["range_requests"] += 1
                start = int(rng.split("=")[1].split("-")[0])
                if key == STATE["no_range_for"]:
                    # Server that ignores Range: must be handled, not appended to.
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data); return
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{len(data)-1}/{len(data)}")
                self.send_header("Content-Length", str(len(data) - start))
                self.end_headers()
                self.wfile.write(data[start:]); return

            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data); return

        self._json({"error": "unknown path " + path}, 404)

def serve():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv

def make_jwt(sub, exp, issuer="https://gopro.com"):
    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{seg({'alg':'HS256'})}.{seg({'sub':sub,'iss':issuer,'exp':exp})}.sig_{sub}"
