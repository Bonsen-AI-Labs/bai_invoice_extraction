from pathlib import Path

DATASET_PATH = Path("tests/evals/golden_dataset/invoice_extraction_evals_v1.xlsx")
INVOICE_DIR = Path("data/invoices")
CACHE_DIR = Path("data/evals/cache")
REPORT_DIR = Path("out/evals")
LATEST_REPORT = REPORT_DIR / "latest.json"
MIN_TEMPLATE_TRAINING_CASES = 3
MIN_TEMPLATE_HOLDOUT_CASES = 1

HEADER_TO_FIELD = {
    "Biller Name": "billerName",
    "Payee": "payee",
    "Bill Number": "billNumber",
    "Date": "date",
    "GSTIN": "gstin",
    "Biller Address": "billerAddress",
    "Biller Phone Number": "billerPhone",
    "GST %": "gstPct",
    "CGST %": "cgstPct",
    "SGST %": "sgstPct",
    "Other Taxes %": "otherTaxesPct",
    "Sub Total": "subTotal",
    "Total Bill Amount": "totalBillAmount",
    "Currency": "currency",
    "Total Line Items": "totalLineItems",
    "Round Off": "roundOff",
}
