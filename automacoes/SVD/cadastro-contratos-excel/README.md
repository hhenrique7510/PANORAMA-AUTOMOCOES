# Cadastro de Contratos a partir de Excel

Automação que lê uma planilha Excel com a lista mestre de contratos da Prefeitura, filtra os que estão marcados como **"ADICIONAR"** (não cadastrados no Panorama Fiscal ainda) e cria cada um no sistema via `/svd/contrato/salvar`, preenchendo todos os campos: empresa, número, datas, valor e objeto.

---

## Como funciona

1. **Lê** o Excel (`--xlsx` ou `XLSX_PATH` no `.env`) com header na linha 7 (planilha "Planilha1").
2. **Filtra** apenas linhas cujo status (coluna 4) é `ADICIONAR`.
3. **Parseia** cada linha extraindo: número, nome da empresa, CNPJ/CPF, data início, data fim, valor e objeto.
4. **Loga** no Panorama Fiscal e abre `/svd/contrato/salvar` para cada contrato a criar:
   - Seleciona a empresa pelo CNPJ/CPF no dropdown.
   - Preenche número, datas, valor.
   - Vai pra aba "Objeto do Contrato" e cola o texto do objeto.
   - Clica em Salvar.
5. **Gera** `out/report.md`, `out/report.csv` e atualiza `out/history.csv`.

### Arquitetura

```
cadastro-contratos-excel/
├── main.py           # CLI (--xlsx, --debug, --limit, --dry-run, --only)
├── worker.py         # Playwright: login + criar_contrato
├── parser.py         # pandas + regex (funções puras)
├── report.py         # CSV / Markdown / history
├── requirements.txt
├── .env.example
└── out/              # relatórios (gitignored)
```

---

## Setup

Da raiz do repositório:

```bash
source .venv/bin/activate
pip install -r automacoes/cadastro-contratos-excel/requirements.txt
python -m playwright install chromium
```

Copie `.env.example` para `.env`:

```bash
cd automacoes/cadastro-contratos-excel
cp .env.example .env
# edite com URL, login, senha e caminho do .xlsx
```

---

## Como rodar

```bash
cd automacoes/cadastro-contratos-excel

# Dry-run nos 3 primeiros (não salva)
../../.venv/bin/python main.py --dry-run --headed --limit 3

# Pra valer
../../.venv/bin/python main.py

# Sobrescrever o caminho da planilha
../../.venv/bin/python main.py --xlsx "/caminho/para/planilha.xlsx"

# Testar 1 contrato específico (pelo número)
../../.venv/bin/python main.py --only 00080/2025 --headed
```

### Flags

| Flag | Padrão | O que faz |
|---|---|---|
| `--xlsx PATH` | `$XLSX_PATH` | Caminho do .xlsx (sobrescreve .env) |
| `--debug` | `False` | Logs verbosos + browser visível |
| `--headed` | `False` | Abre janela do browser (sem --debug) |
| `--limit N` | (sem limite) | Processa só os N primeiros |
| `--dry-run` | `False` | Não salva — só relata o que faria |
| `--only TEXTO` | — | Processa só contratos cujo número contém esse texto |

---

## Status possíveis

| Status | Significa |
|---|---|
| `criado` | Contrato criado com sucesso |
| `dry_run` | Faria o cadastro, mas `--dry-run` impediu o save |
| `pulado` | Linha tem status diferente de "ADICIONAR" no Excel |
| `erro` | Algo falhou (ver `motivo`, log e screenshot) |

---

## Saídas

`out/` (gitignored):

- `report.csv` / `report.md` — resultado do run atual
- `history.csv` / `history.md` — cumulativo entre runs (append-only)
- `logs/run-*.log` — log completo de cada execução
- `screenshots/erro-*.png` — print do navegador no momento do erro
