"""LLM client — vision arbitration for disputed fields.

An arbiter, not a first-resort extractor. Receives per disputed field: the
definition, the top-k candidates, and cropped image regions; returns strict JSON.
Temperature 0. All disputed fields of one invoice go in a single call.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

from openai import AsyncOpenAI
from openai.types.chat import (
    ChatCompletionContentPartParam,
    ChatCompletionMessageParam,
)

from src.config import OPENAI_VISION_MODEL
from src.env import Settings

_SYSTEM = (
    "You verify fields on Indian GST invoices. Answer ONLY with the JSON schema "
    "provided. Judge from the image; candidates are hints that may be wrong. If "
    "none are correct, supply the value you read. If the field is absent, value=null."
)

# rough per-1k-token blended price, cost accounting only (never logic)
_USD_PER_1K = 0.001


def _data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode()


class LLMClient:
    def __init__(self, settings: Settings):
        self._client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        self._model = os.getenv("OPENAI_VISION_MODEL", OPENAI_VISION_MODEL)

    async def arbitrate(self, disputes: list[dict[str, Any]]) -> dict[str, Any]:
        """disputes: [{field, definition, candidates:[{value,source}], crops:[png], note}].
        Returns {"verdicts": {field: {value, chosenCandidate, confidence, reason}},
                 "cost_usd": float, "fields": [field, ...]}."""
        if not disputes:
            return {"verdicts": {}, "cost_usd": 0.0, "fields": []}

        content: list[ChatCompletionContentPartParam] = [
            {
                "type": "text",
                "text": 'Return JSON {"fields":[{"field":str,"value":any,'
                '"chosenCandidate":int|null,"confidence":number,"reason":str}]}.',
            }
        ]
        for d in disputes:
            content.append(
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "field": d["field"],
                            "definition": d.get("definition", ""),
                            "candidates": d.get("candidates", []),
                            "positionalNote": d.get("note", ""),
                        }
                    ),
                }
            )
            for png in d.get("crops", []):
                content.append(
                    {"type": "image_url", "image_url": {"url": _data_url(png)}}
                )

        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": content},
        ]
        resp = await self._client.chat.completions.create(
            model=self._model,
            # ponytail: gpt-5-mini only supports the default temperature (1);
            # determinism now leans on the strict-JSON schema, not temp=0.
            response_format={"type": "json_object"},
            messages=messages,
        )
        parsed = json.loads(resp.choices[0].message.content or "{}")
        verdicts = {f["field"]: f for f in parsed.get("fields", []) if "field" in f}
        usage = resp.usage
        cost = ((usage.total_tokens if usage else 0) / 1000) * _USD_PER_1K
        return {"verdicts": verdicts, "cost_usd": cost, "fields": list(verdicts)}
