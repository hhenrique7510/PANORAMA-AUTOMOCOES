# Checado de Validade AZ

Bot que audita o sistema **[Panorama Fiscal](https://panoramafiscal.com.br/panorama_fiscal/)** para identificar quais tarefas de cada empresa estão com **data de Confirmação fora do mês/ano alvo** (por padrão **05/2026**).

Roda **3 bots em paralelo** (cada um responsável por um terço das páginas) e gera dois relatórios — CSV pra análise e Markdown pra leitura.

---

## Como funciona

1. Faz login no Panorama Fiscal com o usuário do `.env`.
2. Abre a tela de **Certidões** (tabela DataTables).
3. Filtra a listagem para **100 empresas por página**.
4. Descobre quantas páginas existem no total via API do DataTables.
5. Divide as páginas entre 3 bots e dispara `asyncio.gather`.
6. Cada bot, para cada empresa da sua fatia:
   - Clica no ícone PDF "Gerenciar Tarefas" da linha.
   - Espera o popup **"Tarefas do Diagnóstico Fiscal"** carregar via AJAX.
   - Lê todas as linhas: **Tarefa** + **Data de Confirmação**.
   - Para cada linha cuja data **não esteja no mês/ano alvo**, grava uma anomalia.
   - Fecha o popup (via jQuery `.modal('hide')`).
7. No fim, junta todas as anomalias e gera `out/report.csv` + `out/report.md`.

### Arquitetura dos arquivos

```
checado-de-validade-az/
├── audit.py          # CLI: parse args, orquestra 3 workers
├── worker.py         # 1 worker = 1 Playwright Page (login + páginas designadas)
├── parser.py         # Funções puras: Anomaly + is_out_of_target()
├── report.py         # Geração de CSV e Markdown
├── requirements.txt
├── .env              # credenciais e mês/ano alvo
└── out/              # report.csv e report.md (gitignored)
```

Cada arquivo tem **uma responsabilidade**:
- `parser.py` é **puro** (sem Playwright) e fácil de testar.
- `worker.py` concentra toda a interação com o navegador.
- `audit.py` é fino — só orquestra.
- `report.py` é o último passo (escrita em disco).

---

## Setup (uma vez)

Do repo root:

```bash
# 1) Cria o venv compartilhado (se ainda não existe)
python3.14 -m venv .venv
source .venv/bin/activate

# 2) Instala dependências desta automação
pip install -r automacoes/checado-de-validade-az/requirements.txt

# 3) Baixa o Chromium do Playwright (~150MB, uma vez só)
python -m playwright install chromium
```

### Configurar credenciais

Edite `automacoes/checado-de-validade-az/.env`:

```env
PANORAMA_URL=https://panoramafiscal.com.br/panorama_fiscal/
PANORAMA_USER=robo-306
PANORAMA_PASS=Pano@#!2024
MES_ALVO=05
ANO_ALVO=2026
```

Para auditar outro mês/ano, troque `MES_ALVO` (dois dígitos) e `ANO_ALVO` (quatro dígitos). **O `.env` nunca é commitado.**

---

## Como rodar

Sempre a partir da pasta da automação:

```bash
cd automacoes/checado-de-validade-az
```

### Modos de execução

| Comando | O que faz | Quando usar |
|---|---|---|
| `../../.venv/bin/python audit.py --bots 1 --max-pages 1 --headed` | 1 bot, 1 página, janela visível | **Smoke test** — validar login e leitura |
| `../../.venv/bin/python audit.py --bots 3 --max-pages 2 --headed` | 3 bots paralelos, 2 páginas, janelas visíveis | Validar paralelismo antes do run completo |
| `../../.venv/bin/python audit.py --bots 3 --headed` | 3 bots, todas as páginas, janelas visíveis | Run completo acompanhando visualmente |
| `../../.venv/bin/python audit.py --bots 3` | 3 bots, todas as páginas, **headless** | Run de produção (mais rápido) |

### Flags

| Flag | Padrão | O que controla |
|---|---|---|
| `--bots N` | `3` | Quantos bots paralelos rodar (cada um abre uma aba na mesma sessão) |
| `--headed` | `False` | Abre janela visível do Chromium (útil pra debug) |
| `--max-pages N` | (sem limite) | Limita às primeiras N páginas — pra testes rápidos |

---

## Saídas

Tudo em `out/` (gitignored):

### `report.csv`

```csv
empresa,tarefa,data_confirmacao,pagina,bot
2P CALIFORNIA EXCHANGE LTDA - 40.842.851/0001-04,CERTIDÃO NEGATIVA RFB,17/04/2026,1,0
A.B.F. SERVICOS E EVENTOS LTDA - 55.577.750/0001-12,CERTIDÃO NEGATIVA MTE,10/03/2026,1,0
...
```

### `report.md`

Agrupado por empresa, fácil de ler:

```markdown
# Tarefas fora de 05/2026

Total de anomalias: **47** em **23** empresas.

## 2P CALIFORNIA EXCHANGE LTDA - 40.842.851/0001-04  _(página 1, bot 0)_
- **CERTIDÃO NEGATIVA RFB** — `17/04/2026` (mês 04)

## A.B.F. SERVICOS E EVENTOS LTDA - 55.577.750/0001-12  _(página 1, bot 0)_
- **CERTIDÃO NEGATIVA MTE** — `10/03/2026` (mês 03)
- **CERTIDÃO NEGATIVA RFB** — `17/04/2026` (mês 04)
...
```

---

## Estimativa de tempo

- ~3 segundos por empresa (abrir popup + ler + fechar).
- 280 empresas ÷ 3 bots ≈ **~5 minutos** em paralelo headless.
- Headed (com janelas visíveis) é um pouco mais lento (~7-8 min).

---

## Troubleshooting

| Sintoma | Causa provável | Solução |
|---|---|---|
| `tarefas=0` pra todas as empresas | O popup tem estrutura diferente da esperada | Olhe os logs `empty popup for empresa_id=...` que mostram o HTML do tbody — provavelmente mudou layout |
| `popup never loaded` | AJAX lento ou o link `<a data-target>` não está no DOM | Já temos retry com 1s de pausa; se persistir, aumente `timeout=15000` no `wait_for_function` |
| `ERR_ABORTED` no `goto` | App fez redirect mid-load | `_safe_goto()` já tenta 3x — se ainda falhar, login pode estar quebrado |
| Modal abre mas nunca fecha | Bootstrap em estado bagunçado | `close_modal` tem fallback que força hide via DOM no fim |
| Bot trava em uma empresa | Empresa específica com bug no site | Vai logar como `popup never loaded` e seguir pra próxima |

### Logs

Tudo vai pra stdout. Pra salvar:

```bash
../../.venv/bin/python audit.py --bots 3 2>&1 | tee out/run-$(date +%Y%m%d-%H%M).log
```

---

## Estendendo

### Mudar o critério

Toda a lógica de "está fora do alvo" está em `parser.py:is_out_of_target()`. Se quiser auditar por **dia** também, ou por **range de meses**, edite essa função — o resto não precisa mudar.

### Adicionar mais bots

`--bots N` aceita qualquer número. Mas mais que 3 raramente vale a pena, porque:
1. A sessão é compartilhada (mesma autenticação).
2. O servidor pode rate-limitar.
3. A maior parte do tempo é AJAX, não CPU local.

### Reaproveitar em outra automação

`parser.py` e `report.py` são puros — copie e adapte.
`worker.py` é específico do DataTables do Panorama Fiscal mas o padrão (login → iterar → popup → fechar) é replicável.
