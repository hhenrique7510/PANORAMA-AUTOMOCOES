# Anexar Contratos (PDFs no Panorama / SVD)

Automação que percorre uma pasta de PDFs de contrato e, para cada um, encontra o contrato **já cadastrado** em [panoramafiscal.com.br/svd/contratos](https://panoramafiscal.com.br/svd/contratos) e **anexa o PDF** nele (abrindo a tela de edição `/svd/contrato/alterar/{id}`). Os contratos em geral já existem no sistema — o que falta é anexar o arquivo do contrato.

---

## Como funciona

1. **Lê** todos os PDFs da pasta indicada (`--pdf-dir` ou `PDF_DIR` no `.env`).
2. **Extrai** de cada PDF (via `pdfplumber` + regex) os campos necessários: número do contrato, CNPJ/empresa, datas, valor.
3. **Loga** no Panorama Fiscal (na `PANORAMA_URL`, a URL normal de entrada) e baixa a listagem atual, mapeando **número → ID** de cada contrato (o ID vem do link de alterar de cada linha).
4. O número do contrato vem do **nome do arquivo** (`CONTRATO 00070-2026.pdf` → `00070/2026`), que é a fonte confiável — o texto do PDF cita outros números.
5. Para cada PDF, **casa** o número (com ano) com a listagem:
   - se **encontra** → abre `/svd/contrato/alterar/{id}`:
     - se a tela mostra **"Baixar Contrato"** → já tem PDF → pula (`ja_anexado`).
     - senão → seta o PDF no `#fileinput` e clica em **Salvar** (que faz upload + commit; o `confirm()` do site é aceito automaticamente). Depois **reabre a tela e confirma** que apareceu "Baixar Contrato" — só então marca `anexado` (sem isso dava falso positivo).
   - se **não encontra** → `nao_encontrado` e pula (não cria contrato novo por padrão; `PANORAMA_CRIAR_SE_NAO_EXISTIR=1` ativa criação).
6. **Gera** `out/report.csv`, `out/report.md` e atualiza o histórico em `out/history.csv`.

### Status possíveis no relatório

| Status | Significa |
|---|---|
| `anexado` | PDF anexado com sucesso em contrato existente |
| `ja_anexado` | Contrato existe e já tinha PDF (tela mostra "Baixar Contrato") |
| `criado` | Contrato criado do zero + PDF anexado (precisa `PANORAMA_CRIAR_SE_NAO_EXISTIR=1`) |
| `nao_encontrado` | Número não está na listagem; criação desativada |
| `dry_run` | Faria a ação mas `--dry-run` impediu o save |
| `erro` | Algo falhou (ver `motivo`, log e screenshot) |

### Arquitetura

```
anexar-contratos/
├── main.py           # CLI (--debug, --limit, --dry-run, --pdf-dir, --only)
├── worker.py         # Playwright: login, listagem (número→id), anexar PDF
├── parser.py         # pdfplumber + regex (funções puras)
├── report.py         # gera CSV e Markdown em out/
├── requirements.txt
├── .env.example
└── out/              # relatórios (gitignored)
```

---

## Setup

Da raiz do repositório:

```bash
source .venv/bin/activate
pip install -r automacoes/SVD/anexar-contratos/requirements.txt
python -m playwright install chromium
```

Copie `.env.example` para `.env`:

```bash
cd automacoes/SVD/anexar-contratos
cp .env.example .env
# edite .env com URL, login, senha e caminho dos PDFs
```

---

## Como rodar

```bash
cd automacoes/SVD/anexar-contratos

# Dry-run: extrai PDF, checa quais faltam, NÃO cadastra nada
../../../.venv/bin/python main.py --dry-run --debug

# Rodada de teste com 2 contratos
../../../.venv/bin/python main.py --limit 2 --debug

# Produção
../../../.venv/bin/python main.py
```

### Flags

| Flag | Padrão | O que faz |
|---|---|---|
| `--debug` | `False` | Logs verbosos + browser visível (headless=false) |
| `--limit N` | (sem limite) | Processa só os N primeiros PDFs |
| `--dry-run` | `False` | Não preenche nem salva; só relata o que faria |
| `--pdf-dir DIR` | `$PDF_DIR` | Sobrescreve o caminho da pasta de PDFs |

---

## Saídas

`out/` (gitignored) — toda execução produz:

| Arquivo | O que tem | Sobrescreve? |
|---|---|---|
| `report.csv` / `report.md` | Resultado do **run atual** (1 linha por PDF: criado / ja_existe / erro) | Sim, a cada execução |
| `history.csv` | **Histórico cumulativo** de TODAS as execuções. Inclui `timestamp` e `run_id` em cada linha. | Não — só append |
| `history.md` | Versão legível do `history.csv`, agrupada por execução (mais recente primeiro) | Sim (regerada a partir do CSV) |
| `logs/run-YYYYMMDD-HHMMSS-XXXXXX.log` | Log **completo** daquela execução: passos do worker, stack traces, decisões. Um arquivo por run. | Não — um por execução |
| `screenshots/erro-NNNNN-AAAA-TIMESTAMP.png` | Screenshot da tela do navegador no momento de um erro de cadastro. | Não — um por erro |

**Onde olhar quando algo der errado:**

1. Abra o `out/report.md` — vê quais contratos deram `erro` e o motivo (1 linha).
2. Pra mais detalhe, abra o log da execução em `out/logs/run-*.log` — tem stack trace completo + qual etapa do formulário falhou (passo 1/8, 2/8, etc.).
3. Pra ver o que estava no navegador no momento da falha, abra o `out/screenshots/erro-*.png` correspondente.
4. Pra histórico de longo prazo (auditoria), use `out/history.md` — mostra todas as execuções com data/hora e o que rolou em cada uma.

---

## Troubleshooting

| Sintoma | Causa provável | Solução |
|---|---|---|
| "Número do contrato não encontrado no PDF" | Layout do PDF diferente do esperado | Ajustar regex em `parser.py::extract_numero_contrato` |
| Empresa não aparece no dropdown | CNPJ extraído está com formatação diferente | Conferir `parser.py::normalize_cnpj` |
| Login em loop | Captcha ou MFA no Panorama | Logar manualmente uma vez e usar sessão persistente |
| Erro de upload do arquivo | Caminho do PDF com caracteres especiais | `--pdf-dir` deve ser caminho absoluto |
