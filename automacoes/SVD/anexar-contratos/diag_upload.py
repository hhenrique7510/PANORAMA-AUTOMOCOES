"""Diagnóstico do UPLOAD: o que acontece quando seleciono o PDF no #fileinput.

Uso:
    ../../../.venv/bin/python diag_upload.py [id] [/caminho/pdf]

Loga, abre /alterar/{id}, seleciona o PDF (file chooser no 'Adicionar Contrato'),
captura todas as requisições de rede e observa a barra de progresso por ~25s.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")

from worker import ensure_logged_in, ALTERAR_URL_TMPL, CONTRATOS_URL, _safe_goto  # noqa: E402

CID = sys.argv[1] if len(sys.argv) > 1 else "262"
PDF = sys.argv[2] if len(sys.argv) > 2 else \
    "/Users/henriqueroma/Downloads/CONTRATOS ANTIGOS VIGENTES/CONTRATO 00097-2024.pdf"


async def main() -> None:
    out = HERE / "out"; out.mkdir(parents=True, exist_ok=True)
    reqs: list[str] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()

        def on_request(req):
            if req.resource_type in ("xhr", "fetch", "document") or req.method == "POST":
                reqs.append(f"    [{req.resource_type:8}] {req.method} {req.url}")

        def on_response(resp):
            if resp.request.method == "POST" or resp.request.resource_type in ("xhr", "fetch"):
                reqs.append(f"      -> {resp.status} {resp.url}")

        page.on("request", on_request)
        page.on("response", on_response)

        await ensure_logged_in(page)
        await _safe_goto(page, ALTERAR_URL_TMPL.format(id=CID), referer=CONTRATOS_URL)
        await page.wait_for_load_state("networkidle")
        print(f"\n[1] aberto /alterar/{CID}")

        # console do navegador (erros JS no upload?)
        page.on("console", lambda m: reqs.append(f"    [console:{m.type}] {m.text[:120]}"))

        reqs.clear()
        print(f"[2] selecionando PDF: {PDF}")
        try:
            async with page.expect_file_chooser(timeout=8000) as fc:
                await page.get_by_text("Adicionar Contrato").first.click()
            chooser = await fc.value
            await chooser.set_files(PDF)
            print("    arquivo entregue ao chooser")
        except Exception as exc:
            print(f"    chooser falhou ({exc}); tentando set_input_files direto")
            await page.locator("#fileinput").set_input_files(PDF)

        # observa a barra de progresso e a tela por ~25s
        print("\n[3] observando barra de progresso + tela (25s):")
        for i in range(13):
            await asyncio.sleep(2)
            try:
                vis = await page.locator("#progress-contrato").evaluate(
                    "el => el ? getComputedStyle(el).display : 'no-el'")
            except Exception:
                vis = "?"
            try:
                bar = await page.locator("#bar-contrato").evaluate(
                    "el => el ? (el.style.width + ' / ' + (el.textContent||'')) : 'no-el'")
            except Exception:
                bar = "?"
            baixar = await page.locator(
                'button:has-text("Baixar Contrato"), a:has-text("Baixar Contrato")'
            ).count()
            print(f"    t={i*2:>2}s  progress.display={vis!r}  bar={bar!r}  baixarContrato={baixar}")
            if baixar:
                print("    >>> 'Baixar Contrato' apareceu!")
                break

        print("\n[4] requisições/console capturados:")
        for r in reqs:
            print(r)
        if not reqs:
            print("    (nenhuma requisição/console — o change não disparou nada)")

        shot = out / "diag-upload.png"
        await page.screenshot(path=str(shot), full_page=True)
        print(f"\n[5] screenshot: {shot}")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
