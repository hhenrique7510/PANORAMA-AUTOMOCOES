"""Lista empresas cadastradas no Panorama Fiscal que NÃO têm nenhum contrato.

Loga, baixa /svd/empresas e /svd/contratos, cruza por documento (CNPJ/CPF) e
imprime/salva as empresas sem contrato algum.

Uso:
    cd automacoes/SVD/empresas-sem-contratos
    ../../../.venv/bin/python main.py --debug
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from playwright.async_api import Page, TimeoutError as PWTimeout, async_playwright

log = logging.getLogger("main")

# Reaproveita o .env do cadastro-empresas-excel (mesmas credenciais)
ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
load_dotenv(ROOT.parent / "cadastro-empresas-excel" / ".env")

URL = os.environ.get("PANORAMA_URL", "https://panoramafiscal.com.br/svd")
USER = os.environ.get("PANORAMA_USER", "")
PASS = os.environ.get("PANORAMA_PASS", "")

_parts = urlsplit(URL)
ORIGIN = f"{_parts.scheme}://{_parts.netloc}"
LOGIN_URL = URL
EMPRESAS_URL = f"{ORIGIN}/svd/empresas"
CONTRATOS_URL = f"{ORIGIN}/svd/contratos"

OUT = ROOT / "out"
OUT.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Login (copiado do worker de empresas)
# ---------------------------------------------------------------------------

async def ensure_logged_in(page: Page) -> None:
    await page.goto(LOGIN_URL, wait_until="load", timeout=30000)
    pwd = page.locator('input[type="password"]').first
    try:
        await pwd.wait_for(state="visible", timeout=4000)
    except PWTimeout:
        log.info("já logado")
        return

    if not USER or not PASS:
        raise RuntimeError("PANORAMA_USER/PANORAMA_PASS não configurados")

    user_field = page.locator(
        'input[name="username"], input[name="login"], input[name="usuario"], '
        'input[name="user"], input[name="email"], input[type="text"]'
    ).first
    user_digitos = re.sub(r"\D", "", USER)
    await user_field.click()
    await user_field.fill("")
    await user_field.type(user_digitos, delay=40)
    await pwd.fill(PASS)
    submit = page.locator('button[type="submit"], input[type="submit"]').first
    try:
        await submit.click()
    except Exception:
        await pwd.press("Enter")
    await page.wait_for_load_state("networkidle")
    log.info("login ok")


# ---------------------------------------------------------------------------
# DataTables: muda page-length pra "Todos" / maior
# ---------------------------------------------------------------------------

async def _expandir_page_length(page: Page) -> None:
    try:
        select = page.locator('select[name$="_length"]').first
        await select.wait_for(state="visible", timeout=10000)
        opcoes = await select.locator("option").all_text_contents()
        alvo = None
        for o in opcoes:
            if o.strip().lower() in ("all", "todos", "tudo"):
                alvo = "-1"
                break
        if not alvo:
            nums = []
            for o in opcoes:
                try:
                    nums.append(int(o.strip()))
                except ValueError:
                    pass
            if nums:
                alvo = str(max(nums))
        if alvo:
            await select.select_option(value=alvo)
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(0.5)
    except PWTimeout:
        log.warning("não achei o select de length")


# ---------------------------------------------------------------------------
# Scrapers
# ---------------------------------------------------------------------------

async def _scrape_paginas(page: Page) -> list[list[str]]:
    """Itera todas as páginas (se houver) e devolve todas as linhas (cells)."""
    all_rows: list[list[str]] = []
    while True:
        rows = await page.evaluate(r"""
            () => {
                const tables = document.querySelectorAll('table');
                let trs = [];
                for (const t of tables) {
                    const cur = t.querySelectorAll('tbody tr');
                    if (cur.length > trs.length) trs = [...cur];
                }
                return trs.map(tr =>
                    [...tr.querySelectorAll('td')].map(td => (td.innerText || '').trim())
                );
            }
        """)
        all_rows.extend(rows)
        nxt = page.locator(
            'a.paginate_button.next:not(.disabled), '
            'li.paginate_button.next:not(.disabled) a, '
            '.dataTables_paginate a.next:not(.disabled)'
        ).first
        try:
            if not await nxt.is_visible(timeout=500):
                break
            cls = await nxt.get_attribute("class") or ""
            if "disabled" in cls:
                break
            await nxt.click()
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(0.4)
        except PWTimeout:
            break
    return all_rows


async def listar_empresas(page: Page) -> dict[str, dict]:
    """Retorna {doc_digits: {nome, doc_fmt, doc_digits}}."""
    log.info("abrindo %s", EMPRESAS_URL)
    await page.goto(EMPRESAS_URL, wait_until="networkidle", timeout=30000)
    await _expandir_page_length(page)
    rows = await _scrape_paginas(page)
    log.info("empresas: %d linhas", len(rows))

    empresas: dict[str, dict] = {}
    for cells in rows:
        doc_fmt = ""
        doc_digits = ""
        nome = ""
        for c in cells:
            d = re.sub(r"\D", "", c)
            if len(d) in (11, 14) and not doc_digits:
                doc_digits = d
                doc_fmt = c.strip()
            elif not nome and c and len(d) not in (11, 14):
                nome = c.strip()
        if doc_digits:
            empresas[doc_digits] = {
                "doc_digits": doc_digits,
                "doc_fmt": doc_fmt,
                "nome": nome,
                "linha": " | ".join(cells),
            }
    log.info("empresas únicas (por doc): %d", len(empresas))
    return empresas


async def docs_de_contratos(page: Page) -> set[str]:
    """Retorna o set de documentos (dígitos) que aparecem em qualquer contrato."""
    log.info("abrindo %s", CONTRATOS_URL)
    # /svd/contratos exige Referer; navegamos via home
    try:
        await page.goto(f"{ORIGIN}/svd", wait_until="load", timeout=30000)
        await page.goto(CONTRATOS_URL, wait_until="networkidle", timeout=30000)
    except Exception as exc:
        log.warning("goto direto falhou (%s), tentando clicar no menu", exc)
        alvo = page.locator('a[href$="/contratos"], a[href$="/contratos/"]').first
        await alvo.click()
        await page.wait_for_load_state("networkidle")

    await _expandir_page_length(page)
    rows = await _scrape_paginas(page)
    log.info("contratos: %d linhas", len(rows))

    docs: set[str] = set()
    for cells in rows:
        for c in cells:
            for m in re.finditer(r"\d[\d./-]{10,17}\d", c):
                d = re.sub(r"\D", "", m.group(0))
                if len(d) in (11, 14):
                    docs.add(d)
    log.info("documentos únicos vistos em contratos: %d", len(docs))
    return docs


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def amain(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not args.headed)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await ensure_logged_in(page)

        empresas = await listar_empresas(page)
        docs_contratos = await docs_de_contratos(page)

        await browser.close()

    sem_contrato = [
        e for doc, e in empresas.items() if doc not in docs_contratos
    ]
    sem_contrato.sort(key=lambda e: e["nome"].lower())

    print()
    print(f"=== EMPRESAS SEM CONTRATO: {len(sem_contrato)} de {len(empresas)} ===")
    for e in sem_contrato:
        print(f"  - {e['doc_fmt']:<22} {e['nome']}")

    csv_path = OUT / "empresas-sem-contratos.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["doc", "nome"])
        for e in sem_contrato:
            w.writerow([e["doc_fmt"], e["nome"]])
    print(f"\n→ salvo em {csv_path}")
    return 0


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--debug", action="store_true")
    p.add_argument("--headed", action="store_true", help="navegador visível")
    args = p.parse_args()
    sys.exit(asyncio.run(amain(args)))


if __name__ == "__main__":
    main()
