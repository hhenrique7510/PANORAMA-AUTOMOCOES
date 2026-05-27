"""Leitura e parsing do Excel com a lista mestre de contratos.

Lê a planilha, filtra linhas com status="ADICIONAR" e extrai todos os campos
necessários pra criar o contrato no Panorama Fiscal. Tudo aqui é função pura,
sem I/O com browser.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class ContratoExcel:
    """Uma linha da planilha que vamos cadastrar."""
    numero: str                       # ex: '00080/2025'
    tipo: str = "CONTRATO"            # ex: 'CONTRATO', 'ARP'
    status: str = ""                  # 'OK' ou 'ADICIONAR' (vindo da planilha)
    empresa_raw: str = ""             # 'NOME, CNPJ: 09.308.693/0001-59'
    empresa_nome: str = ""            # só o nome
    cnpj: Optional[str] = None        # 14 dígitos, sem máscara (se for PJ)
    cpf: Optional[str] = None         # 11 dígitos, sem máscara (se for PF)
    data_inicio: Optional[date] = None
    data_fim: Optional[date] = None
    valor: Optional[float] = None
    objeto: str = ""

    @property
    def numero_normalizado(self) -> str:
        """Forma canônica do número (NNNNN/AAAA)."""
        return _normalizar_numero(self.numero)

    @property
    def doc_pessoa(self) -> str:
        """Retorna CNPJ ou CPF normalizado (só dígitos) — o que tiver."""
        return self.cnpj or self.cpf or ""

    def is_valid_para_criar(self) -> bool:
        """Mínimo necessário pra criar: número e data_fim."""
        return bool(self.numero and self.data_fim)


# ---------------------------------------------------------------------------
# Normalizadores
# ---------------------------------------------------------------------------

_RE_CNPJ = re.compile(r"\b(\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2})\b")
_RE_CPF = re.compile(r"\b(\d{3}\.?\d{3}\.?\d{3}-?\d{2})\b")


def _normalizar_numero(raw: str) -> str:
    """'70/2026' | '00070/2026' | '70-2026' -> '00070/2026'."""
    m = re.search(r"(\d{1,6})\s*[/\-]\s*(\d{4})", str(raw or ""))
    if not m:
        return ""
    return f"{m.group(1).zfill(5)}/{m.group(2)}"


def _so_digitos(raw: str) -> str:
    return re.sub(r"\D", "", str(raw or ""))


def parse_empresa(raw: str) -> tuple[str, Optional[str], Optional[str]]:
    """Decompõe 'NOME, CNPJ/CPF: XX.XXX.XXX/XXXX-XX' em (nome, cnpj, cpf).

    Aceita várias variações:
    - 'EMPRESA LTDA, CNPJ: 13.099.984/0001-51'
    - 'EMPRESA LTDA - CNPJ: 13.099.984/0001-51'
    - 'ANA SILVA, CPF: 044.083.954-80'
    - '60.690.678 SMITH MASAK - CNPJ: 60.690.678/0001-XX'  (nome começa com CNPJ-like!)
    """
    s = str(raw or "").strip()

    # PRIMEIRO tenta achar o CNPJ explícito (com "CNPJ:" antes ou depois)
    cnpj = None
    m = re.search(r"CNPJ[:\s\.]*?\s*(\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2})", s, re.IGNORECASE)
    if m:
        cnpj = _so_digitos(m.group(1))
        if len(cnpj) != 14:
            cnpj = None

    cpf = None
    if not cnpj:
        m = re.search(r"CPF[:\s\.Nº°]*?\s*(\d{3}\.?\d{3}\.?\d{3}-?\d{2})", s, re.IGNORECASE)
        if m:
            cpf = _so_digitos(m.group(1))
            if len(cpf) != 11:
                cpf = None

    # Fallback: pega qualquer CNPJ/CPF que aparecer
    if not cnpj and not cpf:
        m = _RE_CNPJ.search(s)
        if m:
            cand = _so_digitos(m.group(1))
            if len(cand) == 14:
                cnpj = cand
        if not cnpj:
            m = _RE_CPF.search(s)
            if m:
                cand = _so_digitos(m.group(1))
                if len(cand) == 11:
                    cpf = cand

    # Nome: tudo antes da primeira vírgula / hífen / "CNPJ:" / "CPF:"
    nome = s
    # Corta nas marcas comuns
    for marca in [",", " - CNPJ", " – CNPJ", " - CPF", " – CPF", "CNPJ:", "CPF:"]:
        idx = nome.upper().find(marca.upper())
        if idx > 0:
            nome = nome[:idx]
    nome = nome.strip(" ,;-–")

    return nome, cnpj, cpf


def parse_data(raw) -> Optional[date]:
    """Aceita string ('2025-03-04'), datetime, ou pd.Timestamp."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    if isinstance(raw, (datetime, pd.Timestamp)):
        return raw.date() if hasattr(raw, "date") else raw
    if isinstance(raw, date):
        return raw
    s = str(raw).strip()
    if not s or s.lower() in ("nan", "nat", "none"):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s.split(".")[0], fmt).date()
        except ValueError:
            continue
    log.warning("não consegui parsear data: %r", raw)
    return None


def parse_valor(raw) -> Optional[float]:
    """Aceita float, int, ou string com vírgula/ponto."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip().replace("R$", "").replace(" ", "")
    if not s:
        return None
    # Heurística BR: vírgula é decimal, ponto é milhar
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        log.warning("não consegui parsear valor: %r", raw)
        return None


# ---------------------------------------------------------------------------
# Leitura do Excel
# ---------------------------------------------------------------------------

# Mapeamento de coluna esperada → nome no header. Tolerante: bate por
# substring case-insensitive, ignorando acentos.
COL_PATTERNS = {
    "tipo":        ["ARP / CONTRATO", "ARP/CONTRATO", "TIPO"],
    "numero":      ["Nº", "N°", "NUMERO"],
    "status":      ["STATUS", "Unnamed: 3"],  # geralmente sem header
    "objeto":      ["OBJETO"],
    "empresa":     ["EMPRESA", "FORNECEDOR", "CONTRATADO"],
    "data_inicio": ["DATA INICIAL", "DATA DE INÍCIO", "INICIO"],
    "data_fim":    ["DATA FINAL", "DATA DE FIM", "TERMINO"],
    "valor":       ["VALOR", "VALOR CONTRATADO"],
}


def _norm(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().upper()
    return re.sub(r"\s+", " ", s).strip()


def _achar_coluna(cols: list[str], patterns: list[str]) -> Optional[str]:
    cols_n = {_norm(c): c for c in cols}
    # Match exato primeiro
    for pat in patterns:
        if _norm(pat) in cols_n:
            return cols_n[_norm(pat)]
    # Match por substring
    for pat in patterns:
        pn = _norm(pat)
        for cn, original in cols_n.items():
            if pn in cn or cn in pn:
                return original
    return None


def _detectar_coluna_status(df: pd.DataFrame) -> Optional[str]:
    """A coluna de status (OK/ADICIONAR) costuma vir SEM header (Unnamed: 3).
    Detecta procurando uma coluna cujos valores são majoritariamente OK/ADICIONAR.
    """
    for c in df.columns:
        vals = df[c].dropna().astype(str).str.strip().str.upper()
        if len(vals) == 0:
            continue
        ok_or_add = vals.isin(["OK", "ADICIONAR"]).sum()
        if ok_or_add / max(len(vals), 1) > 0.5:
            return c
    return None


def parse_xlsx(
    arquivo: Path,
    sheet: str = "Planilha1",
    header_row: int = 7,
    so_adicionar: bool = True,
) -> list[ContratoExcel]:
    """Lê a planilha e retorna lista de ContratoExcel.

    - `header_row` é 1-indexado (linha 7 do Excel = índice 6 no pandas).
    - `so_adicionar`: se True, filtra só linhas com status="ADICIONAR".
    """
    if not arquivo.exists():
        raise FileNotFoundError(f"planilha não existe: {arquivo}")

    log.info("lendo planilha: %s (sheet=%s, header_row=%d)",
             arquivo, sheet, header_row)
    df = pd.read_excel(arquivo, sheet_name=sheet, header=header_row - 1)
    log.info("dimensões: %s", df.shape)
    log.debug("colunas detectadas: %s", list(df.columns))

    # Mapeia colunas
    col_tipo = _achar_coluna(list(df.columns), COL_PATTERNS["tipo"])
    col_num  = _achar_coluna(list(df.columns), COL_PATTERNS["numero"])
    col_obj  = _achar_coluna(list(df.columns), COL_PATTERNS["objeto"])
    col_emp  = _achar_coluna(list(df.columns), COL_PATTERNS["empresa"])
    col_di   = _achar_coluna(list(df.columns), COL_PATTERNS["data_inicio"])
    col_df   = _achar_coluna(list(df.columns), COL_PATTERNS["data_fim"])
    col_val  = _achar_coluna(list(df.columns), COL_PATTERNS["valor"])
    col_st   = _achar_coluna(list(df.columns), COL_PATTERNS["status"]) or _detectar_coluna_status(df)

    log.info("mapeamento de colunas:")
    log.info("  número:      %r", col_num)
    log.info("  status:      %r", col_st)
    log.info("  empresa:     %r", col_emp)
    log.info("  data início: %r", col_di)
    log.info("  data fim:    %r", col_df)
    log.info("  valor:       %r", col_val)
    log.info("  objeto:      %r", col_obj)

    obrigatorios = {"número": col_num, "data fim": col_df, "empresa": col_emp}
    falta = [k for k, v in obrigatorios.items() if v is None]
    if falta:
        raise ValueError(f"colunas obrigatórias não encontradas no Excel: {falta}")

    contratos: list[ContratoExcel] = []
    total_ok = total_add = total_outros = 0
    for _, row in df.iterrows():
        numero_raw = row.get(col_num)
        if pd.isna(numero_raw) or not str(numero_raw).strip():
            continue
        status = str(row.get(col_st, "") or "").strip().upper() if col_st else ""

        if status == "OK":
            total_ok += 1
        elif status == "ADICIONAR":
            total_add += 1
        else:
            total_outros += 1

        if so_adicionar and status != "ADICIONAR":
            continue

        empresa_raw = str(row.get(col_emp, "") or "")
        nome, cnpj, cpf = parse_empresa(empresa_raw)

        c = ContratoExcel(
            numero=_normalizar_numero(numero_raw),
            tipo=str(row.get(col_tipo, "CONTRATO") or "CONTRATO").strip().upper() if col_tipo else "CONTRATO",
            status=status,
            empresa_raw=empresa_raw,
            empresa_nome=nome,
            cnpj=cnpj,
            cpf=cpf,
            data_inicio=parse_data(row.get(col_di)) if col_di else None,
            data_fim=parse_data(row.get(col_df)),
            valor=parse_valor(row.get(col_val)) if col_val else None,
            objeto=str(row.get(col_obj, "") or "").strip() if col_obj else "",
        )
        contratos.append(c)

    log.info("status na planilha: OK=%d  ADICIONAR=%d  outros=%d",
             total_ok, total_add, total_outros)
    log.info("contratos retornados (so_adicionar=%s): %d",
             so_adicionar, len(contratos))
    return contratos
