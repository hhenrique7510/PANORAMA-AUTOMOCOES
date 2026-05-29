"""Compara o que ESTÁ no Panorama com o que ESTÁ no CSV da Fase 1.

Mostra:
  - Contratos NO CSV E NO PANORAMA (esperado — ja existiam ou foram criados)
  - Contratos NO CSV mas NÃO NO PANORAMA (faltam cadastrar)
  - Contratos NO PANORAMA mas NÃO NO CSV (contratos antigos, não veio do portal Bayeux)

Chave de comparação: (numero, cnpj_ou_cpf)
"""
from __future__ import annotations

import asyncio
import csv
import logging
import re
import sys
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
log = logging.getLogger("diff")


def _normalizar_numero(raw: str) -> str:
    m = re.search(r"(\d{1,6})\s*[/\-]\s*(\d{4})", str(raw or ""))
    if not m:
        return ""
    return f"{m.group(1).lstrip('0').zfill(5) or '00000'}/{m.group(2)}"


def ler_csv_fase1() -> dict[tuple[str, str], dict]:
    """{(numero, cnpj_ou_cpf): linha_do_csv}"""
    out: dict[tuple[str, str], dict] = {}
    with CSV_FASE1.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("status") or "") != "baixado":
                continue
            numero = _normalizar_numero(row.get("numero") or "")
            doc = (row.get("cnpj") or "").strip() or (row.get("cpf") or "").strip()
            doc = re.sub(r"\D", "", doc)
            if not numero:
                continue
            out[(numero, doc)] = row
    return out


async def main() -> None:
    log.info("lendo CSV da Fase 1: %s", CSV_FASE1)
    csv_data = ler_csv_fase1()
    log.info("✓ %d linhas únicas (numero+cnpj) no CSV", len(csv_data))

    log.info("carregando workers + login...")
    w = carregar_workers()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        page.on("dialog", lambda d: asyncio.create_task(d.accept()))
        await w.anexar_worker.ensure_logged_in(page)

        log.info("listando TODOS contratos do Panorama (5+ páginas)...")
        panorama = await listar_contratos_com_cnpj(page)
        log.info("✓ %d contratos no Panorama", len(panorama))

        keys_csv = set(csv_data.keys())
        keys_panorama = set(panorama.keys())

        no_csv_e_panorama = keys_csv & keys_panorama
        so_no_csv = keys_csv - keys_panorama
        so_no_panorama = keys_panorama - keys_csv

        log.info("=" * 60)
        log.info("CSV ∩ Panorama (deveriam estar — OK):     %d", len(no_csv_e_panorama))
        log.info("CSV \\ Panorama (faltam cadastrar):        %d", len(so_no_csv))
        log.info("Panorama \\ CSV (não vieram do portal):    %d", len(so_no_panorama))
        log.info("Total Panorama:                            %d", len(panorama))
        log.info("Total CSV:                                 %d", len(csv_data))
        log.info("=" * 60)

        # Gera MD com listagem detalhada
        md_path = OUT_DIR / "diff.md"
        lines = [
            "# Diff CSV (Fase 1) vs Panorama Fiscal",
            "",
            "Chave: `(numero, cnpj_ou_cpf)`",
            "",
            f"- CSV (Fase 1) tem: **{len(csv_data)}** linhas únicas",
            f"- Panorama Fiscal tem: **{len(panorama)}** contratos",
            "",
            "## Resumo",
            "",
            f"- ✓ Em ambos (cadastrados corretamente): **{len(no_csv_e_panorama)}**",
            f"- ⏳ No CSV mas FALTAM no Panorama: **{len(so_no_csv)}**",
            f"- 📜 No Panorama mas NÃO veio do portal Bayeux: **{len(so_no_panorama)}**",
            "",
            f"## Faltam cadastrar ({len(so_no_csv)})",
            "",
        ]
        if so_no_csv:
            lines.append("| Número | CNPJ/CPF | Empresa | Valor |")
            lines.append("|---|---|---|---|")
            for k in sorted(so_no_csv):
                r = csv_data[k]
                lines.append(
                    f"| {k[0]} | `{k[1]}` "
                    f"| {(r.get('fornecedor') or '')[:40]} "
                    f"| {r.get('valor','')} |"
                )
        else:
            lines.append("_(nenhum)_")
        lines.append("")
        lines.append(f"## Já existiam antes do portal ({len(so_no_panorama)})")
        lines.append("")
        lines.append("_Esses contratos estavam no Panorama mas NÃO vieram do portal Bayeux —"
                     " provavelmente foram cadastrados manualmente ou por outra origem._")
        md_path.write_text("\n".join(lines), encoding="utf-8")
        log.info("✓ Relatório: %s", md_path)

        # CSV detalhado dos que faltam cadastrar
        if so_no_csv:
            faltam_csv = OUT_DIR / "faltam_cadastrar.csv"
            with faltam_csv.open("w", newline="", encoding="utf-8") as f:
                wr = csv.writer(f)
                wr.writerow(["numero", "cnpj_ou_cpf", "fornecedor", "valor",
                             "data_inicio", "data_fim", "pdf_path"])
                for k in sorted(so_no_csv):
                    r = csv_data[k]
                    wr.writerow([k[0], k[1], r.get("fornecedor", ""),
                                 r.get("valor", ""), r.get("data_inicio", ""),
                                 r.get("data_fim", ""), r.get("pdf_path", "")])
            log.info("✓ Faltam cadastrar (CSV): %s", faltam_csv)

        await asyncio.sleep(2)
        await browser.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
