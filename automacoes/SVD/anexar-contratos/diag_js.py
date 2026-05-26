"""Extrai o JS da tela de alterar que lida com #fileinput / upload do contrato."""
from __future__ import annotations
import asyncio, re, sys
from pathlib import Path
from dotenv import load_dotenv
from playwright.async_api import async_playwright

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")
from worker import ensure_logged_in, ALTERAR_URL_TMPL, CONTRATOS_URL, _safe_goto  # noqa: E402

CID = sys.argv[1] if len(sys.argv) > 1 else "262"


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await ensure_logged_in(page)
        await _safe_goto(page, ALTERAR_URL_TMPL.format(id=CID), referer=CONTRATOS_URL)
        await page.wait_for_load_state("networkidle")

        html = await page.content()
        # extrai todos os <script> inline
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL | re.IGNORECASE)
        alvo = []
        for s in scripts:
            if re.search(r"fileinput|bar-contrato|progress-contrato|contrato|upload|FormData|XMLHttpRequest|\.ajax", s, re.I):
                alvo.append(s)
        print(f"\n[scripts inline relevantes: {len(alvo)} de {len(scripts)}]")
        for s in alvo:
            # imprime só trechos perto das palavras-chave
            for m in re.finditer(r"(fileinput|bar-contrato|progress-contrato|adicionar\w*contrato|FormData|XMLHttpRequest|\$\.ajax|\.upload)", s, re.I):
                ini = max(0, m.start() - 250)
                fim = min(len(s), m.end() + 400)
                print("\n--- trecho ---")
                print(s[ini:fim].strip())

        # também lista os <script src=...>
        srcs = re.findall(r'<script[^>]*src=["\']([^"\']+)["\']', html, re.I)
        print("\n[scripts externos]:")
        for s in srcs:
            print("   ", s)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
