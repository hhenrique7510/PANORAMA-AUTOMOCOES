"""Playwright worker: login + cadastrar empresa nova no Panorama Fiscal (SVD).

Reaproveita os padrões já provados no worker de contratos (login digitando o CPF
por causa da jQuery Mask, handler de diálogos, detecção de sucesso/falha). O form
de empresa (`/svd/empresa/salvar`) tem suas próprias peculiaridades — ver comentários.
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
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, Page, TimeoutError as PWTimeout

from parser import Empresa

log = logging.getLogger(__name__)

URL = os.environ.get("PANORAMA_URL", "https://panoramafiscal.com.br/svd")
USER = os.environ.get("PANORAMA_USER") or os.environ.get("USER_LOGIN", "")
PASS = os.environ.get("PANORAMA_PASS") or os.environ.get("USER_PASS", "")

_parts = urlsplit(URL)
_ORIGIN = f"{_parts.scheme}://{_parts.netloc}"
LOGIN_URL = URL
EMPRESAS_URL = os.environ.get("PANORAMA_EMPRESAS_URL", f"{_ORIGIN}/svd/empresas")
NOVA_EMPRESA_URL = os.environ.get("PANORAMA_NOVA_EMPRESA_URL", f"{_ORIGIN}/svd/empresa/salvar")


# ---------------------------------------------------------------------------
# Helpers básicos (navegação, login) — espelham o worker de contratos
# ---------------------------------------------------------------------------

async def _safe_goto(page: Page, url: str) -> None:
    for attempt in range(3):
        try:
            await page.goto(url, wait_until="load", timeout=30000)
            return
        except Exception as exc:
            log.warning("goto attempt %d failed (%s): %s", attempt + 1, url, exc)
            await asyncio.sleep(1.0)
    await page.goto(url, wait_until="commit", timeout=30000)


async def ensure_logged_in(page: Page) -> None:
    await _safe_goto(page, LOGIN_URL)
    pwd = page.locator('input[type="password"]').first
    try:
        await pwd.wait_for(state="visible", timeout=4000)
    except PWTimeout:
        log.info("já logado")
        return

    if not USER or not PASS:
        raise RuntimeError("PANORAMA_USER/PANORAMA_PASS não configurados no .env")

    user_field = page.locator(
        'input[name="username"], input[name="login"], input[name="usuario"], '
        'input[name="user"], input[name="email"], input[type="text"]'
    ).first

    # Campo de CPF usa jQuery Mask: precisa DIGITAR (type) — fill() não dispara a máscara.
    user_digitos = re.sub(r"\D", "", USER)
    await user_field.click()
    await user_field.fill("")
    await user_field.type(user_digitos, delay=40)
    await pwd.fill(PASS)

    submit = page.locator(
        'button[type="submit"], input[type="submit"], '
        'button:has-text("Entrar"), button:has-text("Acessar")'
    ).first
    try:
        await submit.click()
    except Exception:
        await pwd.press("Enter")
    await page.wait_for_load_state("networkidle")

    body = (await page.inner_text("body")).lower()
    ainda_no_login = await page.locator('input[type="password"]').first.is_visible()
    if ainda_no_login or any(k in body for k in ("inválid", "incorret", "credenciais")):
        raise RuntimeError(
            "login FALHOU — o servidor rejeitou as credenciais. Confira "
            "PANORAMA_USER (CPF) e PANORAMA_PASS no .env."
        )
    log.info("login concluído (autenticado)")


# ---------------------------------------------------------------------------
# Helpers de form
# ---------------------------------------------------------------------------

async def _fill_if_present(page: Page, selector: str, value: str) -> bool:
    if not value:
        return False
    loc = page.locator(selector).first
    try:
        await loc.wait_for(state="visible", timeout=3000)
    except PWTimeout:
        log.debug("campo %s não visível — pulando", selector)
        return False
    await loc.fill(value)
    return True


async def _select_by_label(page: Page, selector: str, label_substring: str) -> bool:
    """Seleciona a <option> cujo texto contenha label_substring."""
    if not label_substring:
        return False
    select = page.locator(selector).first
    try:
        await select.wait_for(state="visible", timeout=3000)
    except PWTimeout:
        return False
    for o in await select.locator("option").all():
        txt = (await o.text_content() or "").strip()
        if label_substring.lower() in txt.lower():
            value = await o.get_attribute("value")
            if value is not None:
                await select.select_option(value=value)
                return True
    return False


async def _set_documento(page: Page, doc_fmt: str) -> None:
    """Seta o campo de documento (#cnpj) já FORMATADO via JS.

    Digitar embaralha a máscara desse campo (testado: 50506511000158 vira
    05.065.110/0015-85). Setar o valor já formatado + disparar os eventos é
    confiável, e o buscarCnpj()/concluir() leem o valor correto.
    """
    await page.locator('#cnpj, input[name="cnpj"]').first.evaluate(
        """(el, val) => {
            el.value = val;
            for (const ev of ['input', 'keyup', 'change', 'blur']) {
                el.dispatchEvent(new Event(ev, {bubbles: true}));
            }
            if (window.jQuery) { try { jQuery(el).trigger('input').trigger('change'); } catch (e) {} }
        }""",
        doc_fmt,
    )


async def _buscar_cnpj(page: Page) -> bool:
    """Clica na lupa (buscarCnpj) e espera a Razão Social (#nome) popular.

    Retorna True se a Receita preencheu o nome; False se não achou/erro
    (os diálogos de erro são aceitos pelo handler de page.on('dialog')).
    """
    try:
        await page.locator('button[onclick^="buscarCnpj"]').first.click(timeout=3000)
    except Exception:
        log.warning("   botão da lupa (buscarCnpj) não encontrado")
        return False
    for _ in range(20):  # ~10s
        await asyncio.sleep(0.5)
        if (await page.locator('#nome').first.input_value()).strip():
            return True
    return False


# ---------------------------------------------------------------------------
# Listagem de empresas já cadastradas (pra não duplicar — idempotente)
# ---------------------------------------------------------------------------

async def listar_empresas_existentes(page: Page) -> set[str]:
    """Carrega /svd/empresas e devolve o set de documentos (só dígitos) já cadastrados."""
    await _safe_goto(page, EMPRESAS_URL)
    await page.wait_for_load_state("networkidle")
    # aumenta o page-length do DataTables (máx 100) pra paginar menos
    try:
        await page.select_option(
            'select[name$="_length"], select[name*="length" i]', value="100"
        )
        await asyncio.sleep(1.0)
    except Exception:
        pass

    docs: set[str] = set()
    for _ in range(30):  # guarda contra loop infinito
        rows = await page.evaluate(
            """() => [...document.querySelectorAll('table tbody tr')]
                       .map(r => { const c = r.querySelector('td'); return c ? c.innerText : ''; })"""
        )
        for r in rows:
            d = re.sub(r"\D", "", r or "")
            if len(d) in (11, 14):
                docs.add(d)
        nxt = page.locator('.dataTables_paginate a.next, .paginate_button.next, li.next a').first
        try:
            disabled = await nxt.evaluate(
                """el => { const li = el.closest('li');
                           return (li && li.className.includes('disabled'))
                                  || el.className.includes('disabled'); }"""
            )
        except Exception:
            break
        if disabled:
            break
        await nxt.click()
        await asyncio.sleep(0.8)

    log.info("empresas já cadastradas no Panorama: %d", len(docs))
    return docs


# ---------------------------------------------------------------------------
# Criar empresa
# ---------------------------------------------------------------------------

async def criar_empresa(page: Page, empresa: Empresa, *, dry_run: bool = False) -> None:
    """Abre /svd/empresa/salvar, preenche e salva (a menos que dry_run)."""
    if not empresa.doc and not empresa.nome:
        raise ValueError("empresa sem documento e sem nome — não dá pra cadastrar")

    log.info("→ [passo 1/5] abrindo form de empresa: %s  (%s %s)",
             empresa.nome[:40], empresa.tipo_pessoa, empresa.doc_fmt or "—")
    await _safe_goto(page, NOVA_EMPRESA_URL)
    await page.wait_for_load_state("networkidle")

    # --- tipo de documento (PF/PJ) ---
    if empresa.is_pj:
        await _select_by_label(page, '#tipoDocumento, select[name="tipoDocumento"]', "CNPJ")
    else:
        await _select_by_label(page, '#tipoDocumento, select[name="tipoDocumento"]', "CPF")
        await _select_by_label(page, '#matriz, select[name="matriz"]', "Pessoa Física")
    await asyncio.sleep(0.3)

    # --- documento (#cnpj, formatado via JS) ---
    log.info("→ [passo 2/5] documento: %s", empresa.doc_fmt or "(sem doc)")
    if empresa.doc:
        await _set_documento(page, empresa.doc_fmt)

    # --- razão social: lupa (Receita) pra CNPJ, manual pra CPF/sem-doc ---
    nome_loc = page.locator('#nome, input[name="nome"]').first
    if empresa.is_pj and empresa.doc:
        log.info("→ [passo 3/5] buscando dados na Receita (lupa)…")
        achou = await _buscar_cnpj(page)
        nome_atual = (await nome_loc.input_value()).strip()
        if achou and nome_atual:
            log.info("   ✓ Receita preencheu: %r", nome_atual[:50])
        else:
            log.warning("   lupa não preencheu — usando nome da planilha: %r", empresa.nome[:50])
            await nome_loc.fill(empresa.nome)
    else:
        log.info("→ [passo 3/5] razão social (manual): %s", empresa.nome[:50])
        await nome_loc.fill(empresa.nome)

    if not (await nome_loc.input_value()).strip():
        raise RuntimeError(
            f"razão social ficou VAZIA p/ {empresa.doc_fmt or empresa.nome[:30]} — não dá pra salvar"
        )

    # --- defaults: situação ATIVA, tributação (só PJ) ---
    log.info("→ [passo 4/5] situação=ATIVA%s", " · tributação=LUCRO PRESUMIDO" if empresa.is_pj else "")
    await _select_by_label(page, 'select[name="situacao.id"], #situacao\\.id', "ATIVA")
    if empresa.is_pj:
        await _select_by_label(page, 'select[name="tributacao.id"], #tributacao\\.id', "LUCRO PRESUMIDO")

    # Inscrição Estadual/Municipal: o sistema EXIGE "--" quando a empresa não tem.
    # Vazio → o POST é recusado silenciosamente (não salva, sem alerta). Só
    # preenchemos quando o campo está vazio (não sobrescreve o que a lupa trouxe).
    for sel in ('#inscricaoEstadual, input[name="inscricaoEstadual"]',
                '#inscricaoMunicipal, input[name="inscricaoMunicipal"]'):
        loc = page.locator(sel).first
        try:
            if not (await loc.input_value()).strip():
                await loc.evaluate(
                    """el => { el.value = '--';
                              el.dispatchEvent(new Event('input',  {bubbles:true}));
                              el.dispatchEvent(new Event('change', {bubbles:true})); }"""
                )
        except Exception:
            pass

    # --- salvar ---
    if dry_run:
        log.info("→ [passo 5/5] [DRY-RUN] NÃO salvando — %s", empresa.doc_fmt or empresa.nome[:30])
        return

    # Salvar dispara confirm + alert (aceitos pelo handler em processar_empresas).
    log.info("→ [passo 5/5] clicando em Salvar (confirm + alert aceitos automaticamente)")
    dialogs = getattr(page, "_svd_dialogs", None)
    if dialogs is not None:
        dialogs.clear()
    save = page.locator('button[onclick^="concluir"], button:has-text("Salvar")').first
    await save.click()
    await asyncio.sleep(1.5)

    msgs = list(dialogs or [])
    FALHA_KW = ("não realizado", "nao realizado", "inválid", "invalid", "erro",
                "já cadastr", "ja cadastr", "existe", "obrigat", "preencher", "suporte")
    falha = next((m for m in msgs if any(k in m.lower() for k in FALHA_KW)), None)
    if falha:
        raise RuntimeError(f"site RECUSOU o cadastro de {empresa.doc_fmt or empresa.nome[:30]}: {falha!r}")

    sucesso_alert = any("sucesso" in m.lower() or "cadastrad" in m.lower() for m in msgs)
    try:
        await page.wait_for_url(re.compile(r"/svd/empresas(?:\?|$|/)"), timeout=12000)
        log.info("✓ empresa %s CADASTRADA (redirecionou pra listagem)", empresa.doc_fmt or empresa.nome[:30])
    except PWTimeout:
        if sucesso_alert:
            log.info("✓ empresa %s CADASTRADA (alert de sucesso)", empresa.doc_fmt or empresa.nome[:30])
            return
        raise RuntimeError(
            f"timeout confirmando cadastro de {empresa.doc_fmt or empresa.nome[:30]} "
            f"(diálogos do site: {msgs or '—'})"
        )


# ---------------------------------------------------------------------------
# Orquestração
# ---------------------------------------------------------------------------

async def _capturar_screenshot(page: Page, screenshots_dir: Path, ident: str) -> Optional[str]:
    try:
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe = re.sub(r"[^\w\-]", "_", ident or "sem-id")
        path = screenshots_dir / f"erro-{safe}-{ts}.png"
        await page.screenshot(path=str(path), full_page=True)
        log.info("screenshot do erro salvo em: %s", path)
        return str(path)
    except Exception as exc:
        log.warning("não consegui salvar screenshot: %s", exc)
        return None


async def processar_empresas(
    context: BrowserContext,
    empresas: list[Empresa],
    *,
    dry_run: bool = False,
    screenshots_dir: Optional[Path] = None,
    run_id: str = "",
) -> list[dict]:
    """Loga, lista existentes e cadastra as que faltam. Retorna registros pro report."""
    page = await context.new_page()

    # Aceita os diálogos do site (confirm + alert) e registra as mensagens.
    dialog_msgs: list[str] = []
    async def _on_dialog(d):
        dialog_msgs.append(d.message or "")
        log.info("   ⤷ diálogo do site [%s]: %r — aceitando", d.type, d.message)
        try:
            await d.accept()
        except Exception:
            pass
    page.on("dialog", lambda d: asyncio.create_task(_on_dialog(d)))
    page._svd_dialogs = dialog_msgs  # type: ignore[attr-defined]

    await ensure_logged_in(page)

    log.info("buscando empresas já cadastradas no sistema…")
    existentes = await listar_empresas_existentes(page)

    resultados: list[dict] = []
    total = len(empresas)
    for idx, e in enumerate(empresas, start=1):
        log.info("─" * 60)
        log.info("[%d/%d] %s — %s %s", idx, total, e.nome[:50], e.tipo_pessoa, e.doc_fmt or "(sem doc)")

        rec = {
            "razao_social": e.nome,
            "doc": e.doc_fmt,
            "tipo_pessoa": e.tipo_pessoa,
            "status": "",
            "motivo": "",
            "screenshot": "",
        }

        if not e.doc:
            rec["status"] = "sem_documento"
            rec["motivo"] = "sem CPF/CNPJ na planilha — o form exige documento"
            log.warning("[%d/%d] ⚠ sem documento, pulando: %s", idx, total, e.nome[:50])
            resultados.append(rec)
            continue

        if e.doc in existentes:
            rec["status"] = "ja_cadastrada"
            rec["motivo"] = "já estava cadastrada no Panorama"
            log.info("[%d/%d] ↷ já cadastrada: %s", idx, total, e.doc_fmt)
            resultados.append(rec)
            continue

        try:
            await criar_empresa(page, e, dry_run=dry_run)
            rec["status"] = "dry_run" if dry_run else "cadastrada"
            if not dry_run and e.doc:
                existentes.add(e.doc)  # evita recriar no mesmo run
            log.info("[%d/%d] ✓ status=%s para %s", idx, total, rec["status"], e.doc_fmt or e.nome[:30])
        except Exception as exc:
            log.error("[%d/%d] ✗ FALHA cadastrando %s", idx, total, e.doc_fmt or e.nome[:30])
            log.error("       exceção: %s: %s", type(exc).__name__, exc)
            log.error("       stack trace:\n%s", traceback.format_exc())
            sp = None
            if screenshots_dir:
                sp = await _capturar_screenshot(page, screenshots_dir, e.doc or e.nome)
            rec["status"] = "erro"
            rec["motivo"] = f"{type(exc).__name__}: {str(exc)[:250]}"
            if sp:
                rec["screenshot"] = sp
        resultados.append(rec)

    log.info("─" * 60)
    await page.close()
    return resultados
