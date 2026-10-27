"""Small SNMPv3 (and v2c) client for reading the router: get, walk and table reads.

pysnmp is asyncio-based; each client keeps one event loop in a background thread so
Flask's threads can call it like a normal blocking function."""
import asyncio
import threading

from pysnmp.hlapi.v3arch import asyncio as S

AUTH = {"sha": S.usmHMACSHAAuthProtocol, "sha224": S.usmHMAC128SHA224AuthProtocol,
        "sha256": S.usmHMAC192SHA256AuthProtocol, "sha384": S.usmHMAC256SHA384AuthProtocol,
        "sha512": S.usmHMAC384SHA512AuthProtocol, "md5": S.usmHMACMD5AuthProtocol}
PRIV = {"aes128": S.usmAesCfb128Protocol, "aes192": S.usmAesCfb192Protocol,
        "aes256": S.usmAesCfb256Protocol, "des": S.usmDESPrivProtocol}


class SnmpError(Exception):
    pass


def _value(v):
    """pysnmp value -> int, str (IP addresses, OIDs), bytes (octet strings) or None (missing)."""
    cls = v.__class__.__name__
    if cls in ("Integer", "Integer32", "Counter32", "Counter64", "Gauge32", "Unsigned32", "TimeTicks"):
        return int(v)
    if cls == "IpAddress":
        return ".".join(str(b) for b in v.asOctets())
    if cls == "ObjectIdentifier" or cls == "ObjectName":
        return str(v)
    if cls in ("NoSuchObject", "NoSuchInstance", "EndOfMibView", "Null"):
        return None
    return v.asOctets() if hasattr(v, "asOctets") else bytes(v)


def text(v):
    """Octet string -> text (empty for missing values)."""
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace").strip("\x00").strip()
    return str(v)


def mac(v):
    """6-byte octet string -> 'xxxx-xxxx-xxxx' like the router CLI ('' if missing or all zeros)."""
    if not isinstance(v, bytes) or len(v) != 6 or not any(v):
        return ""
    h = v.hex()
    return f"{h[0:4]}-{h[4:8]}-{h[8:12]}"


class SnmpClient:
    def __init__(self, host, user="", auth_proto="sha256", auth_key="", priv_proto="aes128", priv_key="",
                 community="", port=161, timeout=3, retries=1):
        if not user and not community:
            raise SnmpError("No SNMP login configured.")
        self.host, self.port, self.timeout, self.retries = host, int(port), timeout, retries
        if user:
            if auth_proto not in AUTH or priv_proto not in PRIV:
                raise SnmpError("Unsupported SNMP authentication or privacy protocol.")
            self.auth = S.UsmUserData(user, auth_key or None, priv_key or None,
                                      authProtocol=AUTH[auth_proto] if auth_key else S.usmNoAuthProtocol,
                                      privProtocol=PRIV[priv_proto] if priv_key else S.usmNoPrivProtocol)
        else:
            self.auth = S.CommunityData(community, mpModel=1)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True, name=f"snmp-{host}")
        self.thread.start()
        self.engine = None
        self.target = None

    def _call(self, coro, timeout=120):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return fut.result(timeout)
        except SnmpError:
            raise
        except Exception as exc:
            fut.cancel()
            raise SnmpError(str(exc) or exc.__class__.__name__) from exc

    async def _ensure(self):
        if self.engine is None:
            self.engine = S.SnmpEngine()
            self.target = await S.UdpTransportTarget.create((self.host, self.port), timeout=self.timeout, retries=self.retries)

    @staticmethod
    def _check(err_ind, err_stat, err_idx):
        if err_ind:
            raise SnmpError(str(err_ind))
        if err_stat:
            raise SnmpError(err_stat.prettyPrint())

    async def _get(self, oids):
        await self._ensure()
        res = await S.get_cmd(self.engine, self.auth, self.target, S.ContextData(),
                              *[S.ObjectType(S.ObjectIdentity(o)) for o in oids])
        self._check(*res[:3])
        return {str(n): _value(v) for n, v in res[3]}

    async def _walk(self, oid, max_rows, reps=40):
        await self._ensure()
        out = {}
        prefix = oid.rstrip(".") + "."
        async for err_ind, err_stat, err_idx, binds in S.bulk_walk_cmd(
                self.engine, self.auth, self.target, S.ContextData(), 0, reps,
                S.ObjectType(S.ObjectIdentity(oid)), lexicographicMode=False):
            self._check(err_ind, err_stat, err_idx)
            for n, v in binds:
                name = str(n)
                if not name.startswith(prefix):
                    return out
                out[name[len(prefix):]] = _value(v)
                if len(out) >= max_rows:
                    return out
        return out

    def get(self, *oids):
        """{oid: value} for scalar OIDs (missing ones are None)."""
        return self._call(self._get(list(oids)), timeout=self.timeout * (self.retries + 1) + 5)

    def walk(self, oid, max_rows=20000, reps=40):
        """{index-suffix: value} for every row under oid. reps is the GETBULK size: tables with long
        rows (LLDP descriptions) need a small one, or the response gets too big and the router drops it."""
        try:
            return self._call(self._walk(oid, max_rows, reps))
        except SnmpError as exc:
            if reps <= 10 or "timeout" not in str(exc).lower():
                raise
            return self._call(self._walk(oid, max_rows, 10))    # retry once with small requests

    def table(self, base, columns, max_rows=20000):
        """{index: {column-name: value}} for the given columns {name: column-number} of an SNMP table."""
        rows = {}
        for name, col in columns.items():
            for idx, val in self.walk(f"{base}.{col}", max_rows).items():
                rows.setdefault(idx, {})[name] = val
        return rows

    def close(self):
        try:
            if self.engine is not None:
                self.engine.close_dispatcher()
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
