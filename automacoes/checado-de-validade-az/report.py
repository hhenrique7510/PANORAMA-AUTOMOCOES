"""Write report.csv and report.md from a list of Anomaly objects."""
from __future__ import annotations

import csv
from pathlib import Path
from collections import defaultdict

from parser import Anomaly


def write_csv(anomalies: list[Anomaly], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["empresa", "tarefa", "data_confirmacao", "pagina", "bot"])
        for a in anomalies:
            w.writerow([a.empresa, a.tarefa, a.data, a.pagina, a.bot])


def write_markdown(anomalies: list[Anomaly], path: Path, mes_alvo: str, ano_alvo: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    grouped: dict[str, list[Anomaly]] = defaultdict(list)
    for a in anomalies:
        grouped[a.empresa].append(a)

    lines: list[str] = []
    lines.append(f"# Tarefas fora de {mes_alvo}/{ano_alvo}")
    lines.append("")
    lines.append(f"Total de anomalias: **{len(anomalies)}** em **{len(grouped)}** empresas.")
    lines.append("")

    for empresa in sorted(grouped):
        items = grouped[empresa]
        first = items[0]
        lines.append(f"## {empresa}  _(página {first.pagina}, bot {first.bot})_")
        for a in items:
            mes = (a.data.split("/")[1] if a.data.count("/") == 2 else "??")
            lines.append(f"- **{a.tarefa}** — `{a.data}` (mês {mes})")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
