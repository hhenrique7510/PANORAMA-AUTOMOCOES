"""Funções puras — parse de linhas da listagem do portal de transparência.

Sem I/O, sem Playwright. Recebe dicts/strings já extraídas do HTML e devolve
ContratoTransparencia estruturado. Fácil de unit-test.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class ContratoTransparencia:
    """Um contrato extraído do portal Bayeux."""
    # Identificação
    contrato_id: str = ""                # ID interno do portal (.../detalhamento-de-contrato/{id})
    numero: str = ""                     # NNNNN/AAAA — canônico
    ano: str = ""

    # Licitação
    licitacao: str = ""                  # ex: '30/2026'
    licitacao_url: str = ""              # link da licitação

    # Fornecedor
    fornecedor_nome: str = ""
    cnpj: Optional[str] = None           # 14 dígitos sem máscara
    cpf: Optional[str] = None            # 11 dígitos sem máscara
    fiscal: str = ""

    # Contrato
    objeto: str = ""
    data_inicio: Optional[date] = None
    data_fim: Optional[date] = None
    valor: Optional[float] = None

    # URLs
    detalhe_url: str = ""                # /detalhamento-de-contrato/{id}
    pdf_url: str = ""                    # URL absoluta do PDF (preenchida depois)
    pdf_local_path: str = ""             # caminho local após download

    @property
    def doc_pessoa(self) -> str:
        return self.cnpj or self.cpf or ""

    @property
    def is_complete(self) -> bool:
        return bool(self.numero and self.fornecedor_nome and self.detalhe_url)


# ---------------------------------------------------------------------------
# Normalizadores
# ---------------------------------------------------------------------------

def _strip_tags(html: str) -> str:
    """Tira tags HTML e normaliza espaços. Mantém quebras de linha como '|'."""
    s = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", "", s)
    # decode entities básicos
    s = (s.replace("&amp;", "&").replace("&lt;", "<")
           .replace("&gt;", ">").replace("&nbsp;", " ")
           .replace("&quot;", '"').replace("&#39;", "'"))
    return s


def _norm_text(s: str) -> str:
    """Trim + colapsa whitespace."""
    return re.sub(r"\s+", " ", s or "").strip()


def normalizar_numero(num: str, ano: str = "") -> str:
    """'189' + '2026' -> '00189/2026'. '00189' + '2026' -> '00189/2026'."""
    digits = re.sub(r"\D", "", str(num or ""))
    if not digits:
        return ""
    ano_d = re.sub(r"\D", "", str(ano or ""))
    if ano_d:
        return f"{digits.lstrip('0').zfill(5) or '00000'}/{ano_d}"
    # tenta extrair do próprio num
    m = re.search(r"(\d{1,6})\s*[/\-]\s*(\d{4})", str(num or ""))
    if m:
        return f"{m.group(1).lstrip('0').zfill(5) or '00000'}/{m.group(2)}"
    return digits.lstrip('0').zfill(5) or '00000'


def _so_digitos(s: str) -> str:
    return re.sub(r"\D", "", s or "")


_RE_CNPJ = re.compile(r"\b(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})\b")
_RE_CPF = re.compile(r"\b(\d{3}\.\d{3}\.\d{3}-\d{2})\b")


def parse_cnpj_cpf(texto: str) -> tuple[Optional[str], Optional[str]]:
    """Extrai (cnpj, cpf) — só dígitos. Aceita só o primeiro match de cada."""
    cnpj = cpf = None
    m = _RE_CNPJ.search(texto or "")
    if m:
        d = _so_digitos(m.group(1))
        if len(d) == 14:
            cnpj = d
    if not cnpj:
        m = _RE_CPF.search(texto or "")
        if m:
            d = _so_digitos(m.group(1))
            if len(d) == 11:
                cpf = d
    return cnpj, cpf


_RE_DATA = re.compile(r"(\d{2})/(\d{2})/(\d{4})")


def parse_vigencia(s: str) -> tuple[Optional[date], Optional[date]]:
    """'14/05/2026 a 31/12/2026' -> (date(2026,5,14), date(2026,12,31))."""
    datas = _RE_DATA.findall(s or "")
    out = []
    for d, m, a in datas:
        try:
            out.append(date(int(a), int(m), int(d)))
        except ValueError:
            continue
    if len(out) >= 2:
        return out[0], out[1]
    if len(out) == 1:
        return out[0], None
    return None, None


def parse_valor_br(s: str) -> Optional[float]:
    """'R$ 420.000,00' -> 420000.0"""
    if not s:
        return None
    s = s.strip().replace("R$", "").replace(" ", "")
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Parse de uma linha da listagem (a partir do HTML do <tr>)
# ---------------------------------------------------------------------------

def parse_fornecedor_celula(html_celula: str) -> tuple[str, Optional[str], Optional[str], str]:
    """A célula 'Fornecedor' vem assim:

        NOME DA EMPRESA<br>00.000.000/0000-00<br><small><strong>Fiscal do Contrato</strong>: <br>NOME DO FISCAL ()</small>

    Retorna (nome, cnpj, cpf, fiscal).
    """
    texto = _strip_tags(html_celula)
    linhas = [l.strip() for l in texto.split("\n") if l.strip()]
    nome = linhas[0] if linhas else ""

    cnpj, cpf = parse_cnpj_cpf(texto)

    # Fiscal: linha após "Fiscal do Contrato:"
    fiscal = ""
    for i, l in enumerate(linhas):
        if "Fiscal do Contrato" in l:
            # pega a linha seguinte
            if i + 1 < len(linhas):
                fiscal = re.sub(r"\s*\(.*?\)\s*$", "", linhas[i + 1]).strip()
            else:
                # pode estar na mesma linha, depois dos dois pontos
                m = re.search(r"Fiscal do Contrato\s*:\s*(.+)", l)
                if m:
                    fiscal = re.sub(r"\s*\(.*?\)\s*$", "", m.group(1)).strip()
            break

    return nome, cnpj, cpf, fiscal


def parse_row_html(tr_html: str) -> Optional[ContratoTransparencia]:
    """Recebe o HTML de UM `<tr>` da listagem e retorna ContratoTransparencia.
    Retorna None se a linha não tiver o link de detalhamento."""
    # Extrai células
    cells = re.findall(r"<td[^>]*>(.*?)</td>", tr_html, flags=re.DOTALL)
    if len(cells) < 9:
        return None

    # Mapeamento baseado no thead observado:
    # [0] hidden (número numérico pra sort)
    # [1] Ano
    # [2] Número
    # [3] Licitação (link)
    # [4] Fornecedor + CNPJ + Fiscal
    # [5] Objeto
    # [6] Vigência
    # [7] Valor
    # [8] Opção (link de detalhamento)

    ano = _norm_text(_strip_tags(cells[1]))
    numero_raw = _norm_text(_strip_tags(cells[2]))
    numero = normalizar_numero(numero_raw, ano)

    # Licitação — link e texto
    licitacao = _norm_text(_strip_tags(cells[3]))
    licitacao_url = ""
    m = re.search(r'href="([^"]+licitacao[^"]+)"', cells[3])
    if m:
        licitacao_url = m.group(1)

    # Fornecedor
    nome, cnpj, cpf, fiscal = parse_fornecedor_celula(cells[4])

    objeto = _norm_text(_strip_tags(cells[5]))

    # Vigência
    vig_txt = _norm_text(_strip_tags(cells[6]))
    di, df = parse_vigencia(vig_txt)

    # Valor
    valor = parse_valor_br(_norm_text(_strip_tags(cells[7])))

    # Link de detalhamento → ID
    detalhe_url = ""
    contrato_id = ""
    m = re.search(r'href="([^"]+/detalhamento-de-contrato/(\d+))"', cells[8])
    if m:
        detalhe_url = m.group(1)
        contrato_id = m.group(2)

    if not detalhe_url:
        return None

    return ContratoTransparencia(
        contrato_id=contrato_id,
        numero=numero,
        ano=ano,
        licitacao=licitacao,
        licitacao_url=licitacao_url,
        fornecedor_nome=nome,
        cnpj=cnpj,
        cpf=cpf,
        fiscal=fiscal,
        objeto=objeto,
        data_inicio=di,
        data_fim=df,
        valor=valor,
        detalhe_url=detalhe_url,
    )


def parse_listagem_html(html: str) -> list[ContratoTransparencia]:
    """Recebe o HTML inteiro da página de listagem e devolve a lista de contratos."""
    contratos = []
    # Acha o tbody principal
    m = re.search(r"<tbody[^>]*>(.*?)</tbody>", html, flags=re.DOTALL)
    if not m:
        return contratos
    tbody = m.group(1)
    for tr_html in re.findall(r"<tr[^>]*>.*?</tr>", tbody, flags=re.DOTALL):
        c = parse_row_html(tr_html)
        if c:
            contratos.append(c)
    return contratos


# ---------------------------------------------------------------------------
# Parse da página de detalhamento → link do PDF
# ---------------------------------------------------------------------------

def extrair_link_pdf(html_detalhamento: str) -> Optional[str]:
    """Procura na página de detalhamento o link 'Download do contrato' apontando
    pra um .pdf nos uploads."""
    # 1) prioriza href que aponta pra /uploads/.../.pdf
    m = re.search(
        r'href="(https?://[^"]*/uploads/[^"]+\.pdf)"',
        html_detalhamento, re.IGNORECASE,
    )
    if m:
        return m.group(1)
    # 2) qualquer href .pdf
    m = re.search(r'href="([^"]+\.pdf)"', html_detalhamento, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


# ---------------------------------------------------------------------------
# Filename sanitizer
# ---------------------------------------------------------------------------

def slug_filename(numero: str, fornecedor: str, ano: str) -> str:
    """Gera nome de arquivo legível: 'CONTRATO 00189-2026 - ODONTOMASTER.pdf'."""
    # número sem barra
    num_safe = numero.replace("/", "-") if numero else f"sem-numero-{ano}"
    # fornecedor: primeiras 4-5 palavras, sem acento, sem caractere especial
    nome = unicodedata.normalize("NFKD", fornecedor or "").encode("ascii", "ignore").decode()
    nome = re.sub(r"[^A-Za-z0-9 ]", " ", nome)
    nome = " ".join(nome.split()[:5])
    return f"CONTRATO {num_safe} - {nome}.pdf" if nome else f"CONTRATO {num_safe}.pdf"
