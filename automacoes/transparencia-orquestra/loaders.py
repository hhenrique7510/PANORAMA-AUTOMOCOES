"""Carrega os 3 workers de SVD via importlib.

Cada um tem seu próprio `parser.py` e `worker.py` no mesmo diretório, e usa
`from parser import ...`. Se a gente importar tudo como pacote, dá conflito.
A solução é carregar cada um com nome único via `importlib.util`.

Uso:
    workers = carregar_workers()
    workers.empresas.criar_empresa(page, empresa, dry_run=False)
    workers.contratos.criar_contrato(page, contrato, dry_run=False)
    workers.anexar.anexar_pdf_em_existente(page, contrato, contrato_id, dry_run=False)
"""
from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

ROOT_SVD = Path(__file__).resolve().parent.parent / "SVD"


def _load(modname: str, file_path: Path) -> ModuleType:
    """Carrega um arquivo .py como módulo com nome explícito.

    Registra em sys.modules ANTES do exec — dataclasses precisam disso pra
    resolver anotações de tipo.
    """
    spec = importlib.util.spec_from_file_location(modname, file_path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@dataclass
class WorkerBundle:
    """Conjunto de módulos carregados."""
    # Empresas
    empresas_parser: ModuleType
    empresas_worker: ModuleType
    # Contratos (criar)
    contratos_parser: ModuleType
    contratos_worker: ModuleType
    # Anexar PDFs
    anexar_parser: ModuleType
    anexar_worker: ModuleType


def carregar_workers() -> WorkerBundle:
    """Carrega os 3 conjuntos parser+worker como módulos isolados."""
    # Contratos PRIMEIRO porque o parser de Empresas reusa o parse_xlsx dele.
    cp = _load("svd_contratos_parser", ROOT_SVD / "cadastro-contratos-excel" / "parser.py")
    # Antes de importar o worker, o worker faz `from parser import ContratoExcel`
    # — registramos um alias "parser" temporário pra ele resolver.
    sys.modules["parser"] = cp
    cw = _load("svd_contratos_worker", ROOT_SVD / "cadastro-contratos-excel" / "worker.py")
    del sys.modules["parser"]

    ep = _load("svd_empresas_parser", ROOT_SVD / "cadastro-empresas-excel" / "parser.py")
    sys.modules["parser"] = ep
    ew = _load("svd_empresas_worker", ROOT_SVD / "cadastro-empresas-excel" / "worker.py")
    del sys.modules["parser"]

    ap = _load("svd_anexar_parser", ROOT_SVD / "anexar-contratos" / "parser.py")
    sys.modules["parser"] = ap
    aw = _load("svd_anexar_worker", ROOT_SVD / "anexar-contratos" / "worker.py")
    del sys.modules["parser"]

    return WorkerBundle(
        empresas_parser=ep, empresas_worker=ew,
        contratos_parser=cp, contratos_worker=cw,
        anexar_parser=ap, anexar_worker=aw,
    )
