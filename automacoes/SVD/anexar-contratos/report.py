"""Geração de saídas — CSV pra análise, Markdown pra leitura, histórico cumulativo."""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

FIELDS = ["arquivo", "numero", "cnpj", "empresa", "data_inicio", "data_fim",
          "valor", "status", "motivo"]

HISTORY_FIELDS = ["timestamp", "run_id", "arquivo", "numero", "cnpj", "empresa",
                  "data_inicio", "data_fim", "valor", "status", "motivo",
                  "screenshot"]


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

    stats = _stats(items)
    titulo = "Cadastro de Contratos (DRY-RUN)" if dry_run else "Cadastro de Contratos"

    lines = [
        f"# Relatório — {titulo}",
        "",
        f"Total de PDFs processados: **{len(items)}**",
        "",
        "## Resumo",
        "",
    ]
    for status, count in sorted(stats.items()):
        lines.append(f"- **{status}**: {count}")
    lines.append("")

    # Agrupa por status (ordem: ações bem-sucedidas → puladas → erros)
    for status in ("anexado", "criado", "dry_run",
                   "ja_anexado", "ja_existe", "nao_encontrado", "erro"):
        do_status = [it for it in items if it.get("status") == status]
        if not do_status:
            continue
        lines.append(f"## {status} ({len(do_status)})")
        lines.append("")
        lines.append("| Número | Arquivo | Empresa | Data Fim | Valor | Motivo |")
        lines.append("|---|---|---|---|---|---|")
        for it in do_status:
            lines.append(
                f"| {it.get('numero','')} "
                f"| `{it.get('arquivo','')}` "
                f"| {it.get('empresa','')[:40]} "
                f"| {it.get('data_fim','')} "
                f"| {it.get('valor','')} "
                f"| {it.get('motivo','')[:80]} |"
            )
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def append_history(items: list[dict], path: Path, run_id: str) -> None:
    """Adiciona ao history.csv (cumulativo entre execuções).

    Se o arquivo ainda não existe, escreve o header. Caso contrário, só apende
    — o histórico nunca é sobrescrito, sempre cresce.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    file_existe = path.exists()
    timestamp = datetime.now().isoformat(timespec="seconds")

    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        if not file_existe:
            w.writeheader()
        for it in items:
            row = {k: it.get(k, "") for k in HISTORY_FIELDS}
            row["timestamp"] = timestamp
            row["run_id"] = run_id
            w.writerow(row)


def write_history_md(history_csv: Path, history_md: Path) -> None:
    """Gera uma versão Markdown agrupada por run_id do history.csv inteiro.

    Útil pra dar uma olhada rápida em "o que rodou em cada execução".
    """
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
        # contagem por status nesta execução
        stats: dict[str, int] = {}
        for r in rows:
            stats[r["status"]] = stats.get(r["status"], 0) + 1
        stats_str = " · ".join(f"{k}={v}" for k, v in sorted(stats.items()))

        lines.append(f"## {ts}  `{run_id}`")
        lines.append("")
        lines.append(f"_{stats_str}_")
        lines.append("")
        lines.append("| Status | Número | Arquivo | Empresa | Motivo |")
        lines.append("|---|---|---|---|---|")
        for r in rows:
            lines.append(
                f"| {r['status']} "
                f"| {r['numero']} "
                f"| `{r['arquivo']}` "
                f"| {r['empresa'][:40]} "
                f"| {r['motivo'][:80]} |"
            )
        lines.append("")

    history_md.write_text("\n".join(lines), encoding="utf-8")
