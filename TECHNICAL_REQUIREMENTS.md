# Invoice Extraction Pipeline — Technical Design Document

| | |
|---|---|
| Version | 1.0 (Draft) |
| Date | 2026-07-18 |
| Scope | Phase 1 — SharePoint → Cron → Extraction → Master Workbook |
| Status | For review |
| Systems | Azure Document Intelligence, Vision LLM, Azure Cosmos DB, Azure Blob Storage, SharePoint (Microsoft Graph), Python |

---

## Table of Contents

1. Purpose, Scope, and Non-Goals
2. Output Schema and Field Definitions
3. System Architecture Overview
4. Ingestion and Deduplication
5. Preprocessing Pipeline
6. Azure Document Intelligence Pass
7. Layered Extraction Engine
8. Deterministic Validation Layer
9. Vision LLM Arbitration
10. Template System
11. Persistence Layer — Cosmos DB and Blob Storage
12. Master Workbook (Phase 1 Presentation Layer)
13. Cron Orchestration, Idempotency, and Failure Handling
14. Observability and Cost Accounting
15. Security Notes
16. Phase Roadmap and Evaluation Harness
17. Suggestions and Future Enhancements
18. Appendices (Tunable Constants, Anchor Lexicon, Open Decisions)

---

## 1. Purpose, Scope, and Non-Goals

### 1.1 Purpose

Build a Python-based pipeline that extracts a fixed header-level schema from scanned invoice PDFs (primarily Indian GST invoices) deposited in a SharePoint folder, writes the results into a master Excel workbook, and maintains Cosmos DB as the durable system of record. The pipeline is designed to become cheaper and more accurate over time through a per-vendor template system and, later, a human-correction feedback loop.

The core extraction strategy is layered: a single expensive Azure Document Intelligence (DI) pass captures text, positions, and structure; multiple cheap deterministic layers (anchors, position priors, data-type patterns, table structure, template priors) generate ranked candidates per field; arithmetic and format validation promotes or demotes candidates; and a vision LLM is invoked only for fields that remain ambiguous, receiving cropped image regions plus the top candidate guesses rather than the full document.

### 1.2 In Scope (Phase 1)

Automated detection and download of new PDFs from a designated SharePoint folder via a scheduled cron job; content-hash deduplication; DI extraction; layered candidate generation and confidence fusion; deterministic validation; conditional vision-LLM arbitration on cropped regions; template creation, matching, and prior injection keyed on vendor identity (GSTIN); persistence of all results and run metadata in Cosmos DB with raw artifacts in Blob Storage; append/upsert of extracted rows into a master Excel workbook on SharePoint; failure isolation and idempotent re-runs.

### 1.3 Out of Scope (Phase 1)

Line-item-level output schema (the line-item table is parsed internally for counting and validation, but individual rows are not emitted to the workbook); the custom human-validation UI (planned Phase 2 — the workbook carries correction columns as an interim mechanism); confidence calibration against labeled data (requires the eval corpus that Phase 1 begins to accumulate); non-INR / non-GST invoice regimes beyond graceful degradation; storing invoice binaries inside Cosmos DB (explicit decision: values and metadata only in Cosmos; binaries live in Blob and SharePoint).

### 1.4 Design Principles

1. Cosmos DB is the source of truth. The master workbook is a generated, human-facing view that can be rebuilt from Cosmos at any time.
2. Pay for DI once per file, pay for the LLM only when deterministic layers disagree or fail validation. Persist every expensive intermediate (DI JSON, rendered pages) so no failure forces a re-purchase.
3. Every extraction decision is auditable: which layers proposed which candidates, what the fusion score was, what validation concluded, and whether the LLM overrode.
4. Templates are priors, never ground truth. Every template-driven answer still passes through validation, and templates degrade field-by-field when they stop working.
5. The whole run is idempotent: running the cron twice with no new files produces zero changes.

---

## 2. Output Schema and Field Definitions

### 2.1 Field Table

The master workbook and the Cosmos invoice document share this logical schema. Not every field is present on every invoice; absent fields are emitted as null/blank, never guessed.

| # | Field | Type | Origin | Definition and notes |
|---|-------|------|--------|----------------------|
| 1 | Eval ID | string | System-generated | Deterministic identity of one extracted invoice. See §11.4. Join key across workbook, Cosmos, corrections, and evals. |
| 2 | Biller Name | string | Extracted | Legal/trade name of the party issuing the invoice (the vendor / "From" party). |
| 3 | Payee | string | Extracted | **Definition decision:** in this system, Payee means the bill-to party — the customer who pays (normally our own organization). Note the naming is inverted relative to common usage (a "payee" ordinarily receives payment); recommend renaming to `BillToName` in Phase 2 to avoid downstream confusion. Until then this definition is authoritative. |
| 4 | Bill Number | string | Extracted | Vendor's invoice/bill identifier, preserved verbatim including separators (e.g., `INV-1042/25-26`). |
| 5 | Date | date (ISO 8601 stored; dd-mm-yyyy displayed) | Extracted | Invoice date, not due date, not e-way bill date. Indian day-first formats dominate; see §8.6 for parsing rules. |
| 6 | GSTIN | string(15) | Extracted | The **biller's** GSTIN. Invoices routinely show two GSTINs (biller and payee); disambiguation rules in §7.4 and §8.2. Checksum-validated. |
| 7 | Biller Address | string (multi-line collapsed) | Extracted | Assembled from the vertically grouped address block adjacent to the biller name (§5.4). |
| 8 | Biller Phone Number | string | Extracted | Normalized to digits with optional `+91` prefix retained. |
| 9 | GST % | decimal | Extracted / derived | Aggregate GST rate. Intra-state: CGST% + SGST%. Inter-state: the IGST rate is recorded here with CGST%/SGST% left null (decision D-03, §18.3). Mixed-rate invoices: see §8.4. |
| 10 | CGST % | decimal | Extracted | Central GST rate. |
| 11 | SGST % | decimal | Extracted | State GST rate. Intra-state invoices satisfy CGST% = SGST%. |
| 12 | Other Taxes % | decimal | Extracted | Cess, TCS, or any additional levy expressed as a rate. If only an amount is printed, the rate is derived as amount/SubTotal when SubTotal is trusted, else null with the amount preserved in Cosmos metadata. |
| 13 | Sub Total | decimal(2) | Extracted | Taxable value before taxes (anchors: "Sub Total", "Taxable Value", "Taxable Amt"). |
| 14 | Total Bill Amount | decimal(2) | Extracted | Grand total payable (anchors: "Grand Total", "Amount Payable", "Total (INR)"). |
| 15 | Currency | string(3) | Extracted / inferred | ISO 4217. `₹`/`Rs.`/`INR` → INR. Valid GSTIN with no contrary symbol → INR inferred (export/SEZ exception flagged, §8.5). |
| 16 | Total Line Items | integer | Derived | Count of item rows in the main line-item table (data rows only — header, subtotal, tax, and total rows excluded). Cross-checked against printed "Total Items"/"No. of Items" text when present. Caution: "Total Qty" is **not** the item count. |
| 17 | Round Off | decimal(2) | Extracted | Signed rounding adjustment; expected \|value\| < 1.00 (§8.3). |
| 18 | File Path | string | System | SharePoint path of the source PDF (plus driveId/itemId stored in Cosmos for durable reference). |

### 2.2 Workbook-Only Companion Columns

Beyond the 18 schema fields, the workbook row carries operational columns (populated by the cron) and correction columns (populated only by humans). Full layout in §12.2. The critical rule established for the feedback loop: extracted values and human-corrected values live in separate columns, keyed by Eval ID, so the (predicted, corrected) pair is never destroyed — that pair is simultaneously eval data, template-update signal, and future layer-weight training data.

### 2.3 Per-Field Provenance (Cosmos Only)

For every field, Cosmos stores what the workbook cannot: the full ranked candidate list, per-candidate contributing layers and scores, winning source layer, DI polygon and page number, validation verdicts touching that field, and the LLM's verdict if arbitration ran. This provenance is what makes per-layer accuracy analysis possible later (§14, §16).

---

## 3. System Architecture Overview

### 3.1 Component Map

```
SharePoint drop folder
        │  (Graph API delta listing, cron-triggered)
        ▼
[ Ingestion & Dedup ] ── SHA-256 gate ──► known file → skip / soft-dup check
        │ new file
        ▼
[ Staging ]  original.pdf → Blob (invoices-raw/{sha256}/)
        │
        ▼
[ Preprocessing ]  render pages @300 DPI → Blob; page metrics; block grouping prep
        │
        ▼
[ Azure Document Intelligence ]  prebuilt-invoice (+ prebuilt-layout)
        │  raw JSON → Blob immediately (cost protection)
        ▼
[ Invoice Segmentation ]  1 file → N logical invoices (Eval IDs minted here)
        │  per logical invoice
        ▼
[ Template Lookup ]  GSTIN regex over raw text → Cosmos templates → fingerprint match
        │  (match: inject priors as extraction layer L6)
        ▼
[ Layered Extraction Engine ]  L1..L6 → ranked candidates per field
        │
        ▼
[ Deterministic Validation ]  arithmetic identities, GSTIN checksum, symmetry, FY
        │
        ├── all fields confident & validated ──────────────► skip LLM
        ▼
[ Vision LLM Arbitration ]  per-field crops + candidates → verdicts (conditional)
        │
        ▼
[ Persistence ]  Cosmos upsert (invoices) ── artifacts already in Blob
        │
        ▼
[ Master Workbook Writer ]  Graph API Excel table upsert by Eval ID
        │
        ▼
[ Template Learner ]  confirmed extractions → prior updates / new templates
        │
        ▼
[ Run Finalizer ]  run document in Cosmos (runs container), lock release
```

### 3.2 Per-File Processing State Machine

Each file (and each logical invoice within it) advances through explicit states persisted in Cosmos, enabling resume-from-stage on retry without repeating paid work:

`LISTED → HASHED → STAGED → RENDERED → DI_DONE → SEGMENTED → EXTRACTED → VALIDATED → LLM_DONE → PERSISTED → SHEET_WRITTEN → TEMPLATE_UPDATED → DONE`

Failure at any stage records `FAILED_{STAGE}` with the error payload; §13.4 defines retry and poison semantics per stage.

### 3.3 Technology Choices

| Concern | Choice | Rationale |
|---|---|---|
| OCR / structure | Azure Document Intelligence v4 GA API (pin the API version in config; verify current GA version at build time) | `prebuilt-invoice` covers ~60–70% of the schema directly with per-field confidences; `prebuilt-layout` supplies words/lines/tables/roles for the custom layers. |
| PDF rendering | PyMuPDF (fitz) | Fast 300-DPI rasterization; deterministic page geometry for crop mapping. |
| Arbitration | Vision LLM via API (provider is Open Decision D-01) | Receives crops + candidates, returns strict JSON. Temperature 0. |
| State & truth | Azure Cosmos DB (NoSQL API) | Three containers: `invoices`, `runs`, `templates` (templates decision confirmed — templates and their configs live in Cosmos alongside history). |
| Artifacts | Azure Blob Storage | Original PDFs mirror, DI raw JSON, rendered pages, ephemeral crops. |
| Workbook I/O | Microsoft Graph workbook API (table-object operations, not raw ranges) | Appends survive user sorting/filtering; session + retry semantics for lock contention. |
| Scheduler | Cron (App Service WebJob / Azure Function timer / container cron — Open Decision D-02) | Single concurrent run enforced by a lease (§13.1). |

---

## 4. Ingestion and Deduplication

### 4.1 Listing New Files

The cron lists the drop folder via Graph (`/drives/{driveId}/root:/{folderPath}:/children`, or delta queries once volume warrants). The `lastModifiedDateTime > lastSuccessfulRunTimestamp` filter is used **only as a listing optimization** — it narrows which files get downloaded and hashed. It is never the source of truth for novelty, because modified-time is unreliable in exactly the ways that matter: re-uploads reset it, metadata edits bump it without content change, and clock skew exists.

### 4.2 File Identity: Content Hash

Novelty is decided by `SHA-256(file bytes)` against the Cosmos `invoices` container (and a lightweight `fileIndex` document type, §11.2). Rules:

1. Hash unseen → new file → proceed to staging.
2. Hash seen with terminal status `DONE` → skip; increment the run's `knownSkipped` counter; if the SharePoint path differs from the recorded one, append the new path to the Cosmos record's `alternatePaths` (same bytes uploaded twice under different names is a common user behavior and must not double-process).
3. Hash seen with non-terminal status (`FAILED_*`, mid-pipeline) → resume from the recorded stage (§13.3).

### 4.3 Invoice-Level Soft Duplicates

Two different scans of the same physical invoice produce different byte hashes but identical business identity. After extraction, before workbook write, run the soft-duplicate check:

```
tuple = (vendorKey, normalize(billNumber), invoiceDate, round(totalBillAmount, 2))
if Cosmos query finds an existing DONE invoice with the same tuple:
    status flag: duplicateSuspectOf = <existing evalId>
    workbook: row still written, NeedsReview = Y, ValidationStatus += "DUP?"
```

Flag, never silently drop and never silently double-insert — double-counted invoices are the fastest way to lose finance-user trust, but so is data that vanishes without explanation. The human resolves the flag.

### 4.4 Staging

New files are copied byte-for-byte to `invoices-raw/{sha256}/original.pdf` in Blob before any processing. Consequences: (a) reprocessing (after layer improvements) replays from our own storage without re-crawling SharePoint; (b) a user deleting or moving the SharePoint file after ingestion is a non-event; (c) DI is always fed from the staged copy, so what was analyzed is exactly what is archived.

### 4.5 Intake Guards

Before paying for DI: password-protected PDFs → `FAILED_STAGED (encrypted)`; zero-byte or non-PDF magic bytes → `FAILED_STAGED (not_pdf)`; page count > `MAX_PAGES` (default 30) or size > `MAX_MB` (default 40) → parked for manual review rather than burning DI pages on a likely mis-upload. All guards surface in the workbook Failures tab (§12.4).

---

## 5. Preprocessing Pipeline

### 5.1 What We Send to DI vs. What We Render Ourselves

The **original staged PDF** goes to DI untouched — DI performs its own internal preprocessing, and aggressive client-side binarization/denoising has been observed to hurt more than help. Our own 300-DPI PyMuPDF render exists for two purposes only: crop generation for the LLM, and visual artifacts for the future validation UI. This split creates one iron rule:

> The render used for cropping is never geometrically altered (no deskew, no rotation, no margin trim). All skew/rotation handling for **our layout math** is done in coordinate space, not image space, so DI polygons and crop pixels always refer to the same geometry.

### 5.2 Rendering

Each page rendered at `RENDER_DPI = 300` to PNG, stored at `invoices-derived/{sha256}/pages/p{n}@300.png`. Page pixel dimensions are recorded next to DI's reported page width/height/unit, giving the exact scale factors for §9.2.

### 5.3 Skew and Rotation (Coordinate-Space Handling)

DI reports per-page `angle` (detected text rotation). For whitespace analysis, row grouping, and template registration, incoming polygons are rotated by `-angle` around the page center into an upright working frame. Crops are still cut from the untouched render — a crop of slightly skewed text is perfectly legible to a vision LLM and the padding absorbs it, whereas rotating the raster would desynchronize it from DI coordinates. Scanner *translation* (page shifted on the platen) is not handled here at all; it is absorbed by template registration (§10.5), which exists precisely so per-field tolerance only has to cover genuine layout wobble.

### 5.4 Whitespace Analysis and Text-Block Grouping

Goal: convert DI's flat line list into semantically grouped blocks (address blocks, header clusters, totals stack), which raises anchor-association accuracy and produces sane multi-line values like Biller Address.

Algorithm (per page, in the upright working frame):

```
lines = DI layout lines sorted by (y, x)
h_med = median(line height)
new block when:
    vertical_gap(prev, cur) > BLOCK_GAP_FACTOR × h_med      # default 1.6
    or horizontal overlap(prev, cur) < MIN_X_OVERLAP         # default 0.25 → side-by-side columns split
block bbox = union of member line bboxes
```

Edge whitespace (page margins) is measured per page and recorded; a sudden margin change between instances of the same template is a cheap drift hint (§10.8). Address assembly = the block containing/adjacent-to the Biller Name line, joined top-to-bottom with `", "`.

### 5.5 Multi-Page and Multi-Invoice Handling

Two distinct cases, both mandatory:

**One invoice spanning pages.** Signals: line-item table columns continue with matching x-signature on page n+1; "continued" markers; totals block appears only on the final page. Treatment: single logical invoice, single Eval ID, `pageRange = [first, last]`; the table layer stitches per-page tables whose column x-centers match within tolerance (§7.5).

**Multiple invoices in one PDF.** Signals: recurrence of the document-title anchor ("TAX INVOICE"), a *new* bill-number anchor with a different value, a fresh totals block, vendor header repetition. Each segment becomes its own logical invoice with its own Eval ID (`-01`, `-02`, …) and independent pipeline run from SEGMENTED onward. Segmentation confidence below threshold → park file with `FAILED_SEGMENTED (ambiguous_boundaries)` for human review rather than guessing boundaries on financial data.

---

## 6. Azure Document Intelligence Pass

### 6.1 Model Strategy

Both models run on the staged PDF in the single expensive pass:

1. **`prebuilt-invoice`** — primary. Directly emits, with per-field confidence: VendorName, VendorAddress(+Recipient), CustomerName, CustomerAddress, InvoiceId, InvoiceDate, DueDate, SubTotal, TotalTax, InvoiceTotal, AmountDue, CurrencyCode (v4: per-amount `valueCurrency`), PaymentTerm, and the `Items` array (Description, Quantity, UnitPrice, Amount, ProductCode, …) plus a `TaxDetails` array (rate/amount pairs).
2. **`prebuilt-layout`** — supplementary, and the substrate for every custom layer: words with polygons + OCR confidence, lines, paragraphs with roles (`title`, `sectionHeading`, `pageHeader`, `pageFooter`), tables as structured cell grids (rowIndex/columnIndex/rowSpan/columnSpan, `columnHeader` kind), selection marks, per-page `angle`, `unit`, `width`, `height`.

Both raw JSON responses are written to Blob **immediately on receipt**, before any parsing — a downstream crash must never force re-paying for DI (§13.3).

### 6.2 Mapping `prebuilt-invoice` to Our Schema

| Our field | prebuilt-invoice source | Gap handling |
|---|---|---|
| Biller Name | VendorName | — |
| Payee (bill-to) | CustomerName | Beware VendorAddressRecipient vs CustomerName confusion on some layouts; relational check in §7.4. |
| Bill Number | InvoiceId | — |
| Date | InvoiceDate | Re-parse raw text ourselves too; day-first ambiguity, §8.6. |
| Sub Total | SubTotal | — |
| Total Bill Amount | InvoiceTotal (fallback AmountDue) | — |
| Currency | valueCurrency / CurrencyCode | GSTIN inference fallback, §8.5. |
| Total Line Items | count(Items) | Cross-checked vs layout table row count, §8.7. |
| GSTIN, Biller Address*, Phone, GST/CGST/SGST/Other %, Round Off | **Not reliably emitted** | Custom layers own these. TaxDetails gives rate/amount pairs but does **not** semantically label CGST vs SGST vs IGST — the split is recovered by anchoring each TaxDetails entry to its source text region (L2) and reading the adjacent label. |

*VendorAddress is emitted but Indian multi-line blocks are frequently truncated or merged; the block-grouping assembly (§5.4) is authoritative, DI's value is a candidate.

### 6.3 Coordinate System Facts (Load-Bearing for §9)

DI `analyzeResult.pages[]` reports `unit`: **`inch` for PDF/TIFF input, `pixel` for image input**. Our input is PDF, so all polygons are in inches with origin top-left. Polygons are 4-point (8 floats), ordered from the text's top-left, following text orientation. Normalized coordinates used throughout the template system: `nx = x / page.width`, `ny = y / page.height`. Every stored polygon in Cosmos keeps `(page, unit, pageWidth, pageHeight)` alongside it so the transform to any raster is self-contained.

### 6.4 Cost Posture

DI charges per page per model; running invoice+layout doubles page cost (order of magnitude ~US$0.01/page/model — **confirm current pricing at build time**, do not hard-code assumptions). This is accepted in Phase 1 because layout underpins every custom layer. Optimization D-07 (§18.3) revisits layout-on-demand once template coverage is high. Per-file DI cost is recorded in the run and invoice documents (§14).

---

## 7. Layered Extraction Engine

### 7.1 The Layer Stack

Each logical invoice is evaluated by six layers. Every layer, for every field it can speak to, emits zero or more candidates: `(field, value, polygon, page, layerScore ∈ [0,1], evidence)`. Layers never veto each other — disagreement is resolved by fusion (§7.6) and validation (§8), or ultimately the LLM (§9).

| Layer | Name | Emits | Nature of its score |
|---|---|---|---|
| L1 | DI prebuilt-invoice | Direct field values | DI's own field confidence (best-calibrated input we have) |
| L2 | Anchor / keyword | Value text spatially related to a lexicon anchor (right-of, below, same-line) | Anchor match quality × spatial-relation quality |
| L3 | Data-type / pattern | Regex- and parser-recognized values (GSTIN, dates, amounts, phone, percent) | Pattern specificity (a checksum-valid GSTIN scores far above "some 15-char token") |
| L4 | Positional prior | Values in canonically likely regions (bill number top-right, totals bottom-right…) | Weak; distance from prior centroid. Exists mainly so *something* proposes when L1/L2 fail |
| L5 | Table structure | Line-item table identity, row count, totals-block members, column semantics | Structural coherence (header match, column-x stability across rows) |
| L6 | Template prior | Per-field predicted bbox + learned value pattern for a matched template | Template health × instance count × pattern match (§10.7) |

L2 details worth pinning: anchor lexicon is per-field, versioned config (Appendix §18.2); matching is case-insensitive with OCR-tolerant fuzzy matching (Levenshtein ≤ 1 per 5 chars, so `lnvoice No` still anchors); spatial relations searched in priority order right-of → below → same-block; the value region is the nearest block/line satisfying the relation within `ANCHOR_MAX_DIST` (default 0.15 × page width).

### 7.2 Candidate Object (Canonical Shape)

```json
{
  "field": "billNumber",
  "value": "INV-1042/25-26",
  "valueRaw": "INV-1042/25-26",
  "page": 1,
  "polygon": [4.91, 0.82, 6.03, 0.82, 6.03, 0.98, 4.91, 0.98],
  "layers": {"L2": 0.86, "L3": 0.55, "L6": 0.93},
  "fusedScore": 0.88,
  "evidence": {"anchor": "Invoice No", "relation": "right-of", "templateField": true}
}
```

`valueRaw` preserves the exact OCR string; `value` is the normalized form (amount → decimal, date → ISO). Both persist to Cosmos.

### 7.3 Candidate Merging

Candidates from different layers referring to the same physical text (polygon IoU > 0.5 on the same page, compatible normalized values) merge into one candidate carrying all contributing layer scores. This is what makes fusion meaningful — a value proposed independently by anchor, pattern, and template is one strong candidate, not three weak ones.

### 7.4 Relational Fields: Biller vs. Payee, Biller GSTIN vs. Payee GSTIN

These fields are only distinguishable **relative to each other**, so they are resolved jointly, not independently:

1. Collect all name/address block candidates and all checksum-valid GSTINs with their blocks.
2. Assign roles by evidence: anchors ("Bill To", "Buyer", "Consignee", "Ship To" vs. letterhead position/`pageHeader` role/"From"); the block containing the invoice title and logo region is biller-side; DI's VendorName/CustomerName votes; template priors vote hardest when present.
3. A GSTIN inherits the role of the block it sits in (or nearest block above within the same column).
4. Constraint: Biller GSTIN ≠ Payee GSTIN. If only one GSTIN exists on the page, role assignment relies on block role alone; if its block is ambiguous, this is a forced LLM arbitration with a **composite crop** (§9.4) because no amount of isolated evidence resolves it.
5. Self-billing sanity: if the resolved *biller* name fuzzy-matches our own organization names (config list), roles are almost certainly flipped → swap and flag `ROLE_SWAP_APPLIED`.

### 7.5 Table Identification (L5)

The line-item table is found primarily from DI layout's structured tables: score each table by header-token overlap with the item-header lexicon (`Description/Particulars`, `HSN/SAC`, `Qty`, `Rate`, `Amount`…) and by row count; the winner is the item table. The user-specified spacing heuristic is the **fallback and the extender**, applied when DI splits or misses: rows whose cell x-centers match the established column x-signature within `COL_X_TOL` (default 0.015 normalized) belong to the table; a row whose x-signature breaks (or whose leading cell matches totals-lexicon like "Sub Total") terminates it. The same x-signature match stitches multi-page continuations (§5.5). Outputs: data-row count (→ Total Line Items), totals-block location (feeds L2 for SubTotal/taxes/Total/RoundOff), column semantic map (persisted to the template).

The table is always treated as **one unit** downstream: validation counts its rows as a set, and if the table itself is ambiguous, the LLM receives the entire table region as a single crop with the serialized cell grid — never cell-by-cell fragments.

### 7.6 Confidence Fusion

Layer scores are heterogeneous and uncalibrated (a 0.9 regex "confidence" ≠ a 0.9 DI confidence). Phase 1 deliberately uses transparent weighted voting, with real calibration deferred until labeled data exists (§16):

```
fused(c) = Σ_l  w_l · s_l(c)   /   Σ_l  w_l · 1[layer l produced any candidate for this field]

initial weights: w_L1=0.30, w_L2=0.25, w_L6=0.25·min(instancesSeen/10, 1),
                 w_L3=0.10, w_L4=0.05, w_L5=0.05 (structural fields only)
```

Normalizing by *participating* layers means absence of a layer (no template yet, no anchor found) doesn't punish a field. Weights live in versioned config; every invoice records the weight-set version used, so weight changes are attributable in eval history.

### 7.7 Decision Rule per Field

```
top1, top2 = best two candidates by fusedScore (post-validation adjustment, §8.8)
accept top1 without LLM  iff  top1 ≥ ACCEPT_CONF (0.80)  and  (top1 − top2) ≥ ACCEPT_MARGIN (0.15)
else → arbitration set (top-2, or top-3 when scores cluster within 0.10), routed to §9
no candidates at all → field = null, NeedsReview = Y (never invent)
```

---

## 8. Deterministic Validation Layer

Invoices are self-checking documents; this layer converts that property into free accuracy. Checks run after fusion, adjust candidate scores (§8.8), decide LLM escalation, and their verdicts persist per invoice.

### 8.1 Arithmetic Identity (V-ARITH)

```
SubTotal + CGST_amt + SGST_amt + IGST_amt + Other_amt + RoundOff  ≟  TotalBillAmount
```

Amounts, not percentages, are compared. Sourcing amounts: printed tax amounts when found (totals block / TaxDetails); else derived `amt = SubTotal × rate/100` — but **derivation is only trusted when the invoice is single-rate** (§8.4). Tolerance: `±0.05` when RoundOff was extracted; `±1.00` when RoundOff is null (an unextracted round-off is the most common benign gap). Verdicts: PASS / FAIL / UNDECIDABLE (insufficient trusted terms).

The identity is also run **combinatorially across candidates**: if swapping in the #2 SubTotal candidate makes the identity pass exactly while #1 fails it, that is near-conclusive evidence #2 is correct — the arithmetic picks winners, not just audits them.

### 8.2 GSTIN Format and Checksum (V-GSTIN)

Structure: `^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$` — 2-digit state code (valid range 01–38, plus 97 Other Territory; anything else FLAG), embedded 10-char PAN, entity digit, literal `Z`, check character. Check character (mod-36 Luhn variant):

```python
A = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
def gstin_check(g15: str) -> bool:
    total = 0
    for i, ch in enumerate(g15[:14]):
        v = A.index(ch) * (2 if i % 2 else 1)   # factors 1,2,1,2,… from position 0
        total += v // 36 + v % 36
    return A[(36 - total % 36) % 36] == g15[14]
```

A checksum-valid GSTIN is promoted aggressively (it is 1-in-36 to pass by luck *after* already fitting the structural regex); a near-miss (fails checksum, edit distance 1 from validity) triggers OCR-confusion repair attempts (`0↔O`, `1↔I`, `5↔S`, `8↔B`, `2↔Z`) — a repaired-and-valid GSTIN is kept with flag `GSTIN_OCR_REPAIRED` and slightly capped confidence. Bonus cross-check: state code (chars 1–2) should be consistent with the biller address's state when that is extractable.

### 8.3 GST Structural Rules (V-TAX)

Intra-state: CGST% = SGST% (tolerance 0.01) and GST% = CGST% + SGST%. Inter-state: IGST present ⇒ CGST/SGST absent (mutual exclusivity; both present = hard FAIL on someone's extraction). RoundOff: \|value\| < 1.00 expected; \|value\| ≥ 1.00 → FLAG (occasionally legitimate, usually a mis-anchored amount). Rate plausibility: extracted GST rates should belong to `{0, 0.1, 0.25, 1, 1.5, 3, 5, 12, 18, 28}` (config list, updateable as slabs change) — off-list rates FLAG rather than FAIL.

### 8.4 Mixed-Rate Invoices

When line items carry different GST rates, a single header-level GST% is ill-defined. Policy: if the totals block prints a per-rate tax breakup, GST% = rate of the largest taxable share, with `MIXED_RATES` flag and the full breakup preserved in Cosmos `fields.gstPct.meta`. Rate-derived amounts are excluded from V-ARITH in this case; only printed amounts participate.

### 8.5 Currency Inference (V-CUR)

`₹` / `Rs.` / `INR` tokens or DI valueCurrency → INR directly. Else, checksum-valid biller GSTIN → INR **inferred** with flag `CUR_INFERRED`. Exception path: export/SEZ invoices ("SUPPLY MEANT FOR EXPORT", "LUT", IGST 0%) may legitimately bill USD/EUR under a GSTIN — presence of these markers suppresses the inference and forces explicit currency extraction or null+review.

### 8.6 Date Discipline (V-DATE)

Parse order for ambiguous numerics: dd/mm/yyyy → dd-MMM-yy(yy) → mm/dd/yyyy (last, and only if day-first parse was impossible, e.g., 13+ in the middle slot). Sanity: not in the future beyond +2 days skew; older than 24 months → FLAG. Financial-year cross-check: Indian FY runs 1 Apr–31 Mar; if Bill Number embeds an FY fragment (`25-26`, `2025-26` — extremely common in Indian numbering), the invoice date must fall inside that FY. A violation almost always means the *date* was mis-picked (due date or e-way date grabbed instead) — this check has outsized practical value.

### 8.7 Line-Item Count (V-ITEMS)

`TotalLineItems = L5 data-row count`, cross-checked against `count(DI Items)` and against printed "Total Items: N". Two-of-three agreement wins; three-way disagreement escalates the whole table to LLM arbitration as one unit.

### 8.8 Effect on Scores and Routing

```
candidate participates in a satisfied V-ARITH identity      → fused = max(fused, 0.97)
candidate contradicts a satisfied identity                  → fused ×= 0.30
GSTIN checksum PASS                                        → fused = max(fused, 0.98)
V-DATE FY violation on the winning date                    → force arbitration of Date field
any hard FAIL                                              → the implicated fields (only) join the arbitration set
```

This is the mechanism behind the cost curve: validation passes → confidence floors → §7.7 accepts → LLM never called. Validation failures escalate *only the implicated fields*, not the invoice.

---

## 9. Vision LLM Arbitration

### 9.1 Contract

The LLM is an arbiter, not an extractor of first resort. It receives, per disputed field: the field definition, the top-k candidates (values + one-line provenance each), targeted crop image(s), and minimal positional context. It returns strict JSON only. It is explicitly permitted to answer "none of the above" with its own reading, and to return null with reason `not_present`.

### 9.2 Coordinate Mapping: DI Polygon → Render Pixels

Input is PDF, so DI units are inches (§6.3); the render is `RENDER_DPI = 300`:

```
scale_x = page_px_width  / page.width_inches      # ≈ 300, kept exact per page
scale_y = page_px_height / page.height_inches
crop_box_px = [min_x·scale_x, min_y·scale_y, max_x·scale_x, max_y·scale_y] over polygon points
```

No rotation is applied to the raster (§5.3 rule); the axis-aligned bounding box of the (possibly skewed) polygon plus padding covers the text. Every crop is generated fresh from the stored page PNG; crops themselves are cache/ephemeral (§11.5).

### 9.3 Padding Policy

`pad = max(PAD_FRAC × bbox_dim, PAD_MIN_PX)` per side, defaults 12% and 24 px — a tight crop of `₹4,532.00` with zero surround is unanswerable. When a matched template provides positional variance for the field, padding widens to `mean_dim/2 + 3σ` (learned wobble), clamped to page bounds. Amount fields additionally extend left to swallow their row label (so the crop shows `Grand Total    ₹4,532.00`, letting the LLM verify the *association*, not just read digits).

### 9.4 Relational Fields Get Composite Crops

Isolated crops destroy exactly the context that distinguishes Biller from Payee (§7.4). For role-relational disputes the payload is either (a) one composite crop containing both candidate blocks plus the title band, or (b) both block crops plus explicit positional metadata in the prompt: `Block A at top-left under the logo; Block B at mid-right beneath the text "Bill To"`. Tables likewise ship as one whole-table crop (header row included) plus the serialized DI cell grid; the question posed is about the table as an object ("how many data rows; which column is Amount"), never per-cell.

### 9.5 Prompt / Response Contract

```
system: You verify fields on Indian GST invoices. Answer ONLY the JSON schema
        provided. Judge from the image; candidates are hints that may be wrong.
        If none are correct, supply the value you read. If absent, value=null.

user (per batch): invoice-level context {billerNameBestGuess, page count},
  then per field: {field, definition, candidates:[{value, source}], images:[crop…],
  positionalNote}. Response schema:
  {"fields":[{"field":"...","value":...,"chosenCandidate":0|1|2|-1|null,
              "confidence":0.0-1.0,"reason":"<one line>"}]}
```

Temperature 0. All disputed fields of one invoice batch into **one call** (crops are small; one call amortizes the system prompt). Malformed JSON → one re-ask with the error; second failure → fields fall back to top fused candidate with `LLM_FAILED` flag and NeedsReview = Y. Guard: if the LLM returns a *checksum-invalid* GSTIN or a value that newly breaks a previously satisfied V-ARITH, the deterministic result stands and the conflict is flagged — validation outranks the arbiter too.

### 9.6 Invocation Economics

Recorded per invoice: whether called, fields arbitrated, model, input/output tokens, cost. The target trajectory (measurable from §14 data): early corpus majority-arbitrated → recurring vendors with healthy templates arbitrating ≈ 0 fields, i.e., the 11th invoice from a recurring vendor costs one DI pass and zero LLM calls.

---

## 10. Template System

The compounding-value component. A template is a bundle of learned priors about one vendor's one layout, stored in Cosmos, injected as layer L6, and updated only from confirmed extractions.

### 10.1 Template Identity: Vendor → Layout Variant

Neither "one template per GSTIN" nor "one template per layout" is sufficient alone: one GSTIN emits multiple layouts (POS receipt vs. formal tax invoice vs. per-branch software), and one billing package (Tally, Zoho Books, Busy, Marg, Vyapar) emits near-identical layouts under thousands of GSTINs. Identity is therefore two-level:

```
templateId = vendorKey :: v{N}
vendorKey  = GSTIN (checksum-valid)                       — primary, rock-solid key
           | "NAME::" + normalizedVendorName              — fallback when GSTIN absent
normalizedVendorName = upper, strip punctuation and legal suffixes
                       (PVT/PRIVATE, LTD/LIMITED, LLP, & CO, TRADERS…), collapse spaces
```

`v{N}` is the layout variant, minted by fingerprint mismatch (§10.6). Vendor-name fallback matching inside Cosmos (no native fuzzy search) is handled pragmatically: the vendor registry (§11.2) is small enough to pull the candidate name list for the first letter/prefix and fuzzy-match in memory (token-set ratio ≥ 0.90); Azure AI Search becomes the durable answer if the registry grows large (suggestion S-03).

### 10.2 Layout Fingerprint — Learned Stability, Not Assumed Regions

Design correction adopted from review: stable layout content is **not** assumed to live in the top/bottom bands of the page. Big-company layouts put GSTIN blocks mid-left and totals mid-right; the line-item table is usually central but not reliably so. Stability is therefore *learned*:

1. **Instance 1** of a layout: fingerprint the whole page. There is no stability knowledge yet; anchor-like tokens (lexicon keywords, short strings ending in `:`, `sectionHeading`/`title` roles) serve as provisional static tokens. A top/bottom weighting may be used as a first-instance *tiebreaker only* — never as a filter.
2. **Instance 2+**: diff instances in normalized upright coordinates. Blocks with same position and same text across instances → **static tokens** (structure: labels, headers, company block wherever it sits). Same position, different text → **data fields**. Regions whose vertical extent varies → **elastic zones** (the item table, wherever it lives). The fingerprint converges to "the static token set and geometry," free of positional assumptions.

### 10.3 Fingerprint Content

```json
"fingerprint": {
  "gridBitmap":    {"rows": 12, "cols": 8, "bitsHex": "A3F0C1…"},
  "staticTokens":  [{"text": "TAX INVOICE", "cx": 0.50, "cy": 0.04},
                    {"text": "GSTIN",       "cx": 0.12, "cy": 0.09}, …],
  "tableSignature":{"colCenters": [0.18, 0.42, 0.55, 0.68, 0.85],
                    "headerTokens": ["Description","HSN/SAC","Qty","Rate","Amount"]},
  "registrationAnchors": ["TAX INVOICE", "GSTIN", "Authorised Signatory"],
  "elasticZones":  [{"yTop": 0.34, "role": "itemTable"}]
}
```

Both agreed representations coexist because they serve different steps: the **coarse grid occupancy bitmap** (12×8 cells over the normalized page, bit = cell contains text) is the cheap hash — XOR/Hamming comparison, inherently shift-tolerant at cell granularity; the **raw normalized geometry** (static token centers, table column x-centers, and per-field x/y/w/h priors in §10.4) is the precise representation used for scoring and extraction, with explicit tolerance bands to absorb scan shift and skew. Grid for speed, coordinates for truth.

### 10.4 Template Document (Full Schema)

```json
{
  "id": "27AABCU9603R1ZM::v2",
  "type": "template",
  "vendorKey": "27AABCU9603R1ZM",
  "variant": 2,
  "fingerprint": { …as §10.3… },
  "fields": {
    "billNumber": {
      "anchor":   {"text": "Invoice No", "relation": "right-of", "offset": [0.08, 0.0]},
      "bboxPrior": {"n": 14,
                    "mean": [0.71, 0.11, 0.14, 0.02],        // cx, cy, w, h (normalized)
                    "M2":   [0.0003, 0.0001, 0.0002, 0.00005]}, // Welford accumulators
      "valuePattern": {"regex": "INV-\\d{4}/\\d{2}-\\d{2}",
                       "samples": ["INV-1023/25-26","INV-1031/25-26","INV-1042/25-26"],
                       "n": 14, "active": true, "violations": 0},
      "hit": 14, "miss": 0, "consecutiveMiss": 0
    },
    "gstin":        { … }, "subTotal": { … }, "totalBillAmount": { … }, …
  },
  "table": {"colFieldMap": {"0":"description","1":"hsn","2":"qty","3":"rate","4":"amount"},
            "totalsBlockAnchor": "Sub Total"},
  "validationProfile": {"taxType": "CGST_SGST", "typicalRates": [9.0, 9.0],
                        "hasRoundOff": true, "currency": "INR"},
  "stats": {"instancesSeen": 14, "lastSeen": "2026-07-02",
            "createdFromEvalId": "EVL-…-01", "health": "ACTIVE",
            "matchScoreEwma": 0.93},
  "schemaVersion": 1
}
```

Load-bearing details: **bboxPrior carries variance**, so after several instances the system knows not just where a field is but how much it wanders — which sets per-field crop padding (§9.3) and per-field match tolerance instead of a global constant. **valuePattern is induced, not hand-written** (§10.7). **hit/miss/consecutiveMiss are per-field**, so a template degrades limb-by-limb rather than dying whole. **validationProfile encodes vendor normality** — this vendor always bills 9+9 with round-off — turning deviations into anomaly flags that have value beyond extraction (a recurring vendor suddenly billing 14% is worth a human's attention regardless of extraction accuracy).

### 10.5 Registration Before Matching

To neutralize scanner translation before any geometric comparison: find 2–3 `registrationAnchors` in the incoming page (fuzzy text match), compute the median Δ between their template centers and incoming centers, apply that translation (optionally the small rotation from two-anchor angle, though §5.3's coordinate-space deskew usually leaves nothing to do) to **all incoming coordinates**. Per-field tolerance then only covers genuine layout wobble, not platen placement. Fewer than 2 anchors found → skip registration, widen stage-B tolerances by ×1.5, and note `REGISTRATION_SKIPPED`.

### 10.6 Matching Pipeline (Two Stages + Verdict Bands)

```
candidates = templates where vendorKey ∈ {billerGSTIN, payeeGSTIN?, nameFallback}   // §10.9 order
Stage A (cheap prefilter):  Hamming similarity of grid bitmaps ≥ 0.80
Stage B (precise score), after registration:
    S = 0.45·Jaccard(staticTokens, fuzzy text + center within 0.02)
      + 0.35·bboxAlignment(mean of per-token center-distance scores, d ≤ 0.03 full credit)
      + 0.20·tableSignature(col-center match within COL_X_TOL, header-token overlap)
Verdict:
    S ≥ 0.85            → MATCH: inject L6 priors
    0.60 ≤ S < 0.85     → GRAY ZONE: extract WITHOUT priors; if results validate,
                           fork new variant v{N+1} for this vendor (never corrupt v{N})
    S < 0.60            → NEW VARIANT: extract without priors; on confirmation, create template
```

The gray-zone fork is the drift release valve: a vendor's redesign produces a sibling variant, both live until decay retires the stale one.

### 10.7 Injection and Learning

**Injection (L6).** On MATCH, per template field: (a) the predicted bbox directly proposes the text found at that location as a candidate — even when the anchor layer failed (OCR mangled `Invoice No` into `lnvo1ce No`), position alone proposes; (b) `valuePattern.active` regex match multiplies that candidate's L6 score ×1.3 capped at 1.0 (a value matching the vendor's own numbering format is almost certainly right); (c) L6's fusion weight scales with maturity, `0.25 · min(instancesSeen/10, 1)` (§7.6). Everything still flows through §8 — a wrong template guess almost always breaks V-ARITH, which is precisely why aggressive trust is safe.

**Learning gate.** Templates update **only** from confirmed extractions: V-ARITH PASS, or human-approved (Phase 1.5+), or LLM high-confidence *and* validation-consistent. Unvalidated guesses never teach — the corruption of a template by its own errors is the classic failure mode of these systems.

**Update mechanics (per confirmed instance).** Welford per bbox dimension:

```
n += 1;  δ = x − mean;  mean += δ/n;  M2 += δ·(x − mean);  σ = sqrt(M2/(n−1)) for n ≥ 2
```

hit/miss and consecutiveMiss updated per field (miss = prior proposed, final confirmed value came from elsewhere). Pattern induction: per-character class generalization over samples (digit→`\d`, A–Z→`[A-Z]`, stable literals kept literal), runs collapsed to counted quantifiers, activated at `n ≥ 3` samples, deactivated after 2 confirmed violations and re-induced from the updated sample set. Raw learning residue: keep the last 3 instances' relevant DI JSON pointers on the template for debugging/re-induction; older residue pruned (bounded template size).

**Human corrections are the strongest signal** (when the loop exists): a corrected field immediately re-centers the bbox prior on the corrected location (weight of ~3 ordinary instances), marks the old anchor unreliable for this template, and enters the eval set (§16).

### 10.8 Drift, Demotion, Decay

Field-level: `consecutiveMiss ≥ 3` → zero that field's L6 weight (field prior demoted; rest of template unaffected); a subsequent confirmed hit through other layers re-teaches it. Template-level: on a supposed MATCH, if < 50% of field priors hit → treat as mismatch retroactively, decrement `matchScoreEwma`, raise this template's match threshold by +0.03 (self-tightening); `validationProfile` deviations (rate change, round-off disappears) flag `VENDOR_PROFILE_DRIFT` on the invoice. Margin-change hint from §5.4 feeds the same flag. Time decay: `lastSeen > 180 days` → health `DORMANT` (matchable but L6 weight ×0.5); superseded gray-zone ancestors decay naturally. Templates are never hard-deleted in Phase 1 — storage is trivial and history is diagnostic.

### 10.9 Cold Start and the Bootstrap Order

Chicken-and-egg resolution (GSTIN is both a lookup key and an extraction target): the pipeline first runs the cheap L3 GSTIN regex over the **raw full text** — costs nothing, needs no field assignment, and typically surfaces both GSTINs on the page. Both are tried as vendorKey lookups (the payee GSTIN — ours — will simply match no vendor templates; optionally short-circuited by configuring our own GSTINs as a known-self list). Template lookup therefore precedes field-level extraction, and L6 participates in the very pass that formally assigns the GSTIN field.

Zero-template cold start is a hard requirement — templates only sharpen, never gate. Additionally, ship **software-level seed templates** for the dominant Indian SME billing packages (Tally's layout is practically a fixture; Zoho Books, Busy, Marg, Vyapar similarly recognizable): vendorKey `SOFTWARE::TALLY` etc., matched by fingerprint only (no vendor key), injected at half weight (`0.125` cap), and **never updated** by learning (they are shared priors; per-vendor learning always forks a real vendor template on first confirmation).

---

## 11. Persistence Layer — Cosmos DB and Blob Storage

### 11.1 Container Layout

| Container | Partition key | Document types | Notes |
|---|---|---|---|
| `invoices` | `/vendorKey` | `invoice`, `fileIndex` | Vendor-scoped queries dominate (history, recurrence, soft-dup, template learning). Per-vendor volumes are modest → no hot-partition risk. Point-read by Eval ID uses (vendorKey from the workbook row, id). |
| `runs` | `/yearMonth` | `run` | Small documents, time-scoped queries ("this month's runs"). id = runId. |
| `templates` | `/vendorKey` | `template`, `vendor`, `config` | One in-partition query returns all of a vendor's variants — exactly the §10.6 candidate set. `config` docs (anchor lexicon version, fusion weights, tunables) live here under vendorKey `_GLOBAL`, giving versioned, auditable config without another container. |

### 11.2 `invoice` Document (Authoritative Record)

```json
{
  "id": "EVL-3f9c2a7b41de-01",
  "type": "invoice",
  "vendorKey": "27AABCU9603R1ZM",
  "evalId": "EVL-3f9c2a7b41de-01",
  "file": {
    "sha256": "3f9c2a7b41de…",
    "sharePoint": {"driveId": "b!…", "itemId": "01ABC…", "path": "/Invoices/2026/07/abc.pdf",
                   "alternatePaths": []},
    "blob": {"original": "invoices-raw/3f9c…/original.pdf"}
  },
  "pageRange": [1, 2],
  "template": {"id": "27AABCU9603R1ZM::v2", "matchScore": 0.91, "stage": "MATCH"},
  "fields": {
    "billNumber": {
      "value": "INV-1042/25-26", "valueRaw": "INV-1042/25-26",
      "confidence": 0.99, "sourceLayer": "L2+L6",
      "candidates": [ …full §7.2 objects… ],
      "polygon": [ … ], "page": 1, "unit": "inch",
      "llm": null
    }, …all 16 extracted fields…
  },
  "validation": {"arith": "PASS", "gstin": "PASS", "tax": "PASS", "date": "PASS",
                 "items": "PASS", "currency": "EXPLICIT", "flags": [],
                 "details": {"arithDelta": 0.00}},
  "llm": {"called": true, "fieldsArbitrated": ["billerAddress"], "model": "…",
          "inputTokens": 1834, "outputTokens": 92, "costUsd": 0.0061},
  "di": {"invoiceJson": "invoices-derived/3f9c…/di-invoice.json",
         "layoutJson":  "invoices-derived/3f9c…/di-layout.json",
         "pages": 2, "costUsd": 0.02},
  "duplicateSuspectOf": null,
  "status": "DONE", "stage": "SHEET_WRITTEN",
  "review": {"status": "PENDING", "corrections": {}},
  "configVersions": {"weights": 3, "lexicon": 5, "tunables": 2},
  "runId": "RUN-20260718-0630", "schemaVersion": 1,
  "timestamps": {"ingested": "…", "diDone": "…", "persisted": "…", "sheetWritten": "…"}
}
```

Size discipline: **no DI JSON inline** (multi-page layout output easily reaches hundreds of KB; Cosmos docs must stay far under the 2 MB item limit and, practically, under a few hundred KB) — Blob pointers only. Candidates keep top-3 per field, not the full firehose. `fileIndex` companion doc (id = sha256, vendorKey = `_FILES`) makes the §4.2 hash gate a point-read.

### 11.3 `run` Document

```json
{
  "id": "RUN-20260718-0630", "type": "run", "yearMonth": "2026-07",
  "startedAt": "…", "endedAt": "…", "trigger": "cron",
  "lock": {"leaseId": "…", "acquiredAt": "…"},
  "listing": {"sinceOptimization": "2026-07-18T06:00:00Z",
              "filesSeen": 12, "newFiles": 3, "knownSkipped": 9},
  "files": [
    {"sha256": "3f9c…", "name": "abc.pdf", "outcome": "DONE",
     "evalIds": ["EVL-3f9c2a7b41de-01"], "resumedFromStage": null,
     "durationMs": 41230, "costUsd": 0.027, "error": null}
  ],
  "totals": {"processed": 3, "failed": 0, "parked": 0, "invoices": 4,
             "llmCalls": 2, "llmArbitratedFields": 3,
             "costUsd": {"di": 0.06, "llm": 0.012, "total": 0.072}},
  "errors": [], "pipelineVersion": "1.0.3", "schemaVersion": 1
}
```

### 11.4 Eval ID Specification

```
EvalID = "EVL-" + sha256(fileBytes)[0:12] + "-" + zeroPad(invoiceIndex, 2)
```

Deterministic by construction: same bytes → same file hash; `invoiceIndex` = 1-based ordinal of the logical invoice within the file in segmentation order (§5.5). Reprocessing a file therefore **upserts** the same Cosmos ids and the same workbook rows instead of duplicating — this single property carries most of the pipeline's idempotency. `pageRange` is stored, not encoded in the ID (segmentation refinements must not change identities). Collision risk at 12 hex chars (48 bits) is negligible at this corpus scale; the full sha256 remains in the document as the tiebreaker of record.

### 11.5 Blob Layout and Lifecycle

```
invoices-raw/      {sha256}/original.pdf                      # retained indefinitely (replay source)
invoices-derived/  {sha256}/di-invoice.json                   # retained (re-extraction without DI cost)
                   {sha256}/di-layout.json                    # retained
                   {sha256}/pages/p{n}@300.png                # retained (crop source + Phase-2 UI)
                   {sha256}/crops/{evalId}/{field}-{rank}.png # lifecycle rule: delete after 30 days
```

Retaining raw DI JSON + page renders means any future layer improvement replays the entire history as pure computation — zero DI spend, zero SharePoint crawling. Crops are pure derivatives and regenerable, hence ephemeral.

---

## 12. Master Workbook (Phase 1 Presentation Layer)

### 12.1 Position in the Architecture

Explicit Phase-1 decision: the workbook stays, its fragility acknowledged and contained. Containment = Cosmos is the system of record and the workbook is a **generated view**; a corrupted, locked, sorted, or deleted workbook is a recoverable annoyance (regenerate from Cosmos), never data loss. Phase 2 replaces human interaction with a custom validation UI (§16.3); the workbook then survives, if at all, as a read-only export.

### 12.2 Structure

One workbook on SharePoint containing an Excel **table object** named `Invoices` (table, not raw cells — table-row operations via Graph survive user sorting and filtering) and a `Failures` sheet. Column groups:

```
Identity      : EvalID | FileName | FilePath
Extracted     : BillerName | Payee | BillNumber | Date | GSTIN | BillerAddress |
                BillerPhone | GSTPct | CGSTPct | SGSTPct | OtherTaxesPct |
                SubTotal | TotalBillAmount | Currency | TotalLineItems | RoundOff
Operational   : TemplateId | OverallConfidence | ValidationStatus | LLMUsed |
                NeedsReview | ProcessedAt | RunId
Human-only    : ReviewStatus | ReviewedBy | ReviewedAt |
                Corr_BillerName | Corr_Payee | Corr_BillNumber | Corr_Date |
                Corr_GSTIN | Corr_BillerAddress | Corr_BillerPhone | Corr_GSTPct |
                Corr_CGSTPct | Corr_SGSTPct | Corr_OtherTaxesPct | Corr_SubTotal |
                Corr_TotalBillAmount | Corr_Currency | Corr_TotalLineItems | Corr_RoundOff
```

Write-ownership rule (the feedback-loop guarantee): the cron writes Identity/Extracted/Operational and **never touches** Human-only columns; humans correct **only** in `Corr_*` (originals stay intact), yielding the (predicted, corrected) pair per field keyed by EvalID. `NeedsReview = Y` when any field confidence < `ACCEPT_CONF`, any validation FAIL/FLAG, `duplicateSuspectOf` set, or `LLM_FAILED`.

### 12.3 Write Path

Graph workbook API with a persisted session (`workbook-session-id`, `persistChanges: true`). New EvalID → `POST …/tables/Invoices/rows/add`. Existing EvalID (reprocess) → locate row by EvalID column (Phase-1 volumes permit a ranged column read; a `rowIndexHint` cached in Cosmos accelerates it) and PATCH the Extracted/Operational range only. Lock contention (HTTP 423/409 while a user has the file open) → exponential backoff 30 s × 5 attempts inside the run; still locked → invoice rests at `PERSISTED` with flag `SHEET_DEFERRED`, and the next run's opening step flushes all deferred rows before new work. Nothing is lost — Cosmos already holds truth.

### 12.4 Failures Sheet

One row per parked/failed item: FileName, SharePoint path, sha256, failed stage, error class, human-readable message, first/last attempt, attempt count. Written from Cosmos state each run (regenerated, not appended), so it always reflects current reality.

---

## 13. Cron Orchestration, Idempotency, and Failure Handling

### 13.1 Run Lifecycle and Single-Flight Lock

Schedule: configurable, default every 30 min (D-02). A run begins by acquiring a **blob lease** on a well-known lock blob (60 s lease, renewed by heartbeat); acquisition failure = a run is already active → exit silently (prevents overlap when a big batch outlasts the schedule interval). Crash recovery is automatic: an orphaned lease expires in ≤ 60 s. Run steps: acquire lock → flush `SHEET_DEFERRED` backlog → list & hash-gate files → per-file pipeline → template learning pass → finalize run doc → release lock.

### 13.2 Per-File Isolation

Every file executes inside its own failure boundary. One poisoned PDF (encrypted, corrupt, 40 MB scan, DI 500s, LLM timeout) records its `FAILED_{stage}` outcome and the loop continues — a single file can never kill a run. Uncaught errors at run scope (Cosmos unreachable, lock lost) abort the run with `run.status = ABORTED`; per-file states already persisted keep the next run's resume exact.

### 13.3 Resume-From-Stage and Partial-Failure Economics

Because every stage's output persists (staged PDF, page PNGs, DI JSON in Blob; stage cursor in Cosmos), retry resumes at the failed stage. The canonical expensive case — **DI succeeded, LLM failed** — resumes at `LLM` and re-reads `di-*.json` from Blob: DI is never re-purchased for a retry. This is the concrete payoff of §6.1's persist-before-parse rule.

### 13.4 Retry and Poison Policy

| Failure class | Examples | In-run handling | Cross-run |
|---|---|---|---|
| Transient service | DI/LLM/Graph/Cosmos 429, 5xx, timeouts | Exponential backoff `5s·2^k`, 3 attempts | Retry next run from stage cursor |
| Deterministic file | Encrypted, non-PDF, oversized, ambiguous segmentation | No retry; park immediately | `PARKED` until human action (Failures sheet) |
| Logic errors | Parser exception on odd layout | 1 attempt (no retry — same input, same crash) | Retry only when `pipelineVersion` changes; else park after 3 versions… practically: park with stack hash for the dev |
| Workbook lock | 423/409 | Backoff ×5 | `SHEET_DEFERRED`, flushed next run |
| Poison threshold | Any file failing in 3 distinct runs | — | `PARKED (poison)`, alert |

### 13.5 Idempotency Ledger

The properties that jointly make double-firing the cron a non-event: content-hash gate (a processed file cannot re-enter), deterministic Eval IDs (a reprocessed invoice overwrites itself in Cosmos and the workbook), Cosmos upserts everywhere (no blind inserts), workbook writes keyed by EvalID, template learning gated on per-instance `learnedFrom` markers (an instance teaches a template at most once), single-flight lock (no concurrent mutation). Formal statement: `run(); run();` with no new files ⇒ zero state changes anywhere.

---

## 14. Observability and Cost Accounting

Structured logs (JSON) tagged with runId/sha256/evalId/stage. Per-run rollups already in the run document (§11.3): files, invoices, failures, LLM call count, arbitrated-field count, cost split DI vs LLM. Per-invoice cost attribution enables the two curves that prove the architecture: **LLM-arbitration rate over time** (should fall as templates mature — the recurring-vendor 11th-invoice claim is measurable, not aspirational) and **cost per invoice over time**. Once corrections exist: per-field accuracy, per-layer win/override rates ("how often did L6 beat L2", "how often did the LLM overturn fusion — and was it right"), per-vendor accuracy (the only way a stale template quietly dragging one vendor down becomes visible, §10.8). Alert conditions: run ABORTED, poison parks, DI/LLM error-rate spike, workbook deferred-backlog growth, spend-per-run threshold.

## 15. Security Notes

Managed Identity for Graph/SharePoint, Blob, and Cosmos (Key Vault only where unavoidable, e.g., third-party LLM key); least-privilege scoping (Graph `Sites.Selected` to the one site; Blob containers private; Cosmos per-container RBAC). Invoice data is business-sensitive (GSTINs, amounts, addresses): encryption at rest is platform-default, TLS in transit, no invoice content in logs (log field *names* and confidences, not values). LLM egress is the one boundary crossing — crops of invoice regions leave the Azure tenant if the arbiter is external; provider choice D-01 should weigh an in-tenant option (Azure-hosted model) if data-residency policy requires. Retention: Blob originals indefinite (replay), crops 30 d; deletion requests satisfiable by sha256 across Blob + Cosmos.

## 16. Phase Roadmap and Evaluation Harness

### 16.1 Build Order

1. **Skeleton first**: ingestion, hash gate, staging, DI pass, persist-raw, L1-only extraction, Cosmos writes, workbook append. This alone is a working (if modest) product and creates the data-shape reality the rest builds on.
2. Layers L2/L3/L5 + fusion + validation (the accuracy jump), LLM arbitration path.
3. Template **matching and injection** machinery (L6) with hand-seeded templates.
4. Correction columns live → **eval harness** (16.2) → only then enable the template **learning loop**. Sequencing rationale, restated as a rule: template-system failures are *silent* (one vendor's accuracy quietly rots); learning must not run before per-vendor measurement exists to catch it.
5. Phase 2: validation UI (16.3), calibration, remaining suggestions (§17) by measured value.

### 16.2 Eval Harness

The corpus: every human-corrected invoice auto-becomes a labeled case (predicted vs corrected per field — the workbook's paired-column design exists for this); target ≥ a few hundred invoices spanning vendors, scan qualities, and layouts before trusting aggregate numbers. Mechanics: replay any pipeline version against the corpus from Blob artifacts (no DI/SharePoint cost, LLM optionally mocked from recorded verdicts); score exact-match per field (amounts at 2 dp, dates ISO, GSTIN case-fold); report per-field / per-layer / per-vendor accuracy and deltas vs. the previous pipeline version. Gate: layer-weight or lexicon changes ship only with a non-regressing eval delta. Every human correction simultaneously (a) enters the corpus, (b) feeds template learning (§10.7), (c) accumulates toward calibration (S-05).

### 16.3 Phase-2 Validation UI (Direction Only)

Everything the UI needs is already persisted by Phase 1 by design: page PNGs (render), per-field polygons + pages (Cosmos), candidates with provenance, validation verdicts. The UI is therefore a renderer — page image, field overlays, click-to-correct writing to `review.corrections` in Cosmos — with zero new extraction machinery. This is deliberate: Phase 1's storage choices are the Phase-2 spec.

---

## 17. Suggestions and Future Enhancements

**S-01 — e-Invoice IRN QR decode (highest-leverage suggestion).** Indian B2B invoices above the e-invoicing turnover threshold carry a government-mandated, NIC-signed QR code embedding seller GSTIN, buyer GSTIN, document number, document date, total value, and IRN. Decoding it (pyzbar on the page render; payload is a signed JWS whose claims are readable without signature verification, and verifiable with NIC's public key if desired) yields **ground truth** for Bill Number, Date, both GSTINs, and Total — converting extraction into confirmation for a large share of B2B volume, feeding template learning with perfect labels, and short-circuiting LLM arbitration entirely on those fields. Natural slot: a validation-layer sibling (V-QR) that outranks everything.

**S-02 — DI custom extraction models for stubborn high-volume vendors.** Where a vendor's layout defeats the generic stack, DI's custom neural training (≈5 labeled samples) is a per-vendor alternative; the template system's per-vendor accuracy report (§14) identifies exactly which vendors justify the labeling effort.

**S-03 — Azure AI Search for vendor-alias resolution** when the in-memory fuzzy fallback (§10.1) outgrows itself; embedding-based matching also groups OCR-mangled vendor names.

**S-04 — Queue-based scale-out**: replace the in-run file loop with Storage Queue/Service Bus + Functions (or Durable Functions per-file orchestration) when volume makes the 30-min batch window binding. The per-file state machine (§3.2) is already the message contract; the cron shrinks to a lister/enqueuer.

**S-05 — Confidence calibration** (isotonic or Platt per layer) once the corpus passes a few hundred labeled fields, replacing §7.6's hand weights with fitted ones — the weights were designed to be replaced.

**S-06 — Line-item schema expansion** (HSN/SAC, qty, rate, per-line tax) — L5 and the template `colFieldMap` already parse it; this is an output-schema decision away.

**S-07 — Vendor-profile anomaly surfacing** as a first-class report (rate changes, amount outliers vs. history, round-off behavior changes) — the `validationProfile` drift flags (§10.8) already detect; this suggestion is to *route* them to finance eyes.

**S-08 — Shadow/canary pipeline**: run the next `pipelineVersion` in shadow over live traffic (replay-style, no writes), diff against production output before promotion; the Blob replay design makes this nearly free.

**S-09 — Amount-in-words cross-check**: Indian invoices print the total in words ("Rupees Four Thousand Five Hundred Thirty-Two Only"); parsing words→number is a cheap, fully deterministic second witness for Total Bill Amount and catches decimal OCR slips that V-ARITH tolerance lets through.

**S-10 — Duplicate-tolerant re-ingestion UX**: a tiny "reprocess this file" trigger (drop a marker file / flip a Cosmos flag) so humans can force a re-run after fixing a source PDF without touching the pipeline.

---

## 18. Appendices

### 18.1 Tunable Constants (initial values; all live in the `_GLOBAL/config` document, versioned)

| Constant | Default | Used in |
|---|---|---|
| RENDER_DPI | 300 | §5.2, §9.2 |
| MAX_PAGES / MAX_MB | 30 / 40 | §4.5 |
| BLOCK_GAP_FACTOR | 1.6 × median line height | §5.4 |
| MIN_X_OVERLAP | 0.25 | §5.4 |
| ANCHOR_MAX_DIST | 0.15 × page width | §7.1 |
| ANCHOR_FUZZ | Levenshtein ≤ 1 per 5 chars | §7.1 |
| COL_X_TOL | 0.015 (normalized) | §7.5, §10.6 |
| Fusion weights | L1 .30 / L2 .25 / L6 .25·min(n/10,1) / L3 .10 / L4 .05 / L5 .05 | §7.6 |
| ACCEPT_CONF / ACCEPT_MARGIN | 0.80 / 0.15 | §7.7 |
| Validation floor / contradiction factor | 0.97 / ×0.30 | §8.8 |
| V-ARITH tolerance | ±0.05 (RoundOff present) / ±1.00 (absent) | §8.1 |
| GST rate slabs | {0, 0.1, 0.25, 1, 1.5, 3, 5, 12, 18, 28} | §8.3 |
| PAD_FRAC / PAD_MIN_PX | 12% / 24 px | §9.3 |
| Grid bitmap | 12 × 8 cells | §10.3 |
| Stage A Hamming | ≥ 0.80 | §10.6 |
| Stage B bands | MATCH ≥ 0.85; GRAY 0.60–0.85 | §10.6 |
| Stage B weights | tokens .45 / bbox .35 / table .20 | §10.6 |
| Registration anchors | 2–3; skip-widen ×1.5 | §10.5 |
| Pattern activation / deactivation | n ≥ 3 / 2 violations | §10.7 |
| Field demotion | consecutiveMiss ≥ 3 | §10.8 |
| Template dormancy | lastSeen > 180 d → weight ×0.5 | §10.8 |
| Template match self-tighten | +0.03 per bad match | §10.8 |
| Correction learning weight | ≈ 3 instances | §10.7 |
| Retry policy | 5s·2^k × 3; poison after 3 runs | §13.4 |
| Workbook lock backoff | 30 s × 5 | §12.3 |
| Crop lifecycle | 30 days | §11.5 |
| Lock lease | 60 s, heartbeat-renewed | §13.1 |

### 18.2 Anchor Lexicon (v1 seed; per-field, extendable, versioned)

| Field | Anchor variants (case-insensitive, OCR-fuzzy) |
|---|---|
| Bill Number | Invoice No, Invoice #, Inv No, Inv., Bill No, Bill #, Voucher No, Tax Invoice No, Reference No, Doc No |
| Date | Invoice Date, Bill Date, Dated, Date, Dt. (exclude: Due Date, e-Way Bill Date, Order Date — negative lexicon) |
| GSTIN | GSTIN, GSTIN/UIN, GST No, GST Reg No, GST Registration No |
| Biller identity | From, Sold By, Seller (plus letterhead/pageHeader role, logo band) |
| Payee identity | Bill To, Billed To, Buyer, Customer, M/s, Consignee (Ship To ≠ Bill To — prefer Bill To when both) |
| Phone | Ph, Phone, Tel, Mob, Mobile, Contact, ☎ |
| Sub Total | Sub Total, Subtotal, Taxable Value, Taxable Amt, Total Before Tax, Assessable Value |
| CGST / SGST / IGST | CGST, SGST, UTGST, IGST (rate often inline: "CGST @ 9%") |
| Other Taxes | Cess, Compensation Cess, TCS, Additional Tax |
| Total | Grand Total, Total Amount, Amount Payable, Net Payable, Net Amount, Invoice Total, Total (INR), Balance Due, Total After Tax |
| Round Off | Round Off, Rounded Off, Rounding, R/O, Round(+/-) |
| Items count | Total Items, No. of Items, Item Count (NOT Total Qty) |
| Table headers | Description, Particulars, Item, HSN, HSN/SAC, SAC, Qty, Quantity, Rate, Price, Unit Price, Amount, Value, Per, Disc |
| Totals-block terminators | Sub Total, Total, Amount in Words, Declaration, Authorised Signatory, E&OE |

### 18.3 Open Decisions

| # | Decision | Options / lean |
|---|---|---|
| D-01 | Vision LLM provider & model | In-tenant (Azure-hosted) vs external API; weigh data residency (§15) vs capability; temperature 0 regardless |
| D-02 | Cron host & schedule | Function timer vs WebJob vs container cron; 30 min default |
| D-03 | IGST column mapping | **Adopted in §2.1**: IGST rate → GST %, CGST/SGST null; revisit if reporting needs a dedicated column |
| D-04 | Payee naming | Keep "Payee = bill-to" for Phase 1 (defined §2.1); rename `BillToName` in Phase 2 |
| D-05 | Workbook reprocess policy | Adopted: in-place PATCH by EvalID (§12.3); alternative append-with-Version rejected as duplicate-prone |
| D-06 | Our-own-GSTIN config list | Populate at deploy (enables §7.4 role-swap guard and §10.9 short-circuit) |
| D-07 | Layout-model economics | Revisit running prebuilt-layout unconditionally once template hit-rate is high (skip layout when a MATCH template + prebuilt-invoice suffice?) — measure first via §14 |
| D-08 | DI locale hint | Test `locale=en-IN` effect on prebuilt-invoice date/amount parsing during build |

### 18.4 Glossary

DI — Azure Document Intelligence. L1–L6 — extraction layers (§7.1). V-* — validation checks (§8). Logical invoice — one invoice within a file (a file may contain several). Eval ID — deterministic identity of a logical invoice (§11.4). vendorKey — GSTIN or normalized-name vendor identity (§10.1). Template — per-(vendor, layout-variant) prior bundle (§10). Confirmed extraction — validation-passed / human-approved / LLM-high-confidence-and-consistent result; the only kind that teaches templates. Registration — translation alignment of an incoming page to a template via shared anchors (§10.5). Gray zone — fingerprint score band that forks a new variant instead of corrupting an existing template (§10.6).

---

*End of document — v1.0 draft for review.*
