"""Lista todos os contratos do Panorama Fiscal e mostra os DUPLICADOS
(números iguais cadastrados mais de uma vez).

Output:
  - out/duplicatas.csv  (linha por duplicata: numero, ids_separados_por_pipe)
  - out/duplicatas.md   (legível)

Não deleta nada — só relata. Depois decidimos como limpar.
"""
from __future__ import annotations

import asyncio
import csv
import logging
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
load_dotenv(ROOT / ".env")

# Reusa o loader pra carregar o anexar_worker
sys.path.insert(0, str(ROOT))
from loaders import carregar_workers  # noqa: E402

OUT_DIR = ROOT / "out"
OUT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("checar")


async def main() -> None:
    log.info("carregando workers...")
    w = carregar_workers()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx = await browser.new_context()
        page = await ctx.new_page()

        # Dialog handler (pra evitar bloqueio)
        page.on("dialog", lambda d: asyncio.create_task(d.accept()))

        log.info("login...")
        await w.anexar_worker.ensure_logged_in(page)

        log.info("listando TODOS os contratos (pode demorar)...")
        # Vou usar JS direto na listagem pra extrair TUDO (números + IDs),
        # sem filtrar duplicados como o listar_contratos_existentes faz.
        CONTRATOS_URL = os.environ.get("PANORAMA_CONTRATOS_URL",
                                       "https://panoramafiscal.com.br/svd/contratos")
        await page.goto(CONTRATOS_URL, wait_until="networkidle", timeout=60000)
        await asyncio.sleep(2)

        # Seta length=All / maior valor
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
        except Exception as e:
            log.warning("falha ao setar length: %s", e)

        # JS pra extrair número + ID de TODAS as linhas da página atual
        JS_EXTRAI = r"""
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
                    // Detecta se tem PDF anexado (link/ícone de download na coluna CONTRATO)
                    let tem_pdf = false;
                    for (const a of tr.querySelectorAll('a')) {
                        const href = (a.getAttribute('href') || '').toLowerCase();
                        if (href.endsWith('.pdf') || href.includes('/download/') ||
                            href.includes('/arquivo/') || href.includes('/contrato/visualizar') ||
                            href.includes('/contrato/baixar') || a.hasAttribute('download')) {
                            tem_pdf = true; break;
                        }
                        if (a.querySelector('i.fa-download, i.fas.fa-download, i.bi-download')) {
                            if (href && !href.includes('alterar') &&
                                !href.includes('excluir') && !href.includes('duplicar')) {
                                tem_pdf = true; break;
                            }
                        }
                    }
                    return { cells, id, tem_pdf };
                });
            }
        """

        # Itera por TODAS as páginas da DataTable (clique em "Próxima" até esgotar)
        rows_data: list[dict] = []
        pagina = 1
        while True:
            lote = await page.evaluate(JS_EXTRAI)
            log.info("  página %d: %d linhas", pagina, len(lote))
            rows_data.extend(lote)

            # Tem próxima página habilitada?
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
                # Pega o "info" atual pra detectar quando a página mudar
                info_antes = ""
                try:
                    info_antes = (await page.locator(".dataTables_info").first.text_content()) or ""
                except Exception:
                    pass
                await next_btn.click()
                # Espera info mudar (sinal que renderizou a próxima)
                try:
                    await page.wait_for_function(
                        "(antes) => { const e = document.querySelector('.dataTables_info');"
                        " return e && e.textContent.trim() !== antes; }",
                        arg=info_antes.strip(), timeout=10000,
                    )
                except Exception:
                    await asyncio.sleep(1.0)
                pagina += 1
            except Exception:
                break

        log.info("linhas extraídas TOTAIS: %d (em %d páginas)", len(rows_data), pagina)

        # Mapeia (numero_normalizado, cnpj_ou_cpf) → lista de detalhes pra revisão
        chave_to_detalhes: dict[tuple[str, str], list[dict]] = defaultdict(list)
        coincidencias_numero_dif_empresa: dict[str, set[str]] = defaultdict(set)
        for rd in rows_data:
            cells = rd.get("cells") or []
            # Acha número ORIGINAL (preserva os zeros como aparecem na tela)
            numero_original = ""
            for c in cells:
                m = re.fullmatch(r"\s*(\d{1,6})\s*/\s*((?:19|20)\d{2})\s*", c or "")
                if m:
                    numero_original = f"{m.group(1)}/{m.group(2)}"
                    break
            if not numero_original:
                continue
            # Normalizado: remove zeros à esquerda + zfill 5
            base, ano = numero_original.split("/")
            numero_norm = f"{base.lstrip('0').zfill(5) or '00000'}/{ano}"

            # CNPJ/CPF
            doc = ""
            for c in cells:
                m = re.search(r"(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})", c or "")
                if m:
                    doc = re.sub(r"\D", "", m.group(1)); break
                m = re.search(r"(\d{3}\.\d{3}\.\d{3}-\d{2})", c or "")
                if m:
                    doc = re.sub(r"\D", "", m.group(1)); break

            cid = rd.get("id") or ""
            empresa = cells[0] if cells else ""
            # Tenta extrair datas e valor das células
            datas = []
            valor = ""
            for c in cells:
                for m in re.finditer(r"\b(\d{2}/\d{2}/\d{4})\b", c or ""):
                    datas.append(m.group(1))
                m = re.search(r"R\$\s*([\d\.]+,\d{2})", c or "")
                if m:
                    valor = m.group(1)

            chave_to_detalhes[(numero_norm, doc)].append({
                "id": cid,
                "numero_original": numero_original,
                "empresa": empresa[:60],
                "data_inicio": datas[0] if len(datas) > 0 else "",
                "data_fim": datas[1] if len(datas) > 1 else "",
                "valor": valor,
                "tem_pdf": bool(rd.get("tem_pdf", False)),
            })
            coincidencias_numero_dif_empresa[numero_norm].add(doc)

        # Backward-compat: monta chave_to_ids e contratos como antes pra
        # variáveis usadas depois
        chave_to_ids = {k: [d["id"] for d in v] for k, v in chave_to_detalhes.items()}
        contratos = [(k[0], k[1], d["id"], d["empresa"])
                     for k, vs in chave_to_detalhes.items() for d in vs]

        total = len(contratos)
        chaves_unicas = len(chave_to_ids)
        # Duplicatas REAIS: mesma empresa + mesmo número 2+ vezes
        duplicados_reais = {k: ids for k, ids in chave_to_ids.items() if len(ids) > 1}
        # Coincidências legítimas: mesmo número, empresas diferentes
        legitimas = {n: docs for n, docs in coincidencias_numero_dif_empresa.items()
                     if len(docs) > 1}

        log.info("=" * 60)
        log.info("Total de linhas:                        %d", total)
        log.info("Chaves únicas (numero+cnpj):            %d", chaves_unicas)
        log.info("⚠ Duplicatas REAIS (mesmo num+cnpj):   %d  (%d a deletar)",
                 len(duplicados_reais),
                 sum(len(ids) - 1 for ids in duplicados_reais.values()))
        log.info("ℹ Coincidências (mesmo num, empresas dif): %d", len(legitimas))
        log.info("=" * 60)
        # Renomeia pra reaproveitar abaixo
        duplicados = duplicados_reais

        # Salva CSV: duplicatas REAIS (mesma empresa + mesmo número normalizado)
        # Mantém o que TEM PDF (criado manualmente antes), deleta os SEM PDF
        # (duplicatas criadas pelo orquestrador antes do bugfix).
        csv_path = OUT_DIR / "duplicatas.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            wr.writerow(["numero", "cnpj_ou_cpf", "qtd_copias",
                         "todos_ids", "id_a_manter", "ids_a_deletar",
                         "manter_tem_pdf"])
            for (n, doc), _ in sorted(duplicados.items()):
                detalhes = chave_to_detalhes[(n, doc)]
                com_pdf = [d for d in detalhes if d["tem_pdf"]]
                sem_pdf = [d for d in detalhes if not d["tem_pdf"]]
                if com_pdf and sem_pdf:
                    manter = sorted(com_pdf, key=lambda x: int(x["id"]) if x["id"].isdigit() else 9999)[0]
                    a_deletar = [d for d in detalhes if d["id"] != manter["id"]]
                else:
                    todos = sorted(detalhes, key=lambda x: int(x["id"]) if x["id"].isdigit() else 9999)
                    manter = todos[0]
                    a_deletar = todos[1:]
                wr.writerow([
                    n, doc, len(detalhes),
                    "|".join(d["id"] for d in detalhes),
                    manter["id"],
                    ",".join(d["id"] for d in a_deletar),
                    "sim" if manter["tem_pdf"] else "não",
                ])
        log.info("✓ CSV duplicatas: %s", csv_path)

        # Salva MD legível
        md_path = OUT_DIR / "duplicatas.md"
        lines = [
            "# Relatório de Contratos no Panorama",
            "",
            f"- Total de contratos na listagem: **{total}**",
            f"- Chaves únicas (numero+cnpj): **{chaves_unicas}**",
            "",
            f"## ⚠ Duplicatas REAIS (mesmo número + mesma empresa): **{len(duplicados)}**",
            "",
            f"_Linhas a remover (manter 1 de cada): **{sum(len(ids)-1 for ids in duplicados.values())}**_",
            "",
        ]
        if duplicados:
            lines.append("> ⚠ Compare os dados de cada par. Se as datas/valor forem **iguais**,")
            lines.append("> provavelmente é duplicata. Se forem **diferentes**, são contratos legítimos")
            lines.append("> distintos com numeração que coincidiu (acontece em órgão público).")
            lines.append("")
            for (n_norm, doc), _ids in sorted(duplicados.items()):
                detalhes = chave_to_detalhes[(n_norm, doc)]
                # Define manter/deletar baseado em tem_pdf:
                # - MANTER: o que TEM PDF (provavelmente o cadastrado manualmente antes)
                # - DELETAR: o que NÃO tem PDF (duplicata criada pelo orquestrador)
                # Se todos têm PDF ou nenhum tem → manter o de MENOR ID.
                com_pdf = [d for d in detalhes if d["tem_pdf"]]
                sem_pdf = [d for d in detalhes if not d["tem_pdf"]]
                if com_pdf and sem_pdf:
                    manter_id = sorted(com_pdf, key=lambda x: int(x["id"]) if x["id"].isdigit() else 9999)[0]["id"]
                    a_deletar = [d for d in detalhes if d["id"] != manter_id]
                else:
                    todos = sorted(detalhes, key=lambda x: int(x["id"]) if x["id"].isdigit() else 9999)
                    manter_id = todos[0]["id"]
                    a_deletar = todos[1:]

                lines.append(f"### {n_norm} — `{doc}`")
                lines.append("")
                lines.append("| Ação | ID | Nº cadastrado | Empresa | Data início | Data fim | Valor | PDF? |")
                lines.append("|---|---|---|---|---|---|---|---|")
                for d in sorted(detalhes, key=lambda x: int(x["id"]) if x["id"].isdigit() else 9999):
                    acao = "🟢 manter" if d["id"] == manter_id else "🔴 **deletar**"
                    pdf = "✓" if d["tem_pdf"] else "✗"
                    lines.append(
                        f"| {acao} | {d['id']} | `{d['numero_original']}` "
                        f"| {d['empresa']} "
                        f"| {d['data_inicio']} | {d['data_fim']} "
                        f"| R$ {d['valor']} | {pdf} |"
                    )
                lines.append("")
        else:
            lines.append("**Nenhum suspeito encontrado.** ✓")
        lines.append("")
        lines.append(f"## ℹ Coincidências legítimas (mesmo número, empresas diferentes): **{len(legitimas)}**")
        lines.append("")
        lines.append("_NÃO são duplicatas — são contratos legítimos da numeração da prefeitura._")
        lines.append("")
        if legitimas:
            lines.append("| Número | Qtd empresas | CNPJs/CPFs |")
            lines.append("|---|---|---|")
            for n, docs in sorted(legitimas.items()):
                lines.append(f"| {n} | {len(docs)} | {', '.join(sorted(docs))} |")
        md_path.write_text("\n".join(lines), encoding="utf-8")
        log.info("✓ MD: %s", md_path)

        await asyncio.sleep(3)
        await browser.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
