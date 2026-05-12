"""Funções puras — sem I/O, sem Playwright. Fáceis de unit-test."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Anomaly:
    """Um registro do que sua automação encontrou. Adapte os campos."""
    label: str
    value: str
    extra: str = ""


def is_anomaly(value: str) -> bool:
    """Sua lógica de decisão. Substitua."""
    return False
