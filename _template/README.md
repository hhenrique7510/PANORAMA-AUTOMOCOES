# [Nome da Automação]

> Esqueleto para uma nova automação. **Antes de tudo**: copie esta pasta inteira para `automacoes/<nome-kebab-case>/` e edite os arquivos.

Resumo de 1 parágrafo do que essa automação faz, onde ela roda, e qual problema resolve.

---

## Como funciona

1. Passo 1 do fluxo (ex: login)
2. Passo 2 (ex: navega pra tal tela)
3. Passo 3 (ex: itera por tal coisa)
4. Passo final (ex: gera relatório em `out/`)

### Arquitetura

```
<nome-da-automacao>/
├── main.py           # CLI / entrypoint
├── worker.py         # lógica principal (Playwright, API client, etc.)
├── parser.py         # funções puras, fáceis de testar
├── report.py         # geração de saídas (CSV / Markdown / JSON)
├── requirements.txt
├── .env              # credenciais (NUNCA commitar)
├── _dev/             # scripts de debug e exploração (opcional)
└── out/              # saídas (gitignored)
```

Cada arquivo tem **uma responsabilidade clara** — facilita manutenção e reuso entre automações.

---

## Setup

Do repo root:

```bash
source .venv/bin/activate
pip install -r automacoes/<nome-kebab-case>/requirements.txt

# Se usa Playwright:
python -m playwright install chromium
```

Copie `.env.example` para `.env` e preencha as credenciais.

---

## Como rodar

```bash
cd automacoes/<nome-kebab-case>

# Modo debug (verboso, lento, visual)
../../.venv/bin/python main.py --debug

# Modo produção
../../.venv/bin/python main.py
```

### Flags

| Flag | Padrão | O que faz |
|---|---|---|
| `--debug` | `False` | Modo verboso |
| `--limit N` | (sem limite) | Limita a N items pra testes |

---

## Saídas

`out/` (gitignored):

- `report.csv` — dados estruturados
- `report.md` — leitura humana
- `errors.log` — erros não-fatais

---

## Troubleshooting

| Sintoma | Causa | Solução |
|---|---|---|
| (preencher conforme aparecer) | | |
