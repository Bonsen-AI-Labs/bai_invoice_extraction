"""OCR client — Azure Document Intelligence pass.

The single expensive pass. Runs prebuilt-invoice (direct field values) and
prebuilt-layout (words/lines/tables with polygons — the substrate for the
custom layers). Uses the SDK's async client directly, not HTTPClient.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from azure.ai.documentintelligence.aio import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
from azure.core.credentials import AzureKeyCredential

from src.env import Settings

# Order-of-magnitude DI price per page per model. Confirm at build time;
# used only for cost accounting in the history doc, never for logic.
DI_USD_PER_PAGE_PER_MODEL = 0.01


@dataclass
class DIResult:
    invoice: Any  # AnalyzeResult from prebuilt-invoice
    layout: Any  # AnalyzeResult from prebuilt-layout
    pages: int
    cost_usd: float


class OCRClient:
    def __init__(self, settings: Settings):
        self._client = DocumentIntelligenceClient(
            endpoint=settings.DOC_INTEL_ENDPOINT,
            credential=AzureKeyCredential(settings.DOC_INTEL_KEY),
        )

    async def aclose(self) -> None:
        await self._client.close()

    async def _analyze(self, model: str, pdf_bytes: bytes):
        poller = await self._client.begin_analyze_document(
            model, AnalyzeDocumentRequest(bytes_source=pdf_bytes)
        )
        return await poller.result()

    async def analyze_invoice(self, pdf_bytes: bytes) -> DIResult:
        invoice = await self._analyze("prebuilt-invoice", pdf_bytes)
        layout = await self._analyze("prebuilt-layout", pdf_bytes)
        pages = len(layout.pages or [])
        return DIResult(
            invoice=invoice,
            layout=layout,
            pages=pages,
            cost_usd=pages * 2 * DI_USD_PER_PAGE_PER_MODEL,
        )
