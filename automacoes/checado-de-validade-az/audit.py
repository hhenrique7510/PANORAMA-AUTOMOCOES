"""Entry point: orchestrates 3 Playwright workers across all pages of Certidões."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")

from worker import run_worker, ensure_logged_in, set_page_length_100, discover_total_pages  # noqa: E402
from report import write_csv, write_markdown  # noqa: E402
from parser import Anomaly  # noqa: E402

OUT_DIR = HERE / "out"


def split_ranges(n_total: int, n_bots: int) -> list[list[int]]:
    """Distribute pages 1..n_total across n_bots in contiguous chunks."""
    chunks: list[list[int]] = [[] for _ in range(n_bots)]
    for i in range(1, n_total + 1):
        chunks[(i - 1) * n_bots // n_total].append(i)
    return chunks


async def main(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("audit")
    headless = not args.headed

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context()

        # Use a single page to log in and read total pages.
        scout = await context.new_page()
        await ensure_logged_in(scout)
        await set_page_length_100(scout)
        n_total = await discover_total_pages(scout)
        log.info("Total de páginas (100/p): %d", n_total)
        await scout.close()

        if args.max_pages:
            n_total = min(n_total, args.max_pages)
            log.info("Limitando a %d páginas via --max-pages", n_total)

        ranges = split_ranges(n_total, args.bots)
        for i, r in enumerate(ranges):
            log.info("Bot %d → páginas %s", i, r if len(r) < 8 else f"{r[0]}..{r[-1]} ({len(r)} pg)")

        results = await asyncio.gather(*[
            run_worker(context, bot_id=i, pages=ranges[i])
            for i in range(args.bots)
            if ranges[i]  # skip empty
        ])
        await browser.close()

    anomalies: list[Anomaly] = [a for sub in results for a in sub]
    log.info("Total de anomalias coletadas: %d", len(anomalies))

    write_csv(anomalies, OUT_DIR / "report.csv")
    write_markdown(
        anomalies,
        OUT_DIR / "report.md",
        mes_alvo=os.environ.get("MES_ALVO", "05"),
        ano_alvo=os.environ.get("ANO_ALVO", "2026"),
    )
    log.info("Relatórios: %s, %s", OUT_DIR / "report.csv", OUT_DIR / "report.md")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Audita tarefas fora do mês alvo no Panorama Fiscal.")
    p.add_argument("--bots", type=int, default=3, help="Nº de bots paralelos (default: 3)")
    p.add_argument("--headed", action="store_true", help="Abre janelas visíveis")
    p.add_argument("--max-pages", type=int, default=None, help="Limita ao topo das páginas (debug)")
    return p.parse_args()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
