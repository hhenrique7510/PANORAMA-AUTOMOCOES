"""Script de exploração — abre o portal Bayeux e salva HTML pra Claude analisar.

Uso (do diretório transparencia-bayeux/):
    ../../.venv/bin/python _dev/explore.py

O portal tem captcha de "confirme se é humano" no 1º acesso. O script:
- usa contexto PERSISTENTE (salva cookies em _dev/profile/),
  então depois que você resolver o captcha uma vez, ele NÃO pede de novo
- PAUSA depois de abrir a página esperando você resolver o captcha,
  e só continua quando você apertar Enter no terminal

Salva 3 arquivos em _dev/snapshots/:
  - 01-listagem.html         → página de listagem com todos os contratos do ano
  - 02-detalhamento.html     → página de detalhamento do 1º contrato
  - 03-resumo.txt            → resumo do que foi encontrado
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

HERE = Path(__file__).parent
SNAP_DIR = HERE / "snapshots"
PROFILE_DIR = HERE / "profile"   # ← contexto persistente do browser
SNAP_DIR.mkdir(parents=True, exist_ok=True)
PROFILE_DIR.mkdir(parents=True, exist_ok=True)

URL_BASE = "https://transparencia.bayeux.pb.gov.br/app/pb/bayeux/1/contratos"
ANO_TESTE = "2026"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("explore")


def pausa(msg: str) -> None:
    """Pausa interativa — espera você apertar Enter."""
    print("\n" + "─" * 60)
    print(msg)
    print("─" * 60)
    try:
        input(">>> Aperte Enter quando estiver pronto pra continuar... ")
    except EOFError:
        pass


async def main() -> None:
    async with async_playwright() as p:
        # Usa o Chrome REAL do Mac (não o Chromium do Playwright).
        # Cloudflare bloqueia Chromium puro, mas deixa passar Chrome de boa.
        # `channel="chrome"` exige que tenha o Chrome instalado em /Applications.
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            channel="chrome",                    # ← Chrome real, não Chromium
            viewport={"width": 1280, "height": 900},
            # Flags anti-detecção do Cloudflare
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--no-sandbox",
            ],
            ignore_default_args=["--enable-automation"],
            # User-agent real (sem "HeadlessChrome")
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="pt-BR",
        )
        # Mata o flag navigator.webdriver via init script
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        log.info("abrindo: %s", URL_BASE)
        # NÃO usa 'networkidle' aqui — se tiver captcha de Cloudflare a página
        # nunca fica idle. 'domcontentloaded' é o mais leve possível.
        try:
            await page.goto(URL_BASE, wait_until="domcontentloaded", timeout=60000)
        except PWTimeout:
            log.warning("timeout no goto inicial — seguindo (pode ser captcha)")

        pausa(
            "✋ ABRIU A PÁGINA. SE TIVER CAPTCHA DE 'CONFIRME QUE É HUMANO',\n"
            "    RESOLVA AGORA NO BROWSER, espere a página da listagem aparecer\n"
            "    com o select de ano e a tabela visível, AÍ aperte Enter aqui."
        )

        # A partir daqui, supomos que a listagem renderizou
        log.info("aguardando o select#txtAno aparecer (até 30s)...")
        try:
            await page.wait_for_selector("select#txtAno", state="visible", timeout=30000)
            log.info("select#txtAno OK")
        except PWTimeout:
            log.warning("select#txtAno NÃO apareceu — você ainda está na página certa?")

        # Seleciona ano
        try:
            log.info("selecionando ano %s", ANO_TESTE)
            await page.select_option("select#txtAno", value=ANO_TESTE)
            await asyncio.sleep(2)
        except Exception as e:
            log.warning("falha ao selecionar ano: %s", e)

        # Procura botão pesquisar/filtrar
        for sel in ('button:has-text("Pesquisar")', 'button:has-text("Filtrar")',
                    'button:has-text("Consultar")', 'button[type="submit"]',
                    'input[type="submit"]'):
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=800):
                    log.info("clicando em '%s'", sel)
                    await btn.click()
                    await asyncio.sleep(2)
                    break
            except Exception:
                continue

        # Espera a tabela carregar
        try:
            await page.wait_for_selector("table tbody tr", state="visible", timeout=20000)
        except PWTimeout:
            log.warning("tabela não apareceu — tentando salvar HTML mesmo assim")

        # --- snapshot 1: listagem ---
        html_list = await page.content()
        (SNAP_DIR / "01-listagem.html").write_text(html_list, encoding="utf-8")
        log.info("salvou: 01-listagem.html (%d KB)", len(html_list) // 1024)

        n_rows = await page.locator("table tbody tr").count()
        log.info("linhas na tabela: %d", n_rows)

        # --- snapshot 2: 1º detalhamento ---
        detalhe_link = page.locator('a[href*="/detalhamento-de-contrato/"]').first
        href = None
        try:
            href = await detalhe_link.get_attribute("href")
        except Exception:
            pass
        log.info("primeiro link de detalhamento: %s", href)

        link_pdf = None
        if href:
            log.info("navegando para detalhamento (em nova aba pra preservar a listagem)")
            page2 = await ctx.new_page()
            try:
                await page2.goto(href, wait_until="domcontentloaded", timeout=60000)
            except PWTimeout:
                log.warning("timeout no goto do detalhamento — seguindo")
            # Espera renderizar
            await asyncio.sleep(3)

            html_det = await page2.content()
            (SNAP_DIR / "02-detalhamento.html").write_text(html_det, encoding="utf-8")
            log.info("salvou: 02-detalhamento.html (%d KB)", len(html_det) // 1024)

            try:
                pdf_loc = page2.locator(
                    'a:has-text("Download do contrato"), '
                    'a[href$=".pdf"], '
                    'a.btn-danger[href*=".pdf"]'
                ).first
                link_pdf = await pdf_loc.get_attribute("href")
                log.info("link do PDF: %s", link_pdf)
            except Exception as e:
                log.warning("não achei link do PDF: %s", e)

        # --- snapshot 3: resumo ---
        resumo = f"""Resumo da exploração
=====================
URL base: {URL_BASE}
Ano testado: {ANO_TESTE}

Linhas na tabela (1ª página): {n_rows}

Primeiro detalhamento:
  URL: {href}

Link do PDF (1º contrato):
  {link_pdf}

Arquivos salvos em _dev/snapshots/:
  - 01-listagem.html
  - 02-detalhamento.html
  - 03-resumo.txt
"""
        (SNAP_DIR / "03-resumo.txt").write_text(resumo, encoding="utf-8")
        print("\n" + "=" * 60)
        print(resumo)
        print("=" * 60)

        pausa("Tudo salvo. Aperte Enter pra fechar o browser.")
        await ctx.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
