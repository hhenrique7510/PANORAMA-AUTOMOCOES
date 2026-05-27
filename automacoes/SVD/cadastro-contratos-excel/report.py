"""Geração de saídas — CSV, Markdown e histórico cumulativo."""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

FIELDS = ["numero", "tipo", "empresa", "cnpj_cpf", "data_inicio", "data_fim",
          "valor", "objeto_inicio", "status", "motivo", "screenshot"]

HISTORY_FIELDS = ["timestamp", "run_id"] + FIELDS


def write_csv(items: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for it in items:
            w.writerow({k: it.get(k, "") for k in FIELDS})


def _stats(items: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        s = it.get("status", "?")
        out[s] = out.get(s, 0) + 1
    return out


def write_markdown(items: list[dict], path: Path, *, dry_run: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    titulo = "Cadastro de Contratos via Excel (DRY-RUN)" if dry_run else "Cadastro de Contratos via Excel"

    stats = _stats(items)
    lines = [
        f"# Relatório — {titulo}",
        "",
        f"Total processado: **{len(items)}**",
        "",
        "## Resumo",
        "",
    ]
    for status, count in sorted(stats.items()):
        lines.append(f"- **{status}**: {count}")
    lines.append("")

    for status in ("criado", "dry_run", "erro"):
        rows = [it for it in items if it.get("status") == status]
        if not rows:
            continue
        lines.append(f"## {status} ({len(rows)})")
        lines.append("")
        lines.append("| Número | Empresa | CNPJ/CPF | Data Fim | Valor | Motivo |")
        lines.append("|---|---|---|---|---|---|")
        for it in rows:
            lines.append(
                f"| {it.get('numero','')} "
                f"| {it.get('empresa','')[:40]} "
                f"| {it.get('cnpj_cpf','')} "
                f"| {it.get('data_fim','')} "
                f"| {it.get('valor','')} "
                f"| {it.get('motivo','')[:80]} |"
            )
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def append_history(items: list[dict], path: Path, run_id: str) -> None:
    """Cumulativo append-only."""
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
    runs: dict[str, list[dict]] = {}
    with history_csv.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            runs.setdefault(row["run_id"], []).append(row)

    lines = ["# Histórico de execuções", "", f"Total de execuções: **{len(runs)}**", ""]
    for run_id in sorted(runs.keys(), reverse=True):
        rows = runs[run_id]
        ts = rows[0].get("timestamp", "?")
        stats: dict[str, int] = {}
        for r in rows:
            stats[r["status"]] = stats.get(r["status"], 0) + 1
        stats_str = " · ".join(f"{k}={v}" for k, v in sorted(stats.items()))

        lines.append(f"## {ts}  `{run_id}`")
        lines.append("")
        lines.append(f"_{stats_str}_")
        lines.append("")
        lines.append("| Status | Número | Empresa | Motivo |")
        lines.append("|---|---|---|---|")
        for r in rows:
            lines.append(
                f"| {r['status']} | {r['numero']} | {r['empresa'][:40]} | {r['motivo'][:80]} |"
            )
        lines.append("")
    history_md.write_text("\n".join(lines), encoding="utf-8")
