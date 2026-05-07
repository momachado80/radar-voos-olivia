"""Estado de ciclo: divide as rotas em chunks pra caber na cota da API por execução."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CycleState:
    path: Path
    cursor: int = 0

    @classmethod
    def load(cls, path: Path) -> "CycleState":
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
            return cls(path=path, cursor=int(data.get("cursor", 0)))
        return cls(path=path, cursor=0)

    def next_chunk(self, total: int, chunk_size: int) -> tuple[int, int]:
        if total == 0:
            return (0, 0)
        start = self.cursor % total
        end = min(start + chunk_size, total)
        return (start, end)

    def advance(self, total: int, chunk_size: int) -> None:
        if total == 0:
            self.cursor = 0
            return
        self.cursor = (self.cursor + chunk_size) % total

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"cursor": self.cursor}, indent=2), encoding="utf-8")
