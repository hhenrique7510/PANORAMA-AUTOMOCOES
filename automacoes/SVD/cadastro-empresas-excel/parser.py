"""Parser: extrai as empresas únicas a cadastrar a partir da planilha de contratos.

Reusa o `parse_xlsx` da automação de contratos (mesma planilha, mesmo mapeamento
de colunas) via um shim de sys.path — assim a lógica de leitura/coluna fica em um
lugar só e não corre risco de divergir.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Reusa o parser de contratos (mesma planilha, mesma extração de empresa).
# Carregado por CAMINHO com nome único — `import parser` colidiria com ESTE
# módulo (ambos os arquivos se chamam parser.py). Precisa ser registrado em
# sys.modules antes do exec pra o @dataclass dele resolver as anotações.
_CONTRATOS_PARSER = (
    Path(__file__).resolve().parent.parent / "cadastro-contratos-excel" / "parser.py"
)
_spec = importlib.util.spec_from_file_location("contratos_parser", _CONTRATOS_PARSER)
_contratos_parser = importlib.util.module_from_spec(_spec)
sys.modules["contratos_parser"] = _contratos_parser
_spec.loader.exec_module(_contratos_parser)  # type: ignore[union-attr]
parse_xlsx = _contratos_parser.parse_xlsx

log = logging.getLogger(__name__)


@dataclass
class Empresa:
    """Uma empresa/pessoa a ser cadastrada no Panorama."""
    nome: str                      # razão social / nome
    cnpj: Optional[str] = None     # 14 dígitos, sem máscara (se PJ)
    cpf: Optional[str] = None      # 11 dígitos, sem máscara (se PF)
    raw: str = ""                  # célula original da planilha

    @property
    def doc(self) -> str:
        """CNPJ ou CPF (só dígitos) — o que tiver."""
        return self.cnpj or self.cpf or ""

    @property
    def is_pj(self) -> bool:
        return bool(self.cnpj)

    @property
    def tipo_pessoa(self) -> str:
        if self.cnpj:
            return "PJ"
        if self.cpf:
            return "PF"
        return "?"

    @property
    def doc_fmt(self) -> str:
        """Documento já formatado com máscara (pra setar no campo do form)."""
        c = self.doc
        if self.cnpj and len(c) == 14:
            return f"{c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}"
        if self.cpf and len(c) == 11:
            return f"{c[:3]}.{c[3:6]}.{c[6:9]}-{c[9:]}"
        return c


def empresas_a_cadastrar(
    arquivo: Path,
    *,
    sheet: str = "Planilha1",
    header_row: int = 7,
    so_adicionar: bool = True,
) -> list[Empresa]:
    """Lê a planilha, extrai as empresas dos contratos e DEDUPLICA por documento.

    Quando a linha não tem CNPJ/CPF, deduplica por nome (e cadastra sem a lupa).
    """
    contratos = parse_xlsx(
        arquivo, sheet=sheet, header_row=header_row, so_adicionar=so_adicionar
    )
    vistos: set[str] = set()
    empresas: list[Empresa] = []
    sem_doc = 0
    for c in contratos:
        nome = (c.empresa_nome or "").strip()
        doc = c.cnpj or c.cpf or ""
        chave = doc or (f"nome:{nome.lower()}" if nome else "")
        if not chave or chave in vistos:
            continue
        vistos.add(chave)
        if not doc:
            sem_doc += 1
        empresas.append(Empresa(nome=nome, cnpj=c.cnpj, cpf=c.cpf, raw=c.empresa_raw))

    log.info(
        "empresas únicas na planilha (so_adicionar=%s): %d  (sem documento: %d)",
        so_adicionar, len(empresas), sem_doc,
    )
    return empresas
