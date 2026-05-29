"""Orquestrador: lê CSV da Fase 1 e dispara empresa→contrato→anexa no Panorama."""
from __future__ import annotations

import asyncio
import csv
import logging
import os
import re
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from playwright.async_api import BrowserContext, Page, TimeoutError as PWTimeout

from loaders import carregar_workers, WorkerBundle

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Linha do CSV gerado pela Fase 1
# ---------------------------------------------------------------------------

@dataclass
class LinhaCSV:
    """Reflete o report.csv da transparencia-bayeux."""
    ano: str
    numero: str
    fornecedor: str
    cnpj: str
    cpf: str
    fiscal: str
    data_inicio: str           # 'dd/mm/yyyy'
    data_fim: str
    valor: str                 # '1234,56'
    objeto: str
    pdf_path: str
    status_origem: str         # 'baixado' | 'erro_download' | 'sem_pdf'

    @property
    def doc(self) -> str:
        return (self.cnpj or self.cpf or "").strip()


def _parse_date_br(s: str) -> Optional[date]:
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_valor(s: str) -> Optional[float]:
    if not s:
        return None
    s = str(s).strip().replace("R$", "").replace(" ", "")
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def ler_csv(path: Path, *, so_baixados: bool = True) -> list[LinhaCSV]:
    """Lê o report.csv da Fase 1 e devolve linhas filtradas."""
    if not path.exists():
        raise FileNotFoundError(f"CSV não existe: {path}")
    linhas: list[LinhaCSV] = []
    with path.open(encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            status = (row.get("status") or "").strip()
            if so_baixados and status != "baixado":
                continue
            linhas.append(LinhaCSV(
                ano=row.get("ano") or "",
                numero=row.get("numero") or "",
                fornecedor=row.get("fornecedor") or "",
                cnpj=row.get("cnpj") or "",
                cpf=row.get("cpf") or "",
                fiscal=row.get("fiscal") or "",
                data_inicio=row.get("data_inicio") or "",
                data_fim=row.get("data_fim") or "",
                valor=row.get("valor") or "",
                objeto=row.get("objeto") or "",
                pdf_path=row.get("pdf_path") or "",
                status_origem=status,
            ))
    return linhas


# ---------------------------------------------------------------------------
# Conversores: LinhaCSV → dataclasses dos outros workers
# ---------------------------------------------------------------------------

def linha_to_empresa(linha: LinhaCSV, EmpresaCls):
    return EmpresaCls(
        nome=linha.fornecedor.strip(),
        cnpj=linha.cnpj or None,
        cpf=linha.cpf or None,
        raw=f"{linha.fornecedor} {linha.cnpj or linha.cpf}".strip(),
    )


def linha_to_contrato_excel(linha: LinhaCSV, ContratoExcelCls):
    return ContratoExcelCls(
        numero=linha.numero,
        tipo="CONTRATO",
        status="ADICIONAR",
        empresa_raw=f"{linha.fornecedor}, CNPJ: {linha.cnpj}" if linha.cnpj else
                    f"{linha.fornecedor}, CPF: {linha.cpf}" if linha.cpf else linha.fornecedor,
        empresa_nome=linha.fornecedor.strip(),
        cnpj=linha.cnpj or None,
        cpf=linha.cpf or None,
        data_inicio=_parse_date_br(linha.data_inicio),
        data_fim=_parse_date_br(linha.data_fim),
        valor=_parse_valor(linha.valor),
        objeto=linha.objeto,
    )


def linha_to_contrato_pdf(linha: LinhaCSV, ContratoPDFCls):
    """Pra anexar_pdf_em_existente — basta número e caminho do PDF."""
    return ContratoPDFCls(
        arquivo=Path(linha.pdf_path),
        numero=linha.numero,
        cnpj=linha.cnpj or None,
        empresa=linha.fornecedor,
    )


# ---------------------------------------------------------------------------
# Helper: normalizar número pro formato que aparece no Panorama
# ---------------------------------------------------------------------------

def _normalizar_numero(raw: str) -> str:
    m = re.search(r"(\d{1,6})\s*[/\-]\s*(\d{4})", str(raw or ""))
    if not m:
        return ""
    # IMPORTANTE: lstrip remove zeros à esquerda PRIMEIRO. Assim '00167',
    # '000167' e '167' viram o mesmo '00167' após zfill(5). Sem isso, o cache
    # vê números do mesmo contrato como distintos e duplica.
    return f"{m.group(1).lstrip('0').zfill(5) or '00000'}/{m.group(2)}"


def _so_digitos(s: str) -> str:
    return re.sub(r"\D", "", s or "")


# ---------------------------------------------------------------------------
# Pipeline por linha
# ---------------------------------------------------------------------------

async def _screenshot_erro(page: Page, screenshots_dir: Path, numero: str, etapa: str) -> str:
    """Salva screenshot + URL atual quando dá erro. Retorna path do arquivo."""
    try:
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe = re.sub(r"[^\w\-]", "_", numero or "sem-numero")
        path = screenshots_dir / f"erro-{etapa}-{safe}-{ts}.png"
        await page.screenshot(path=str(path), full_page=True)
        log.info("  📷 screenshot: %s | URL no momento: %s",
                 path.name, page.url)
        return str(path)
    except Exception as e:
        log.warning("  não consegui salvar screenshot: %s", e)
        return ""


async def processar_uma_linha(
    page: Page,
    linha: LinhaCSV,
    w: WorkerBundle,
    cache_empresas: set[str],
    cache_contratos: dict[tuple[str, str], str],   # (número, cnpj_ou_cpf) → ID
    *,
    dry_run: bool,
    skip_empresa: bool,
    skip_contrato: bool,
    skip_anexar: bool,
    idx: int,
    total: int,
    screenshots_dir: Path,
) -> dict:
    """Roda os 3 passos: empresa → contrato → anexa. Atualiza caches in-place."""
    numero_canon = _normalizar_numero(linha.numero)
    doc = _so_digitos(linha.doc)

    rec = {
        "numero": numero_canon, "fornecedor": linha.fornecedor[:60],
        "doc": doc, "pdf_path": linha.pdf_path,
        "empresa_criada": "", "contrato_criado": "", "pdf_anexado": "",
        "status": "", "motivo": "",
    }
    log.info("─" * 60)
    log.info("[%d/%d] %s — %s", idx, total, numero_canon, linha.fornecedor[:50])

    # -------- 1) EMPRESA --------
    if skip_empresa:
        rec["empresa_criada"] = "skip"
    elif not doc:
        log.warning("  ⚠ sem CNPJ/CPF — pulando passo de empresa")
        rec["empresa_criada"] = "sem_doc"
    elif doc in cache_empresas:
        log.info("  ✓ empresa já existe (%s)", doc)
        rec["empresa_criada"] = "ja_existia"
    else:
        log.info("  ▶ criando empresa: %s (%s)", linha.fornecedor[:40], doc)
        try:
            empresa = linha_to_empresa(linha, w.empresas_parser.Empresa)
            await w.empresas_worker.criar_empresa(page, empresa, dry_run=dry_run)
            rec["empresa_criada"] = "dry_run" if dry_run else "criada"
            if not dry_run:
                cache_empresas.add(doc)
            log.info("  ✓ empresa %s", rec["empresa_criada"])
        except Exception as e:
            log.error("  ✗ erro criando empresa: %s", e)
            shot = await _screenshot_erro(page, screenshots_dir, numero_canon, "empresa")
            rec["status"] = "erro"
            rec["motivo"] = f"empresa: {type(e).__name__}: {str(e)[:200]}"
            rec["screenshot"] = shot
            return rec

    # -------- 2) CONTRATO --------
    # Chave composta: (numero, cnpj_ou_cpf). O mesmo número pode pertencer a
    # empresas diferentes no Panorama (acontece em órgãos públicos).
    chave = (numero_canon, doc)
    if skip_contrato:
        rec["contrato_criado"] = "skip"
    elif chave in cache_contratos:
        log.info("  ✓ contrato já existe (id=%s)", cache_contratos[chave])
        rec["contrato_criado"] = "ja_existia"
    else:
        log.info("  ▶ criando contrato: %s (empresa %s)", numero_canon, doc or "—")
        try:
            contrato = linha_to_contrato_excel(linha, w.contratos_parser.ContratoExcel)
            await w.contratos_worker.criar_contrato(page, contrato, dry_run=dry_run)
            rec["contrato_criado"] = "dry_run" if dry_run else "criado"
            log.info("  ✓ contrato %s", rec["contrato_criado"])
            # recarrega cache com (numero, cnpj) → id
            if not dry_run:
                novo = await listar_contratos_com_cnpj(page)
                cache_contratos.update(novo)
        except Exception as e:
            log.error("  ✗ erro criando contrato: %s", e)
            shot = await _screenshot_erro(page, screenshots_dir, numero_canon, "contrato")
            rec["status"] = "erro"
            rec["motivo"] = f"contrato: {type(e).__name__}: {str(e)[:200]}"
            rec["screenshot"] = shot
            # Recovery: às vezes salvou mesmo com timeout do detector
            try:
                novo = await listar_contratos_com_cnpj(page)
                if chave in novo:
                    log.warning("  ↷ contrato %s/%s ESTÁ no Panorama apesar do timeout",
                                numero_canon, doc)
                    rec["contrato_criado"] = "criado_apesar_timeout"
                    rec["status"] = ""
                    rec["motivo"] = ""
                    cache_contratos.update(novo)
                else:
                    return rec
            except Exception:
                return rec

    # -------- 3) ANEXAR PDF --------
    if skip_anexar:
        rec["pdf_anexado"] = "skip"
        rec["status"] = _decidir_status(rec)
        return rec

    if not linha.pdf_path:
        log.warning("  ⚠ sem pdf_path no CSV — não dá pra anexar")
        rec["pdf_anexado"] = "sem_pdf"
        rec["status"] = _decidir_status(rec)
        return rec
    if not Path(linha.pdf_path).exists():
        log.warning("  ⚠ PDF não existe no disco: %s", linha.pdf_path)
        rec["pdf_anexado"] = "pdf_nao_existe"
        rec["status"] = "erro"
        rec["motivo"] = f"PDF não está em {linha.pdf_path}"
        return rec

    contrato_id = cache_contratos.get((numero_canon, doc), "")
    if not contrato_id:
        log.warning("  ⚠ contrato (%s, %s) não está no cache — pulando anexo",
                    numero_canon, doc)
        rec["pdf_anexado"] = "sem_id"
        rec["status"] = _decidir_status(rec)
        return rec

    log.info("  ▶ anexando PDF: %s", Path(linha.pdf_path).name)
    try:
        contrato_pdf = linha_to_contrato_pdf(linha, w.anexar_parser.ContratoPDF)
        await w.anexar_worker.anexar_pdf_em_existente(
            page, contrato_pdf, contrato_id, dry_run=dry_run,
        )
        rec["pdf_anexado"] = "dry_run" if dry_run else "anexado"
        log.info("  ✓ pdf %s", rec["pdf_anexado"])
    except w.anexar_worker.PDFJaAnexado:
        log.info("  ↷ PDF já estava anexado nesse contrato — pulando")
        rec["pdf_anexado"] = "ja_anexado"
    except Exception as e:
        log.error("  ✗ erro anexando: %s", e)
        shot = await _screenshot_erro(page, screenshots_dir, numero_canon, "anexar")
        rec["status"] = "erro"
        rec["motivo"] = f"anexar: {type(e).__name__}: {str(e)[:200]}"
        rec["screenshot"] = shot
        return rec

    rec["status"] = _decidir_status(rec)
    return rec


def _decidir_status(rec: dict) -> str:
    """Decide o status final com base nos 3 passos."""
    if rec.get("status") == "erro":
        return "erro"
    emp = rec.get("empresa_criada", "")
    con = rec.get("contrato_criado", "")
    pdf = rec.get("pdf_anexado", "")
    if pdf == "dry_run" or emp == "dry_run" or con == "dry_run":
        return "dry_run"
    if pdf == "anexado" and con == "criado":
        return "criou_contrato_e_anexou"
    if pdf == "anexado" and emp == "criada":
        return "criou_empresa_e_anexou"
    if pdf == "anexado":
        return "anexado"
    if pdf == "ja_anexado":
        return "pdf_ja_anexado"
    return pdf or "incompleto"


# ---------------------------------------------------------------------------
# Orquestração geral
# ---------------------------------------------------------------------------

async def listar_contratos_com_cnpj(page: Page) -> dict[tuple[str, str], str]:
    """Lista todos os contratos do Panorama varrendo TODAS as páginas.
    Retorna dict[(numero, cnpj_ou_cpf), id_interno].

    Diferente do `anexar_worker.listar_contratos_existentes`, esse usa chave
    composta — permite distinguir contratos com mesmo número mas empresas
    diferentes (caso real: 00155/2023 da JG SERVICOS e da MIGUEL ELIAS).
    """
    contratos_url = os.environ.get("PANORAMA_CONTRATOS_URL",
                                   "https://panoramafiscal.com.br/svd/contratos")
    await page.goto(contratos_url, wait_until="networkidle", timeout=60000)
    await asyncio.sleep(1)

    # Set length=max
    try:
        sel = page.locator('select[name$="_length"]').first
        opcoes = await sel.locator("option").all_text_contents()
        valor = None
        for o in opcoes:
            if o.strip().lower() in ("all", "todos", "tudo"):
                valor = "-1"; break
        if not valor:
            nums = [int(o) for o in opcoes if o.strip().isdigit()]
            if nums:
                valor = str(max(nums))
        if valor:
            await sel.select_option(value=valor)
            await asyncio.sleep(1.5)
    except Exception:
        pass

    JS = r"""
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
                for (const a of tr.querySelectorAll('a[href]')) {
                    const m = (a.getAttribute('href') || '').match(/\/alterar\/(\d+)/);
                    if (m) { id = m[1]; break; }
                }
                return { cells, id };
            });
        }
    """

    out: dict[tuple[str, str], str] = {}
    while True:
        lote = await page.evaluate(JS)
        for rd in lote:
            cells = rd.get("cells") or []
            cid = rd.get("id") or ""
            # Procura número (NNNNN/AAAA) numa célula só
            numero = ""
            for c in cells:
                m = re.fullmatch(r"\s*(\d{1,6})\s*/\s*((?:19|20)\d{2})\s*", c or "")
                if m:
                    numero = f"{m.group(1).zfill(5)}/{m.group(2)}"
                    break
            # Extrai CNPJ/CPF de qualquer célula (a coluna empresa tem ambos)
            doc = ""
            for c in cells:
                m = re.search(r"(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})", c or "")
                if m:
                    doc = re.sub(r"\D", "", m.group(1)); break
                m = re.search(r"(\d{3}\.\d{3}\.\d{3}-\d{2})", c or "")
                if m:
                    doc = re.sub(r"\D", "", m.group(1)); break
            if numero:
                out[(numero, doc)] = cid

        # Próxima página
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
            info_antes = ""
            try:
                info_antes = (await page.locator(".dataTables_info").first.text_content()) or ""
            except Exception:
                pass
            await next_btn.click()
            try:
                await page.wait_for_function(
                    "(antes) => { const e = document.querySelector('.dataTables_info');"
                    " return e && e.textContent.trim() !== antes; }",
                    arg=info_antes.strip(), timeout=10000,
                )
            except Exception:
                await asyncio.sleep(1.0)
        except Exception:
            break

    return out


def _instalar_dialog_handler(page: Page) -> list[str]:
    """Aceita automaticamente confirm/alert do site (sem isso, Salvar é abortado).
    Retorna a lista que vai acumular as mensagens dos diálogos."""
    dialog_msgs: list[str] = []
    async def _on_dialog(d):
        dialog_msgs.append(d.message or "")
        log.info("   ⤷ diálogo [%s]: %r — aceitando", d.type, d.message)
        try:
            await d.accept()
        except Exception:
            pass
    page.on("dialog", lambda d: asyncio.create_task(_on_dialog(d)))
    page._svd_dialogs = dialog_msgs  # type: ignore[attr-defined]
    return dialog_msgs


async def _criar_pagina_logada(ctx: BrowserContext, w: WorkerBundle) -> Page:
    """Cria uma nova Page, instala dialog handler e faz login."""
    page = await ctx.new_page()
    _instalar_dialog_handler(page)
    await w.anexar_worker.ensure_logged_in(page)
    return page


async def processar_csv(
    ctx: BrowserContext,
    csv_path: Path,
    *,
    dry_run: bool = False,
    limit: Optional[int] = None,
    only: Optional[str] = None,
    skip_empresa: bool = False,
    skip_contrato: bool = False,
    skip_anexar: bool = False,
    screenshots_dir: Optional[Path] = None,
    parallel: int = 1,
) -> list[dict]:
    """Pipeline completo. Suporta paralelismo (N abas/workers).

    Caches (`cache_empresas`, `cache_contratos`) são compartilhados e protegidos
    por `asyncio.Lock` — double-check garante que 2 workers não criem a mesma
    empresa/contrato em paralelo.
    """
    if screenshots_dir is None:
        screenshots_dir = Path(__file__).parent / "out" / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    log.info("carregando workers das automações SVD...")
    w = carregar_workers()
    log.info("✓ workers carregados (empresas/contratos/anexar)")

    log.info("lendo CSV: %s", csv_path)
    linhas = ler_csv(csv_path, so_baixados=True)
    log.info("✓ %d linhas com status=baixado", len(linhas))

    if only:
        alvo = only.strip().lower()
        linhas = [l for l in linhas
                  if alvo in l.numero.lower() or alvo in l.fornecedor.lower()]
        log.info("--only %r → %d linhas", only, len(linhas))
    if limit:
        linhas = linhas[:limit]
        log.info("--limit %d → %d linhas", limit, len(linhas))

    if not linhas:
        log.warning("nada a fazer (0 linhas após filtros)")
        return []

    # Page principal pra login + listagem inicial
    page0 = await _criar_pagina_logada(ctx, w)

    log.info("listando empresas existentes no Panorama...")
    cache_empresas: set[str] = set()
    try:
        cache_empresas = await w.empresas_worker.listar_empresas_existentes(page0)
        log.info("✓ %d empresas em cache", len(cache_empresas))
    except Exception as e:
        log.warning("falha listando empresas: %s — continua sem cache", e)

    log.info("listando contratos existentes no Panorama (chave numero+cnpj)...")
    cache_contratos: dict[tuple[str, str], str] = {}
    try:
        cache_contratos = await listar_contratos_com_cnpj(page0)
        log.info("✓ %d contratos em cache (chaves únicas (numero, cnpj/cpf))",
                 len(cache_contratos))
    except Exception as e:
        log.warning("falha listando contratos: %s — continua sem cache", e)

    # Locks pra evitar race conditions na criação
    lock_empresa = asyncio.Lock()
    lock_contrato = asyncio.Lock()

    # Pool de N páginas (cada paralela tem a sua)
    log.info("criando %d página(s) paralela(s)...", parallel)
    pages: list[Page] = [page0]
    for _ in range(parallel - 1):
        p = await _criar_pagina_logada(ctx, w)
        pages.append(p)
    log.info("✓ %d páginas prontas", len(pages))

    # Cada worker concorrente pega uma página fixa (round-robin) via fila
    page_queue: asyncio.Queue[Page] = asyncio.Queue()
    for p in pages:
        await page_queue.put(p)

    resultados: list[dict] = [None] * len(linhas)  # type: ignore[assignment]
    total = len(linhas)

    async def _runner(idx: int, linha: LinhaCSV) -> None:
        page = await page_queue.get()
        try:
            rec = await _processar_linha_com_locks(
                page, linha, w, cache_empresas, cache_contratos,
                lock_empresa=lock_empresa, lock_contrato=lock_contrato,
                dry_run=dry_run,
                skip_empresa=skip_empresa,
                skip_contrato=skip_contrato,
                skip_anexar=skip_anexar,
                idx=idx, total=total,
                screenshots_dir=screenshots_dir,
            )
            resultados[idx - 1] = rec
        except Exception as e:
            log.error("erro fatal na linha %d (%s): %s", idx, linha.numero, e)
            log.debug("stack:\n%s", traceback.format_exc())
            resultados[idx - 1] = {
                "numero": linha.numero, "fornecedor": linha.fornecedor,
                "doc": linha.doc, "pdf_path": linha.pdf_path,
                "status": "erro", "motivo": f"fatal: {e}",
            }
        finally:
            await page_queue.put(page)

    # Dispara tudo em paralelo (limitado pelo número de páginas na fila)
    log.info("iniciando processamento (paralelo=%d, total=%d)…", parallel, total)
    await asyncio.gather(*[
        _runner(idx, linha) for idx, linha in enumerate(linhas, start=1)
    ])

    # Fecha as páginas extras (page0 fica)
    for p in pages[1:]:
        try:
            await p.close()
        except Exception:
            pass

    return resultados


async def _processar_linha_com_locks(
    page: Page,
    linha: LinhaCSV,
    w: WorkerBundle,
    cache_empresas: set[str],
    cache_contratos: dict[tuple[str, str], str],
    *,
    lock_empresa: asyncio.Lock,
    lock_contrato: asyncio.Lock,
    dry_run: bool,
    skip_empresa: bool,
    skip_contrato: bool,
    skip_anexar: bool,
    idx: int,
    total: int,
    screenshots_dir: Path,
) -> dict:
    """Wrapper de `processar_uma_linha` com locks pra evitar duplicação."""
    doc = _so_digitos(linha.doc)
    numero_canon = _normalizar_numero(linha.numero)

    # Antes de criar empresa: lock global + double-check no cache
    if not skip_empresa and doc and doc not in cache_empresas:
        async with lock_empresa:
            if doc not in cache_empresas:
                # Vai criar dentro do lock — depois o cache é atualizado pelo
                # processar_uma_linha. Outros workers que esperarem o lock vão
                # achar o doc no cache e pular pra "ja_existia".
                pass

    # Antes de criar contrato: idem
    if not skip_contrato and numero_canon and (numero_canon, doc) not in cache_contratos:
        async with lock_contrato:
            pass  # double-check ocorre no processar_uma_linha

    # Agora roda o fluxo normal — mas a verificação de cache lá dentro
    # já evita duplicação porque cache_empresas é atualizado in-place.
    return await processar_uma_linha(
        page, linha, w, cache_empresas, cache_contratos,
        dry_run=dry_run,
        skip_empresa=skip_empresa,
        skip_contrato=skip_contrato,
        skip_anexar=skip_anexar,
        idx=idx, total=total,
        screenshots_dir=screenshots_dir,
    )


# Substituí o loop sequencial pelo paralelo dentro de processar_csv (acima).
