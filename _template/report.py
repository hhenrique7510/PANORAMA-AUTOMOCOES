"""Geração de saídas — CSV pra análise, Markdown pra leitura."""
from __future__ import annotations

import csv
from pathlib import Path

from parser import Anomaly


def write_csv(items: list[Anomaly], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["label", "value", "extra"])
        for a in items:
            w.writerow([a.label, a.value, a.extra])


def write_markdown(items: list[Anomaly], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Relatório", "", f"Total: **{len(items)}**", ""]
    for a in items:
        lines.append(f"- **{a.label}** — `{a.value}` {a.extra}")
    path.write_text("\n".join(lines), encoding="utf-8")
