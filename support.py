#!/usr/bin/env python3
"""The donation channels, read from the environment and checked before serving.

A wrong crypto address is money that never comes back, so nothing is published
until it validates: Bitcoin gets a real checksum check (bech32 and base58check),
Monero gets a format check, because its checksum is Keccak-256 and the standard
library only ships the SHA-3 variant. A channel that fails validation is dropped
and logged - never shown half-broken.
"""

import hashlib
import os
import re
import sys

BECH32_CHARS = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
B58_CHARS = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _bech32_polymod(values):
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for v in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            chk ^= gen[i] if ((top >> i) & 1) else 0
    return chk


def _bech32_ok(address):
    """BIP173 / BIP350 checksum. bc1q… is bech32, bc1p… is bech32m."""
    if any(ord(c) < 33 or ord(c) > 126 for c in address):
        return False
    if address.lower() != address and address.upper() != address:
        return False
    address = address.lower()
    pos = address.rfind("1")
    if pos < 1 or pos + 7 > len(address) or len(address) > 90:
        return False
    hrp, data = address[:pos], address[pos + 1:]
    if any(c not in BECH32_CHARS for c in data):
        return False
    values = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    values += [BECH32_CHARS.index(c) for c in data]
    # 1 for bech32 (segwit v0), 0x2bc830a3 for bech32m (taproot).
    return _bech32_polymod(values) in (1, 0x2BC830A3)


def _base58check_ok(address):
    """Legacy 1… and 3… addresses: last four bytes are a double-SHA256 checksum."""
    if not (25 <= len(address) <= 35) or any(c not in B58_CHARS for c in address):
        return False
    n = 0
    for c in address:
        n = n * 58 + B58_CHARS.index(c)
    raw = n.to_bytes(25, "big") if n.bit_length() <= 200 else None
    if raw is None:
        return False
    # Leading '1' characters are leading zero bytes; they have to line up.
    pad = len(address) - len(address.lstrip("1"))
    body, check = raw[:-4], raw[-4:]
    if raw[0] != 0 and pad:
        return False
    return hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4] == check


def check_btc(address):
    if address.lower().startswith("bc1"):
        return _bech32_ok(address)
    if address[:1] in ("1", "3"):
        return _base58check_ok(address)
    return False


def check_xmr(address):
    """Format only. A standard address is 95 chars from 4, an integrated one 106,
    a subaddress starts with 8. The checksum needs Keccak-256, which is not the
    same thing as hashlib's sha3_256, so it is deliberately not claimed here."""
    if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]+", address):
        return False
    return (address[0] == "4" and len(address) in (95, 106)) or \
           (address[0] == "8" and len(address) == 95)


# Each channel: the env var, how it is shown, and what makes it valid.
CHANNELS = [
    {
        "id": "kofi",
        "env": "KOFI_URL",
        "kind": "link",
        "label": "Ko-fi",
        "blurb": "@sup.kofi",
        "check": lambda v: v.startswith(("https://ko-fi.com/", "https://www.buymeacoffee.com/",
                                        "https://buymeacoffee.com/")),
        "why": "precisa ser uma URL do ko-fi.com ou do buymeacoffee.com",
    },
    {
        "id": "btc",
        "env": "BTC_ADDRESS",
        "kind": "address",
        "label": "Bitcoin",
        "blurb": "@sup.btc",
        "check": check_btc,
        "why": "o endereço não passou na verificação de checksum",
        "verified": "@sup.btc_ok",
    },
    {
        "id": "xmr",
        "env": "XMR_ADDRESS",
        "kind": "address",
        "label": "Monero",
        "blurb": "@sup.xmr",
        "check": check_xmr,
        "why": "o endereço não tem o formato de um endereço Monero",
        "verified": "@sup.xmr_ok",
    },
]


def channels():
    """The channels that are configured and valid. Anything wrong is dropped and
    complained about in the log, so a typo in .env is loud instead of costly."""
    out = []
    for spec in CHANNELS:
        value = (os.environ.get(spec["env"]) or "").strip()
        if not value:
            continue
        if not spec["check"](value):
            print(f"  aviso: {spec['env']} ignorado - {spec['why']}: {value[:24]}…",
                  file=sys.stderr)
            continue
        out.append({
            "id": spec["id"],
            "kind": spec["kind"],
            "label": spec["label"],
            "blurb": spec["blurb"],
            "value": value,
            "verified": spec.get("verified"),
        })
    return out


def state():
    ready = channels()
    return {
        "channels": ready,
        # The page needs to know the difference between "nothing set up yet" and
        # "set up but everything failed validation".
        "configured": bool(ready),
        "expected": [s["env"] for s in CHANNELS],
    }


if __name__ == "__main__":
    import json
    print(json.dumps(state(), indent=1, ensure_ascii=False))
