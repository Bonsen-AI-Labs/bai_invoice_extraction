"""Shared text helpers: fuzzy matching, value parsing, GSTIN checksum.

Used by parsing, extraction (candidate generation), and validation. Kept
dependency-free and pure so they carry their own self-checks.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta


# --- fuzzy string matching ----------------------------------------------------
def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def fuzzy_eq(token: str, anchor: str, per: int = 5) -> bool:
    """OCR-tolerant equality: edit distance ≤ 1 per `per` chars."""
    a, b = norm(token), norm(anchor)
    if not b:
        return False
    budget = max(1, len(b) // per)
    return levenshtein(a, b) <= budget


# --- value parsing ------------------------------------------------------------
_AMOUNT_RE = re.compile(r"-?\d[\d,]*\.?\d*")


def parse_amount(s: str) -> float | None:
    """Extract a decimal amount from noisy text ('₹ 4,532.00' -> 4532.0)."""
    if s is None:
        return None
    m = _AMOUNT_RE.search(str(s).replace(" ", ""))
    if not m:
        return None
    try:
        return float(m.group().replace(",", ""))
    except ValueError:
        return None


def parse_percent(s: str) -> float | None:
    m = re.search(r"-?\d+(?:\.\d+)?", str(s or ""))
    return float(m.group()) if m else None


def parse_date(s: str) -> str | None:
    """Day-first parse of an invoice date -> ISO 8601 string."""
    if not s:
        return None
    s = str(s).strip()
    fmts = [
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d.%m.%Y",
        "%d/%m/%y",
        "%d-%m-%y",
        "%d-%b-%Y",
        "%d-%b-%y",
        "%d %b %Y",
        "%Y-%m-%d",
    ]
    for f in fmts:
        try:
            return datetime.strptime(s, f).date().isoformat()
        except ValueError:
            continue
    return None


def future_beyond(iso: str, skew_days: int = 2) -> bool:
    try:
        d = date.fromisoformat(iso)
    except (ValueError, TypeError):
        return False
    return d > date.today() + timedelta(days=skew_days)


# --- GSTIN -------------------------------------------------------------
GSTIN_RE = re.compile(r"[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]")
_GSTIN_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def gstin_check(g15: str) -> bool:
    """Validate the GSTIN mod-36 check character."""
    if not GSTIN_RE.fullmatch(g15 or ""):
        return False
    total = 0
    for i, ch in enumerate(g15[:14]):
        v = _GSTIN_ALPHABET.index(ch) * (2 if i % 2 else 1)
        total += v // 36 + v % 36
    return _GSTIN_ALPHABET[(36 - total % 36) % 36] == g15[14]


def _demo() -> None:
    assert fuzzy_eq("lnvoice No", "Invoice No")
    assert not fuzzy_eq("Ship To", "Bill To")
    assert parse_amount("₹ 4,532.00") == 4532.0
    assert parse_percent("CGST @ 9%") == 9.0
    assert parse_date("13/07/2026") == "2026-07-13"
    # A real, checksum-valid GSTIN and a corrupted one.
    assert gstin_check("27AAPFU0939F1ZV")
    assert not gstin_check("27AAPFU0939F1ZX")
    print("text self-check ok")


if __name__ == "__main__":
    _demo()
