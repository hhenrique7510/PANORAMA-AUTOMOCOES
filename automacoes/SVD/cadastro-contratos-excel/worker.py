"""Playwright worker: login + criar contrato novo a partir de ContratoExcel.

Padrão segue os outros workers do repo (async + uma Page por worker).
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

from parser import ContratoExcel

log = logging.getLogger(__name__)

URL = os.environ.get("PANORAMA_URL", "https://panoramafiscal.com.br/svd")
USER = os.environ.get("PANORAMA_USER") or os.environ.get("USER_LOGIN", "")
PASS = os.environ.get("PANORAMA_PASS") or os.environ.get("USER_PASS", "")
DEFAULT_GESTOR = os.environ.get("DEFAULT_GESTOR", "PREFEITURA MUNICIPAL DE BAYEUX")
DEFAULT_FISCAL = os.environ.get("DEFAULT_FISCAL", "PREFEITURA MUNICIPAL DE BAYEUX")
DEFAULT_TIPO = os.environ.get("DEFAULT_TIPO_CONTRATO", "CONTRATO")

_parts = urlsplit(URL)
_ORIGIN = f"{_parts.scheme}://{_parts.netloc}"
LOGIN_URL = URL
CONTRATOS_URL = os.environ.get("PANORAMA_CONTRATOS_URL", f"{_ORIGIN}/svd/contratos")
NOVO_CONTRATO_URL = os.environ.get("PANORAMA_NOVO_CONTRATO_URL", f"{_ORIGIN}/svd/contrato/salvar")


# ---------------------------------------------------------------------------
# Helpers básicos (login, navegação)
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


async def _abrir_form_novo_contrato(page: Page) -> None:
    """Abre a tela 'Cadastro de Contrato' a partir da listagem.

    Acesso direto a /svd/contrato/salvar via GET retorna erro HTTP — esse
    endpoint só aceita POST (é onde o form submete). A tela real de cadastro
    abre clicando no botão '+ Novo contrato' na página /svd/contratos.
    """
    # Vai pra listagem
    await _safe_goto(page, CONTRATOS_URL)
    await page.wait_for_load_state("networkidle")

    # Clica no botão "+ Novo contrato"
    botoes = [
        'a:has-text("Novo contrato")',
        'button:has-text("Novo contrato")',
        'a:has-text("+ Novo contrato")',
        'a.btn:has-text("Novo")',
    ]
    clicou = False
    for sel in botoes:
        loc = page.locator(sel).first
        try:
            if await loc.is_visible(timeout=2000):
                await loc.click()
                clicou = True
                log.debug("cliquei em '%s'", sel)
                break
        except PWTimeout:
            continue

    if not clicou:
        raise RuntimeError("botão '+ Novo contrato' não encontrado na listagem")

    # Espera a tela de cadastro carregar (a URL muda pra /contrato/salvar OU pra outra)
    await page.wait_for_load_state("networkidle")
    # Sanity check: deve ter o campo Número do Contrato visível
    try:
        await page.locator(
            'input[name*="numero" i], input[placeholder*="úmero" i]'
        ).first.wait_for(state="visible", timeout=8000)
    except PWTimeout:
        raise RuntimeError(
            f"após clicar em 'Novo contrato' a tela de cadastro não apareceu "
            f"(url atual: {page.url})"
        )


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

    # O campo de CPF usa jQuery Mask: precisa DIGITAR (type) para a máscara
    # formatar e o valor chegar correto. fill() escreve direto sem disparar a
    # máscara e o servidor recebe um CPF quebrado → "Usuário/Senha inválido(s)".
    # Mandamos só os dígitos; a máscara aplica os pontos/traço sozinha.
    user_digitos = re.sub(r"\D", "", USER)
    await user_field.click()
    await user_field.fill("")  # limpa antes de digitar
    await user_field.type(user_digitos, delay=40)
    valor_visivel = await user_field.input_value()
    log.debug("CPF digitado: dígitos=%s → campo exibe %r", user_digitos, valor_visivel)

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

    # Verifica se o login REALMENTE deu certo. O servidor reexibe o form de
    # login (com o campo senha) quando as credenciais são inválidas.
    body = (await page.inner_text("body")).lower()
    ainda_no_login = await page.locator('input[type="password"]').first.is_visible()
    msg_erro = any(kw in body for kw in ("inválid", "incorret", "credenciais"))
    if ainda_no_login or msg_erro:
        # tenta extrair a mensagem exata mostrada na tela
        detalhe = ""
        for kw in ("usuário/senha inválido", "inválid", "incorret", "credenciais"):
            for linha in body.splitlines():
                if kw in linha and len(linha) < 120:
                    detalhe = linha.strip()
                    break
            if detalhe:
                break
        raise RuntimeError(
            "login FALHOU — o servidor rejeitou as credenciais"
            + (f" ({detalhe!r})" if detalhe else "")
            + f". Confira PANORAMA_USER (CPF só dígitos, ex: 04408395424) e "
            f"PANORAMA_PASS no .env. CPF informado tem {len(USER)} caractere(s)."
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


async def _preencher_data(page: Page, selector: str, value: str, *, label: str) -> bool:
    """Preenche um campo de data (datepicker flatpickr).

    Os campos são READONLY e controlados pelo flatpickr — `fill()` trava e setar
    só `.value` via JS NÃO atualiza o estado interno do flatpickr
    (`selectedDates`), então o backend recebe data vazia ("Cadastro não
    realizado"). Usamos a API oficial `el._flatpickr.setDate(val, true, "d/m/Y")`,
    que atualiza input + estado interno + dispara o change. Fallback: set direto.
    """
    if not value:
        return False
    loc = page.locator(selector).first
    try:
        await loc.wait_for(state="attached", timeout=4000)
    except PWTimeout:
        log.warning("   campo de data '%s' não encontrado (%s)", label, selector)
        return False
    await loc.evaluate(
        """(el, val) => {
            if (el._flatpickr) {
                el._flatpickr.setDate(val, true, "d/m/Y");  // input + selectedDates + change
                return;
            }
            el.removeAttribute('readonly');
            el.value = val;
            for (const ev of ['input', 'keyup', 'change', 'blur']) {
                el.dispatchEvent(new Event(ev, {bubbles: true}));
            }
        }""",
        value,
    )
    visivel = (await loc.input_value()).strip()
    log.debug("   data '%s' setada (flatpickr) → campo exibe %r", label, visivel)
    return bool(visivel)


async def _select_by_label(page: Page, selector: str, label_substring: str) -> bool:
    """Seleciona uma <option> cujo texto contenha label_substring."""
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
    if v is None:
        return ""
    return f"{v:.2f}".replace(".", ",")


# ---------------------------------------------------------------------------
# Selecionar empresa pelo CNPJ/CPF
# ---------------------------------------------------------------------------

async def _selecionar_empresa(page: Page, contrato: ContratoExcel) -> bool:
    """Acha a empresa no dropdown 'Empresa' por CNPJ/CPF/nome.

    Tenta 3 tipos de widget:
    1. <select> HTML nativo — usa select_option pelo label
    2. Select2 / Choices.js — clica no widget, digita pra filtrar, clica na opção
    3. Input autocomplete simples — digita e clica na sugestão
    """
    # Monta lista de termos de busca (do mais específico pro mais genérico)
    candidatos = []
    if contrato.cnpj:
        c = contrato.cnpj
        candidatos += [
            f"{c[:2]}.{c[2:5]}.{c[5:8]}",   # 13.099.984
            c[:8],                            # 13099984
            c,                                # CNPJ inteiro
        ]
    if contrato.cpf:
        c = contrato.cpf
        candidatos += [
            f"{c[:3]}.{c[3:6]}.{c[6:9]}",   # 044.083.954
            c[:9],                            # 044083954
            c,                                # CPF inteiro
        ]
    if contrato.empresa_nome:
        nome = contrato.empresa_nome.strip()
        # Nome completo + um prefixo longo (tolera sufixos tipo LTDA/ME). NUNCA
        # só o primeiro nome: "ANA" casaria com "ANA JULIA ..." — outra empresa.
        # Match genérico cadastraria o contrato na EMPRESA ERRADA.
        candidatos.append(nome)
        if len(nome) > 20:
            candidatos.append(nome[:20])

    log.debug("   candidatos pra empresa: %s", candidatos)

    # ----- Estratégia 1: <select> HTML nativo -----
    select_sel = 'select[name*="empresa" i], select[id*="empresa" i]'
    sel = page.locator(select_sel).first
    try:
        if await sel.is_visible(timeout=1500):
            n_opcoes = await sel.locator("option").count()
            log.info("   campo empresa é <select> nativo com %d opções", n_opcoes)
            for cand in candidatos:
                if await _select_by_label(page, select_sel, cand):
                    log.info("   ✓ empresa selecionada (select nativo) por '%s'", cand)
                    return True
            log.warning("   nenhum dos candidatos casou no <select> nativo")
    except PWTimeout:
        pass

    # ----- Estratégia 2: Select2 -----
    s2 = page.locator(
        '.select2-selection:near(:text("Empresa")), '
        '.select2-container:has(.select2-selection):near(:text("Empresa")), '
        '.select2:has(.select2-selection)'
    ).first
    try:
        if await s2.is_visible(timeout=1500):
            log.info("   campo empresa parece Select2 — usando clique + digitação")
            await s2.click()
            await asyncio.sleep(0.3)
            for cand in candidatos:
                # input de busca do Select2
                search = page.locator('.select2-search__field, input.select2-search__field').first
                try:
                    await search.wait_for(state="visible", timeout=2000)
                except PWTimeout:
                    continue
                await search.fill("")
                await search.type(cand, delay=30)
                await asyncio.sleep(0.6)
                # 1ª sugestão visível
                opt = page.locator('.select2-results__option:not(.select2-results__message)').first
                try:
                    if await opt.is_visible(timeout=1500):
                        txt = (await opt.text_content() or "").strip()[:60]
                        await opt.click()
                        log.info("   ✓ empresa selecionada (Select2) com busca '%s' → %r", cand, txt)
                        return True
                except PWTimeout:
                    continue
            log.warning("   nenhum candidato retornou sugestão no Select2")
            # fecha o dropdown
            await page.keyboard.press("Escape")
    except PWTimeout:
        pass

    # ----- Estratégia 3: input autocomplete simples -----
    inp = page.locator(
        'input[name*="empresa" i]:not([type="hidden"]), '
        'input[id*="empresa" i]:not([type="hidden"])'
    ).first
    try:
        if await inp.is_visible(timeout=1500):
            log.info("   campo empresa é <input> simples — usando digitação")
            for cand in candidatos:
                await inp.fill("")
                await inp.type(cand, delay=30)
                await asyncio.sleep(0.5)
                sug = page.locator(
                    'ul.autocomplete li, ul.ui-autocomplete li, '
                    'li[role="option"], div.suggestion'
                ).first
                try:
                    if await sug.is_visible(timeout=1500):
                        await sug.click()
                        log.info("   ✓ empresa selecionada (autocomplete) com '%s'", cand)
                        return True
                except PWTimeout:
                    continue
    except PWTimeout:
        pass

    return False


# ---------------------------------------------------------------------------
# Criar contrato
# ---------------------------------------------------------------------------

async def criar_contrato(page: Page, contrato: ContratoExcel, *, dry_run: bool = False) -> None:
    """Abre /svd/contrato/salvar, preenche todos os campos e clica em Salvar."""
    if not contrato.is_valid_para_criar():
        raise ValueError(
            f"contrato sem campos obrigatórios (numero + data_fim): {contrato.numero}"
        )

    log.info("→ [passo 1/8] abrindo form de cadastro (via clique em '+ Novo contrato') p/ %s", contrato.numero)
    await _abrir_form_novo_contrato(page)

    # --- empresa ---
    log.info("→ [passo 2/8] selecionando empresa (cnpj=%s cpf=%s nome=%s)",
             contrato.cnpj, contrato.cpf, contrato.empresa_nome[:40])
    if not await _selecionar_empresa(page, contrato):
        raise RuntimeError(
            f"empresa não encontrada no dropdown do Panorama: "
            f"{contrato.empresa_nome[:60]!r} (cnpj={contrato.cnpj} cpf={contrato.cpf}). "
            f"Ela precisa estar PRÉ-CADASTRADA no sistema antes de criar o contrato — "
            f"NÃO seleciono outra pra não cadastrar na empresa errada."
        )

    # --- tipo de contrato ---
    log.debug("→ [passo 3/8] tipo: %s", contrato.tipo or DEFAULT_TIPO)
    await _select_by_label(page, 'select[name*="tipo" i]', contrato.tipo or DEFAULT_TIPO)

    # --- número ---
    log.info("→ [passo 4/8] número: %s", contrato.numero)
    if not await _fill_if_present(page, 'input[name*="numero" i]', contrato.numero or ""):
        raise RuntimeError("campo 'Número do Contrato' não encontrado")

    # --- datas (campos readonly/datepicker → setados via JS) ---
    log.info("→ [passo 5/8] datas: %s → %s",
             _fmt_data_br(contrato.data_inicio), _fmt_data_br(contrato.data_fim))
    # nomes reais dos campos (readonly/datepicker): #dataInicial e #dataFinal
    # ("final", não "fim"). #dataFinalApolice é outro campo — não confundir.
    await _preencher_data(
        page,
        'input#dataInicial, input[name="dataInicial"]',
        _fmt_data_br(contrato.data_inicio),
        label="início",
    )
    if not await _preencher_data(
        page,
        'input#dataFinal, input[name="dataFinal"]',
        _fmt_data_br(contrato.data_fim),
        label="fim",
    ):
        raise RuntimeError("campo 'Data Fim de Contrato' (#dataFinal) não encontrado")

    # --- gestor/fiscal ---
    if DEFAULT_GESTOR:
        await _select_by_label(page, 'select[name*="gestor" i]', DEFAULT_GESTOR[:20])
    if DEFAULT_FISCAL:
        await _select_by_label(page, 'select[name*="fiscal" i]', DEFAULT_FISCAL[:20])

    # --- valor (campo com máscara de dinheiro #valorTexto) ---
    # fill() não dispara a máscara → backend recebe valor cru e recusa. Digitamos
    # os centavos (ex: 2968020) e a máscara monta "R$ 29.680,20".
    log.info("→ [passo 6/8] valor: R$ %s", _fmt_valor_br(contrato.valor) or "—")
    if contrato.valor is not None:
        centavos = str(int(round(contrato.valor * 100)))
        vt = page.locator('#valorTexto, input[name="valorTexto"]').first
        try:
            await vt.wait_for(state="visible", timeout=3000)
            await vt.click()
            await vt.fill("")
            await vt.type(centavos, delay=30)
            log.debug("   valor digitado (%s centavos) → campo exibe %r",
                      centavos, await vt.input_value())
        except PWTimeout:
            log.warning("   campo de valor (#valorTexto) não encontrado")

    # --- aba "Objeto do Contrato" ---
    # A tela tem abas (Situação Cadastral / Objeto do Contrato / Itens). A
    # textarea de objeto fica na aba #tab-objeto, que só fica acessível depois de
    # clicar no link da aba (#custom-content-above-tab-objeto).
    if contrato.objeto:
        log.info("→ [passo 7/8] aba Objeto do Contrato — preenchendo %d chars",
                 len(contrato.objeto))
        try:
            aba = page.locator(
                '#custom-content-above-tab-objeto, '
                'a[href="#tab-objeto"], '
                'a:has-text("Objeto do Contrato"), button:has-text("Objeto do Contrato")'
            ).first
            await aba.click(timeout=3000)
            await asyncio.sleep(0.4)
        except PWTimeout:
            log.warning("   aba 'Objeto do Contrato' não encontrada — tentando textarea no form principal")

        # O campo de objeto é um <input id="objeto"> (não textarea), dentro de
        # #tab-objeto, que só fica visível depois de clicar na aba.
        objeto_sel = (
            '#tab-objeto input[name="objeto"], '
            'input#objeto, '
            'input[name="objeto"], '
            'div.tab-pane.active textarea, textarea'
        )
        if not await _fill_if_present(page, objeto_sel, contrato.objeto):
            log.warning("   campo de objeto não encontrado — seguindo sem preencher")
        else:
            # O objeto só é registrado depois de clicar "Incluir Objeto"
            # (onclick=salvarObjeto()), que o adiciona à tabela. Sem isso o
            # Salvar é rejeitado (o contrato fica sem objeto).
            try:
                incluir = page.locator(
                    '[onclick*="salvarObjeto"], '
                    '#tab-objeto button:has-text("Incluir Objeto"), '
                    '#tab-objeto a:has-text("Incluir Objeto"), '
                    ':is(button,a,input):has-text("Incluir Objeto")'
                ).first
                await incluir.click(timeout=4000)
                await asyncio.sleep(0.6)
                log.info("   ✓ objeto incluído na lista (Incluir Objeto)")
            except PWTimeout:
                log.warning("   botão 'Incluir Objeto' não encontrado — objeto pode não ser salvo")
    else:
        log.info("→ [passo 7/8] sem objeto pra preencher")

    # --- salvar ---
    if dry_run:
        log.info("→ [passo 8/8] [DRY-RUN] NÃO salvando — contrato %s", contrato.numero)
        return

    # Ao clicar em Salvar o site dispara confirm("Você confirma cadastro.") e
    # depois um alert() de sucesso. Ambos são aceitos pelo handler registrado em
    # processar_contratos (page.on("dialog", ...)). Sem isso o confirm volta
    # False e o cadastro é abortado silenciosamente.
    log.info("→ [passo 8/8] clicando em Salvar (confirm + alert aceitos automaticamente)")
    dialogs = getattr(page, "_svd_dialogs", None)
    if dialogs is not None:
        dialogs.clear()  # zera pra capturar só os diálogos deste Salvar

    save_btn = page.locator(
        'button:has-text("Salvar"), input[type="submit"][value*="alvar" i]'
    ).first
    await save_btn.click()
    await asyncio.sleep(1.5)  # deixa confirm + alert do backend serem processados

    # Se o site mostrou um alert de FALHA, o "cadastro" não aconteceu — mesmo
    # que a URL mude. Tratamos como erro (senão reportamos falso "criado").
    msgs = list(dialogs or [])
    FALHA_KW = ("não realizado", "nao realizado", "não foi", "erro", "suporte",
                "inválid", "invalid", "falh", "preencher", "obrigat")
    falha = next((m for m in msgs if any(k in m.lower() for k in FALHA_KW)), None)
    if falha:
        raise RuntimeError(f"site RECUSOU o cadastro de {contrato.numero}: {falha!r}")

    sucesso_alert = any("sucesso" in m.lower() or "cadastrado" in m.lower() for m in msgs)
    try:
        await page.wait_for_url(re.compile(r"contratos?(?!/salvar)"), timeout=15000)
        log.info("✓ contrato %s SALVO (redirecionou pra listagem)", contrato.numero)
    except PWTimeout:
        if sucesso_alert:
            log.info("✓ contrato %s SALVO (alert de sucesso)", contrato.numero)
            return
        toast = page.locator('.toast, .alert-success, .swal2-success').first
        try:
            await toast.wait_for(state="visible", timeout=3000)
            log.info("✓ contrato %s SALVO (toast de sucesso)", contrato.numero)
        except PWTimeout:
            raise RuntimeError(
                f"timeout esperando confirmação de salvamento de {contrato.numero} "
                f"(diálogos do site: {msgs or '—'})"
            )


# ---------------------------------------------------------------------------
# Orquestração
# ---------------------------------------------------------------------------

async def _capturar_screenshot(page: Page, screenshots_dir: Path, numero: str) -> Optional[str]:
    try:
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe = re.sub(r"[^\w\-]", "_", numero or "sem-numero")
        path = screenshots_dir / f"erro-{safe}-{ts}.png"
        await page.screenshot(path=str(path), full_page=True)
        log.info("screenshot do erro salvo em: %s", path)
        return str(path)
    except Exception as exc:
        log.warning("não consegui salvar screenshot: %s", exc)
        return None


async def processar_contratos(
    context: BrowserContext,
    contratos: list[ContratoExcel],
    *,
    dry_run: bool = False,
    screenshots_dir: Optional[Path] = None,
    run_id: str = "",
) -> list[dict]:
    """Loga e cria cada contrato. Retorna registros pro report."""
    page = await context.new_page()
    # O site usa confirm("Você confirma cadastro.") + alert() no fluxo de Salvar.
    # Por padrão o Playwright CANCELA diálogos (o confirm vira False e aborta o
    # cadastro). Aceitamos todos automaticamente e LOGAMOS a mensagem — útil pra
    # ver validações do site (ex: "preencha o objeto") que de outra forma somem.
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

    resultados: list[dict] = []
    total = len(contratos)
    for idx, c in enumerate(contratos, start=1):
        log.info("─" * 60)
        log.info("[%d/%d] processando: %s — %s",
                 idx, total, c.numero, c.empresa_nome[:50])

        rec = {
            "numero": c.numero_normalizado or "",
            "tipo": c.tipo,
            "empresa": c.empresa_nome,
            "cnpj_cpf": c.doc_pessoa,
            "data_inicio": _fmt_data_br(c.data_inicio),
            "data_fim": _fmt_data_br(c.data_fim),
            "valor": _fmt_valor_br(c.valor),
            "objeto_inicio": (c.objeto[:80] + "...") if len(c.objeto) > 80 else c.objeto,
            "status": "",
            "motivo": "",
            "screenshot": "",
        }

        if not c.is_valid_para_criar():
            falta = []
            if not c.numero: falta.append("número")
            if not c.data_fim: falta.append("data_fim")
            rec["status"] = "erro"
            rec["motivo"] = f"linha com campos obrigatórios faltando: {', '.join(falta)}"
            log.warning("PULANDO %s: %s", c.numero, rec["motivo"])
            resultados.append(rec)
            continue

        try:
            await criar_contrato(page, c, dry_run=dry_run)
            rec["status"] = "dry_run" if dry_run else "criado"
            log.info("[%d/%d] ✓ status=%s para %s",
                     idx, total, rec["status"], c.numero)
        except Exception as exc:
            log.error("[%d/%d] ✗ FALHA cadastrando %s", idx, total, c.numero)
            log.error("       exceção: %s: %s", type(exc).__name__, exc)
            log.error("       stack trace:\n%s", traceback.format_exc())
            screenshot_path = None
            if screenshots_dir:
                screenshot_path = await _capturar_screenshot(
                    page, screenshots_dir, c.numero
                )
            rec["status"] = "erro"
            rec["motivo"] = f"{type(exc).__name__}: {str(exc)[:250]}"
            if screenshot_path:
                rec["screenshot"] = screenshot_path
        resultados.append(rec)

    log.info("─" * 60)
    await page.close()
    return resultados
