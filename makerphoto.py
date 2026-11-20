"""The maker's product photo of a device, for routers that have no photo of their own yet.

Looked up on the maker's own product page (its og:image), fetched by the server, and kept under
<data>/panel/maker/ so each model is fetched once. Only the makers' own sites are contacted."""
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request

MAX_PAGE = 2 * 1024 * 1024
MAX_IMAGE = 6 * 1024 * 1024
VERSION = 2                      # bump when the choice of picture changes: older maker photos are fetched again
RETRY_AFTER = 24 * 3600          # a model the maker's site had no photo for is asked again a day later
UA = "Mozilla/5.0 (compatible; NE8000-Manager device photo)"
KINDS = {b"\xff\xd8\xff": "image/jpeg", b"\x89PNG": "image/png", b"RIFF": "image/webp", b"GIF8": "image/gif"}
HOSTS = {"mikrotik": ("mikrotik.com",)}



class PhotoError(Exception):
    pass


def slug(model):
    return re.sub(r"[^a-z0-9]+", "_", str(model or "").lower()).strip("_")[:80]


def product_pages(vendor, model):
    s = slug(model)
    if not s:
        return []
    if vendor == "mikrotik":
        return [f"https://mikrotik.com/product/{s}"]
    return []      # Huawei's site refuses requests that don't come from a browser: upload a photo instead


def _allowed(url, vendor):
    u = urllib.parse.urlsplit(url)
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == h or host.endswith("." + h) for h in HOSTS.get(vendor, ()))


def _get(url, vendor, limit):
    if not _allowed(url, vendor):
        raise PhotoError(f"Not the maker's site: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=12) as res:
        if not _allowed(res.geturl(), vendor):
            raise PhotoError("The maker's site sent us elsewhere.")
        data = res.read(limit + 1)
    if len(data) > limit:
        raise PhotoError("Too large.")
    return data


def image_urls(html, page_url, model=""):
    """Candidate photo URLs on a product page, best first. MikroTik pages name the device's own picture
    rb_images/<id>_tm.webp in og:image; the same id has a full-size _hi_res.png and smaller _xl/_lg webp.
    (product_files/ PNGs are not used: those are block diagrams and drawings.)"""
    og = ""
    for pat in (r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image',
                r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)'):
        m = re.search(pat, html, re.I)
        if m:
            og = urllib.parse.urljoin(page_url, m.group(1).replace("&amp;", "&"))
            break
    out = []
    m = re.search(r"^(.*/rb_images/\d+)_[a-z]+\.(?:webp|png|jpe?g)$", og)
    if m:
        out += [m.group(1) + "_hi_res.png", m.group(1) + "_xl.webp", m.group(1) + "_lg.webp"]
    if og:
        out.append(og)
    return list(dict.fromkeys(out))


class MakerPhotos:
    def __init__(self, data_dir, fetch=None):
        self.dir = os.path.join(data_dir, "panel", "maker")
        os.makedirs(self.dir, exist_ok=True)
        self.lock = threading.Lock()
        self.fetch = fetch or _get

    def _index_path(self):
        return os.path.join(self.dir, "index.json")

    def _index(self):
        try:
            with open(self._index_path()) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_index(self, idx):
        tmp = self._index_path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(idx, f, indent=1)
        os.replace(tmp, self._index_path())

    def get(self, vendor, model):
        """(bytes, mime, source page) of the maker's photo for this model; PhotoError if there is none."""
        key = f"v{VERSION}-{vendor}-{slug(model)}"
        with self.lock:
            idx = self._index()
            e = idx.get(key)
            if e and e.get("file") and os.path.exists(os.path.join(self.dir, e["file"])):
                with open(os.path.join(self.dir, e["file"]), "rb") as f:
                    return f.read(), e["mime"], e["page"]
            if e and e.get("error") and time.time() - e.get("at", 0) < RETRY_AFTER:
                raise PhotoError(e["error"])
            try:
                raw, mime, page = self._lookup(vendor, model)
            except Exception as exc:     # network, parse or size: remember it so pages don't keep asking
                msg = str(exc) if isinstance(exc, PhotoError) else f"The maker's site could not be reached ({exc.__class__.__name__})."
                idx[key] = {"error": msg, "at": int(time.time())}
                self._save_index(idx)
                raise PhotoError(msg) from exc
            name = f"{key}.{mime.split('/')[1]}"
            with open(os.path.join(self.dir, name), "wb") as f:
                f.write(raw)
            idx[key] = {"file": name, "mime": mime, "page": page, "at": int(time.time())}
            self._save_index(idx)
            return raw, mime, page

    def _lookup(self, vendor, model):
        pages = product_pages(vendor, model)
        if not pages:
            raise PhotoError(f"No product page known for {model or 'this model'}.")
        last = None
        for page in pages:
            try:
                html = self.fetch(page, vendor, MAX_PAGE).decode("utf-8", "replace")
                urls = image_urls(html, page, model)
                if not urls:
                    raise PhotoError(f"The product page for {model} has no photo.")
                for url in urls:
                    try:
                        raw = self.fetch(url, vendor, MAX_IMAGE)
                    except Exception as exc:
                        last = exc
                        continue
                    mime = next((v for magic, v in KINDS.items() if raw.startswith(magic)), None)
                    if mime and not (mime == "image/webp" and raw[8:12] != b"WEBP"):
                        return raw, mime, page
                    last = PhotoError("The maker's photo is not an image we can show.")
                raise last
            except Exception as exc:
                last = exc
        raise last
