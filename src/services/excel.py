"""Excel client — master workbook read/upsert via the Graph workbook API.

Table-object operations (not raw ranges) so writes survive user sorting/filtering.
Rows are keyed by the EvalID column; upsert = PATCH existing row or add a new one.
"""

from __future__ import annotations

import base64
from typing import Any

from src.config import FAILURE_TABLE, INVOICE_TABLE
from src.env import Settings
from src.services.http import HTTPClient


def _share_token(url: str) -> str:
    return "u!" + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


class ExcelClient:
    def __init__(self, http: HTTPClient, settings: Settings):
        self._http = http
        self._doc_url = settings.MASTER_EXCEL_DOCUMENT_URL
        self._drive_id: str | None = None
        self._item_id: str | None = None
        self._headers: list[str] | None = None

    async def _wb(self) -> str:
        """Return the workbook base path, resolving the doc URL once."""
        if not (self._drive_id and self._item_id):
            item = await self._http.graph_json(
                "GET", f"/shares/{_share_token(self._doc_url)}/driveItem"
            )
            self._drive_id = item["parentReference"]["driveId"]
            self._item_id = item["id"]
        return f"/drives/{self._drive_id}/items/{self._item_id}/workbook"

    async def _table_headers(self) -> list[str]:
        if self._headers is None:
            wb = await self._wb()
            data = await self._http.graph_json(
                "GET", f"{wb}/tables/{INVOICE_TABLE}/headerRowRange?$select=values"
            )
            self._headers = data["values"][0]
        assert self._headers is not None
        return self._headers

    async def read_rows(self) -> list[dict[str, Any]]:
        """All data rows as {header: value} dicts (soft-dup lookup source)."""
        wb = await self._wb()
        headers = await self._table_headers()
        data = await self._http.graph_json(
            "GET", f"{wb}/tables/{INVOICE_TABLE}/rows?$select=values"
        )
        return [
            dict(zip(headers, row))
            for r in data.get("value", [])
            for row in r["values"]
        ]

    async def find_row(self, eval_id: str) -> int | None:
        """Row index (0-based within the table body) for an EvalID, or None."""
        for i, row in enumerate(await self.read_rows()):
            if row.get("EvalID") == eval_id:
                return i
        return None

    async def upsert_row(self, eval_id: str, values_by_header: dict[str, Any]) -> None:
        wb = await self._wb()
        headers = await self._table_headers()
        # Only the cron-owned columns; never write human Corr_*/Review columns.
        ordered = [[_cell(values_by_header.get(h)) for h in headers]]
        idx = await self.find_row(eval_id)
        if idx is None:
            await self._http.graph_json(
                "POST",
                f"{wb}/tables/{INVOICE_TABLE}/rows/add",
                json={"values": ordered},
            )
        else:
            await self._http.graph_json(
                "PATCH",
                f"{wb}/tables/{INVOICE_TABLE}/rows/itemAt(index={idx})",
                json={"values": ordered},
            )

    async def add_failure(self, row: dict[str, Any]) -> None:
        """Append to the Failures table if it exists (best-effort)."""
        wb = await self._wb()
        resp = await self._http.graph(
            "GET", f"{wb}/tables/{FAILURE_TABLE}/headerRowRange?$select=values"
        )
        if resp.status_code != 200:
            return
        headers = resp.json()["values"][0]
        await self._http.graph_json(
            "POST",
            f"{wb}/tables/{FAILURE_TABLE}/rows/add",
            json={"values": [[_cell(row.get(h)) for h in headers]]},
        )


def _cell(v: Any) -> Any:
    """Graph rejects None; blank cells must be empty strings."""
    return "" if v is None else v
