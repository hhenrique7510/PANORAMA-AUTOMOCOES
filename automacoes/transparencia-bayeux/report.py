"""Geração de saídas — CSV, MD e histórico cumulativo."""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

FIELDS = ["ano", "numero", "contrato_id", "fornecedor", "cnpj", "cpf",
          "fiscal", "licitacao", "data_inicio", "data_fim", "valor",
          "objeto", "detalhe_url", "pdf_url", "pdf_path", "status", "motivo"]

HISTORY_FIELDS = ["timestamp", "run_id"] + FIELDS


def write_csv(items: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for it in items:
            w.writerow({k: it.get(k, "") for k in FIELDS})


def write_markdown(items: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stats = {}
    for it in items:
        s = it.get("status", "?")
        stats[s] = stats.get(s, 0) + 1

    lines = ["# Relatório — Scraper Transparência Bayeux", "",
             f"Total processado: **{len(items)}**", "", "## Resumo", ""]
    for s, n in sorted(stats.items()):
        lines.append(f"- **{s}**: {n}")
    lines.append("")

    for status in ("baixado", "sem_pdf", "erro_download", "erro"):
        rows = [it for it in items if it.get("status") == status]
        if not rows:
            continue
        lines.append(f"## {status} ({len(rows)})")
        lines.append("")
        lines.append("| Número | Fornecedor | CNPJ/CPF | Valor | Arquivo / Motivo |")
        lines.append("|---|---|---|---|---|")
        for it in rows:
            arq_ou_motivo = it.get("pdf_path") or it.get("motivo") or ""
            arq_ou_motivo = Path(arq_ou_motivo).name if arq_ou_motivo.startswith("/") else arq_ou_motivo
            lines.append(
                f"| {it.get('numero','')} "
                f"| {it.get('fornecedor','')[:40]} "
                f"| {it.get('cnpj') or it.get('cpf','')} "
                f"| {it.get('valor','')} "
                f"| {arq_ou_motivo[:60]} |"
            )
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def append_history(items: list[dict], path: Path, run_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existe = path.exists()
    ts = datetime.now().isoformat(timespec="seconds")
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        if not existe:
            w.writeheader()
        for it in items:
            row = {k: it.get(k, "") for k in HISTORY_FIELDS}
            row["timestamp"] = ts
            row["run_id"] = run_id
            w.writerow(row)


def write_history_md(history_csv: Path, history_md: Path) -> None:
    if not history_csv.exists():
        return
    runs = {}
    with history_csv.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            runs.setdefault(row["run_id"], []).append(row)

    lines = ["# Histórico de execuções", "",
             f"Total de runs: **{len(runs)}**", ""]
    for rid in sorted(runs.keys(), reverse=True):
        rows = runs[rid]
        ts = rows[0].get("timestamp", "?")
        stats = {}
        for r in rows:
            stats[r["status"]] = stats.get(r["status"], 0) + 1
        stats_str = " · ".join(f"{k}={v}" for k, v in sorted(stats.items()))
        lines.append(f"## {ts}  `{rid}`")
        lines.append("")
        lines.append(f"_{stats_str}_")
        lines.append("")
    history_md.write_text("\n".join(lines), encoding="utf-8")
