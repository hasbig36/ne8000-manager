"""Prices and server sizes.

Licence: STORE_FIRST_DEVICE_USD for the first device and STORE_EXTRA_DEVICE_USD for each
further one, per billing period (STORE_PERIOD: month, year or once).
Hosting: the DigitalOcean droplet price plus STORE_HOSTING_MARKUP percent, billed monthly.
"""
import math
import os

FIRST = float(os.environ.get("STORE_FIRST_DEVICE_USD", "100"))
EXTRA = float(os.environ.get("STORE_EXTRA_DEVICE_USD", "30"))
MARKUP = float(os.environ.get("STORE_HOSTING_MARKUP", "30"))
PERIOD = os.environ.get("STORE_PERIOD", "month")          # month | year | once
KES_PER_USD = float(os.environ.get("STORE_KES_PER_USD", "129"))
MAX_DEVICES = 200

# Recommended DigitalOcean droplet by number of devices. The app polls every router over SNMP,
# keeps graphs and collects NetFlow, so memory grows with the device count.
# (max devices, DO size slug, label, fallback monthly USD price if the DO API is not set up)
SIZES = [
    (2, "s-1vcpu-2gb", "1 vCPU, 2 GB RAM, 50 GB SSD", 12.0),
    (5, "s-2vcpu-4gb", "2 vCPU, 4 GB RAM, 80 GB SSD", 24.0),
    (15, "s-4vcpu-8gb", "4 vCPU, 8 GB RAM, 160 GB SSD", 48.0),
    (40, "s-8vcpu-16gb", "8 vCPU, 16 GB RAM, 320 GB SSD", 96.0),
    (MAX_DEVICES, "g-8vcpu-32gb", "8 dedicated vCPU, 32 GB RAM, 100 GB SSD", 252.0),
]

DEVICE_TYPES = {
    "ne8000-m8": "Huawei NetEngine 8000 M8",
    "ne8000-m14": "Huawei NetEngine 8000 M14",
    "ne8000-f": "Huawei NetEngine 8000 F series",
    "ne40e": "Huawei NE40E",
    "other-vrp": "Other Huawei VRP8 router",
}


class PriceError(ValueError):
    pass


def clean_devices(items):
    """[{type, count}] from the order form -> list with known types and a total of 1..MAX_DEVICES."""
    out = []
    for it in items or []:
        t = str((it or {}).get("type") or "")
        try:
            n = int((it or {}).get("count") or 0)
        except (TypeError, ValueError):
            raise PriceError("Device counts must be whole numbers.")
        if t not in DEVICE_TYPES:
            raise PriceError("Unknown device type.")
        if n < 0 or n > MAX_DEVICES:
            raise PriceError("Invalid device count.")
        if n:
            out.append({"type": t, "count": n})
    total = sum(x["count"] for x in out)
    if not 1 <= total <= MAX_DEVICES:
        raise PriceError(f"Choose between 1 and {MAX_DEVICES} devices.")
    return out


def licence_price(devices):
    """Price of a licence for this many devices, for one period."""
    if devices < 1:
        return 0.0
    return round(FIRST + EXTRA * (devices - 1), 2)


def extra_devices_price(added):
    return round(EXTRA * added, 2)


def server_for(devices, prices=None):
    """Recommended droplet for this many devices. prices: {slug: monthly USD} from the DO API."""
    for max_n, slug, label, usd in SIZES:
        if devices <= max_n:
            cost = float((prices or {}).get(slug, usd))
            return {"slug": slug, "label": label, "do_monthly": cost, "max_devices": max_n,
                    "hosted_monthly": hosted_price(cost)}
    raise PriceError("Too many devices.")


def hosted_price(do_monthly):
    return round(math.ceil(round(do_monthly * (1 + MARKUP / 100) * 100, 6)) / 100, 2)


def quote(devices, hosting, prices=None):
    """Full quote. hosting: 'self' or 'hosted'."""
    if hosting not in ("self", "hosted"):
        raise PriceError("Choose your own server or hosting by us.")
    server = server_for(devices, prices)
    lic = licence_price(devices)
    host = server["hosted_monthly"] if hosting == "hosted" else 0.0
    # hosting is monthly; with a yearly licence the first year of hosting is billed with it
    months = 12 if PERIOD == "year" else 1
    total = round(lic + host * months, 2)
    return {"devices": devices, "hosting": hosting, "server": server, "licence_usd": lic,
            "hosting_usd": round(host * months, 2), "total_usd": total, "total_kes": to_kes(total),
            "period": PERIOD, "first_device_usd": FIRST, "extra_device_usd": EXTRA, "markup_pct": MARKUP}


def to_kes(usd):
    return int(math.ceil(round(usd * KES_PER_USD, 6)))
