"""Playwright worker: login, listagem de contratos cadastrados, criação de novo contrato.

Padrão segue o `automacoes/PANORAMA/checado-de-validade-az/worker.py` (mesmo projeto):
async + uma `Page` por worker, helpers de login com retry.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.async_api import BrowserContext, Locator, Page, TimeoutError as PWTimeout

from parser import ContratoPDF


@dataclass
class ContratoListado:
    """Uma linha da listagem /svd/contratos."""
    numero: str   # 'NNNNN/AAAA'
    id: str       # ID interno (lido do link de alterar /svd/contrato/alterar/{id})


class PDFJaAnexado(Exception):
    """Sinaliza que o contrato existente já tem PDF anexado (não há `#fileinput`
    na tela de edição). Não é um erro — é fluxo normal de pular."""

log = logging.getLogger(__name__)

from urllib.parse import urlsplit

# PANORAMA_URL agora é a URL "normal" de entrada/login (ex.: .../svd). O bot
# loga ali e DEPOIS navega pras telas de contratos — abrir /svd/contratos
# direto sem sessão trava (redireciona pro login).
URL = os.environ.get("PANORAMA_URL", "https://panoramafiscal.com.br/svd")
# Aceita tanto PANORAMA_USER/PANORAMA_PASS quanto USER_LOGIN/USER_PASS.
USER = os.environ.get("PANORAMA_USER") or os.environ.get("USER_LOGIN", "")
PASS = os.environ.get("PANORAMA_PASS") or os.environ.get("USER_PASS", "")
DEFAULT_GESTOR = os.environ.get("DEFAULT_GESTOR", "")
DEFAULT_FISCAL = os.environ.get("DEFAULT_FISCAL", "")
DEFAULT_TIPO = os.environ.get("DEFAULT_TIPO_CONTRATO", "CONTRATO")
# Por padrão NÃO cria contrato novo: o trabalho é só anexar o PDF nos que já
# existem. Defina como 1/true pra criar quando o número não estiver na listagem.
CRIAR_SE_NAO_EXISTIR = os.environ.get("PANORAMA_CRIAR_SE_NAO_EXISTIR", "").lower() in (
    "1", "true", "yes", "sim"
)

# Origem (scheme://host) pra montar as URLs internas independentemente do
# caminho que vier na PANORAMA_URL.
_parts = urlsplit(URL)
_ORIGIN = f"{_parts.scheme}://{_parts.netloc}"

LOGIN_URL = URL  # onde o login acontece (pode ser a home/landing)
CONTRATOS_URL = os.environ.get("PANORAMA_CONTRATOS_URL", f"{_ORIGIN}/svd/contratos")
NOVO_CONTRATO_URL = os.environ.get(
    "PANORAMA_NOVO_CONTRATO_URL", f"{_ORIGIN}/svd/contrato/salvar"
)
# Edição de contrato existente. O {id} é o ID interno do contrato, lido do
# link de alterar na listagem. Ex.: .../svd/contrato/alterar/458
ALTERAR_URL_TMPL = os.environ.get(
    "PANORAMA_ALTERAR_URL_TMPL", f"{_ORIGIN}/svd/contrato/alterar/{{id}}"
)


def _normalizar_numero(raw: str) -> str:
    """'70/2026' | '00070/2026' | '70-2026' -> '00070/2026' (canônico p/ casar
    com ContratoPDF.numero_normalizado).

    Exige ano 19xx/20xx pra NÃO casar com o CNPJ da linha (ex: '251/0001' de
    '22.147.251/0001-36')."""
    m = re.search(r"(\d{1,6})\s*[/\-]\s*((?:19|20)\d{2})\b", raw or "")
    if not m:
        return ""
    return f"{m.group(1).zfill(5)}/{m.group(2)}"


# ---------------------------------------------------------------------------
# Login + navegação base (clonado/adaptado do worker existente)
# ---------------------------------------------------------------------------

async def _safe_goto(page: Page, url: str, *, referer: Optional[str] = None) -> None:
    for attempt in range(3):
        try:
            await page.goto(url, wait_until="load", timeout=30000, referer=referer)
            return
        except Exception as exc:
            log.warning("goto attempt %d failed (%s): %s", attempt + 1, url, exc)
            await asyncio.sleep(1.0)
    await page.goto(url, wait_until="commit", timeout=30000, referer=referer)


async def _ir_para_contratos(page: Page) -> None:
    """Abre a listagem de contratos.

    GET direto em /svd/contratos retorna 404 SEM Referer; com Referer da home
    (/svd/) o servidor responde 200 — é exatamente o que o clique no menu faz
    ([document] GET /svd/contratos). Então navegamos com Referer; se falhar,
    caímos pro clique no menu.
    """
    # garante estar numa página do sistema (pra ter Referer válido)
    if "/svd" not in page.url:
        await _safe_goto(page, LOGIN_URL)

    # 1) navegação direta COM Referer (reproduz o GET disparado pelo menu)
    try:
        await page.goto(CONTRATOS_URL, wait_until="load", timeout=30000,
                        referer=f"{_ORIGIN}/svd/")
        await page.wait_for_selector("table tbody tr", timeout=8000)
        log.info("entrou na listagem (goto+referer): %s", page.url)
        return
    except Exception as exc:
        log.warning("goto+referer falhou (%s) — tentando pelo menu", exc)

    # 2) fallback: clicar no menu (Contratos pai expande, filho navega)
    try:
        await page.get_by_text("Contratos", exact=True).first.click(timeout=3000)
        await asyncio.sleep(0.4)
        alvo = page.locator('a[href$="/contratos"], a[href$="/contratos/"]').first
        if await alvo.count() and await alvo.is_visible():
            await alvo.click()
        else:
            await page.get_by_text("Contratos", exact=True).nth(1).click(timeout=3000)
        await page.wait_for_load_state("networkidle")
        await page.wait_for_selector("table tbody tr", timeout=8000)
        log.info("entrou na listagem (menu): %s", page.url)
    except Exception as exc:
        log.error("não consegui abrir a listagem de contratos: %s", exc)
        raise


async def ensure_logged_in(page: Page) -> None:
    """Abre a URL de login; se aparecer form de senha, autentica.

    Preenche o CPF DIGITANDO tecla por tecla (`press_sequentially`) pra respeitar
    a máscara do campo (444.444.444-44) — `fill` direto não dispara a máscara e o
    form pode ir vazio. No fim, VERIFICA de fato se logou (não confia em log
    otimista): se o campo de senha continuar na tela, o login falhou.
    """
    await _safe_goto(page, LOGIN_URL)
    pwd = page.locator('input[type="password"]:visible').first
    try:
        await pwd.wait_for(state="visible", timeout=5000)
    except PWTimeout:
        log.info("já logado (sem form de senha)")
        return

    if not USER or not PASS:
        raise RuntimeError("PANORAMA_USER/PANORAMA_PASS não configurados no .env")

    # Campo de usuário: o 1º input VISÍVEL que não seja senha/oculto/botão.
    user_field = page.locator(
        'input:visible:not([type="password"]):not([type="hidden"])'
        ':not([type="submit"]):not([type="button"])'
        ':not([type="checkbox"]):not([type="radio"])'
    ).first
    await user_field.wait_for(state="visible", timeout=5000)

    await user_field.click()
    await user_field.fill("")                 # limpa qualquer valor/máscara residual
    await user_field.press_sequentially(USER, delay=40)   # respeita máscara do CPF
    await pwd.click()
    await pwd.fill("")
    await pwd.press_sequentially(PASS, delay=40)

    # loga o que ficou no campo (mascarado) pra diagnóstico — sem expor a senha
    val_user = await user_field.input_value()
    log.info("CPF digitado no campo de usuário: %r", val_user)
    if not val_user.strip():
        raise RuntimeError(
            "campo de usuário (CPF) ficou VAZIO após digitar — seletor do campo "
            "errado. Rode diag.py pra inspecionar o form de login."
        )

    submit = page.locator(
        'button[type="submit"], input[type="submit"], '
        'button:has-text("Entrar"), button:has-text("Acessar"), '
        'button:has-text("Login"), button:has-text("ENTRAR")'
    ).first
    try:
        await submit.click()
    except Exception:
        await pwd.press("Enter")

    # espera a navegação assentar
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeout:
        pass
    await asyncio.sleep(1.0)

    # VERIFICAÇÃO REAL: se ainda há campo de senha visível, não logou.
    if await page.locator('input[type="password"]:visible').count():
        # tenta capturar mensagem de erro da tela
        msg = ""
        for sel in (".alert", ".invalid-feedback", ".text-danger",
                    ".swal2-html-container", ".toast-message"):
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=500):
                    msg = (await el.text_content() or "").strip()
                    if msg:
                        break
            except Exception:
                continue
        raise RuntimeError(
            f"login NÃO efetuado (campo de senha ainda na tela). URL={page.url} "
            f"| mensagem do site: {msg or '(nenhuma)'} — confira CPF/senha no .env"
        )

    log.info("login OK — URL atual: %s", page.url)


# ---------------------------------------------------------------------------
# Listagem de contratos existentes
# ---------------------------------------------------------------------------

async def listar_contratos_existentes(page: Page) -> dict[str, ContratoListado]:
    """Carrega TODA a listagem e mapeia número -> ContratoListado (id).

    Retorno: dict {'NNNNN/AAAA': ContratoListado(numero, id)}.
    - `id`: lido do link `/svd/contrato/alterar/{id}` da linha (botão de editar).

    Nota: NÃO checamos aqui se o contrato já tem PDF anexado. A forma confiável
    de saber é abrir a tela de edição e ver se o `<input id="fileinput">` está
    presente — isso é feito em `anexar_pdf_em_existente`.
    """
    await _ir_para_contratos(page)

    # Espera a tabela aparecer. O sistema usa DataTables; tentamos primeiro
    # selecionar "All" ou o maior valor disponível pra carregar tudo de uma vez.
    try:
        select = page.locator('select[name$="_length"]').first
        await select.wait_for(state="visible", timeout=15000)
        # Pega a maior opção disponível
        opcoes = await select.locator("option").all_text_contents()
        log.debug("opções de length: %s", opcoes)
        # Prioridade: "All" / "-1" / o maior número
        valor_alvo = None
        for o in opcoes:
            if o.strip().lower() in ("all", "todos", "tudo"):
                valor_alvo = "-1"
                break
        if not valor_alvo:
            # pega o maior número
            nums = []
            for o in opcoes:
                try:
                    nums.append(int(o.strip()))
                except ValueError:
                    continue
            if nums:
                valor_alvo = str(max(nums))
        if valor_alvo:
            await select.select_option(value=valor_alvo)
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(0.5)
    except PWTimeout:
        log.warning("não achei o <select> de length — seguindo sem ajustar")

    # Extração em batch via JS — pra 248 linhas é muito mais rápido que iterar
    # com locators do Python. Pra cada linha pegamos: células (pro número) e
    # ID (do href de alterar).
    contratos: dict[str, ContratoListado] = {}
    sem_id = 0
    while True:
        rows_data = await page.evaluate(r"""
            () => {
                const tables = document.querySelectorAll('table');
                let rows = [];
                for (const t of tables) {
                    const trs = t.querySelectorAll('tbody tr');
                    if (trs.length > rows.length) rows = [...trs];
                }
                return rows.map(tr => {
                    const cells = [...tr.querySelectorAll('td')].map(td => td.textContent || '');
                    let id = '';
                    const links = [...tr.querySelectorAll('a[href]')];
                    for (const a of links) {
                        const m = (a.getAttribute('href') || '').match(/\/alterar\/(\d+)/);
                        if (m) { id = m[1]; break; }
                    }
                    if (!id) {
                        const m = tr.outerHTML.match(/\/alterar\/(\d+)/);
                        if (m) id = m[1];
                    }
                    return { cells, id };
                });
            }
        """)

        for rd in rows_data:
            cells = rd.get("cells", []) or []
            numero = ""
            for c in cells:
                if re.fullmatch(r"\s*\d{1,6}\s*/\s*(?:19|20)\d{2}\s*", c or ""):
                    numero = _normalizar_numero(c)
                    break
            if not numero:
                for c in cells:
                    numero = _normalizar_numero(c)
                    if numero:
                        break
            if not numero:
                continue

            contrato_id = rd.get("id", "") or ""
            if not contrato_id:
                sem_id += 1

            contratos[numero] = ContratoListado(numero=numero, id=contrato_id)

        # Paginação — só usada se length=All não funcionou
        next_btn = page.locator(
            'a.paginate_button.next:not(.disabled), '
            'li.paginate_button.next:not(.disabled) a'
        ).first
        try:
            if await next_btn.is_visible(timeout=500) and "disabled" not in (
                await next_btn.get_attribute("class") or ""
            ):
                await next_btn.click()
                await page.wait_for_load_state("networkidle")
                await asyncio.sleep(0.3)
                continue
        except PWTimeout:
            pass
        break

    com_id = sum(1 for c in contratos.values() if c.id)
    log.info("contratos na listagem: %d  (com ID p/ editar: %d, sem ID: %d)",
             len(contratos), com_id, sem_id)
    return contratos


# ---------------------------------------------------------------------------
# Criação de um novo contrato
# ---------------------------------------------------------------------------

async def _fill_if_present(page: Page, selector: str, value: str) -> bool:
    """fill que não falha se o campo não existir. Retorna True se preencheu."""
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
    """Tenta selecionar uma <option> cujo texto contenha label_substring."""
    if not label_substring:
        return False
    select = page.locator(selector).first
    try:
        await select.wait_for(state="visible", timeout=3000)
    except PWTimeout:
        return False
    opcoes = await select.locator("option").all()
    for o in opcoes:
        txt = (await o.text_content() or "").strip()
        if label_substring.lower() in txt.lower():
            value = await o.get_attribute("value")
            if value:
                await select.select_option(value=value)
                return True
    return False


def _fmt_data_br(d) -> str:
    return d.strftime("%d/%m/%Y") if d else ""


def _fmt_valor_br(v: Optional[float]) -> str:
    """1268400.0 -> '1268400,00' (input HTML aceita vírgula em campos numéricos
    PT-BR; sistema costuma reconhecer)."""
    if v is None:
        return ""
    return f"{v:.2f}".replace(".", ",")


async def criar_contrato(page: Page, contrato: ContratoPDF, *, dry_run: bool = False) -> None:
    """Preenche o formulário 'Novo contrato' e salva (a menos que dry_run).

    Cada etapa é logada explicitamente — se falhar no meio, fica claro EM QUAL
    passo deu pau (útil pra diagnóstico via log).
    """
    if not contrato.is_valid_para_criar():
        raise ValueError(
            f"contrato sem campos obrigatórios para CRIAR (numero + data_fim): "
            f"{contrato.arquivo.name}"
        )

    log.info("→ [passo 1/8] abrindo form de cadastro para contrato %s (arquivo: %s)",
             contrato.numero, contrato.arquivo.name)
    await _safe_goto(page, NOVO_CONTRATO_URL)
    await page.wait_for_load_state("networkidle")

    # ---- Aba Situação Cadastral ----
    log.info("→ [passo 2/8] selecionando empresa (cnpj=%s)", contrato.cnpj or "—")
    empresa_ok = False
    if contrato.cnpj:
        empresa_ok = await _select_by_label(page, 'select[name*="empresa" i]', contrato.cnpj[:10])
    if not empresa_ok and contrato.empresa:
        empresa_ok = await _select_by_label(page, 'select[name*="empresa" i]', contrato.empresa[:20])
    if not empresa_ok:
        log.warning("   não consegui selecionar empresa — pode ficar a primeira opção do dropdown")

    # Tipo de Contrato
    log.debug("→ [passo 3/8] tipo de contrato: %s", DEFAULT_TIPO)
    await _select_by_label(page, 'select[name*="tipo" i]', DEFAULT_TIPO)

    # Número
    log.info("→ [passo 4/8] preenchendo número: %s", contrato.numero)
    if not await _fill_if_present(page, 'input[name*="numero" i]', contrato.numero or ""):
        raise RuntimeError("campo 'Número do Contrato' não encontrado na tela")

    # Datas
    log.info("→ [passo 5/8] datas: inicio=%s  fim=%s",
             _fmt_data_br(contrato.data_inicio), _fmt_data_br(contrato.data_fim))
    await _fill_if_present(page, 'input[name*="dataInicio" i], input[name*="data_inicio" i]',
                           _fmt_data_br(contrato.data_inicio))
    if not await _fill_if_present(page, 'input[name*="dataFim" i], input[name*="data_fim" i]',
                                  _fmt_data_br(contrato.data_fim)):
        raise RuntimeError("campo 'Data Fim de Contrato' não encontrado na tela")

    # Gestor / Fiscal
    if DEFAULT_GESTOR:
        await _select_by_label(page, 'select[name*="gestor" i]', DEFAULT_GESTOR[:15])
    if DEFAULT_FISCAL:
        await _select_by_label(page, 'select[name*="fiscal" i]', DEFAULT_FISCAL[:15])

    # Valor
    log.info("→ [passo 6/8] valor: R$ %s", _fmt_valor_br(contrato.valor) or "—")
    await _fill_if_present(page, 'input[name*="valor" i]:not([name*="utilizado" i])',
                           _fmt_valor_br(contrato.valor))

    # ---- Anexar Contrato (PDF) ----
    log.info("→ [passo 7/8] anexando PDF: %s", contrato.arquivo.name)
    await _anexar_arquivo(page, contrato)

    # ---- Aba Objeto do Contrato ----
    if contrato.objeto:
        try:
            await page.locator(
                'a:has-text("Objeto do Contrato"), button:has-text("Objeto do Contrato")'
            ).first.click(timeout=2000)
            await asyncio.sleep(0.3)
            await _fill_if_present(page, 'textarea[name*="objeto" i], textarea', contrato.objeto)
        except PWTimeout:
            log.debug("aba 'Objeto do Contrato' não encontrada — pulando")

    # ---- Salvar ----
    if dry_run:
        log.info("→ [passo 8/8] [DRY-RUN] NÃO clicando em Salvar — contrato %s", contrato.numero)
        return

    log.info("→ [passo 8/8] clicando em Salvar")
    await _clicar_salvar(page, contrato)


# ---------------------------------------------------------------------------
# Anexar PDF em contrato JÁ existente (/svd/contrato/alterar/{id})
# ---------------------------------------------------------------------------

async def _anexar_arquivo(page: Page, contrato: ContratoPDF) -> None:
    """Localiza o input[type=file] de contrato e anexa o PDF. Levanta se não achar."""
    file_inputs = await page.locator('input[type="file"]').all()
    log.debug("   inputs[type=file] encontrados: %d", len(file_inputs))
    file_input = None
    for f in file_inputs:
        name_attr = (await f.get_attribute("name") or "").lower()
        if "contrato" in name_attr:
            file_input = f
            break
    if file_input is None and file_inputs:
        file_input = file_inputs[-1]  # fallback: último
    if file_input is None:
        raise RuntimeError("nenhum input[type=file] encontrado na tela")

    await file_input.set_input_files(str(contrato.arquivo))
    log.info("   PDF anexado: %s", contrato.arquivo)


async def _clicar_salvar(page: Page, contrato: ContratoPDF) -> None:
    """Clica em Salvar e espera redirect/toast de confirmação."""
    save_btn = page.locator(
        'button:has-text("Salvar"), input[type="submit"][value*="alvar" i]'
    ).first
    await save_btn.click()

    try:
        await page.wait_for_url(re.compile(r"contratos?(?!/salvar)"), timeout=15000)
        log.info("✓ contrato %s SALVO (redirecionou pra listagem)", contrato.numero)
    except PWTimeout:
        toast = page.locator('.toast, .alert-success, .swal2-success').first
        try:
            await toast.wait_for(state="visible", timeout=3000)
            log.info("✓ contrato %s SALVO (toast de sucesso)", contrato.numero)
        except PWTimeout:
            raise RuntimeError(
                f"timeout esperando confirmação de salvamento de {contrato.numero} "
                f"— verifique screenshot e log"
            )


async def _abrir_alterar_via_listagem(page: Page, contrato_id: str) -> bool:
    """Se o link de editar (/alterar/{id}) estiver visível na página atual, clica.

    Retorna True se conseguiu navegar por clique; False se não achou o link
    (aí o chamador faz goto direto).
    """
    link = page.locator(f'a[href*="/alterar/{contrato_id}"]').first
    try:
        if await link.is_visible(timeout=1000):
            await link.click(timeout=3000)
            await page.wait_for_load_state("networkidle")
            return True
    except Exception:
        pass
    return False


async def _achar_input_contrato(page: Page) -> Optional[Locator]:
    """Acha o input[type=file] do CONTRATO, EXCLUINDO o da apólice (#fileinput2
    ou qualquer um cujo contexto mencione 'apólice'). Retorna o Locator ou None.

    set_input_files funciona mesmo em input oculto, então não precisamos clicar
    em botão pra revelá-lo.
    """
    # 1) id confirmado no diagnóstico: o campo do CONTRATO é #fileinput
    #    (e o da APÓLICE é #fileinput2 — nunca usar esse).
    direto = page.locator('#fileinput')
    if await direto.count():
        return direto.first

    # 2) heurística: qualquer input[type=file] que NÃO seja a apólice
    inputs = await page.locator('input[type="file"]').all()
    candidatos = []
    for f in inputs:
        fid = (await f.get_attribute("id")) or ""
        if fid == "fileinput2":
            continue  # campo da apólice
        try:
            ctx = (await f.evaluate(
                "el => (el.closest('div,section,form,td,li')?.textContent || '')"
            )).lower()
        except Exception:
            ctx = ""
        if "apólice" in ctx or "apolice" in ctx:
            continue
        candidatos.append((f, ctx))

    if not candidatos:
        return None
    for f, ctx in candidatos:
        if "contrato" in ctx:
            return f
    return candidatos[0][0]


async def _tem_contrato_anexado(page: Page) -> bool:
    """True se a tela de edição mostra 'Baixar/Excluir Contrato' (= já tem PDF)."""
    loc = page.locator(
        'button:has-text("Baixar Contrato"), a:has-text("Baixar Contrato"), '
        'button:has-text("Excluir Contrato"), a:has-text("Excluir Contrato")'
    ).first
    try:
        return await loc.is_visible(timeout=2500)
    except PWTimeout:
        return False


async def _abrir_alterar(page: Page, contrato_id: str) -> None:
    """Abre /alterar/{id} — preferindo o clique na linha da listagem (Referer ok)."""
    if not await _abrir_alterar_via_listagem(page, contrato_id):
        await _safe_goto(page, ALTERAR_URL_TMPL.format(id=contrato_id),
                         referer=CONTRATOS_URL)
    await page.wait_for_load_state("networkidle")


async def anexar_pdf_em_existente(
    page: Page, contrato: ContratoPDF, contrato_id: str, *, dry_run: bool = False
) -> None:
    """Anexa o PDF do contrato e CONFIRMA que ficou anexado (reabrindo a tela).

    Fluxo (espelha o upload da apólice):
      1. abre /alterar/{id}
      2. já tem 'Baixar Contrato'? → PDFJaAnexado (pula)
      3. seta o PDF no #fileinput (campo do CONTRATO; nunca o #fileinput2/apólice)
      4. clica em 'Adicionar Contrato' (upload AJAX) e depois 'Salvar'
      5. REABRE e confirma que agora aparece 'Baixar Contrato' — só então conta
         como anexado. (Sem essa verificação tínhamos falso positivo: o "SALVO"
         antigo só via o redirect, sem garantir que o arquivo subiu.)
    """
    if not contrato_id:
        raise RuntimeError(
            f"contrato {contrato.numero_normalizado} achado na listagem mas sem "
            f"ID de alterar — não dá pra abrir a tela de edição"
        )

    log.info("→ [1/5] abrindo contrato existente (id=%s)", contrato_id)
    await _abrir_alterar(page, contrato_id)

    log.info("→ [2/5] já tem PDF? (botão 'Baixar Contrato')")
    if await _tem_contrato_anexado(page):
        log.info("   sim → %s JÁ TEM PDF, pulando", contrato.numero_normalizado)
        raise PDFJaAnexado(
            f"contrato {contrato.numero_normalizado} (id={contrato_id}) já tem PDF anexado"
        )

    if dry_run:
        # confirma que o gatilho existe, mas não sobe nada
        n = await page.get_by_text("Adicionar Contrato").count()
        log.info("→ [DRY-RUN] 'Adicionar Contrato' encontrado=%s — não anexo — %s (id=%s)",
                 bool(n), contrato.numero_normalizado, contrato_id)
        return

    # [3] Setar o PDF no #fileinput. O concluirContrato() lê
    # $('#fileinput').prop('files'), faz o upload (salvarArquivos → POST
    # /svd/svd_contrato_arquivo) e SÓ DEPOIS comita (salvar_json). Por isso
    # precisamos do Salvar completo, não só do upload.
    log.info("→ [3/5] setando o PDF no #fileinput: %s", contrato.arquivo.name)
    file_input = await _achar_input_contrato(page)
    if file_input is None:
        raise RuntimeError(
            "não achei o #fileinput do contrato — tela diferente do esperado."
        )
    await file_input.set_input_files(str(contrato.arquivo))

    # [4] Clicar Salvar = concluirContrato(): upload (svd_contrato_arquivo) E
    # commit (salvar_json). Depende do confirm "Você confirma cadastro." ser
    # ACEITO — o handler de diálogo (registrado em processar_pdfs) faz isso.
    # Esperamos a resposta do salvar_json: ela só ocorre se os uploads terminaram.
    log.info("→ [4/5] clicando em Salvar (concluirContrato: upload + commit)")
    try:
        async with page.expect_response(
            lambda r: "salvar_json" in r.url, timeout=120000
        ) as resp_info:
            await page.locator(
                'button:has-text("Salvar"), input[type=submit][value*="alvar" i]'
            ).first.click()
        resp = await resp_info.value
        log.info("   salvar_json → HTTP %s", resp.status)
    except PWTimeout:
        raise RuntimeError(
            "concluirContrato não chegou no salvar_json — alguma validação "
            "abortou (ex: 'Preencher os campos do Objeto do Contrato'). "
            "Confira o screenshot."
        )

    # [5] VERIFICAÇÃO REAL: reabre e confirma que agora tem 'Baixar Contrato'
    log.info("→ [5/5] verificando se o PDF REALMENTE ficou anexado")
    await _abrir_alterar(page, contrato_id)
    if not await _tem_contrato_anexado(page):
        raise RuntimeError(
            f"anexo NÃO confirmado: após anexar, o contrato "
            f"{contrato.numero_normalizado} (id={contrato_id}) ainda não mostra "
            f"'Baixar Contrato'. O PDF NÃO foi anexado de verdade."
        )
    log.info("   ✓ confirmado: %s agora tem 'Baixar Contrato' (PDF anexado)",
             contrato.numero_normalizado)


# ---------------------------------------------------------------------------
# Orquestração interna por worker
# ---------------------------------------------------------------------------

async def _capturar_screenshot(page: Page, screenshots_dir: Path, numero: str) -> Optional[str]:
    """Salva screenshot da página atual. Retorna o caminho ou None se falhar."""
    try:
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_numero = re.sub(r"[^\w\-]", "_", numero or "sem-numero")
        path = screenshots_dir / f"erro-{safe_numero}-{ts}.png"
        await page.screenshot(path=str(path), full_page=True)
        log.info("screenshot do erro salvo em: %s", path)
        return str(path)
    except Exception as exc:
        log.warning("não consegui salvar screenshot: %s", exc)
        return None


async def processar_pdfs(
    context: BrowserContext,
    pdfs: list[ContratoPDF],
    *,
    dry_run: bool = False,
    screenshots_dir: Optional[Path] = None,
    run_id: str = "",
) -> list[dict]:
    """Loga, lista existentes e cadastra os que faltam. Retorna registros pro report.

    Cada exceção durante criar_contrato gera:
    - stack trace completo no log
    - screenshot da tela em out/screenshots/
    - motivo (1 linha) no relatório
    """
    page = await context.new_page()
    # O site usa confirm("Você confirma cadastro.") + alert() no fluxo de salvar.
    # Por padrão o Playwright CANCELA diálogos (o confirm virava False e abortava
    # o cadastro). Aceitamos todos automaticamente. Registrado UMA vez por página.
    page.on("dialog", lambda d: asyncio.create_task(d.accept()))
    await ensure_logged_in(page)

    log.info("buscando contratos já cadastrados no sistema...")
    existentes = await listar_contratos_existentes(page)

    resultados: list[dict] = []
    total = len(pdfs)
    for idx, pdf in enumerate(pdfs, start=1):
        log.info("─" * 60)
        log.info("[%d/%d] processando: %s", idx, total, pdf.arquivo.name)

        rec = {
            "arquivo": pdf.arquivo.name,
            "numero": pdf.numero_normalizado or "",
            "cnpj": pdf.cnpj or "",
            "empresa": pdf.empresa or "",
            "data_inicio": _fmt_data_br(pdf.data_inicio),
            "data_fim": _fmt_data_br(pdf.data_fim),
            "valor": _fmt_valor_br(pdf.valor),
            "status": "",
            "motivo": "",
            "screenshot": "",
        }
        # Pra ANEXAR só precisamos do número (pra casar com a listagem). A
        # data_fim só é exigida no caminho de CRIAR contrato novo — validada
        # mais à frente, dentro de criar_contrato.
        if not pdf.numero:
            rec["status"] = "erro"
            rec["motivo"] = "PDF sem número de contrato — não dá pra casar com a listagem"
            log.warning("PULANDO %s — %s", pdf.arquivo.name, rec["motivo"])
            resultados.append(rec)
            continue

        # Caminho principal: o contrato já existe no sistema — falta anexar o PDF.
        if pdf.numero_normalizado not in existentes:
            if CRIAR_SE_NAO_EXISTIR:
                acao = "criar"
            else:
                rec["status"] = "nao_encontrado"
                rec["motivo"] = (
                    f"número {pdf.numero_normalizado} não está na listagem do sistema "
                    f"(não cadastro automaticamente — defina PANORAMA_CRIAR_SE_NAO_EXISTIR=1 p/ criar)"
                )
                log.warning("não encontrado na listagem: %s — pulando", pdf.numero_normalizado)
                resultados.append(rec)
                continue
        else:
            acao = "anexar"

        try:
            if acao == "anexar":
                contrato = existentes[pdf.numero_normalizado]
                contrato_id = contrato.id
                await anexar_pdf_em_existente(page, pdf, contrato_id, dry_run=dry_run)
                rec["status"] = "dry_run" if dry_run else "anexado"
                rec["motivo"] = f"id={contrato_id}"
            else:  # criar (fallback opcional)
                await criar_contrato(page, pdf, dry_run=dry_run)
                rec["status"] = "dry_run" if dry_run else "criado"
            log.info("[%d/%d] ✓ status=%s para %s", idx, total, rec["status"], pdf.numero_normalizado)
        except PDFJaAnexado as exc:
            # Não é erro — é o caso normal "contrato já tem PDF anexado, pula".
            rec["status"] = "ja_anexado"
            rec["motivo"] = str(exc)
            log.info("[%d/%d] ↷ status=ja_anexado para %s (pulando)",
                     idx, total, pdf.numero_normalizado)
        except Exception as exc:
            # Stack trace completo no log
            log.error("[%d/%d] ✗ FALHA processando %s", idx, total, pdf.arquivo.name)
            log.error("       exceção: %s: %s", type(exc).__name__, exc)
            log.error("       stack trace:\n%s", traceback.format_exc())

            # Screenshot pra diagnóstico
            screenshot_path = None
            if screenshots_dir:
                screenshot_path = await _capturar_screenshot(
                    page, screenshots_dir, pdf.numero_normalizado
                )

            rec["status"] = "erro"
            rec["motivo"] = f"{type(exc).__name__}: {str(exc)[:250]}"
            if screenshot_path:
                rec["screenshot"] = screenshot_path
        resultados.append(rec)

    log.info("─" * 60)
    await page.close()
    return resultados
