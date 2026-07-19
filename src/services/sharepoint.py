"""SharePoint client — list and download PDFs from the drop folder.

Resolves the configured folder sharing URL to a Graph drive/item, then lists and
downloads children. All Graph traffic goes through HTTPClient.
"""

from __future__ import annotations

import base64

from src.env import Settings
from src.models import FileRef
from src.services.http import HTTPClient


def _share_token(url: str) -> str:
    """Encode a sharing URL into a Graph /shares token (u!<base64url>)."""
    b64 = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return "u!" + b64


class SharePointClient:
    def __init__(self, http: HTTPClient, settings: Settings):
        self._http = http
        self._folder_url = settings.SHAREPOINT_FOLDER_URL
        self._drive_id: str | None = None
        self._item_id: str | None = None

    async def _resolve_folder(self) -> tuple[str, str]:
        if self._drive_id and self._item_id:
            return self._drive_id, self._item_id
        item = await self._http.graph_json(
            "GET", f"/shares/{_share_token(self._folder_url)}/driveItem"
        )
        drive_id = item["parentReference"]["driveId"]
        item_id = item["id"]
        self._drive_id, self._item_id = drive_id, item_id
        return drive_id, item_id

    async def list_new_pdfs(self, since: str | None = None) -> list[FileRef]:
        """List PDF children. `since` (ISO-8601) is a listing optimization only —
        novelty is decided later by content hash."""
        drive_id, item_id = await self._resolve_folder()
        refs: list[FileRef] = []
        path = f"/drives/{drive_id}/items/{item_id}/children?$top=200"
        while path:
            page = await self._http.graph_json("GET", path)
            for child in page.get("value", []):
                if "file" not in child or not child["name"].lower().endswith(".pdf"):
                    continue
                if since and child.get("lastModifiedDateTime", "") <= since:
                    continue
                refs.append(
                    FileRef(
                        name=child["name"],
                        drive_id=drive_id,
                        item_id=child["id"],
                        path=child.get("webUrl", child["name"]),
                        size=child.get("size", 0),
                        last_modified=child.get("lastModifiedDateTime"),
                    )
                )
            path = page.get("@odata.nextLink")
        return refs

    async def download(self, ref: FileRef) -> bytes:
        # Fetch the short-lived pre-authenticated URL, then download without the
        # bearer header (the storage backend rejects it). ponytail: one extra GET
        # beats juggling redirect auth.
        item = await self._http.graph_json(
            "GET",
            f"/drives/{ref.drive_id}/items/{ref.item_id}"
            "?$select=@microsoft.graph.downloadUrl",
        )
        return await self._http.download(item["@microsoft.graph.downloadUrl"])
