"""Local versioned storage for active and candidate templates."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol

from pydantic import TypeAdapter

from src import config
from src.models import TemplateDocument

_TEMPLATES = TypeAdapter(list[TemplateDocument])


class TemplateStore(Protocol):
    def load_active(self) -> list[TemplateDocument]: ...

    def load_candidate(self) -> list[TemplateDocument]: ...

    def save_candidate(self, templates: list[TemplateDocument]) -> None: ...

    def promote(self) -> None: ...


class JsonTemplateStore:
    """Single-process JSON store with atomic replacement for crash safety."""

    def __init__(
        self,
        active_path: str | Path = config.TEMPLATE_ACTIVE_PATH,
        candidate_path: str | Path = config.TEMPLATE_CANDIDATE_PATH,
    ) -> None:
        self.active_path = Path(active_path)
        self.candidate_path = Path(candidate_path)

    def load_active(self) -> list[TemplateDocument]:
        return self._load(self.active_path)

    def load_candidate(self) -> list[TemplateDocument]:
        candidate = self._load(self.candidate_path)
        return candidate or [t.model_copy(deep=True) for t in self.load_active()]

    def save_candidate(self, templates: list[TemplateDocument]) -> None:
        self._write(self.candidate_path, templates)

    def promote(self) -> None:
        templates = self._load(self.candidate_path)
        if not templates:
            raise ValueError("candidate template store is empty")
        for template in templates:
            template.stats.health = "ACTIVE"
        self._write(self.active_path, templates)

    @staticmethod
    def _load(path: Path) -> list[TemplateDocument]:
        if not path.exists():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != config.TEMPLATE_SCHEMA_VERSION:
            raise ValueError(f"unsupported template schema in {path}")
        return _TEMPLATES.validate_python(payload.get("templates", []))

    @staticmethod
    def _write(path: Path, templates: list[TemplateDocument]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": config.TEMPLATE_SCHEMA_VERSION,
            "templates": [template.model_dump(mode="json") for template in templates],
        }
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        os.replace(temporary, path)
