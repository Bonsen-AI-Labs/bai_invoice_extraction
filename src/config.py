"""Phase-1 tunable constants and the anchor lexicon.

Constants and the anchor lexicon. In later phases these move to the
Cosmos `_GLOBAL/config` document; for Phase 1 they live here as module config.
"""

# --- Field schema -----------------------------------------------
# Canonical field keys in workbook column order. `Eval ID` and `File Path` are
# system-set; the rest are extracted.
FIELDS = [
    "billerName",
    "payee",
    "billNumber",
    "date",
    "gstin",
    "billerAddress",
    "billerPhone",
    "gstPct",
    "cgstPct",
    "sgstPct",
    "otherTaxesPct",
    "subTotal",
    "totalBillAmount",
    "currency",
    "totalLineItems",
    "roundOff",
]

AMOUNT_FIELDS = {"subTotal", "totalBillAmount", "roundOff"}
PERCENT_FIELDS = {"gstPct", "cgstPct", "sgstPct", "otherTaxesPct"}

# Internal field key -> master-workbook column header.
FIELD_TO_HEADER = {
    "billerName": "BillerName",
    "payee": "Payee",
    "billNumber": "BillNumber",
    "date": "Date",
    "gstin": "GSTIN",
    "billerAddress": "BillerAddress",
    "billerPhone": "BillerPhone",
    "gstPct": "GSTPct",
    "cgstPct": "CGSTPct",
    "sgstPct": "SGSTPct",
    "otherTaxesPct": "OtherTaxesPct",
    "subTotal": "SubTotal",
    "totalBillAmount": "TotalBillAmount",
    "currency": "Currency",
    "totalLineItems": "TotalLineItems",
    "roundOff": "RoundOff",
}
# Name of the Excel table object the cron writes to.
INVOICE_TABLE = "Invoices"
FAILURE_TABLE = "Failures"

# --- Rendering / crops --------------------------------------
RENDER_DPI = 300
PAD_FRAC = 0.12
PAD_MIN_PX = 24

# --- Intake guards -----------------------------------------------------
MAX_PAGES = 30
MAX_MB = 40

# --- Block grouping ----------------------------------------------------
BLOCK_GAP_FACTOR = 1.6
MIN_X_OVERLAP = 0.25

# --- Anchors -----------------------------------------------------------
ANCHOR_MAX_DIST = 0.15  # × page width
ANCHOR_FUZZ_PER = 5  # Levenshtein ≤ 1 per this many chars

# --- Fusion weights ----------------------------------------------------
LAYER_WEIGHTS = {"L1": 0.30, "L2": 0.25, "L3": 0.10}

# --- Template engine (L6) ----------------------------------------------
TEMPLATE_SCHEMA_VERSION = 1
TEMPLATE_ACTIVE_PATH = "data/templates/active.json"
TEMPLATE_CANDIDATE_PATH = "data/templates/candidate.json"
TEMPLATE_GATE_PATH = "data/templates/gate.json"
TEMPLATE_GRID_ROWS = 12
TEMPLATE_GRID_COLS = 8
TEMPLATE_STAGE_A_MIN = 0.80
TEMPLATE_MATCH_MIN = 0.85
TEMPLATE_GRAY_MIN = 0.60
TEMPLATE_TOKEN_WEIGHT = 0.45
TEMPLATE_BBOX_WEIGHT = 0.35
TEMPLATE_TABLE_WEIGHT = 0.20
TEMPLATE_TOKEN_TOL = 0.02
TEMPLATE_BBOX_TOL = 0.03
TEMPLATE_REGISTRATION_WIDEN = 1.5
TEMPLATE_PATTERN_MIN_SAMPLES = 3
TEMPLATE_PATTERN_MAX_VIOLATIONS = 2
TEMPLATE_FIELD_DEMOTE_MISSES = 3
TEMPLATE_DORMANT_DAYS = 180
TEMPLATE_L6_MAX_WEIGHT = 0.25
TEMPLATE_CORRECTION_WEIGHT = 3

# --- Decision rule -----------------------------------------------------
ACCEPT_CONF = 0.80
ACCEPT_MARGIN = 0.15
CLUSTER_BAND = 0.10

# --- Validation ----------------------------------------------------------
VALID_FLOOR = 0.97  # score floor when a candidate satisfies an identity
CONTRADICTION_FACTOR = 0.30  # score multiplier when it contradicts one
ARITH_TOL_WITH_ROUNDOFF = 0.05
ARITH_TOL_NO_ROUNDOFF = 1.00
GST_SLABS = {0.0, 0.1, 0.25, 1.0, 1.5, 3.0, 5.0, 12.0, 18.0, 28.0}
GSTIN_STATE_CODES = {f"{n:02d}" for n in range(1, 39)} | {"97"}

# --- LLM -----------------------------------------------------------------
# Vision-capable OpenAI mini model. Confirm the exact id for your account (C1);
# override with env OPENAI_VISION_MODEL if set.
OPENAI_VISION_MODEL = "gpt-5-mini"

# --- Anchor lexicon. `-` prefix = negative anchor (must not match). ----
ANCHOR_LEXICON = {
    "billNumber": [
        "Invoice No",
        "Invoice #",
        "Inv No",
        "Inv.",
        "Bill No",
        "Bill #",
        "Voucher No",
        "Tax Invoice No",
        "Reference No",
        "Doc No",
    ],
    "date": [
        "Invoice Date",
        "Bill Date",
        "Dated",
        "Date",
        "Dt.",
        "-Due Date",
        "-e-Way Bill Date",
        "-Order Date",
    ],
    "gstin": ["GSTIN", "GSTIN/UIN", "GST No", "GST Reg No", "GST Registration No"],
    "billerName": ["From", "Sold By", "Seller"],
    "payee": [
        "Bill To",
        "Billed To",
        "Buyer",
        "Customer",
        "M/s",
        "Consignee",
        "-Ship To",
    ],
    "billerPhone": ["Ph", "Phone", "Tel", "Mob", "Mobile", "Contact"],
    "subTotal": [
        "Sub Total",
        "Subtotal",
        "Taxable Value",
        "Taxable Amt",
        "Total Before Tax",
        "Assessable Value",
    ],
    "cgstPct": ["CGST"],
    "sgstPct": ["SGST", "UTGST"],
    "gstPct": ["IGST"],
    "otherTaxesPct": ["Cess", "Compensation Cess", "TCS", "Additional Tax"],
    "totalBillAmount": [
        "Grand Total",
        "Total Amount",
        "Amount Payable",
        "Net Payable",
        "Net Amount",
        "Invoice Total",
        "Total (INR)",
        "Balance Due",
        "Total After Tax",
    ],
    "roundOff": ["Round Off", "Rounded Off", "Rounding", "R/O", "Round"],
    "totalLineItems": ["Total Items", "No. of Items", "Item Count", "-Total Qty"],
}

ITEM_HEADER_TOKENS = [
    "Description",
    "Particulars",
    "Item",
    "HSN",
    "HSN/SAC",
    "SAC",
    "Qty",
    "Quantity",
    "Rate",
    "Price",
    "Unit Price",
    "Amount",
    "Value",
    "Per",
    "Disc",
]
TOTALS_TERMINATORS = [
    "Sub Total",
    "Total",
    "Amount in Words",
    "Declaration",
    "Authorised Signatory",
    "E&OE",
]
INVOICE_TITLE_ANCHORS = ["TAX INVOICE", "INVOICE", "BILL OF SUPPLY"]
