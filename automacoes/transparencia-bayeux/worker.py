"""Worker: conecta no Chrome via CDP, lê listagem do portal e baixa os PDFs.

NÃO inicia um Chrome novo — se conecta no que VOCÊ já abriu com:

    /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome \\
      --remote-debugging-port=9222 \\
      --user-data-dir=/tmp/chrome-bayeux-debug

Por que isso? Cloudflare detecta Chromium do Playwright e fica num loop de
"verifique se é humano". Usando o Chrome real (onde você resolveu o captcha
uma vez), todas as requisições passam sem problema.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

from playwright.async_api import (
    async_playwright, BrowserContext, Page, TimeoutError as PWTimeout,
)

from parser import (
    ContratoTransparencia,
    parse_listagem_html,
    extrair_link_pdf,
    slug_filename,
)

log = logging.getLogger(__name__)

CDP_URL = os.environ.get("CDP_URL", "http://localhost:9222")
URL_BASE = "https://transparencia.bayeux.pb.gov.br/app/pb/bayeux/1/contratos"
DOWNLOADS_DIR = Path(
    os.environ.get("DOWNLOADS_DIR", str(Path.home() / "Downloads" / "CONTRATOS TRANSPARENCIA"))
).expanduser()


# ---------------------------------------------------------------------------
# Conexão
# ---------------------------------------------------------------------------

async def conectar_chrome(playwright) -> tuple[BrowserContext, Page]:
    """Conecta no Chrome via CDP. Retorna (context, page_inicial)."""
    log.info("conectando ao Chrome em %s ...", CDP_URL)
    try:
        browser = await playwright.chromium.connect_over_cdp(CDP_URL)
    except Exception as e:
        raise RuntimeError(
            f"não consegui conectar ao Chrome em {CDP_URL}. "
            f"Abra o Chrome com:\n\n"
            f"    /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome \\\n"
            f"      --remote-debugging-port=9222 \\\n"
            f"      --user-data-dir=/tmp/chrome-bayeux-debug\n\n"
            f"Erro: {e}"
        )

    ctx = browser.contexts[0]
    # Procura aba do Bayeux; se não tiver, cria uma
    page = None
    for p in ctx.pages:
        if "transparencia.bayeux" in p.url:
            page = p
            break
    if page is None:
        log.info("nenhuma aba do Bayeux aberta — criando uma nova")
        page = await ctx.new_page()
        await page.goto(URL_BASE, wait_until="domcontentloaded", timeout=60000)
        try:
            await page.wait_for_selector("select#txtAno", state="visible", timeout=30000)
        except PWTimeout:
            raise RuntimeError(
                "abri a página mas não vi o select de ano — provavelmente "
                "tem captcha. Abre essa URL no Chrome e resolve manualmente:\n"
                f"  {URL_BASE}\n"
                "Depois roda o script de novo."
            )
    else:
        log.info("✓ aba do Bayeux encontrada: %s", page.url)

    return ctx, page


# ---------------------------------------------------------------------------
# Listagem por ano (com paginação)
# ---------------------------------------------------------------------------

async def _esperar_tabela(page: Page) -> None:
    try:
        await page.wait_for_selector("table tbody tr", state="visible", timeout=20000)
    except PWTimeout:
        log.warning("tabela não apareceu em 20s")


async def _set_length_max(page: Page) -> None:
    """Tenta selecionar o maior valor no select de length (DataTables)."""
    try:
        sel = page.locator('select[name$="_length"]').first
        if not await sel.is_visible(timeout=2000):
            return
        opcoes = await sel.locator("option").all_text_contents()
        log.debug("opções de length: %s", opcoes)
        # Prefere 'All'/'-1', senão maior número
        valor = None
        for o in opcoes:
            if o.strip().lower() in ("all", "todos", "tudo"):
                valor = "-1"
                break
        if not valor:
            nums = []
            for o in opcoes:
                try:
                    nums.append(int(o.strip()))
                except ValueError:
                    continue
            if nums:
                valor = str(max(nums))
        if valor:
            await sel.select_option(value=valor)
            await asyncio.sleep(1.5)
            log.info("length da tabela ajustado pra %s", valor)
    except Exception as e:
        log.debug("não consegui ajustar length: %s", e)


async def listar_contratos_do_ano(page: Page, ano: str) -> list[ContratoTransparencia]:
    """Lista TODOS os contratos do ano (varrendo todas as páginas).

    O filtro NÃO funciona com query string (?txtAno=) — o servidor ignora.
    A forma correta é:
      1. selecionar o ano no <select#txtAno>
      2. clicar em <a id="btnPesquisarDados"> (NÃO é um submit do form,
         é um link que dispara busca via JavaScript/AJAX)
      3. esperar a tabela atualizar
    """
    log.info("─" * 60)
    log.info("LISTANDO ano %s", ano)

    # Volta pra listagem zerada (sem query string)
    if URL_BASE.rstrip("/") not in page.url.split("?")[0]:
        log.info("navegando para %s", URL_BASE)
        await page.goto(URL_BASE, wait_until="domcontentloaded", timeout=60000)

    # Espera o select aparecer
    await page.wait_for_selector("select#txtAno", state="visible", timeout=15000)

    # Captura o "info" atual da DataTable (pra detectar mudança depois)
    info_antes = ""
    try:
        info_antes = (await page.locator(".dataTables_info").first.text_content()) or ""
        info_antes = info_antes.strip()
    except Exception:
        pass

    # Seleciona o ano
    log.info("selecionando ano %s no select#txtAno", ano)
    await page.select_option("select#txtAno", value=str(ano))
    await asyncio.sleep(0.3)

    # Clica em CONSULTAR (a#btnPesquisarDados — link com JS, não submit)
    btn = page.locator("a#btnPesquisarDados").first
    try:
        await btn.wait_for(state="visible", timeout=5000)
    except PWTimeout:
        # fallback: tenta por texto
        btn = page.locator(
            'a:has-text("Consultar"), a:has-text("Pesquisar")'
        ).first
    log.info("clicando em #btnPesquisarDados")
    await btn.click()

    # O site mostra um modal "Carregando..." enquanto faz AJAX.
    # Esperamos ele APARECER (curto timeout) e depois SUMIR (longo timeout).
    modal_sel = (
        'div.modal:has-text("Carregando"), '
        'div:has-text("Aguarde alguns instantes")'
    )
    try:
        await page.wait_for_selector(modal_sel, state="visible", timeout=2500)
        log.debug("modal 'Carregando' apareceu — esperando sumir")
    except PWTimeout:
        log.debug("modal 'Carregando' não apareceu (talvez já tenha sumido)")

    # Espera o modal sumir — ou pelo menos não estar mais bloqueando interação.
    modal_sumiu = False
    try:
        await page.wait_for_selector(modal_sel, state="hidden", timeout=60000)
        modal_sumiu = True
        log.debug("✓ modal 'Carregando' sumiu")
    except PWTimeout:
        log.warning("⚠ modal 'Carregando' não sumiu em 60s — fechando manualmente")
        # Força fechar o modal via JS (bug do site: às vezes esquece de remover)
        try:
            await page.evaluate("""
                () => {
                    const modals = document.querySelectorAll('.modal.show, .modal-backdrop.show, .modal');
                    modals.forEach(m => {
                        if (m.textContent.includes('Carregando') ||
                            m.classList.contains('modal-backdrop')) {
                            m.style.display = 'none';
                            m.classList.remove('show', 'in');
                        }
                    });
                    document.body.classList.remove('modal-open');
                    document.body.style.overflow = 'auto';
                }
            """)
        except Exception:
            pass

    # Confirma que a DataTable atualizou (info mudou OU acabou de carregar)
    if modal_sumiu:
        try:
            await page.wait_for_function(
                "(antes) => {"
                "  const el = document.querySelector('.dataTables_info');"
                "  return el && el.textContent.trim() !== antes;"
                "}",
                arg=info_antes, timeout=10000,
            )
            log.debug("✓ dataTables_info mudou")
        except PWTimeout:
            log.warning("⚠ dataTables_info NÃO mudou após o filtro — pode ter 0 contratos no ano")

    await page.wait_for_load_state("networkidle", timeout=10000)
    await _esperar_tabela(page)
    await _set_length_max(page)
    await _esperar_tabela(page)

    # Sanity check
    try:
        ano_atual = await page.eval_on_selector("select#txtAno", "el => el.value")
        if str(ano_atual) != str(ano):
            log.warning("⚠ select#txtAno está em %r, esperado %r", ano_atual, ano)
        else:
            log.debug("✓ select#txtAno = %s", ano_atual)
    except Exception:
        pass

    # extrai página atual + percorre paginação
    contratos: list[ContratoTransparencia] = []
    pagina_n = 1
    while True:
        html = await page.content()
        lote = parse_listagem_html(html)
        log.info("  página %d: %d contratos extraídos", pagina_n, len(lote))
        contratos.extend(lote)

        # próxima página?
        next_btn = page.locator(
            'a.paginate_button.next:not(.disabled), '
            'li.paginate_button.next:not(.disabled) a'
        ).first
        try:
            if not await next_btn.is_visible(timeout=600):
                break
            cls = await next_btn.get_attribute("class") or ""
            if "disabled" in cls:
                break
            await next_btn.click()
            await asyncio.sleep(1.0)
            await _esperar_tabela(page)
            pagina_n += 1
        except PWTimeout:
            break

    # Dedup por contrato_id
    seen = set()
    unicos = []
    for c in contratos:
        if c.contrato_id and c.contrato_id not in seen:
            seen.add(c.contrato_id)
            unicos.append(c)

    log.info("TOTAL %s: %d contratos únicos", ano, len(unicos))
    return unicos


# ---------------------------------------------------------------------------
# Detalhamento + download do PDF
# ---------------------------------------------------------------------------

async def obter_link_pdf(ctx: BrowserContext, contrato: ContratoTransparencia) -> Optional[str]:
    """Abre o detalhamento em nova aba (descarta depois) e devolve URL do PDF."""
    if not contrato.detalhe_url:
        return None
    p2 = await ctx.new_page()
    try:
        await p2.goto(contrato.detalhe_url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(1.0)
        html = await p2.content()
        link = extrair_link_pdf(html)
        if link and link.startswith("/"):
            link = urljoin(URL_BASE, link)
        return link
    finally:
        await p2.close()


async def baixar_pdf(ctx: BrowserContext, contrato: ContratoTransparencia,
                     pasta_destino: Path) -> Optional[Path]:
    """Baixa o PDF e salva em `pasta_destino` com nome legível.

    Usa o request context da própria página (inclui cookies, user-agent
    igual ao navegador — passa por Cloudflare).
    """
    if not contrato.pdf_url:
        log.warning("[%s] sem pdf_url — não baixo", contrato.numero)
        return None

    pasta_destino.mkdir(parents=True, exist_ok=True)
    filename = slug_filename(contrato.numero, contrato.fornecedor_nome, contrato.ano)
    destino = pasta_destino / filename

    if destino.exists() and destino.stat().st_size > 1024:
        log.info("[%s] PDF já existe (%d KB) — pulando download",
                 contrato.numero, destino.stat().st_size // 1024)
        return destino

    log.info("[%s] baixando PDF: %s", contrato.numero, contrato.pdf_url[:80])
    try:
        # Usa o request do contexto (herda cookies/UA)
        resp = await ctx.request.get(contrato.pdf_url, timeout=60000)
        if resp.status != 200:
            log.error("[%s] HTTP %d ao baixar PDF", contrato.numero, resp.status)
            return None
        body = await resp.body()
        destino.write_bytes(body)
        log.info("[%s] ✓ salvo: %s (%d KB)",
                 contrato.numero, destino.name, len(body) // 1024)
        return destino
    except Exception as e:
        log.error("[%s] erro no download: %s", contrato.numero, e)
        return None


# ---------------------------------------------------------------------------
# Orquestração de um ano
# ---------------------------------------------------------------------------

async def _processar_um(
    ctx: BrowserContext,
    c: ContratoTransparencia,
    pasta_ano: Path,
    idx: int,
    total: int,
    sem: asyncio.Semaphore,
) -> dict:
    """Processa 1 contrato sob semáforo (concorrência limitada)."""
    rec = {
        "ano": c.ano, "numero": c.numero, "contrato_id": c.contrato_id,
        "fornecedor": c.fornecedor_nome, "cnpj": c.cnpj or "", "cpf": c.cpf or "",
        "fiscal": c.fiscal, "licitacao": c.licitacao,
        "data_inicio": c.data_inicio.strftime("%d/%m/%Y") if c.data_inicio else "",
        "data_fim": c.data_fim.strftime("%d/%m/%Y") if c.data_fim else "",
        "valor": f"{c.valor:.2f}".replace(".", ",") if c.valor else "",
        "objeto": c.objeto,
        "detalhe_url": c.detalhe_url,
        "pdf_url": "", "pdf_path": "", "status": "", "motivo": "",
    }
    async with sem:
        log.info("[%d/%d] ▶ %s — %s", idx, total, c.numero, c.fornecedor_nome[:50])
        try:
            link = await obter_link_pdf(ctx, c)
            if not link:
                rec["status"] = "sem_pdf"
                rec["motivo"] = "não encontrei link de PDF no detalhamento"
                log.warning("[%d/%d] ⚠ %s sem PDF", idx, total, c.numero)
                return rec
            c.pdf_url = link
            rec["pdf_url"] = link

            destino = await baixar_pdf(ctx, c, pasta_ano)
            if destino:
                c.pdf_local_path = str(destino)
                rec["pdf_path"] = str(destino)
                rec["status"] = "baixado"
                log.info("[%d/%d] ✓ %s", idx, total, c.numero)
            else:
                rec["status"] = "erro_download"
                rec["motivo"] = "falha no download"
        except Exception as e:
            log.error("[%d/%d] ✗ erro %s: %s", idx, total, c.numero, e)
            log.debug("stack:\n%s", traceback.format_exc())
            rec["status"] = "erro"
            rec["motivo"] = f"{type(e).__name__}: {str(e)[:200]}"
        return rec


async def processar_ano(
    ctx: BrowserContext,
    page: Page,
    ano: str,
    *,
    limit: Optional[int] = None,
    only: Optional[str] = None,
    parallel: int = 5,
) -> list[dict]:
    """Lista, extrai link do PDF e baixa.

    `parallel`: quantos contratos processar em paralelo. Cada um abre sua
    própria aba no detalhamento e baixa o PDF concorrentemente. Default 5 —
    funciona bem sem estressar o servidor de transparência.
    """
    contratos = await listar_contratos_do_ano(page, ano)

    if only:
        alvo = only.strip().lower()
        contratos = [c for c in contratos
                     if alvo in c.numero.lower() or alvo in c.fornecedor_nome.lower()]
        log.info("filtrando por --only %r → %d contratos", only, len(contratos))

    if limit:
        contratos = contratos[:limit]
        log.info("limitando a %d (--limit)", len(contratos))

    pasta_ano = DOWNLOADS_DIR / ano
    total = len(contratos)
    log.info("processando %d contratos em paralelo (max %d concorrentes)",
             total, parallel)

    sem = asyncio.Semaphore(parallel)
    tarefas = [
        _processar_um(ctx, c, pasta_ano, idx, total, sem)
        for idx, c in enumerate(contratos, start=1)
    ]
    resultados = await asyncio.gather(*tarefas, return_exceptions=False)
    return list(resultados)
