"""Conecta no SEU Chrome via Remote Debugging — Cloudflare não detecta porque
é literalmente o seu navegador.

PRÉ-REQUISITO (faça antes de rodar):
─────────────────────────────────────────────────────────────────────
1) FECHE todo o Chrome que estiver aberto (importante!)
2) Abra o Terminal e cole esse comando pra abrir o Chrome com debug port:

   /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome \\
     --remote-debugging-port=9222 \\
     --user-data-dir=/tmp/chrome-bayeux-debug

3) No Chrome que abriu, vai em:
   https://transparencia.bayeux.pb.gov.br/app/pb/bayeux/1/contratos
4) Resolve o captcha "confirme que é humano" — você é humano de verdade aqui,
   passa de primeira
5) DEIXE a aba aberta na página de contratos (com a tabela já visível)
6) AGORA roda este script noutro terminal:

   cd automacoes/transparencia-bayeux
   ../../.venv/bin/python _dev/explore_cdp.py
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

HERE = Path(__file__).parent
SNAP_DIR = HERE / "snapshots"
SNAP_DIR.mkdir(parents=True, exist_ok=True)

CDP_URL = "http://localhost:9222"
ANO_TESTE = "2026"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("explore-cdp")


async def main() -> None:
    async with async_playwright() as p:
        log.info("conectando ao Chrome em %s ...", CDP_URL)
        try:
            browser = await p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            print("\n❌ Não consegui conectar no Chrome.\n"
                  "Verifica se você abriu o Chrome com:\n\n"
                  "    /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome \\\n"
                  "      --remote-debugging-port=9222 \\\n"
                  "      --user-data-dir=/tmp/chrome-bayeux-debug\n\n"
                  f"Erro: {e}")
            sys.exit(1)

        log.info("conectado! procurando a aba do portal Bayeux...")
        ctx = browser.contexts[0]

        # Procura a aba do portal Bayeux
        page = None
        for p_existente in ctx.pages:
            url = p_existente.url
            log.info("  aba aberta: %s", url[:80])
            if "transparencia.bayeux" in url:
                page = p_existente
                break

        if page is None:
            print("\n❌ Não achei nenhuma aba com 'transparencia.bayeux'.\n"
                  "Abre essa URL no Chrome que está rodando com debug port:\n"
                  "  https://transparencia.bayeux.pb.gov.br/app/pb/bayeux/1/contratos\n"
                  "Resolve o captcha, espera carregar a listagem, e rode este script de novo.")
            sys.exit(1)

        log.info("✓ aba do Bayeux encontrada: %s", page.url)

        # Espera o select de ano aparecer (já deveria estar visível)
        try:
            await page.wait_for_selector("select#txtAno", state="visible", timeout=10000)
            log.info("✓ select#txtAno visível")
        except PWTimeout:
            log.warning("select#txtAno NÃO encontrado — você está na página certa?")
            print("\nVerifica que a aba está na URL: .../app/pb/bayeux/1/contratos\n"
                  "(não em outra página).\n")
            sys.exit(1)

        # Seleciona ano
        try:
            log.info("selecionando ano %s", ANO_TESTE)
            await page.select_option("select#txtAno", value=ANO_TESTE)
            await asyncio.sleep(2)
        except Exception as e:
            log.warning("falha ao selecionar ano (talvez já estava): %s", e)

        # Clica em pesquisar se tiver
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

        # Espera a tabela aparecer
        try:
            await page.wait_for_selector("table tbody tr", state="visible", timeout=20000)
        except PWTimeout:
            log.warning("tabela não apareceu — salvando HTML mesmo assim")

        # --- snapshot 1: listagem ---
        html_list = await page.content()
        (SNAP_DIR / "01-listagem.html").write_text(html_list, encoding="utf-8")
        log.info("✓ salvou 01-listagem.html (%d KB)", len(html_list) // 1024)

        n_rows = await page.locator("table tbody tr").count()
        log.info("linhas na tabela: %d", n_rows)

        # --- snapshot 2: detalhamento ---
        detalhe_link = page.locator('a[href*="/detalhamento-de-contrato/"]').first
        href = None
        try:
            href = await detalhe_link.get_attribute("href")
        except Exception:
            pass
        log.info("primeiro link de detalhamento: %s", href)

        link_pdf = None
        if href:
            log.info("abrindo detalhamento em NOVA ABA do seu Chrome...")
            page2 = await ctx.new_page()
            try:
                await page2.goto(href, wait_until="domcontentloaded", timeout=60000)
            except PWTimeout:
                log.warning("timeout no goto do detalhamento — seguindo")
            await asyncio.sleep(3)

            # Se aparecer captcha NESSA aba também, pausa
            challenge = await page2.locator(
                ':has-text("Verifying"), :has-text("verifique que"), '
                ':has-text("Confirme")'
            ).count()
            if challenge > 0:
                print("\n⚠️  Captcha no detalhamento. Resolva no browser e aperte Enter aqui... ")
                try:
                    input()
                except EOFError:
                    pass

            html_det = await page2.content()
            (SNAP_DIR / "02-detalhamento.html").write_text(html_det, encoding="utf-8")
            log.info("✓ salvou 02-detalhamento.html (%d KB)", len(html_det) // 1024)

            try:
                pdf_loc = page2.locator(
                    'a:has-text("Download do contrato"), '
                    'a[href$=".pdf"], '
                    'a.btn-danger[href*=".pdf"]'
                ).first
                link_pdf = await pdf_loc.get_attribute("href")
                log.info("✓ link do PDF: %s", link_pdf)
            except Exception as e:
                log.warning("não achei link do PDF: %s", e)

        resumo = f"""Resumo da exploração (via CDP)
=====================
Conexão: {CDP_URL}
Ano testado: {ANO_TESTE}

Linhas na tabela: {n_rows}

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
        print("\n✓ Snapshots salvos. NÃO precisa fechar o Chrome — ele é seu mesmo.")
        # Não fecha o browser — é do usuário


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
