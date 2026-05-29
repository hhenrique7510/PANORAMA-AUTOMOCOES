"""Scraper do Portal de Transparência Bayeux — baixa PDFs e gera CSV.

Conecta no Chrome via CDP. Pré-requisito: abrir o Chrome com:

    /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome \\
      --remote-debugging-port=9222 \\
      --user-data-dir=/tmp/chrome-bayeux-debug

E resolver o captcha de "confirme que é humano" UMA vez no portal.
"""
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

from worker import conectar_chrome, processar_ano, DOWNLOADS_DIR  # noqa: E402
from report import write_csv, write_markdown, append_history, write_history_md  # noqa: E402

OUT_DIR = HERE / "out"
LOGS_DIR = OUT_DIR / "logs"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Baixa PDFs do Portal de Transparência Bayeux."
    )
    p.add_argument("--ano", type=str, default=None, nargs="+",
                   help="Ano(s) a processar (ex: --ano 2026 ou --ano 2026 2025). "
                        "Default: 2026")
    p.add_argument("--limit", type=int, default=None,
                   help="Processa só os N primeiros contratos POR ANO")
    p.add_argument("--only", type=str, default=None,
                   help="Filtra contratos cujo número ou fornecedor contenha esse texto")
    p.add_argument("--parallel", type=int, default=5,
                   help="Quantos contratos processar em paralelo (default: 5)")
    p.add_argument("--debug", action="store_true", help="Logs verbosos")
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
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.DEBUG if debug else logging.INFO)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if not debug:
        logging.getLogger("playwright").setLevel(logging.WARNING)
        logging.getLogger("asyncio").setLevel(logging.WARNING)
    return log_file


async def run(args: argparse.Namespace, run_id: str) -> None:
    log = logging.getLogger("main")
    log.info("=" * 70)
    log.info("RUN ID: %s  |  limit=%s  |  only=%s", run_id, args.limit, args.only)
    log.info("=" * 70)

    anos = args.ano if args.ano else ["2026"]
    log.info("anos a processar: %s", anos)
    log.info("destino dos PDFs: %s", DOWNLOADS_DIR)

    async with async_playwright() as p:
        ctx, page = await conectar_chrome(p)

        resultados_todos = []
        for ano in anos:
            try:
                res = await processar_ano(ctx, page, ano,
                                          limit=args.limit, only=args.only,
                                          parallel=args.parallel)
                resultados_todos.extend(res)
            except Exception:
                log.exception("erro processando ano %s", ano)

    # relatórios
    write_csv(resultados_todos, OUT_DIR / "report.csv")
    write_markdown(resultados_todos, OUT_DIR / "report.md")
    history_csv = OUT_DIR / "history.csv"
    append_history(resultados_todos, history_csv, run_id=run_id)
    write_history_md(history_csv, OUT_DIR / "history.md")

    log.info("relatório: %s", OUT_DIR / "report.md")
    by_status = {}
    for r in resultados_todos:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    log.info("=== resumo ===")
    for s, n in sorted(by_status.items()):
        log.info("  %-15s %d", s, n)


def main() -> None:
    args = parse_args()
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    log_file = _setup_logging(args.debug, run_id)
    log = logging.getLogger("main")
    log.info("log: %s", log_file)
    try:
        asyncio.run(run(args, run_id))
    except SystemExit:
        raise
    except Exception:
        log.exception("execução interrompida")
        sys.exit(1)


if __name__ == "__main__":
    main()
