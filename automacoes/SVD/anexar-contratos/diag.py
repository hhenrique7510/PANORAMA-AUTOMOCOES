"""Diagnóstico da TELA DE EDIÇÃO (/svd/contrato/alterar/{id}): valida a detecção
de "já tem PDF anexado".

Uso:
    ../../../.venv/bin/python diag.py [id]      # id padrão: 405 (contrato 00025/2026)

Loga, abre a tela de alterar do contrato e imprime:
- todos os input[type=file] (id, name, class, visível?)
- se existe #fileinput
- elementos/textos com 'anexar', 'upload', 'contrato', 'arquivo', 'pdf'
- links que pareçam um PDF já anexado (download/visualizar/.pdf)
- abas presentes na tela
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

CID = sys.argv[1] if len(sys.argv) > 1 else "405"


async def main() -> None:
    out = HERE / "out"
    out.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        await ensure_logged_in(page)
        url = ALTERAR_URL_TMPL.format(id=CID)
        print(f"\n[0] abrindo {url}")
        await _safe_goto(page, url, referer=CONTRATOS_URL)
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(2)
        print(f"[1] URL atual: {page.url}")

        # [2] input[type=file]
        print("\n[2] input[type=file] na página:")
        files = await page.locator('input[type="file"]').evaluate_all(
            "els => els.map(e => ({id:e.id, name:e.getAttribute('name'),"
            " cls:e.getAttribute('class'), accept:e.getAttribute('accept'),"
            " visible: !!(e.offsetWidth||e.offsetHeight||e.getClientRects().length)}))"
        )
        if files:
            for f in files:
                print(f"    id={f['id']!r} name={f['name']!r} visible={f['visible']} "
                      f"accept={f['accept']!r} cls={f['cls']!r}")
        else:
            print("    (NENHUM input[type=file])")

        # [3] existe #fileinput?
        n_fi = await page.locator("#fileinput").count()
        print(f"\n[3] #fileinput existe? count={n_fi}")

        # [4] abas/seções
        print("\n[4] abas (a[role=tab], .nav-link, .nav-item a):")
        abas = await page.locator('a[role="tab"], .nav-link, .nav-tabs a').evaluate_all(
            "els => els.slice(0,15).map(e => (e.textContent||'').trim().slice(0,30))"
        )
        for a in [x for x in abas if x]:
            print(f"    - {a!r}")

        # [5] textos com palavras-chave de upload
        print("\n[5] elementos com 'anexar/upload/arquivo/pdf' no texto:")
        kws = await page.locator(
            ':text-matches("anexar|upload|arquivo|\\\\.pdf|contrato anexado", "i")'
        ).evaluate_all(
            "els => Array.from(new Set(els.slice(0,30).map(e =>"
            " (e.textContent||'').trim().slice(0,60)))).slice(0,15)"
        )
        for k in [x for x in kws if x]:
            print(f"    - {k!r}")

        # [6] links que parecem PDF já anexado
        print("\n[6] links com '.pdf'/download/visualizar:")
        links = await page.locator(
            'a[href$=".pdf"], a[href*="download"], a:has-text("Visualizar"), a:has-text("Baixar")'
        ).evaluate_all(
            "els => els.slice(0,10).map(e => ({txt:(e.textContent||'').trim().slice(0,30),"
            " href:e.getAttribute('href')}))"
        )
        if links:
            for l in links:
                print(f"    txt={l['txt']!r} href={l['href']!r}")
        else:
            print("    (nenhum)")

        # [6b] TODOS os botões/links-botão da tela (pra achar 'Adicionar Contrato')
        print("\n[6b] todos os botões / a.btn da página:")
        btns = await page.locator(
            'button, a.btn, input[type=button], input[type=submit]'
        ).evaluate_all(
            "els => els.slice(0,40).map(e => ({tag:e.tagName,"
            " txt:(e.textContent||e.value||'').trim().slice(0,35),"
            " id:e.id, cls:(e.getAttribute('class')||'').slice(0,40),"
            " onclick:(e.getAttribute('onclick')||'').slice(0,50)}))"
        )
        for b in btns:
            t = b['txt']
            if t:
                print(f"    <{b['tag']}> {t!r} id={b['id']!r} onclick={b['onclick']!r}")

        # [6c] elementos "Adicionar ..." (tag/onclick/href/for) — é o gatilho do upload
        print("\n[6c] elementos com texto 'Adicionar' (gatilho do upload?):")
        adds = await page.locator(':text("Adicionar")').evaluate_all(
            "els => els.slice(0,15).map(e => ({tag:e.tagName,"
            " txt:(e.textContent||'').trim().slice(0,30),"
            " href:e.getAttribute('href'), onclick:(e.getAttribute('onclick')||'').slice(0,60),"
            " forAttr:e.getAttribute('for'), id:e.id}))"
        )
        for a in adds:
            print(f"    <{a['tag']}> {a['txt']!r} href={a['href']!r} "
                  f"for={a['forAttr']!r} onclick={a['onclick']!r}")
        if not adds:
            print("    (nenhum)")

        # [6d] HTML do container ao redor do #fileinput (o widget de upload do contrato)
        print("\n[6d] HTML do container do #fileinput (widget de upload do contrato):")
        try:
            html = await page.locator("#fileinput").evaluate(
                "el => (el.closest('div.col-md-6, div.col-md-4, div.form-group, div')||el).outerHTML"
            )
            print(html[:1800])
        except Exception as exc:
            print(f"    erro: {exc}")

        shot = out / "diag-alterar.png"
        await page.screenshot(path=str(shot), full_page=True)
        print(f"\n[7] screenshot: {shot}")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
