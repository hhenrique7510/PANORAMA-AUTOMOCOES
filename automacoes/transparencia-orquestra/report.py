"""Geração de saídas — CSV, MD, histórico cumulativo."""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

FIELDS = ["numero", "fornecedor", "doc", "pdf_path",
          "empresa_criada", "contrato_criado", "pdf_anexado",
          "status", "motivo"]
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
    titulo = "DRY-RUN" if dry_run else "Pipeline Transparência → Panorama"
    stats = _stats(items)
    lines = [f"# Relatório — {titulo}", "",
             f"Total processado: **{len(items)}**", "", "## Resumo", ""]
    for s, n in sorted(stats.items()):
        lines.append(f"- **{s}**: {n}")
    lines.append("")
    for status in ("anexado", "pdf_ja_anexado", "criou_contrato_e_anexou",
                   "criou_empresa_e_anexou", "dry_run", "incompleto", "erro"):
        rows = [it for it in items if it.get("status") == status]
        if not rows:
            continue
        lines.append(f"## {status} ({len(rows)})")
        lines.append("")
        lines.append("| Número | Fornecedor | Empresa | Contrato | PDF | Motivo |")
        lines.append("|---|---|---|---|---|---|")
        for it in rows:
            lines.append(
                f"| {it.get('numero','')} "
                f"| {it.get('fornecedor','')[:35]} "
                f"| {it.get('empresa_criada','')} "
                f"| {it.get('contrato_criado','')} "
                f"| {it.get('pdf_anexado','')} "
                f"| {it.get('motivo','')[:60]} |"
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
