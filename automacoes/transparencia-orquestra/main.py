"""Orquestrador: lê CSV da Fase 1 e dispara empresa→contrato→anexa no Panorama."""
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

from worker import processar_csv  # noqa: E402
from report import write_csv, write_markdown, append_history  # noqa: E402

OUT_DIR = HERE / "out"
LOGS_DIR = OUT_DIR / "logs"
SCREENSHOTS_DIR = OUT_DIR / "screenshots"
DEFAULT_CSV = HERE.parent / "transparencia-bayeux" / "out" / "report.csv"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Lê CSV da Fase 1 e cadastra empresa→contrato + anexa PDF."
    )
    p.add_argument("--csv", type=str, default=None,
                   help=f"CSV de entrada (default: {DEFAULT_CSV})")
    p.add_argument("--limit", type=int, default=None,
                   help="Processa só os N primeiros")
    p.add_argument("--only", type=str, default=None,
                   help="Filtra por número ou fornecedor que contenha esse texto")
    p.add_argument("--dry-run", action="store_true",
                   help="Não cadastra nada — só simula")
    p.add_argument("--headed", action="store_true",
                   help="Abre janela do browser")
    p.add_argument("--parallel", type=int, default=1,
                   help="Workers em paralelo (default 1, recomendado 3-5)")
    p.add_argument("--debug", action="store_true", help="Logs verbosos")
    p.add_argument("--skip-empresa", action="store_true",
                   help="Pula criação de empresa (assume que já existem)")
    p.add_argument("--skip-contrato", action="store_true",
                   help="Pula criação de contrato (assume que já existem)")
    p.add_argument("--skip-anexar", action="store_true",
                   help="Pula anexar PDF")
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
    log.info("RUN ID: %s | dry_run=%s | limit=%s | only=%s",
             run_id, args.dry_run, args.limit, args.only)
    log.info("skip: empresa=%s contrato=%s anexar=%s",
             args.skip_empresa, args.skip_contrato, args.skip_anexar)
    log.info("=" * 70)

    csv_path = Path(args.csv) if args.csv else (
        Path(os.environ.get("CSV_PATH") or "") if os.environ.get("CSV_PATH") else DEFAULT_CSV
    )
    if not csv_path.exists():
        raise SystemExit(f"CSV não existe: {csv_path}")
    log.info("CSV: %s", csv_path)

    headless = not (args.headed or args.debug)
    log.info("iniciando Playwright (headless=%s)", headless)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        ctx = await browser.new_context()
        try:
            resultados = await processar_csv(
                ctx, csv_path,
                dry_run=args.dry_run,
                limit=args.limit,
                only=args.only,
                skip_empresa=args.skip_empresa,
                skip_contrato=args.skip_contrato,
                skip_anexar=args.skip_anexar,
                screenshots_dir=SCREENSHOTS_DIR,
                parallel=args.parallel,
            )
        finally:
            await browser.close()

    write_csv(resultados, OUT_DIR / "report.csv")
    write_markdown(resultados, OUT_DIR / "report.md", dry_run=args.dry_run)
    append_history(resultados, OUT_DIR / "history.csv", run_id=run_id)

    log.info("relatório: %s", OUT_DIR / "report.md")
    by_status: dict[str, int] = {}
    for r in resultados:
        by_status[r.get("status", "?")] = by_status.get(r.get("status", "?"), 0) + 1
    log.info("=== resumo ===")
    for s, n in sorted(by_status.items()):
        log.info("  %-25s %d", s, n)


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
