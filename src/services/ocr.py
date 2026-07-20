"""OCR client — Azure Document Intelligence pass.

The single expensive pass. Runs prebuilt-invoice (direct field values) and
prebuilt-layout (words/lines/tables with polygons — the substrate for the
custom layers). Uses the SDK's async client directly, not HTTPClient.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from azure.ai.documentintelligence.aio import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
from azure.core.credentials import AzureKeyCredential

from src.env import Settings
from src.models import OCRSnapshot

# Order-of-magnitude DI price per page per model. Confirm at build time;
# used only for cost accounting in the history doc, never for logic.
DI_USD_PER_PAGE_PER_MODEL = 0.01


@dataclass
class DIResult:
    invoice: Any  # AnalyzeResult from prebuilt-invoice
    layout: Any  # AnalyzeResult from prebuilt-layout
    pages: int
    cost_usd: float

    def to_snapshot(self) -> OCRSnapshot:
        return OCRSnapshot(
            invoice=self.invoice.as_dict(),
            layout=self.layout.as_dict(),
            pages=self.pages,
            cost_usd=self.cost_usd,
        )

    @classmethod
    def from_snapshot(cls, snapshot: OCRSnapshot) -> DIResult:
        return cls(
            invoice=_attribute_tree(snapshot.invoice),
            layout=_attribute_tree(snapshot.layout),
            pages=snapshot.pages,
            cost_usd=snapshot.cost_usd,
        )


class _AttributeMap(dict):
    """Dict that also supports the attribute access used by the extraction code."""

    def __getattr__(self, name: str):
        try:
            return self[name]
        except KeyError as error:
            raise AttributeError(name) from error


def _attribute_tree(value, *, preserve_keys: bool = False):
    if isinstance(value, dict):
        if preserve_keys:
            return _AttributeMap(
                {key: _attribute_tree(item) for key, item in value.items()}
            )
        converted = {}
        for key, item in value.items():
            attribute = _camel_to_snake(key)
            converted[attribute] = _attribute_tree(
                item,
                preserve_keys=attribute in {"fields", "value_object"},
            )
        return _AttributeMap(converted)
    if isinstance(value, list):
        return [_attribute_tree(item) for item in value]
    return value


def _camel_to_snake(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()


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
