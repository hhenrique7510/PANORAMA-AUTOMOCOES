"""Entry point: lê PDFs de contrato, verifica no Panorama Fiscal e cadastra os que faltam."""
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

from parser import parse_diretorio  # noqa: E402
from worker import processar_pdfs  # noqa: E402
from report import (  # noqa: E402
    write_csv, write_markdown, append_history, write_history_md,
)

OUT_DIR = HERE / "out"
LOGS_DIR = OUT_DIR / "logs"
SCREENSHOTS_DIR = OUT_DIR / "screenshots"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Cadastra contratos no Panorama Fiscal a partir de PDFs."
    )
    p.add_argument("--debug", action="store_true", help="Logs verbosos + browser visível")
    p.add_argument("--limit", type=int, default=None, help="Processa só os N primeiros PDFs")
    p.add_argument("--dry-run", action="store_true", help="Não salva — só relata o que faria")
    p.add_argument(
        "--pdf-dir",
        type=str,
        default=None,
        nargs="+",
        help="Uma OU MAIS pastas com PDFs (sobrescreve PDF_DIR do .env). "
             "Ex: --pdf-dir \"/caminho/pasta A\" \"/caminho/pasta B\"",
    )
    p.add_argument("--headed", action="store_true", help="Abre janelas visíveis (mesmo sem --debug)")
    p.add_argument(
        "--only",
        type=str,
        default=None,
        help="Processa só os PDFs cujo número OU nome de arquivo contenha este texto "
             "(ex: --only 0097/2024 ou --only 00325). Útil pra validar 1 contrato.",
    )
    return p.parse_args()


def _setup_logging(debug: bool, run_id: str) -> Path:
    """Configura logging para terminal + arquivo timestamped.

    Retorna o caminho do log file pra exibir ao usuário no final.
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOGS_DIR / f"run-{run_id}.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    # Limpa handlers de execuções anteriores no mesmo processo
    root.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Terminal
    stream = logging.StreamHandler(sys.stdout)
    stream.setLevel(logging.DEBUG if debug else logging.INFO)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    # Arquivo — sempre em DEBUG (queremos tudo registrado)
    file_h = logging.FileHandler(log_file, encoding="utf-8")
    file_h.setLevel(logging.DEBUG)
    file_h.setFormatter(fmt)
    root.addHandler(file_h)

    # Silencia chatices do Playwright em modo INFO
    if not debug:
        logging.getLogger("playwright").setLevel(logging.WARNING)
        logging.getLogger("asyncio").setLevel(logging.WARNING)

    return log_file


def _resolver_pdf_dir(pdf_dir: Path, log: logging.Logger) -> Path:
    """Tolera diferença de espaços no nome (ex: 'PROCESSOS  2026' vs 'PROCESSOS 2026').

    Se o caminho exato não existir, procura na pasta pai uma entrada cujo nome,
    com espaços repetidos colapsados, bata com o alvo. Evita o erro chato de
    'um espaço a mais/menos' no PDF_DIR.
    """
    import re as _re
    if pdf_dir.exists():
        return pdf_dir
    parent = pdf_dir.parent
    alvo = _re.sub(r"\s+", " ", pdf_dir.name).strip().lower()
    if parent.exists():
        for entry in parent.iterdir():
            if entry.is_dir() and _re.sub(r"\s+", " ", entry.name).strip().lower() == alvo:
                log.warning("PDF_DIR '%s' não existe; usando '%s' (espaços diferentes)",
                            pdf_dir, entry)
                return entry
    return pdf_dir  # deixa o parser levantar FileNotFoundError com o caminho original


async def run(args: argparse.Namespace, run_id: str) -> None:
    log = logging.getLogger("main")
    log.info("=" * 70)
    log.info("RUN ID: %s  |  dry_run=%s  |  limit=%s  |  debug=%s",
             run_id, args.dry_run, args.limit, args.debug)
    log.info("=" * 70)

    # --pdf-dir agora é uma lista (nargs='+'). Se não passou, cai pro PDF_DIR
    # do .env (única pasta).
    if args.pdf_dir:
        pdf_dirs_raw = args.pdf_dir if isinstance(args.pdf_dir, list) else [args.pdf_dir]
    else:
        env_dir = os.environ.get("PDF_DIR", "")
        if not env_dir:
            raise SystemExit("Configure PDF_DIR no .env ou passe --pdf-dir")
        pdf_dirs_raw = [env_dir]

    pdf_dirs = [_resolver_pdf_dir(Path(d).expanduser(), log) for d in pdf_dirs_raw]
    log.info("lendo PDFs de %d pasta(s):", len(pdf_dirs))
    for d in pdf_dirs:
        log.info("  - %s", d)

    contratos = []
    for d in pdf_dirs:
        contratos_dir = parse_diretorio(d)
        log.info("  → %s: %d PDFs", d.name, len(contratos_dir))
        contratos.extend(contratos_dir)
    log.info("total combinado: %d PDFs", len(contratos))

    # Resumo do parsing
    extraidos = sum(1 for c in contratos if c.is_valid())
    log.info("PDFs lidos: %d  |  com dados mínimos: %d", len(contratos), extraidos)

    # Log dos PDFs que não conseguimos extrair direito
    for c in contratos:
        if not c.is_valid():
            falta = []
            if not c.numero: falta.append("número")
            if not c.data_fim: falta.append("data_fim")
            log.warning("PDF incompleto: %s — faltando: %s", c.arquivo.name, ", ".join(falta))

    if args.only:
        alvo = args.only.strip().lower()
        contratos = [
            c for c in contratos
            if alvo in (c.numero_normalizado or "").lower()
            or alvo in c.arquivo.name.lower()
        ]
        log.info("filtrando por --only %r → %d PDF(s): %s",
                 args.only, len(contratos), [c.arquivo.name for c in contratos])
        if not contratos:
            raise SystemExit(f"nenhum PDF casou com --only {args.only!r}")

    if args.limit:
        contratos = contratos[: args.limit]
        log.info("limitando a %d PDFs (--limit)", len(contratos))

    headless = not (args.debug or args.headed)
    log.info("iniciando Playwright (headless=%s)", headless)

    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context()
        try:
            resultados = await processar_pdfs(
                context,
                contratos,
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

    # Relatório do run atual
    write_csv(resultados, OUT_DIR / "report.csv")
    write_markdown(resultados, OUT_DIR / "report.md", dry_run=args.dry_run)

    # Histórico cumulativo (append-only) — não é sobrescrito entre runs
    history_csv = OUT_DIR / "history.csv"
    append_history(resultados, history_csv, run_id=run_id)
    write_history_md(history_csv, OUT_DIR / "history.md")

    log.info("relatório do run: %s", OUT_DIR / "report.md")
    log.info("histórico cumulativo: %s", OUT_DIR / "history.md")

    # Stats finais
    by_status: dict[str, int] = {}
    for r in resultados:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    log.info("=== resumo do run %s ===", run_id)
    for status, count in sorted(by_status.items()):
        log.info("  %-12s %d", status, count)


def main() -> None:
    args = parse_args()

    # run_id curto e legível: YYYYMMDD-HHMMSS-shortuuid
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
