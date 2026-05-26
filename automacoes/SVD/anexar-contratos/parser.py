"""Extração de dados de PDFs de contrato.

Funções puras — sem Playwright, sem I/O fora da leitura do PDF.
Cada extrator tem fallback regex pra tentar pegar o campo de várias formas.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pdfplumber

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class ContratoPDF:
    """Dados extraídos de um único PDF de contrato."""
    arquivo: Path
    numero: Optional[str] = None              # ex: "00070/2026"
    cnpj: Optional[str] = None                # 14 dígitos, sem formatação
    empresa: Optional[str] = None             # razão social (texto livre)
    data_inicio: Optional[date] = None
    data_fim: Optional[date] = None
    valor: Optional[float] = None             # em reais
    objeto: Optional[str] = None              # texto livre do objeto
    texto_bruto: str = field(default="", repr=False)  # PDF inteiro, debug

    @property
    def numero_normalizado(self) -> str:
        """Forma canônica do número p/ comparar com a listagem.
        Garante o formato NNNNN/AAAA (ex: 00070/2026)."""
        if not self.numero:
            return ""
        n = self.numero.replace("-", "/").strip()
        # remove zeros à esquerda? Não — mantém como o sistema mostra
        return n

    def is_valid(self) -> bool:
        """Mínimo necessário pra processar (default: anexar em contrato existente).
        Só o número é obrigatório — a data já está cadastrada no contrato."""
        return bool(self.numero)

    def is_valid_para_criar(self) -> bool:
        """Mais campos obrigatórios pra CRIAR um contrato novo do zero."""
        return bool(self.numero and self.data_fim)


# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------

# Número do contrato — várias formas que aparecem em órgãos públicos:
#   "Contrato Nº 00070/2026", "CONTRATO N° 070/2026", "Contrato n. 70-2026"
_RE_NUMERO = re.compile(
    r"(?:contrato|contr\.?)\s*(?:n[º°o\.]*\s*)?[:\-]?\s*"
    r"(\d{1,6})\s*[/\-]\s*(\d{4})",
    re.IGNORECASE,
)

# CNPJ — 14 dígitos com ou sem máscara
_RE_CNPJ = re.compile(
    r"(\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2})"
)

# Data — dd/mm/aaaa ou dd-mm-aaaa
_RE_DATA = re.compile(r"(\d{2})[/\-](\d{2})[/\-](\d{4})")

# Valor — "R$ 1.268.400,00" ou "R$ 30.000,00"
_RE_VALOR = re.compile(
    r"R\$\s*([\d\.]+,\d{2})",
    re.IGNORECASE,
)

# Datas com contexto
_RE_DATA_INICIO = re.compile(
    r"(?:data\s+(?:de\s+)?in[íi]cio|in[íi]cio\s+(?:da\s+)?vig[êe]ncia|"
    r"vig[êe]ncia\s*[:\-]?\s*de)\s*[:\-]?\s*(\d{2}[/\-]\d{2}[/\-]\d{4})",
    re.IGNORECASE,
)
_RE_DATA_FIM = re.compile(
    r"(?:data\s+(?:de\s+)?(?:fim|t[ée]rmino)|t[ée]rmino\s+(?:da\s+)?vig[êe]ncia|"
    r"vig[êe]ncia\s*[:\-]?\s*at[ée])\s*[:\-]?\s*(\d{2}[/\-]\d{2}[/\-]\d{4})",
    re.IGNORECASE,
)

# Empresa — em geral aparece como "CONTRATADA: NOME LTDA"
_RE_EMPRESA = re.compile(
    r"(?:contratada|fornecedor|empresa)\s*[:\-]\s*([A-Z0-9 \.,&\-/]+(?:LTDA|EIRELI|S/A|S\.A\.|ME|EPP)?)",
    re.IGNORECASE,
)

# Objeto — "OBJETO: ..." até o próximo cabeçalho em CAIXA ALTA / quebra dupla
_RE_OBJETO = re.compile(
    r"objeto\s*[:\-]\s*(.{20,500}?)(?:\n\s*\n|\n[A-ZÁÉÍÓÚÂÊÔÃÕÇ ]{4,}:)",
    re.IGNORECASE | re.DOTALL,
)


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------

def normalize_cnpj(raw: str) -> str:
    """Remove tudo que não é dígito. '22.147.251/0001-36' -> '22147251000136'."""
    return re.sub(r"\D", "", raw)


def parse_data_br(raw: str) -> Optional[date]:
    """Tenta dd/mm/aaaa e dd-mm-aaaa."""
    for fmt in ("%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None


def parse_valor_br(raw: str) -> Optional[float]:
    """'1.268.400,00' -> 1268400.0"""
    try:
        return float(raw.replace(".", "").replace(",", "."))
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Extractors (cada um pega 1 campo, com fallback)
# ---------------------------------------------------------------------------

def extract_numero_contrato(texto: str, arquivo: Path) -> Optional[str]:
    """Pega o número do contrato. PRIORIDADE: nome do arquivo.

    O nome ('CONTRATO 00070-2026.pdf') é a fonte confiável — foi quem organizou
    os PDFs que o nomeou com o número do contrato. O TEXTO do PDF cita vários
    números no formato NNNNN/AAAA (processo, contratos relacionados, empenho...)
    e o regex pegaria o primeiro, que NÃO é o número do contrato — isso casava o
    PDF com o contrato errado. Por isso o texto é só último recurso.
    """
    # 1) nome do arquivo — "CONTRATO 00070-2026" / "0048-2026"
    nome = arquivo.stem
    m = re.search(r"(\d{1,6})\s*[\-/]\s*(\d{4})", nome)
    if m:
        return f"{m.group(1).zfill(5)}/{m.group(2)}"

    # 2) fallback: texto do PDF (com contexto "Contrato Nº ...")
    m = _RE_NUMERO.search(texto)
    if m:
        return f"{m.group(1).zfill(5)}/{m.group(2)}"

    return None


def extract_cnpj(texto: str) -> Optional[str]:
    m = _RE_CNPJ.search(texto)
    if not m:
        return None
    cnpj = normalize_cnpj(m.group(1))
    return cnpj if len(cnpj) == 14 else None


def extract_empresa(texto: str) -> Optional[str]:
    m = _RE_EMPRESA.search(texto)
    if m:
        # Limita tamanho — às vezes pega texto demais
        nome = m.group(1).strip()
        return nome[:100] if nome else None
    return None


def extract_datas(texto: str) -> tuple[Optional[date], Optional[date]]:
    """Retorna (inicio, fim). Se não achar com contexto, usa as 2 primeiras
    datas que aparecerem como heurística."""
    inicio = fim = None

    m = _RE_DATA_INICIO.search(texto)
    if m:
        inicio = parse_data_br(m.group(1))
    m = _RE_DATA_FIM.search(texto)
    if m:
        fim = parse_data_br(m.group(1))

    if inicio and fim:
        return inicio, fim

    # Heurística: pega todas as datas, assume primeira=início, última=fim
    datas = [parse_data_br(f"{d[0]}/{d[1]}/{d[2]}") for d in _RE_DATA.findall(texto)]
    datas = [d for d in datas if d is not None]
    if datas:
        if not inicio:
            inicio = datas[0]
        if not fim:
            fim = max(datas)

    return inicio, fim


def extract_valor(texto: str) -> Optional[float]:
    """Pega o maior valor monetário do PDF (geralmente é o do contrato)."""
    valores = [parse_valor_br(v) for v in _RE_VALOR.findall(texto)]
    valores = [v for v in valores if v is not None]
    return max(valores) if valores else None


def extract_objeto(texto: str) -> Optional[str]:
    m = _RE_OBJETO.search(texto)
    if m:
        obj = re.sub(r"\s+", " ", m.group(1)).strip()
        return obj[:500] if obj else None
    return None


# ---------------------------------------------------------------------------
# Entrypoint principal
# ---------------------------------------------------------------------------

def parse_pdf(arquivo: Path) -> ContratoPDF:
    """Lê um PDF e devolve um ContratoPDF com tudo que conseguiu extrair."""
    log.debug("lendo PDF: %s", arquivo.name)

    texto = ""
    try:
        with pdfplumber.open(arquivo) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                texto += page_text + "\n"
    except Exception as exc:
        log.error("falha lendo %s: %s", arquivo.name, exc)
        return ContratoPDF(arquivo=arquivo)

    inicio, fim = extract_datas(texto)
    return ContratoPDF(
        arquivo=arquivo,
        numero=extract_numero_contrato(texto, arquivo),
        cnpj=extract_cnpj(texto),
        empresa=extract_empresa(texto),
        data_inicio=inicio,
        data_fim=fim,
        valor=extract_valor(texto),
        objeto=extract_objeto(texto),
        texto_bruto=texto,
    )


def parse_diretorio(diretorio: Path) -> list[ContratoPDF]:
    """Processa todos os PDFs da pasta, em ordem alfabética."""
    if not diretorio.is_dir():
        raise FileNotFoundError(f"diretório não existe: {diretorio}")
    pdfs = sorted(diretorio.glob("*.pdf"))
    log.info("encontrou %d PDFs em %s", len(pdfs), diretorio)
    return [parse_pdf(p) for p in pdfs]
