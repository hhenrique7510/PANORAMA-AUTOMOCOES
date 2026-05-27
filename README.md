# Panorama Automoções — Automações

Repositório central das automações da Panorama. Cada automação vive em sua própria pasta dentro de `automacoes/` e é **independente** — pode ter sua própria linguagem, dependências e instruções.

## Estrutura do repositório

```
panorama-automocoes/
├── README.md                          # este arquivo (visão geral)
├── .gitignore
├── .venv/                             # ambiente Python compartilhado (gitignored)
│
├── _template/                         # esqueleto pra criar novas automações
│   ├── INSTRUCOES.md                  # como usar este template
│   ├── README.md                      # modelo do README de automação
│   ├── main.py                        # entrypoint mínimo
│   ├── parser.py                      # funções puras
│   ├── report.py                      # geração de saídas
│   ├── requirements.txt
│   └── .env.example
│
└── automacoes/
    └── checado-de-validade-az/        # automação 1
        ├── README.md
        ├── audit.py                   # entrypoint CLI
        ├── worker.py                  # Playwright (login, popups, navegação)
        ├── parser.py                  # decisão de "é anomalia?"
        ├── report.py                  # gera report.csv e report.md
        ├── requirements.txt
        ├── .env                       # credenciais (gitignored)
        ├── _dev/                      # scripts de debug e exploração
        │   └── debug_dom.py
        └── out/                       # relatórios gerados (gitignored)
```

---

## Setup inicial (uma única vez)

**Requisitos:** Python 3.13+ (testado com 3.14) e Git.

```bash
git clone https://github.com/hhenrique7510/PANORAMA-AUTOMOCOES.git
cd PANORAMA-AUTOMOCOES

# Cria o ambiente Python compartilhado (usado por todas as automações)
python3.14 -m venv .venv
source .venv/bin/activate
```

A partir daqui, cada automação tem seu próprio README explicando o que instalar e como rodar.

---

## Como rodar uma automação existente

```bash
# 1) Ative o venv (uma vez por terminal)
source .venv/bin/activate

# 2) Instale as dependências da automação que quer rodar
pip install -r automacoes/PANORAMA/checado-de-validade-az/requirements.txt

# 3) Se a automação usa Playwright (browser), baixe o Chromium
python -m playwright install chromium

# 4) Configure o .env da automação (copie do .env.example se houver)
cd automacoes/PANORAMA/checado-de-validade-az
# edite .env com as credenciais

# 5) Rode — cada automação documenta seus próprios comandos no README
../../.venv/bin/python audit.py --bots 3
```

---

## Como adicionar uma nova automação

Use o template:

```bash
# 1) Copie o esqueleto
cp -r _template automacoes/nome-da-sua-automacao
cd automacoes/nome-da-sua-automacao

# 2) Apague o INSTRUCOES.md (só serve no template)
rm INSTRUCOES.md

# 3) Renomeie main.py para algo descritivo (audit.py, coleta.py, etc.)
# 4) Edite o README.md, .env.example, requirements.txt
# 5) Implemente: parser.py (puro) + worker.py (I/O) + main entrypoint
# 6) Configure .env (copie do .env.example)
cp .env.example .env
# edite com credenciais reais

# 7) Atualize a tabela "Automações disponíveis" abaixo
```

Mais detalhes em [`_template/INSTRUCOES.md`](_template/INSTRUCOES.md).

---

## Automações disponíveis

| Pasta | O que faz | Status |
|---|---|---|
| [`checado-de-validade-az/`](automacoes/PANORAMA/checado-de-validade-az/) | Audita o Panorama Fiscal: identifica tarefas com data de Confirmação fora de um mês/ano alvo. Roda com 3 bots em paralelo. | ✅ Funcionando |
| [`cadastro-contratos-pdf/`](automacoes/SVD/anexar-contratos/) | Lê PDFs de uma pasta, extrai número/empresa/datas/valor e cadastra no Panorama Fiscal os que ainda não existem, anexando o próprio PDF. | 🧪 Em validação |
| [`cadastro-contratos-excel/`](automacoes/cadastro-contratos-excel/) | Lê planilha de gestão de contratos, filtra status=ADICIONAR e cria cada contrato no Panorama Fiscal preenchendo empresa/datas/valor/objeto. | 🧪 Em validação |
| [`cadastro-empresas-excel/`](automacoes/cadastro-empresas-excel/) | Extrai as empresas da planilha de contratos e cadastra no Panorama (`/svd/empresas`) as que ainda não existem — usa a lupa da Receita p/ CNPJ. Pré-requisito do cadastro de contratos. | 🧪 Em validação |

---

## Convenções

Para manter o repo navegável conforme cresce:

### Nomenclatura
- **Pastas em kebab-case**: `checado-de-validade-az`, não `CheadoDeValidadeAZ`.
- **Nome descritivo**: descreva o **resultado**, não a tecnologia (`coleta-notas-fiscais`, não `bot-playwright-nf`).

### Estrutura de uma automação
- **`parser.py`** — funções **puras** (sem I/O, sem rede, sem browser). Fácil de testar.
- **`worker.py`** — toda a interação com sistema externo (Playwright, API, banco).
- **`report.py`** — geração de saídas em `out/`.
- **`main.py`** ou **`audit.py`** ou **`coleta.py`** — entrypoint CLI com `argparse`, fino, só orquestra.
- **`_dev/`** — scripts de exploração/debug que **não** são parte da execução normal.

### Segredos e saídas
- **`.env`** sempre por automação, **nunca commitado** (`.gitignore` já cobre).
- **`.env.example`** sem valores reais, **commitado** como referência.
- **`out/`** — todas as saídas, gitignored.
- **`errors.log`** — erros não-fatais, gitignored.

### Dependências
- **Venv compartilhado** na raiz. Cada automação tem seu `requirements.txt` próprio, mas todos instalam no mesmo `.venv/`.
- Se duas automações precisam de versões diferentes da mesma lib → crie um venv local na pasta da automação e documente no README dela.

### Saídas padronizadas
- **CSV** pra análise programática.
- **Markdown** pra leitura humana.
- Quando fizer sentido: **JSON** estruturado.

---

## Roadmap (futuro)

À medida que mais automações forem adicionadas, pode fazer sentido criar:

- `shared/` — biblioteca com utilitários compartilhados (login Panorama Fiscal, helpers de Playwright, etc.).
- `.github/workflows/` — CI pra rodar automações em schedule (cron).
- `scripts/` — utilidades de manutenção do repo.

Por ora, mantenha cada automação **autossuficiente** — não compartilhe código entre pastas até ter pelo menos 3 automações que se beneficiariam do mesmo código.
