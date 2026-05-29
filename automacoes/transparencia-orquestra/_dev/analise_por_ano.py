"""Analisa de onde vêm os 500+ contratos do Panorama.

Comparações por ANO (extraído do número NNNNN/AAAA):

  1. Quantos contratos cada ANO tem no Panorama
  2. Quantos contratos do portal Bayeux (CSV da Fase 1) por ano
  3. Diferença → contratos que NÃO vieram do portal Bayeux

Possíveis explicações pra contratos "fantasma":
  - Anos antes de 2021 (não baixamos do portal)
  - Contratos cadastrados manualmente no Panorama (não passam pelo portal)
  - Termos aditivos contados como contratos
"""
from __future__ import annotations

import asyncio
import csv
import logging
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
load_dotenv(ROOT / ".env")

sys.path.insert(0, str(ROOT))
from loaders import carregar_workers  # noqa: E402
from worker import listar_contratos_com_cnpj  # noqa: E402

OUT_DIR = ROOT / "out"
OUT_DIR.mkdir(parents=True, exist_ok=True)

CSV_FASE1 = ROOT.parent / "transparencia-bayeux" / "out" / "report.csv"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("analise")


def _ano(numero: str) -> str:
    m = re.search(r"/(\d{4})$", numero or "")
    return m.group(1) if m else "?"


def _norm(s: str) -> str:
    m = re.search(r"(\d{1,6})\s*[/\-]\s*(\d{4})", str(s or ""))
    if not m:
        return ""
    return f"{m.group(1).lstrip('0').zfill(5) or '00000'}/{m.group(2)}"


def ler_csv_fase1() -> set[tuple[str, str]]:
    """{(numero_normalizado, cnpj_ou_cpf)} do CSV da Fase 1."""
    out: set[tuple[str, str]] = set()
    with CSV_FASE1.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("status") or "") != "baixado":
                continue
            n = _norm(row.get("numero") or "")
            doc = (row.get("cnpj") or "").strip() or (row.get("cpf") or "").strip()
            doc = re.sub(r"\D", "", doc)
            if n:
                out.add((n, doc))
    return out


async def main() -> None:
    log.info("lendo CSV Fase 1: %s", CSV_FASE1)
    csv_set = ler_csv_fase1()
    log.info("✓ %d (num, cnpj) únicos no CSV", len(csv_set))

    log.info("carregando workers...")
    w = carregar_workers()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        page.on("dialog", lambda d: asyncio.create_task(d.accept()))
        await w.anexar_worker.ensure_logged_in(page)

        log.info("listando contratos do Panorama (com numero + cnpj)...")
        panorama = await listar_contratos_com_cnpj(page)
        log.info("✓ %d contratos no Panorama", len(panorama))

        # Contagem por ano
        anos_csv: Counter[str] = Counter()
        for (n, _doc) in csv_set:
            anos_csv[_ano(n)] += 1

        anos_panorama_total: Counter[str] = Counter()
        anos_panorama_do_csv: Counter[str] = Counter()
        anos_panorama_so_panorama: Counter[str] = Counter()
        for (n, doc) in panorama.keys():
            ano = _ano(n)
            anos_panorama_total[ano] += 1
            if (n, doc) in csv_set:
                anos_panorama_do_csv[ano] += 1
            else:
                anos_panorama_so_panorama[ano] += 1

        # Imprime tabela
        todos_anos = sorted(set(list(anos_csv) + list(anos_panorama_total)))

        log.info("=" * 88)
        log.info(f"{'ANO':<6} {'CSV (Bayeux)':>14} {'Panorama':>10} "
                 f"{'do CSV':>10} {'só Panorama':>14}  comentário")
        log.info("=" * 88)
        total_csv = total_pano = total_match = total_so_pano = 0
        for ano in todos_anos:
            c1 = anos_csv.get(ano, 0)
            c2 = anos_panorama_total.get(ano, 0)
            c3 = anos_panorama_do_csv.get(ano, 0)
            c4 = anos_panorama_so_panorama.get(ano, 0)
            total_csv += c1; total_pano += c2; total_match += c3; total_so_pano += c4

            coment = ""
            if c1 == 0 and c2 > 0:
                coment = "← ano não baixado do portal"
            elif c2 > c1:
                coment = f"← {c2-c1} contrato(s) a mais no Panorama"
            elif c1 > c2:
                coment = f"← {c1-c2} contrato(s) faltam no Panorama"

            log.info(f"{ano:<6} {c1:>14} {c2:>10} {c3:>10} {c4:>14}  {coment}")
        log.info("-" * 88)
        log.info(f"{'TOTAL':<6} {total_csv:>14} {total_pano:>10} "
                 f"{total_match:>10} {total_so_pano:>14}")
        log.info("=" * 88)

        # Gera MD
        md_path = OUT_DIR / "analise_por_ano.md"
        lines = [
            "# Análise por Ano — Panorama vs Portal Bayeux",
            "",
            f"- CSV Fase 1 (portal Bayeux 2021-2026): **{len(csv_set)}** únicos",
            f"- Panorama Fiscal: **{len(panorama)}** contratos",
            "",
            "## Distribuição por ano",
            "",
            "| Ano | Portal Bayeux | Panorama | Comum (do portal) | Só no Panorama | Observação |",
            "|---|---|---|---|---|---|",
        ]
        for ano in todos_anos:
            c1 = anos_csv.get(ano, 0)
            c2 = anos_panorama_total.get(ano, 0)
            c3 = anos_panorama_do_csv.get(ano, 0)
            c4 = anos_panorama_so_panorama.get(ano, 0)
            coment = ""
            if c1 == 0 and c2 > 0:
                coment = "📜 ano não baixado (portal só cobriu 2021-2026)"
            elif c2 > c1:
                coment = f"⚠ +{c2-c1} no Panorama (manuais? duplicados?)"
            elif c1 > c2:
                coment = f"⏳ faltam {c1-c2} cadastrar"
            lines.append(f"| **{ano}** | {c1} | {c2} | {c3} | {c4} | {coment} |")
        lines.append(f"| **TOTAL** | **{total_csv}** | **{total_pano}** "
                     f"| **{total_match}** | **{total_so_pano}** | |")

        # Lista os contratos "só Panorama" agrupados por ano
        lines.append("")
        lines.append("## Contratos que estão no Panorama mas NÃO no portal Bayeux")
        lines.append("")
        lines.append("Podem ser: contratos cadastrados manualmente, anos antigos não cobertos, "
                     "ou termos aditivos contados como contratos.")
        lines.append("")
        so_pano: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for (n, doc) in panorama.keys():
            if (n, doc) not in csv_set:
                so_pano[_ano(n)].append((n, doc))

        for ano in sorted(so_pano.keys()):
            items = sorted(so_pano[ano])
            lines.append(f"### {ano} — {len(items)} contrato(s)")
            lines.append("")
            for n, doc in items[:30]:
                lines.append(f"- `{n}` doc: `{doc or '—'}`  (ID Panorama: {panorama.get((n, doc), '?')})")
            if len(items) > 30:
                lines.append(f"- _(+{len(items) - 30} outros…)_")
            lines.append("")

        md_path.write_text("\n".join(lines), encoding="utf-8")
        log.info("✓ Relatório: %s", md_path)

        await asyncio.sleep(2)
        await browser.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
