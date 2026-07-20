"""Vendor-layout template matching, L6 candidate generation, and learning."""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from src import config
from src.models import (
    Candidate,
    InvoiceRecord,
    LearningObservation,
    StaticToken,
    TemplateDocument,
    TemplateFieldPrior,
    TemplateFingerprint,
    TemplateMatch,
    TemplateStats,
)
from src.parsing.client import Line, LogicalInvoice
from src.template_generation.store import TemplateStore
from src.utils.render import poly_bbox
from src.utils.text import (
    GSTIN_RE,
    fuzzy_eq,
    gstin_check,
    norm,
    parse_amount,
    parse_date,
    parse_percent,
)


class TemplateEngine:
    """Read active priors for extraction and write quarantined learned candidates."""

    def __init__(self, store: TemplateStore) -> None:
        self._store = store
        self._active = store.load_active()

    def reload(self) -> None:
        self._active = self._store.load_active()

    def propose(self, inv: LogicalInvoice) -> tuple[TemplateMatch, list[Candidate]]:
        incoming = build_fingerprint(inv)
        keys = _vendor_keys(inv)
        choices = [template for template in self._active if template.vendor_key in keys]
        if not choices:
            return TemplateMatch(), []

        ranked = sorted(
            ((_match(template, incoming), template) for template in choices),
            key=lambda item: item[0].score,
            reverse=True,
        )
        match, template = ranked[0]
        if match.verdict != "MATCH":
            return match, []
        return match, _l6_candidates(inv, template, match)

    def learn_candidate(
        self,
        inv: LogicalInvoice,
        record: InvoiceRecord,
        observation: LearningObservation,
    ) -> TemplateDocument | None:
        if not observation.confirmed:
            return None
        templates = self._store.load_candidate()
        fingerprint = build_fingerprint(inv)
        vendor_templates = [
            template
            for template in templates
            if template.vendor_key == observation.vendor_key
        ]
        ranked = sorted(
            (
                (_match(template, fingerprint), template)
                for template in vendor_templates
            ),
            key=lambda item: item[0].score,
            reverse=True,
        )
        matched = ranked[0] if ranked else None
        if matched and matched[0].verdict == "MATCH":
            template = matched[1]
            match_score = matched[0].score
        else:
            next_variant = (
                max((item.variant for item in vendor_templates), default=0) + 1
            )
            template = TemplateDocument(
                id=f"{observation.vendor_key}::v{next_variant}",
                vendor_key=observation.vendor_key,
                variant=next_variant,
                fingerprint=fingerprint,
                stats=TemplateStats(created_from_eval_id=observation.eval_id),
                schema_version=config.TEMPLATE_SCHEMA_VERSION,
            )
            templates.append(template)
            match_score = 0.0

        _update_template(template, inv, record, observation, fingerprint, match_score)
        self._store.save_candidate(templates)
        return template


def build_fingerprint(inv: LogicalInvoice) -> TemplateFingerprint:
    """Build a deterministic whole-page fingerprint in normalised coordinates."""
    pages = {page.number: page for page in inv.parsed.pages}
    bits = 0
    tokens: list[StaticToken] = []
    anchors = {
        norm(anchor.lstrip("-"))
        for variants in config.ANCHOR_LEXICON.values()
        for anchor in variants
    }
    anchors.update(norm(token) for token in config.INVOICE_TITLE_ANCHORS)

    for line in inv.lines:
        page = pages.get(line.page)
        if not page:
            continue
        cx, cy, _width, _height = _normalised_bbox(
            line.polygon, page.width, page.height
        )
        row = min(int(cy * config.TEMPLATE_GRID_ROWS), config.TEMPLATE_GRID_ROWS - 1)
        col = min(int(cx * config.TEMPLATE_GRID_COLS), config.TEMPLATE_GRID_COLS - 1)
        bits |= 1 << (row * config.TEMPLATE_GRID_COLS + col)
        label = line.text.strip()
        if any(
            fuzzy_eq(label.split(":", 1)[0], anchor) for anchor in anchors
        ) or label.endswith(":"):
            tokens.append(StaticToken(text=label, cx=cx, cy=cy))

    col_centres, headers = _table_signature(inv, pages)
    token_names = [token.text for token in tokens]
    registration = [
        text
        for text in config.INVOICE_TITLE_ANCHORS + ["GSTIN", "Authorised Signatory"]
        if any(fuzzy_eq(token, text) for token in token_names)
    ][:3]
    return TemplateFingerprint(
        grid_bits_hex=f"{bits:X}",
        static_tokens=_dedupe_tokens(tokens),
        table_col_centres=col_centres,
        table_header_tokens=headers,
        registration_anchors=registration,
    )


def _vendor_keys(inv: LogicalInvoice) -> set[str]:
    keys = {
        match.group()
        for match in GSTIN_RE.finditer(inv.full_text.replace(" ", "").upper())
        if gstin_check(match.group())
    }
    fields = getattr(inv.di_document, "fields", None) or {}
    vendor = fields.get("VendorName") if hasattr(fields, "get") else None
    value = (
        getattr(vendor, "value_string", None) or getattr(vendor, "content", None)
        if vendor
        else None
    )
    if value:
        keys.add(_name_vendor_key(str(value)))
    return keys


def _name_vendor_key(value: str) -> str:
    cleaned = re.sub(
        r"\b(PVT|PRIVATE|LTD|LIMITED|LLP|AND CO|TRADERS)\b", "", value.upper()
    )
    cleaned = re.sub(r"[^A-Z0-9]+", " ", cleaned)
    return "NAME::" + " ".join(cleaned.split())


def _match(template: TemplateDocument, incoming: TemplateFingerprint) -> TemplateMatch:
    stage_a = _hamming_similarity(
        template.fingerprint.grid_bits_hex, incoming.grid_bits_hex
    )
    if stage_a < config.TEMPLATE_STAGE_A_MIN:
        return TemplateMatch(
            template_id=template.id,
            vendor_key=template.vendor_key,
            stage_a_score=stage_a,
            verdict="NEW_VARIANT",
        )

    dx, dy, registration_skipped = _registration_delta(template.fingerprint, incoming)
    tolerance = config.TEMPLATE_TOKEN_TOL * (
        config.TEMPLATE_REGISTRATION_WIDEN if registration_skipped else 1.0
    )
    token_score = _token_alignment(
        template.fingerprint.static_tokens,
        incoming.static_tokens,
        dx,
        dy,
        tolerance,
    )
    bbox_score = _bbox_alignment(
        template.fingerprint.static_tokens,
        incoming.static_tokens,
        dx,
        dy,
    )
    table_score = _table_alignment(template.fingerprint, incoming)
    score = (
        config.TEMPLATE_TOKEN_WEIGHT * token_score
        + config.TEMPLATE_BBOX_WEIGHT * bbox_score
        + config.TEMPLATE_TABLE_WEIGHT * table_score
    )
    if score >= config.TEMPLATE_MATCH_MIN:
        verdict = "MATCH"
    elif score >= config.TEMPLATE_GRAY_MIN:
        verdict = "GRAY_ZONE"
    else:
        verdict = "NEW_VARIANT"
    return TemplateMatch(
        template_id=template.id,
        vendor_key=template.vendor_key,
        score=score,
        stage_a_score=stage_a,
        verdict=verdict,
        registration_skipped=registration_skipped,
    )


def _l6_candidates(
    inv: LogicalInvoice, template: TemplateDocument, match: TemplateMatch
) -> list[Candidate]:
    pages = {page.number: page for page in inv.parsed.pages}
    maturity = min(template.stats.instances_seen / 10, 1.0)
    weight = config.TEMPLATE_L6_MAX_WEIGHT * maturity
    out: list[Candidate] = []
    for field, prior in template.fields.items():
        if prior.consecutive_miss >= config.TEMPLATE_FIELD_DEMOTE_MISSES:
            continue
        if prior.bbox_prior.n == 0:
            continue
        line, distance = _nearest_prior_line(inv.lines, pages, prior.bbox_prior.mean)
        if not line or distance > config.TEMPLATE_BBOX_TOL:
            continue
        value = _normalise_value(field, line.text)
        if value is None:
            continue
        score = max(0.0, 1.0 - distance / config.TEMPLATE_BBOX_TOL)
        pattern = prior.value_pattern
        if pattern.active and pattern.regex and re.fullmatch(pattern.regex, str(value)):
            score = min(score * 1.3, 1.0)
        out.append(
            Candidate(
                field=field,
                value=value,
                value_raw=line.text,
                page=line.page,
                polygon=line.polygon,
                layers={"L6": score},
                evidence={
                    "template_id": template.id,
                    "template_score": match.score,
                    "l6_weight": weight,
                },
            )
        )
    return out


def _update_template(
    template: TemplateDocument,
    inv: LogicalInvoice,
    record: InvoiceRecord,
    observation: LearningObservation,
    incoming: TemplateFingerprint,
    match_score: float,
) -> None:
    template.stats.instances_seen += 1
    template.stats.last_seen = datetime.now(timezone.utc).isoformat()
    template.stats.health = "QUARANTINED"
    template.stats.match_score_ewma = (
        match_score
        if template.stats.instances_seen == 1
        else 0.8 * template.stats.match_score_ewma + 0.2 * match_score
    )
    if template.stats.instances_seen > 1:
        template.fingerprint.static_tokens = _stable_token_intersection(
            template.fingerprint.static_tokens, incoming.static_tokens
        )
    template.fingerprint.grid_bits_hex = incoming.grid_bits_hex
    if incoming.table_col_centres:
        template.fingerprint.table_col_centres = incoming.table_col_centres
        template.fingerprint.table_header_tokens = incoming.table_header_tokens

    page_by_number = {page.number: page for page in inv.parsed.pages}
    for field, value in observation.field_values.items():
        prior = template.fields.setdefault(field, TemplateFieldPrior())
        polygon = observation.field_polygons.get(field)
        result = record.fields.get(field)
        page = page_by_number.get(result.page) if result and result.page else None
        if polygon and page:
            prior.bbox_prior.update(
                _normalised_bbox(polygon, page.width, page.height),
                observation.correction_weight,
            )
            prior.hit += 1
            prior.consecutive_miss = 0
        else:
            prior.miss += 1
            prior.consecutive_miss += 1
        if value not in (None, ""):
            _update_pattern(prior, str(value))


def _update_pattern(prior: TemplateFieldPrior, sample: str) -> None:
    pattern = prior.value_pattern
    if sample not in pattern.samples:
        pattern.samples = (pattern.samples + [sample])[-10:]
    if pattern.active and pattern.regex and not re.fullmatch(pattern.regex, sample):
        pattern.violations += 1
        if pattern.violations >= config.TEMPLATE_PATTERN_MAX_VIOLATIONS:
            pattern.active = False
    if len(pattern.samples) >= config.TEMPLATE_PATTERN_MIN_SAMPLES:
        pattern.regex = induce_pattern(pattern.samples)
        pattern.active = pattern.regex is not None
        pattern.violations = 0


def induce_pattern(samples: list[str]) -> str | None:
    """Induce a conservative per-character-class regex from confirmed values."""
    if not samples or len({len(sample) for sample in samples}) != 1:
        return None
    columns = list(zip(*samples))
    classes = [_character_class(chars) for chars in columns]
    tokens: list[str] = []
    index = 0
    while index < len(columns):
        end = index + 1
        while end < len(columns) and classes[end] == classes[index]:
            end += 1
        variable_run = any(len(set(columns[pos])) > 1 for pos in range(index, end))
        for pos in range(index, end):
            token = classes[pos]
            if variable_run and token is not None:
                tokens.append(token)
            elif len(set(columns[pos])) == 1:
                tokens.append(re.escape(columns[pos][0]))
            else:
                tokens.append(".")
        index = end
    return _collapse_regex_tokens(tokens)


def _character_class(chars: tuple[str, ...]) -> str | None:
    if all(char.isdigit() for char in chars):
        return r"\d"
    if all("A" <= char <= "Z" for char in chars):
        return "[A-Z]"
    if all(char.isalpha() for char in chars):
        return "[A-Za-z]"
    return None


def _collapse_regex_tokens(tokens: list[str]) -> str:
    out: list[str] = []
    index = 0
    while index < len(tokens):
        end = index + 1
        while end < len(tokens) and tokens[end] == tokens[index]:
            end += 1
        count = end - index
        out.append(tokens[index] if count == 1 else f"{tokens[index]}{{{count}}}")
        index = end
    return "".join(out)


def _normalise_value(field: str, text: str) -> Any:
    if field in config.AMOUNT_FIELDS:
        return parse_amount(text)
    if field in config.PERCENT_FIELDS:
        return parse_percent(text)
    if field == "date":
        return parse_date(text)
    if field == "gstin":
        match = GSTIN_RE.search(text.replace(" ", "").upper())
        return match.group() if match else None
    if field == "totalLineItems":
        amount = parse_amount(text)
        return int(amount) if amount is not None else None
    return text.strip() or None


def _normalised_bbox(
    polygon: list[float], page_width: float, page_height: float
) -> list[float]:
    x0, y0, x1, y1 = poly_bbox(polygon)
    width = page_width or 1.0
    height = page_height or 1.0
    return [
        ((x0 + x1) / 2) / width,
        ((y0 + y1) / 2) / height,
        (x1 - x0) / width,
        (y1 - y0) / height,
    ]


def _nearest_prior_line(
    lines: Iterable[Line], pages: dict[int, Any], mean: list[float]
) -> tuple[Line | None, float]:
    best: Line | None = None
    best_distance = math.inf
    for line in lines:
        page = pages.get(line.page)
        if not page:
            continue
        cx, cy, _width, _height = _normalised_bbox(
            line.polygon, page.width, page.height
        )
        distance = math.hypot(cx - mean[0], cy - mean[1])
        if distance < best_distance:
            best, best_distance = line, distance
    return best, best_distance


def _hamming_similarity(left_hex: str, right_hex: str) -> float:
    left, right = int(left_hex or "0", 16), int(right_hex or "0", 16)
    cells = config.TEMPLATE_GRID_ROWS * config.TEMPLATE_GRID_COLS
    return 1.0 - ((left ^ right).bit_count() / cells)


def _registration_delta(
    template: TemplateFingerprint, incoming: TemplateFingerprint
) -> tuple[float, float, bool]:
    offsets: list[tuple[float, float]] = []
    for anchor in template.registration_anchors:
        expected = next(
            (token for token in template.static_tokens if fuzzy_eq(token.text, anchor)),
            None,
        )
        actual = next(
            (token for token in incoming.static_tokens if fuzzy_eq(token.text, anchor)),
            None,
        )
        if expected and actual:
            offsets.append((expected.cx - actual.cx, expected.cy - actual.cy))
    if len(offsets) < 2:
        return 0.0, 0.0, True
    xs, ys = sorted(item[0] for item in offsets), sorted(item[1] for item in offsets)
    mid = len(offsets) // 2
    return xs[mid], ys[mid], False


def _token_alignment(
    expected: list[StaticToken],
    actual: list[StaticToken],
    dx: float,
    dy: float,
    tolerance: float,
) -> float:
    if not expected:
        return 1.0
    hits = 0
    for token in expected:
        if any(
            fuzzy_eq(other.text, token.text)
            and math.hypot(other.cx + dx - token.cx, other.cy + dy - token.cy)
            <= tolerance
            for other in actual
        ):
            hits += 1
    return hits / len(expected)


def _bbox_alignment(
    expected: list[StaticToken], actual: list[StaticToken], dx: float, dy: float
) -> float:
    distances: list[float] = []
    for token in expected:
        candidates = [other for other in actual if fuzzy_eq(other.text, token.text)]
        if candidates:
            distances.append(
                min(
                    math.hypot(other.cx + dx - token.cx, other.cy + dy - token.cy)
                    for other in candidates
                )
            )
    if not distances:
        return 0.0
    return sum(
        max(0.0, 1.0 - distance / config.TEMPLATE_BBOX_TOL) for distance in distances
    ) / len(distances)


def _table_alignment(
    expected: TemplateFingerprint, actual: TemplateFingerprint
) -> float:
    if not expected.table_col_centres:
        return 1.0
    if len(expected.table_col_centres) != len(actual.table_col_centres):
        return 0.0
    centres = sum(
        max(0.0, 1.0 - abs(left - right) / config.TEMPLATE_BBOX_TOL)
        for left, right in zip(expected.table_col_centres, actual.table_col_centres)
    ) / len(expected.table_col_centres)
    expected_headers = {norm(value) for value in expected.table_header_tokens}
    actual_headers = {norm(value) for value in actual.table_header_tokens}
    overlap = (
        len(expected_headers & actual_headers) / len(expected_headers)
        if expected_headers
        else 1.0
    )
    return (centres + overlap) / 2


def _table_signature(
    inv: LogicalInvoice, pages: dict[int, Any]
) -> tuple[list[float], list[str]]:
    if not inv.tables:
        return [], []
    table = max(inv.tables, key=lambda item: len(getattr(item, "cells", []) or []))
    centres: dict[int, list[float]] = {}
    headers: list[str] = []
    for cell in getattr(table, "cells", []) or []:
        content = str(getattr(cell, "content", "") or "")
        if getattr(cell, "kind", "") == "columnHeader":
            headers.append(content)
        regions = getattr(cell, "bounding_regions", None) or []
        if not regions:
            continue
        region = regions[0]
        page = pages.get(getattr(region, "page_number", 1))
        polygon = list(getattr(region, "polygon", []) or [])
        if not page or not polygon:
            continue
        cx, _cy, _width, _height = _normalised_bbox(polygon, page.width, page.height)
        centres.setdefault(getattr(cell, "column_index", 0), []).append(cx)
    return (
        [sum(values) / len(values) for _, values in sorted(centres.items())],
        headers,
    )


def _dedupe_tokens(tokens: list[StaticToken]) -> list[StaticToken]:
    seen: set[tuple[str, int, int]] = set()
    out: list[StaticToken] = []
    for token in tokens:
        key = (norm(token.text), round(token.cx * 100), round(token.cy * 100))
        if key not in seen:
            seen.add(key)
            out.append(token)
    return out[:100]


def _stable_token_intersection(
    expected: list[StaticToken], actual: list[StaticToken]
) -> list[StaticToken]:
    stable = [
        token
        for token in expected
        if any(
            fuzzy_eq(token.text, other.text)
            and math.hypot(token.cx - other.cx, token.cy - other.cy)
            <= config.TEMPLATE_TOKEN_TOL
            for other in actual
        )
    ]
    return stable or expected
