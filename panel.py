"""A photo of each router's front panel, and where each port is on it (for the live port view).

Files: <data>/panel/<key>.jpg (or .png) and <key>.json ({"marks": {port: [x, y]}, "size": n}), where x and y are
fractions of the photo's width and height and size is the marker diameter in percent of the width."""
import base64
import json
import os
import re
import threading
import time

MAX_BYTES = 3 * 1024 * 1024
PORT_RE = re.compile(r"^[A-Za-z0-9/.:\-_|]{1,64}$")
KINDS = {b"\xff\xd8\xff": ("jpg", "image/jpeg"), b"\x89PNG": ("png", "image/png")}


class PanelStore:
    def __init__(self, data_dir):
        self.dir = os.path.join(data_dir, "panel")
        os.makedirs(self.dir, exist_ok=True)
        self.lock = threading.Lock()

    @staticmethod
    def key(k):
        k = re.sub(r"[^A-Za-z0-9._-]", "_", str(k or ""))[:80].strip(".")
        if not k:
            raise ValueError("No router to keep a photo for.")
        return k

    def _photo(self, key):
        for ext, mime in (("jpg", "image/jpeg"), ("png", "image/png")):
            p = os.path.join(self.dir, f"{key}.{ext}")
            if os.path.exists(p):
                return p, mime
        return None, None

    def _meta_path(self, key):
        return os.path.join(self.dir, f"{key}.json")

    def get(self, key):
        key = self.key(key)
        path, _ = self._photo(key)
        meta = {"marks": {}, "size": 4.0}
        try:
            with open(self._meta_path(key)) as f:
                meta.update(json.load(f))
        except (OSError, ValueError):
            pass
        return {"photo": bool(path), "version": int(os.path.getmtime(path)) if path else 0,
                "marks": meta.get("marks") or {}, "size": meta.get("size") or 4.0, "by": meta.get("by", ""), "at": meta.get("at", 0)}

    def photo(self, key):
        return self._photo(self.key(key))

    def set_photo(self, key, data_url, by=""):
        key = self.key(key)
        m = re.match(r"^data:image/(?:jpeg|jpg|png);base64,(.+)$", str(data_url or ""), re.S)
        if not m:
            raise ValueError("Send the photo as a JPEG or PNG image.")
        try:
            raw = base64.b64decode(m.group(1), validate=True)
        except ValueError:
            raise ValueError("The photo could not be read.")
        if len(raw) > MAX_BYTES:
            raise ValueError("The photo is too large (3 MB at most).")
        kind = next((v for magic, v in KINDS.items() if raw.startswith(magic)), None)
        if not kind:
            raise ValueError("That file is not a JPEG or PNG image.")
        with self.lock:
            for ext in ("jpg", "png"):
                try:
                    os.remove(os.path.join(self.dir, f"{key}.{ext}"))
                except OSError:
                    pass
            tmp = os.path.join(self.dir, f".{key}.tmp")
            with open(tmp, "wb") as f:
                f.write(raw)
            os.replace(tmp, os.path.join(self.dir, f"{key}.{kind[0]}"))
            self._write_meta(key, {"marks": {}, "size": 4.0, "by": by, "at": int(time.time())})   # a new photo: the old positions no longer fit
        return self.get(key)

    def set_marks(self, key, marks, size, by=""):
        key = self.key(key)
        if not self._photo(key)[0]:
            raise ValueError("Add a photo first.")
        out = {}
        for name, xy in (marks or {}).items():
            if not PORT_RE.match(str(name)) or not isinstance(xy, (list, tuple)) or len(xy) != 2:
                raise ValueError(f"Bad position for {name}.")
            x, y = float(xy[0]), float(xy[1])
            if not (0 <= x <= 1 and 0 <= y <= 1):
                raise ValueError(f"Position of {name} is outside the photo.")
            out[str(name)] = [round(x, 4), round(y, 4)]
        if len(out) > 512:
            raise ValueError("Too many ports.")
        size = min(15.0, max(1.0, float(size or 4.0)))
        with self.lock:
            self._write_meta(key, {"marks": out, "size": size, "by": by, "at": int(time.time())})
        return self.get(key)

    def delete(self, key):
        key = self.key(key)
        with self.lock:
            for p in (os.path.join(self.dir, f"{key}.jpg"), os.path.join(self.dir, f"{key}.png"), self._meta_path(key)):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def _write_meta(self, key, meta):
        tmp = self._meta_path(key) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(meta, f)
        os.replace(tmp, self._meta_path(key))
