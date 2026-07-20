"""Layered extraction engine.

Per logical invoice: generate candidates from L1 (DI prebuilt-invoice), L2
(anchors), L3 (patterns); merge; fuse; run deterministic validation which
adjusts scores and picks arithmetic winners; apply the accept/dispute rule
; and send only the still-disputed fields to the vision LLM.

Phase-1 simplifications (marked `ponytail:`): no L4/L5/L6, biller-vs-payee GSTIN
role resolution is heuristic, tax amounts are rate-derived (single-rate only).
"""

from __future__ import annotations

import re
from typing import Any

from src import config
from src.config import (
    ACCEPT_CONF,
    ACCEPT_MARGIN,
    ANCHOR_LEXICON,
    ANCHOR_MAX_DIST,
    CONTRADICTION_FACTOR,
    FIELDS,
    GST_SLABS,
    LAYER_WEIGHTS,
    VALID_FLOOR,
)
from src.models import Candidate, FieldResult, InvoiceRecord, ValidationResult
from src.parsing.client import Line, LogicalInvoice
from src.utils import render
from src.utils.text import (
    GSTIN_RE,
    fuzzy_eq,
    gstin_check,
    norm,
    parse_amount,
    parse_date,
    parse_percent,
)

_PHONE_RE = re.compile(r"(?:\+?91[\s-]?)?[6-9]\d{9}")
_FY_RE = re.compile(r"(\d{2})\s*[-/]\s*(\d{2})")  # e.g. 25-26 in a bill number


class Extractor:
    def __init__(self, llm=None, templates=None):
        self._llm = llm  # LLMClient or None (arbitration skipped if None)
        self._templates = templates  # TemplateEngine or None

    async def extract(
        self, inv: LogicalInvoice, pdf_bytes: bytes, eval_id: str, file_path: str
    ) -> InvoiceRecord:
        template_match, l6 = (
            self._templates.propose(inv) if self._templates is not None else (None, [])
        )
        cands = _l1(inv) + _l2(inv) + _l3(inv) + l6
        by_field = _merge(cands)

        rec = InvoiceRecord(
            eval_id=eval_id, file_path=file_path, page_range=inv.page_range
        )
        if template_match is not None:
            rec.template_id = template_match.template_id
            rec.template_score = template_match.score
            rec.template_verdict = template_match.verdict
        for c in by_field.values():
            for cand in c:
                cand.fused_score = _fuse(cand)

        # Deterministic validation adjusts scores and records verdicts.
        validation = _validate(by_field, rec)
        rec.validation = validation

        # Resolve each field + collect disputes.
        disputes: list[dict[str, Any]] = []
        for fkey in FIELDS:
            cands_f = sorted(
                by_field.get(fkey, []), key=lambda c: c.fused_score, reverse=True
            )
            fr = FieldResult(candidates=cands_f[:3])
            if cands_f:
                top1 = cands_f[0]
                top2 = cands_f[1] if len(cands_f) > 1 else None
                accepted = top1.fused_score >= ACCEPT_CONF and (
                    top2 is None or top1.fused_score - top2.fused_score >= ACCEPT_MARGIN
                )
                _apply(fr, top1)
                if not accepted or fkey in validation.details.get(
                    "force_arbitration", []
                ):
                    disputes.append(_dispute(fkey, cands_f, inv, pdf_bytes))
            else:
                rec.needs_review = True
            rec.fields[fkey] = fr

        rec.vendor_key = _vendor_key(rec)
        await self._arbitrate(disputes, rec)
        _finalize_review(rec)
        return rec

    async def _arbitrate(self, disputes: list[dict], rec: InvoiceRecord) -> None:
        if not disputes:
            rec.llm_meta = {"called": False}
            return
        if self._llm is None:
            # No arbiter wired: disputed fields go to review rather than guessing.
            for d in disputes:
                rec.needs_review = True
                rec.validation.flags.append(f"UNRESOLVED:{d['field']}")
            rec.llm_meta = {"called": False, "skipped": True}
            return
        try:
            out = await self._llm.arbitrate(
                [
                    {
                        k: d[k]
                        for k in ("field", "definition", "candidates", "crops", "note")
                    }
                    for d in disputes
                ]
            )
        except Exception as e:  # arbiter failure must not lose the invoice
            rec.needs_review = True
            rec.validation.flags.append("LLM_FAILED")
            rec.llm_meta = {"called": True, "error": str(e)}
            return
        for field, verdict in out["verdicts"].items():
            fr = rec.fields.get(field)
            if not fr:
                continue
            val = verdict.get("value")
            # Guard: LLM cannot override a checksum-valid GSTIN with an invalid one.
            if field == "gstin" and val and not gstin_check(str(val)):
                continue
            fr.value = val
            fr.confidence = float(verdict.get("confidence", 0.7))
            fr.source_layer = "LLM"
            fr.llm = verdict
        rec.llm_meta = {
            "called": True,
            "fields": out["fields"],
            "cost_usd": out["cost_usd"],
        }


# --- L1: DI prebuilt-invoice --------------------------------------------------
_DI_MAP = {  # our field: (DI field name, extractor)
    "billerName": ("VendorName", "string"),
    "payee": ("CustomerName", "string"),
    "billNumber": ("InvoiceId", "string"),
    "date": ("InvoiceDate", "date"),
    "subTotal": ("SubTotal", "amount"),
    "totalBillAmount": ("InvoiceTotal", "amount"),
    "billerAddress": ("VendorAddress", "address"),
}


def _l1(inv: LogicalInvoice) -> list[Candidate]:
    doc = inv.di_document
    if not doc or not getattr(doc, "fields", None):
        return []
    fields = doc.fields
    out: list[Candidate] = []
    for our, (di_name, kind) in _DI_MAP.items():
        f = fields.get(di_name)
        if f is None:
            continue
        val, raw = _di_value(f, kind)
        if val is None:
            continue
        page, poly = _di_region(f)
        out.append(
            Candidate(
                field=our,
                value=val,
                value_raw=raw,
                page=page,
                polygon=poly,
                layers={"L1": float(f.confidence or 0.5)},
                evidence={"di": di_name},
            )
        )
    # Total line items = count of the Items array.
    items = fields.get("Items")
    if items is not None and getattr(items, "value_array", None) is not None:
        out.append(
            Candidate(
                field="totalLineItems",
                value=len(items.value_array),
                value_raw=str(len(items.value_array)),
                layers={"L1": 0.7},
                evidence={"di": "Items"},
            )
        )
    # Currency from any amount's currency code.
    cur = _di_currency(fields)
    if cur:
        out.append(
            Candidate(
                field="currency",
                value=cur,
                value_raw=cur,
                layers={"L1": 0.6},
                evidence={"di": "valueCurrency"},
            )
        )
    return out


def _di_value(f, kind) -> tuple[Any, str]:
    content = getattr(f, "content", "") or ""
    if kind == "string":
        return (getattr(f, "value_string", None) or content or None), content
    if kind == "date":
        d = getattr(f, "value_date", None)
        isoformat = getattr(d, "isoformat", None)
        value = isoformat() if callable(isoformat) else parse_date(str(d or content))
        return value, content
    if kind == "amount":
        cur = getattr(f, "value_currency", None)
        if cur is not None and getattr(cur, "amount", None) is not None:
            return float(cur.amount), content
        return parse_amount(content), content
    if kind == "address":
        return (content or None), content
    return (content or None), content


def _di_region(f) -> tuple[int, list[float]]:
    regions = getattr(f, "bounding_regions", None) or []
    if regions:
        r = regions[0]
        return getattr(r, "page_number", 1), list(getattr(r, "polygon", []) or [])
    return 1, []


def _di_currency(fields) -> str | None:
    for name in ("InvoiceTotal", "SubTotal", "AmountDue"):
        f = fields.get(name)
        cur = getattr(f, "value_currency", None) if f else None
        code = getattr(cur, "currency_code", None) if cur else None
        if code:
            return code
    return None


# --- L2: anchors --------------------------------------------------------------
def _l2(inv: LogicalInvoice) -> list[Candidate]:
    widths = {p.number: p.width for p in inv.parsed.pages}
    out: list[Candidate] = []
    for field, anchors in ANCHOR_LEXICON.items():
        pos = [a for a in anchors if not a.startswith("-")]
        neg = [a[1:] for a in anchors if a.startswith("-")]
        for line in inv.lines:
            hit = _anchor_hit(line.text, pos, neg)
            if not hit:
                continue
            value_text, vline, rel, score = _anchor_value(inv, line, hit, widths)
            if not value_text:
                continue
            val = _normalize(field, value_text)
            if val in (None, ""):
                continue
            page = vline.page if vline else line.page
            poly = vline.polygon if vline else line.polygon
            out.append(
                Candidate(
                    field=field,
                    value=val,
                    value_raw=value_text,
                    page=page,
                    polygon=poly,
                    layers={"L2": score},
                    evidence={"anchor": hit, "relation": rel},
                )
            )
    return out


def _anchor_hit(text: str, pos: list[str], neg: list[str]) -> str | None:
    label = text.split(":", 1)[0] if ":" in text else text
    if any(fuzzy_eq(label, n) or fuzzy_eq(text, n) for n in neg):
        return None
    for a in pos:
        if fuzzy_eq(label, a) or _starts_with(text, a):
            return a
    return None


def _starts_with(text: str, anchor: str) -> bool:
    nt, na = norm(text), norm(anchor)
    return nt.startswith(na[: max(3, len(na) - 1)]) if len(nt) >= len(na) else False


def _anchor_value(inv, line: Line, anchor: str, widths):
    # 1) same line after a colon.
    if ":" in line.text:
        rest = line.text.split(":", 1)[1].strip()
        if rest:
            return rest, line, "same-line", 0.85
    # 2) right-of, 3) below.
    right = _nearest_right(inv, line, widths)
    if right:
        return right.text, right, "right-of", 0.75
    below = _nearest_below(inv, line)
    if below:
        return below.text, below, "below", 0.65
    return None, None, "", 0.0


def _nearest_right(inv, line, widths) -> Line | None:
    a = line.bbox
    max_dist = ANCHOR_MAX_DIST * widths.get(line.page, 8.5)
    best, best_dx = None, 1e9
    for o in inv.lines:
        if o.page != line.page or o is line:
            continue
        b = o.bbox
        y_overlap = min(a[3], b[3]) - max(a[1], b[1])
        if y_overlap <= 0 or b[0] <= a[2]:
            continue
        dx = b[0] - a[2]
        if dx < best_dx and dx <= max_dist:
            best, best_dx = o, dx
    return best


def _nearest_below(inv, line) -> Line | None:
    a = line.bbox
    best, best_dy = None, 1e9
    for o in inv.lines:
        if o.page != line.page or o is line:
            continue
        b = o.bbox
        x_overlap = min(a[2], b[2]) - max(a[0], b[0])
        if x_overlap <= 0 or b[1] <= a[3]:
            continue
        dy = b[1] - a[3]
        if dy < best_dy and dy < 0.5:
            best, best_dy = o, dy
    return best


# --- L3: patterns -------------------------------------------------------------
def _l3(inv: LogicalInvoice) -> list[Candidate]:
    out: list[Candidate] = []
    payee_lines = _payee_line_ids(inv)
    for line in inv.lines:
        for m in GSTIN_RE.finditer(line.text.replace(" ", "").upper()):
            g = m.group()
            valid = gstin_check(g)
            # ponytail: bias biller GSTIN by de-scoring GSTINs sitting in a payee
            # block; full relational resolution is a later phase.
            score = (0.55 if valid else 0.2) - (0.2 if id(line) in payee_lines else 0)
            out.append(
                Candidate(
                    field="gstin",
                    value=g,
                    value_raw=g,
                    page=line.page,
                    polygon=line.polygon,
                    layers={"L3": max(score, 0.05)},
                    evidence={"checksum": valid},
                )
            )
        pm = _PHONE_RE.search(line.text)
        if pm and not GSTIN_RE.search(line.text):
            out.append(
                Candidate(
                    field="billerPhone",
                    value=pm.group(),
                    value_raw=pm.group(),
                    page=line.page,
                    polygon=line.polygon,
                    layers={"L3": 0.5},
                    evidence={"pattern": "phone"},
                )
            )
    return out


def _payee_line_ids(inv) -> set[int]:
    ids = set()
    for line in inv.lines:
        if any(
            fuzzy_eq(line.text.split(":", 1)[0], a)
            for a in ("Bill To", "Buyer", "Consignee", "Customer")
        ):
            ids.add(id(line))
    return ids


# --- normalize ----------------------------------------------------------------
def _normalize(field: str, text: str):
    if field in config.AMOUNT_FIELDS:
        return parse_amount(text)
    if field in config.PERCENT_FIELDS:
        return parse_percent(text)
    if field == "date":
        return parse_date(text)
    if field == "gstin":
        m = GSTIN_RE.search(text.replace(" ", "").upper())
        return m.group() if m else None
    if field == "totalLineItems":
        n = parse_amount(text)
        return int(n) if n is not None else None
    return text.strip()


# --- merge + fuse -------------------------------------------------------------
def _merge(cands: list[Candidate]) -> dict[str, list[Candidate]]:
    by_field: dict[str, list[Candidate]] = {}
    for c in cands:
        bucket = by_field.setdefault(c.field, [])
        for existing in bucket:
            if _same(existing, c):
                for layer, s in c.layers.items():
                    existing.layers[layer] = max(existing.layers.get(layer, 0), s)
                existing.evidence.update(c.evidence)
                if not existing.polygon and c.polygon:
                    existing.polygon, existing.page = c.polygon, c.page
                break
        else:
            bucket.append(c)
    return by_field


def _same(a: Candidate, b: Candidate) -> bool:
    if a.value is None or b.value is None:
        return False
    if isinstance(a.value, float) and isinstance(b.value, (int, float)):
        return abs(a.value - b.value) < 0.01
    return norm(str(a.value)) == norm(str(b.value))


def _fuse(c: Candidate) -> float:
    weights = {
        layer: (
            float(c.evidence.get("l6_weight", 0.0))
            if layer == "L6"
            else LAYER_WEIGHTS.get(layer, 0.05)
        )
        for layer in c.layers
    }
    num = sum(weights[layer] * score for layer, score in c.layers.items())
    den = sum(weights.values()) or 1.0
    return num / den


# --- validation ----------------------------------------------------------
def _best(cands: list[Candidate]):
    return max(cands, key=lambda c: c.fused_score) if cands else None


def _validate(
    by_field: dict[str, list[Candidate]], rec: InvoiceRecord
) -> ValidationResult:
    v = ValidationResult()
    force: list[str] = []

    # GSTIN checksum: promote a checksum-valid candidate.
    for c in by_field.get("gstin", []):
        if gstin_check(str(c.value)):
            c.fused_score = max(c.fused_score, 0.98)
            v.gstin = "PASS"

    # Arithmetic identity, combinatorial over subTotal/total.
    v.arith = _validate_arith(by_field, v)

    # Tax structural.
    _validate_tax(by_field, v)

    # Date FY cross-check.
    if _fy_violation(by_field):
        v.date = "FAIL"
        v.flags.append("DATE_FY_MISMATCH")
        force.append("date")

    # Currency inference.
    v.currency = _validate_currency(by_field, v)

    v.details["force_arbitration"] = force
    return v


def _amt(by_field, field) -> float | None:
    b = _best(by_field.get(field, []))
    return b.value if b and isinstance(b.value, (int, float)) else None


def _rate(by_field, field) -> float:
    b = _best(by_field.get(field, []))
    return b.value if b and isinstance(b.value, (int, float)) else 0.0


def _validate_arith(by_field, v: ValidationResult) -> str:
    sub_cands = sorted(
        by_field.get("subTotal", []), key=lambda c: c.fused_score, reverse=True
    )[:2]
    tot_cands = sorted(
        by_field.get("totalBillAmount", []), key=lambda c: c.fused_score, reverse=True
    )[:2]
    if not sub_cands or not tot_cands:
        return "UNDECIDABLE"
    rate = (
        _rate(by_field, "gstPct")
        + _rate(by_field, "cgstPct")
        + _rate(by_field, "sgstPct")
        + _rate(by_field, "otherTaxesPct")
    )
    roundoff = _amt(by_field, "roundOff") or 0.0
    tol = (
        config.ARITH_TOL_WITH_ROUNDOFF
        if by_field.get("roundOff")
        else config.ARITH_TOL_NO_ROUNDOFF
    )

    best = None
    for sc in sub_cands:
        for tc in tot_cands:
            if not (
                isinstance(sc.value, (int, float))
                and isinstance(tc.value, (int, float))
            ):
                continue
            expected = sc.value * (1 + rate / 100) + roundoff
            if abs(expected - tc.value) <= tol:
                best = (sc, tc)
                break
        if best:
            break
    if best:
        sc, tc = best
        sc.fused_score = max(sc.fused_score, VALID_FLOOR)
        tc.fused_score = max(tc.fused_score, VALID_FLOOR)
        v.details["arith_delta"] = round(
            sc.value * (1 + rate / 100) + roundoff - tc.value, 2
        )
        return "PASS"
    # Nothing reconciles: demote the current tops so disputes surface.
    for c in sub_cands[:1] + tot_cands[:1]:
        c.fused_score *= CONTRADICTION_FACTOR
    return "FAIL"


def _validate_tax(by_field, v: ValidationResult) -> None:
    cgst, sgst = _rate(by_field, "cgstPct"), _rate(by_field, "sgstPct")
    igst = _rate(by_field, "gstPct")
    if cgst and sgst and abs(cgst - sgst) > 0.01:
        v.tax = "FAIL"
        v.flags.append("CGST_SGST_ASYMMETRIC")
    elif igst and (cgst or sgst):
        v.tax = "FAIL"
        v.flags.append("IGST_WITH_CGST_SGST")
    elif cgst or sgst or igst:
        v.tax = "PASS"
    for f in ("gstPct", "cgstPct", "sgstPct"):
        r = _rate(by_field, f)
        if r and r not in GST_SLABS:
            v.flags.append(f"OFFSLAB_{f}")


def _fy_violation(by_field) -> bool:
    bill = _best(by_field.get("billNumber", []))
    d = _best(by_field.get("date", []))
    if not bill or not d or not isinstance(d.value, str):
        return False
    m = _FY_RE.search(str(bill.value))
    if not m:
        return False
    try:
        year = int(d.value[:4])
    except ValueError, TypeError:
        return False
    fy_start = 2000 + int(m.group(1))
    # Indian FY: Apr(start year)–Mar(start+1). Accept either calendar year.
    return year not in (fy_start, fy_start + 1)


def _validate_currency(by_field, v: ValidationResult) -> str:
    cur = _best(by_field.get("currency", []))
    if cur and cur.value:
        return "EXPLICIT"
    gstin = _best(by_field.get("gstin", []))
    if gstin and gstin_check(str(gstin.value)):
        by_field.setdefault("currency", []).append(
            Candidate(
                field="currency",
                value="INR",
                value_raw="INR(inferred)",
                layers={"L3": 0.6},
                fused_score=0.6,
                evidence={"inferred": True},
            )
        )
        v.flags.append("CUR_INFERRED")
        return "INFERRED"
    return "UNDECIDABLE"


# --- helpers ------------------------------------------------------------------
def _apply(fr: FieldResult, c: Candidate) -> None:
    fr.value = c.value
    fr.value_raw = c.value_raw
    fr.confidence = c.fused_score
    fr.source_layer = "+".join(sorted(c.layers))
    fr.page = c.page
    fr.polygon = c.polygon


def _dispute(field: str, cands: list[Candidate], inv, pdf_bytes) -> dict:
    crops = []
    for c in cands[:2]:
        if c.polygon and pdf_bytes:
            try:
                crops.append(render.crop_region(pdf_bytes, c.page - 1, c.polygon))
            except Exception:
                pass
    return {
        "field": field,
        "definition": _FIELD_DEFS.get(field, field),
        "candidates": [
            {"value": c.value, "source": "+".join(c.layers)} for c in cands[:3]
        ],
        "crops": crops,
        "note": "",
    }


def _vendor_key(rec: InvoiceRecord) -> str | None:
    g = rec.fields.get("gstin")
    if g and g.value and gstin_check(str(g.value)):
        return str(g.value)
    n = rec.fields.get("billerName")
    return f"NAME::{norm(n.value)}" if n and n.value else None


def _finalize_review(rec: InvoiceRecord) -> None:
    if any(
        f in ("FAIL",)
        for f in (rec.validation.arith, rec.validation.tax, rec.validation.date)
    ):
        rec.needs_review = True
    for fr in rec.fields.values():
        if fr.value is not None and fr.confidence < ACCEPT_CONF:
            rec.needs_review = True
    if rec.validation.flags:
        rec.needs_review = rec.needs_review or any(
            not f.startswith("CUR_") for f in rec.validation.flags
        )


_FIELD_DEFS = {
    "billerName": "Legal/trade name of the party issuing the invoice (the vendor).",
    "payee": "The bill-to party — the customer who pays.",
    "billNumber": "Vendor's invoice/bill identifier, verbatim.",
    "date": "Invoice date (not due date), day-first.",
    "gstin": "The biller's 15-char GSTIN.",
    "billerAddress": "The biller's address block.",
    "gstPct": "Aggregate GST rate (IGST, or CGST+SGST).",
    "subTotal": "Taxable value before taxes.",
    "totalBillAmount": "Grand total payable.",
    "roundOff": "Signed rounding adjustment, |value|<1.",
}


def _demo() -> None:
    # Combinatorial V-ARITH picks the reconciling pair, not the highest-scored one.
    def c(field, value, score):
        return Candidate(
            field=field, value=value, fused_score=score, layers={"L2": score}
        )

    by_field = {
        "subTotal": [c("subTotal", 1000.0, 0.60), c("subTotal", 1200.0, 0.55)],
        "totalBillAmount": [
            c("totalBillAmount", 1180.0, 0.60),
            c("totalBillAmount", 1500.0, 0.55),
        ],
        "cgstPct": [c("cgstPct", 9.0, 0.7)],
        "sgstPct": [c("sgstPct", 9.0, 0.7)],
    }
    v = ValidationResult()
    assert _validate_arith(by_field, v) == "PASS", "expected identity to reconcile"
    winner_sub = _best(by_field["subTotal"])
    assert winner_sub is not None
    assert winner_sub.value == 1000.0 and winner_sub.fused_score >= VALID_FLOOR, (
        winner_sub
    )
    # Tax symmetry holds (9+9), off-slab flags nothing.
    _validate_tax(by_field, v)
    assert v.tax == "PASS", v.tax
    print("extraction self-check ok")


if __name__ == "__main__":
    _demo()
