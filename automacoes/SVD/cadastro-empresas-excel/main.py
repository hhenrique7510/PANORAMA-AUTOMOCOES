"""Entry point: lê a planilha de contratos, extrai as empresas e cadastra no
Panorama Fiscal as que ainda não existem (/svd/empresas)."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")

from parser import empresas_a_cadastrar  # noqa: E402
from worker import processar_empresas  # noqa: E402
from report import (  # noqa: E402
    write_csv, write_markdown, append_history, write_history_md,
)

OUT_DIR = HERE / "out"
LOGS_DIR = OUT_DIR / "logs"
SCREENSHOTS_DIR = OUT_DIR / "screenshots"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Cadastra empresas no Panorama Fiscal a partir da planilha de contratos."
    )
    p.add_argument("--debug", action="store_true", help="Logs verbosos + browser visível")
    p.add_argument("--headed", action="store_true", help="Abre janela do browser")
    p.add_argument("--limit", type=int, default=None, help="Processa só as N primeiras")
    p.add_argument("--dry-run", action="store_true", help="Não salva — só relata o que faria")
    p.add_argument("--xlsx", type=str, default=None,
                   help="Caminho do .xlsx (sobrescreve XLSX_PATH do .env)")
    p.add_argument("--sheet", type=str, default=None,
                   help="Nome da aba (default: $XLSX_SHEET ou 'Planilha1')")
    p.add_argument("--header-row", type=int, default=None,
                   help="Linha do header (1-indexado, default: $XLSX_HEADER_ROW ou 7)")
    p.add_argument("--only", type=str, default=None,
                   help="Processa só empresas cujo nome OU documento contenha esse texto")
    p.add_argument("--exclude", type=str, default=None, nargs="+",
                   help="Pula empresas cujo nome/documento contenha qualquer um destes textos")
    p.add_argument("--all-status", action="store_true",
                   help="Considera empresas de TODAS as linhas (ignora filtro status=ADICIONAR)")
    return p.parse_args()


def _setup_logging(debug: bool, run_id: str) -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOGS_DIR / f"run-{run_id}.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    root.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    stream = logging.StreamHandler(sys.stdout)
    stream.setLevel(logging.DEBUG if debug else logging.INFO)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    file_h = logging.FileHandler(log_file, encoding="utf-8")
    file_h.setLevel(logging.DEBUG)
    file_h.setFormatter(fmt)
    root.addHandler(file_h)

    if not debug:
        logging.getLogger("playwright").setLevel(logging.WARNING)
        logging.getLogger("asyncio").setLevel(logging.WARNING)

    return log_file


def _match(e, alvo: str) -> bool:
    alvo = alvo.lower()
    return alvo in (e.nome or "").lower() or alvo in (e.doc or "")


async def run(args: argparse.Namespace, run_id: str) -> None:
    log = logging.getLogger("main")
    log.info("=" * 70)
    log.info("RUN ID: %s  |  dry_run=%s  |  limit=%s  |  only=%s",
             run_id, args.dry_run, args.limit, args.only)
    log.info("=" * 70)

    xlsx_path = args.xlsx or os.environ.get("XLSX_PATH", "")
    if not xlsx_path:
        raise SystemExit("Configure XLSX_PATH no .env ou passe --xlsx")
    sheet = args.sheet or os.environ.get("XLSX_SHEET", "Planilha1")
    header_row = args.header_row or int(os.environ.get("XLSX_HEADER_ROW", "7"))

    empresas = empresas_a_cadastrar(
        Path(xlsx_path).expanduser(),
        sheet=sheet,
        header_row=header_row,
        so_adicionar=not args.all_status,
    )

    if args.only:
        alvo = args.only.strip()
        empresas = [e for e in empresas if _match(e, alvo)]
        log.info("filtrando por --only %r → %d empresas", args.only, len(empresas))
        if not empresas:
            raise SystemExit(f"nenhuma empresa casou com --only {args.only!r}")

    if args.exclude:
        termos = [t.strip().lower() for t in args.exclude if t.strip()]
        antes = len(empresas)
        empresas = [e for e in empresas if not any(_match(e, t) for t in termos)]
        log.info("excluindo %r → %d puladas, %d restantes",
                 args.exclude, antes - len(empresas), len(empresas))

    if args.limit:
        empresas = empresas[: args.limit]
        log.info("limitando a %d empresas (--limit)", len(empresas))

    if not empresas:
        log.warning("nada a fazer — 0 empresas a processar")
        return

    headless = not (args.debug or args.headed)
    log.info("iniciando Playwright (headless=%s)", headless)

    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context()
        try:
            resultados = await processar_empresas(
                context, empresas,
                dry_run=args.dry_run,
                screenshots_dir=SCREENSHOTS_DIR,
                run_id=run_id,
            )
        except Exception:
            log.exception("erro fatal durante processamento")
            raise
        finally:
            await browser.close()
            log.info("browser fechado")

    write_csv(resultados, OUT_DIR / "report.csv")
    write_markdown(resultados, OUT_DIR / "report.md", dry_run=args.dry_run)

    history_csv = OUT_DIR / "history.csv"
    append_history(resultados, history_csv, run_id=run_id)
    write_history_md(history_csv, OUT_DIR / "history.md")

    log.info("relatório do run: %s", OUT_DIR / "report.md")
    log.info("histórico cumulativo: %s", OUT_DIR / "history.md")

    by_status: dict[str, int] = {}
    for r in resultados:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    log.info("=== resumo do run %s ===", run_id)
    for status, count in sorted(by_status.items()):
        log.info("  %-14s %d", status, count)


def main() -> None:
    args = parse_args()
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    log_file = _setup_logging(args.debug, run_id)
    log = logging.getLogger("main")
    log.info("log desta execução: %s", log_file)

    try:
        asyncio.run(run(args, run_id))
    except SystemExit:
        raise
    except Exception:
        log.exception("execução interrompida por erro não tratado")
        log.error("veja stack trace completo em: %s", log_file)
        sys.exit(1)


if __name__ == "__main__":
    main()
