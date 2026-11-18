"""Router configuration backups kept on the manager's server.

<data>/backups/<router key>/<id>.cfg holds the configuration text, <id>.json what it is:
{"id", "time", "by", "kind": "manual" | "daily" | "after-change" | "after-save" | "before-restore" | "upload", "note", "pinned",
 "size", "lines", "host"}.

Kept: every pinned copy, every manual and uploaded copy, and the newest KEEP_AUTO automatic ones
(daily, after a change or a save made in the app, before a restore). An automatic copy identical to the newest
copy is not kept, so the list only grows when the configuration really changed. Copies are plain text, so they are covered by any backup of the data folder.
"""
import difflib
import json
import os
import re
import threading
import time

KEEP_AUTO = 60
MAX_BYTES = 8 * 1024 * 1024
ID_RE = re.compile(r"^\d{8}-\d{6}(-\d+)?$")
KINDS = ("manual", "daily", "after-change", "after-save", "before-restore", "upload")
AUTO = ("daily", "after-change", "after-save", "before-restore")


class BackupStore:
    def __init__(self, data_dir):
        self.dir = os.path.join(data_dir, "backups")
        os.makedirs(self.dir, exist_ok=True)
        self.lock = threading.Lock()

    @staticmethod
    def key(k):
        k = re.sub(r"[^A-Za-z0-9._-]", "_", str(k or ""))[:80].strip(".")
        if not k:
            raise ValueError("No router to keep backups for.")
        return k

    def _dir(self, key):
        d = os.path.join(self.dir, self.key(key))
        os.makedirs(d, exist_ok=True)
        return d

    def _path(self, key, bid, ext):
        if not ID_RE.match(str(bid)):
            raise ValueError("Unknown backup.")
        return os.path.join(self._dir(key), f"{bid}.{ext}")

    def add(self, key, text, by, kind="manual", note="", host=""):
        if kind not in KINDS:
            raise ValueError("Unknown backup kind.")
        text = text.replace("\r\n", "\n")
        if not text.strip():
            raise ValueError("The configuration is empty.")
        if len(text.encode()) > MAX_BYTES:
            raise ValueError("That configuration is larger than 8 MB.")
        with self.lock:
            bid = time.strftime("%Y%m%d-%H%M%S")
            n = 1
            while os.path.exists(self._path(key, bid, "cfg")):
                n += 1
                bid = time.strftime("%Y%m%d-%H%M%S") + f"-{n}"
            meta = {"id": bid, "time": time.time(), "by": by, "kind": kind, "note": str(note or "")[:200], "pinned": False,
                    "size": len(text.encode()), "lines": text.count("\n") + 1, "host": host}
            _write(self._path(key, bid, "cfg"), text)
            _write(self._path(key, bid, "json"), json.dumps(meta))
            self._prune(key)
        return meta

    def list(self, key):
        d = self._dir(key)
        out = []
        for f in os.listdir(d):
            if f.endswith(".json"):
                try:
                    with open(os.path.join(d, f)) as fh:
                        out.append(json.load(fh))
                except (OSError, ValueError):
                    continue
        return sorted(out, key=lambda m: m["time"], reverse=True)

    def meta(self, key, bid):
        try:
            with open(self._path(key, bid, "json")) as fh:
                return json.load(fh)
        except FileNotFoundError:
            raise ValueError("Unknown backup.")

    def text(self, key, bid):
        try:
            with open(self._path(key, bid, "cfg")) as fh:
                return fh.read()
        except FileNotFoundError:
            raise ValueError("Unknown backup.")

    def update(self, key, bid, pinned=None, note=None):
        with self.lock:
            m = self.meta(key, bid)
            if pinned is not None:
                m["pinned"] = bool(pinned)
            if note is not None:
                m["note"] = str(note)[:200]
            _write(self._path(key, bid, "json"), json.dumps(m))
            return m

    def delete(self, key, bid):
        with self.lock:
            self.meta(key, bid)
            for ext in ("cfg", "json"):
                try:
                    os.remove(self._path(key, bid, ext))
                except FileNotFoundError:
                    pass

    def last(self, key, kinds=None):
        for m in self.list(key):
            if not kinds or m["kind"] in kinds:
                return m
        return None

    def _prune(self, key):
        auto = [m for m in self.list(key) if m["kind"] in AUTO and not m["pinned"]]
        for m in auto[KEEP_AUTO:]:
            for ext in ("cfg", "json"):
                try:
                    os.remove(self._path(key, m["id"], ext))
                except FileNotFoundError:
                    pass


def _write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# ---- comparing ---------------------------------------------------------------------------------------------------
NOISE = re.compile(r"^(!|#\s*$|\s*$|.*(Software Version|Last configuration was|Last update|Info: It will take))")


def clean(text):
    """Configuration lines worth comparing: no banners, comments, blank lines or the time stamps VRP prints."""
    return [l.rstrip() for l in text.replace("\r\n", "\n").split("\n") if not NOISE.match(l)]


def sections(lines):
    """Top-level blocks: {header line: [lines]} in order (a block is a top line plus its indented lines)."""
    out, cur = {}, None
    for l in lines:
        if not l.startswith(" "):
            cur = l
            out.setdefault(cur, [])
        elif cur is not None:
            out[cur].append(l)
    return out


def diff(old_text, new_text, old_name="backup", new_name="router now"):
    """-> {"unified": text, "added": n, "removed": n, "sections": [{"section", "added", "removed"}]}"""
    a, b = clean(old_text), clean(new_text)
    uni = list(difflib.unified_diff(a, b, old_name, new_name, n=2, lineterm=""))
    sa, sb = sections(a), sections(b)
    per = []
    for head in list(dict.fromkeys(list(sa) + list(sb))):
        x, y = sa.get(head), sb.get(head)
        if x == y:
            continue
        if x is None:
            per.append({"section": head, "change": "added", "added": len(y) + 1, "removed": 0})
        elif y is None:
            per.append({"section": head, "change": "removed", "added": 0, "removed": len(x) + 1})
        else:
            sm = difflib.SequenceMatcher(None, x, y)
            add = sum(j2 - j1 for op, i1, i2, j1, j2 in sm.get_opcodes() if op in ("insert", "replace"))
            rem = sum(i2 - i1 for op, i1, i2, j1, j2 in sm.get_opcodes() if op in ("delete", "replace"))
            per.append({"section": head, "change": "changed", "added": add, "removed": rem})
    return {"unified": "\n".join(uni), "added": sum(1 for l in uni if l.startswith("+") and not l.startswith("+++")),
            "removed": sum(1 for l in uni if l.startswith("-") and not l.startswith("---")), "sections": per}
