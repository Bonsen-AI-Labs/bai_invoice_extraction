"""Cosmos client — execution history only (Phase 1 scope).

The single history container is the SHA-256 dedup gate and the run ledger. No
invoice or template documents are stored here in Phase 1; extracted values live
in the master Excel workbook. The history doc carries `id == sha256` so it works
whether the container is partitioned on /id or /sha256.
"""

from __future__ import annotations


from azure.cosmos.aio import CosmosClient as _CosmosClient

from src.env import Settings
from src.models import HistoryDoc


class CosmosClient:
    def __init__(self, settings: Settings):
        self._client = _CosmosClient(
            settings.COSMOS_ENDPOINT, credential=settings.COSMOS_KEY
        )
        self._container = self._client.get_database_client(
            settings.COSMOS_DB_NAME
        ).get_container_client(settings.COSMOS_HISTORY_CONTAINER)

    async def aclose(self) -> None:
        await self._client.close()

    async def is_processed(self, sha256: str) -> bool:
        """True if a DONE history doc already exists for these bytes."""
        query = "SELECT VALUE c.outcome FROM c WHERE c.id=@id"
        params: list[dict[str, object]] = [{"name": "@id", "value": sha256}]
        async for outcome in self._container.query_items(
            query=query, parameters=params
        ):
            return outcome == "DONE"
        return False

    async def record_history(self, doc: HistoryDoc) -> None:
        await self._container.upsert_item(doc.model_dump())

    async def find_identity(self, identity_tuple: list) -> str | None:
        """Return an existing Eval ID whose stored identity tuple matches.
        Cross-run soft-dup source; the Excel rows are the primary in-run source."""
        query = (
            "SELECT c.eval_ids, c.identity_tuples FROM c "
            "WHERE ARRAY_CONTAINS(c.identity_tuples, @t)"
        )
        params: list[dict[str, object]] = [{"name": "@t", "value": identity_tuple}]
        async for doc in self._container.query_items(query=query, parameters=params):
            evs = doc.get("eval_ids") or []
            return evs[0] if evs else "known"
        return None
