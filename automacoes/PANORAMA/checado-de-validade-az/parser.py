"""Pure parsing helpers — no Playwright, easy to unit-test."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Anomaly:
    empresa: str
    tarefa: str
    data: str
    pagina: int
    bot: int


def is_out_of_target(data: str, mes_alvo: str, ano_alvo: str) -> bool:
    """Return True if `data` (DD/MM/YYYY) is NOT in (mes_alvo, ano_alvo).

    Empty/malformed strings are treated as out-of-target so they show up
    in the report rather than getting silently dropped.
    """
    parts = (data or "").strip().split("/")
    if len(parts) != 3:
        return True
    _, mm, yyyy = parts
    return mm != mes_alvo or yyyy != ano_alvo
