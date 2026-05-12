# Panorama Automoções — Automações

Repositório central das automações da Panorama. Cada automação vive em sua própria pasta dentro de `automacoes/` e é **independente** — pode ter sua própria linguagem, dependências e instruções.

## Estrutura do repositório

```
panorama-automocoes/
├── README.md                          # este arquivo
├── .gitignore
├── .venv/                             # ambiente Python compartilhado
└── automacoes/
    └── checado-de-validade-az/        # 1ª automação: auditoria de validade no Panorama Fiscal
        ├── README.md                  # instruções detalhadas
        ├── audit.py                   # entrypoint CLI
        ├── worker.py                  # lógica Playwright
        ├── parser.py                  # parsing puro de datas
        ├── report.py                  # geração de CSV + Markdown
        ├── requirements.txt
        ├── .env                       # credenciais (gitignored)
        └── out/                       # relatórios gerados (gitignored)
```

## Setup inicial (uma única vez)

Requisitos: **Python 3.13+** e **Git**.

```bash
git clone https://github.com/hhenrique7510/PANORAMA-AUTOMOCOES.git
cd PANORAMA-AUTOMOCOES

# Cria ambiente Python compartilhado
python3.14 -m venv .venv
source .venv/bin/activate
```

A partir daqui, cada automação tem seu próprio README explicando o que precisa instalar (`pip install -r ...`) e como rodar.

## Automações disponíveis

| Pasta | O que faz | Status |
|---|---|---|
| [`checado-de-validade-az/`](automacoes/checado-de-validade-az/) | Audita o Panorama Fiscal: identifica tarefas com data de Confirmação fora de um mês/ano alvo. Roda com 3 bots em paralelo. | ✅ Funcionando |

## Adicionando uma nova automação

1. Crie a pasta: `automacoes/<nome-kebab-case>/`
2. Crie um `README.md` com:
   - O que ela faz (1 parágrafo)
   - Pré-requisitos específicos
   - Como configurar (`.env`)
   - Como rodar (com exemplos de comando)
   - Onde ela escreve a saída
3. Use o `.venv/` compartilhado (raiz do repo) — adicione novas deps ao seu `requirements.txt` local.
4. Adicione a linha na tabela acima.

## Convenções

- **Credenciais sempre em `.env`** dentro da pasta da automação — nunca commitar (já está no `.gitignore`).
- **Saídas em `out/`** dentro da pasta da automação — também gitignored.
- **Logs em `errors.log`** para erros não-fatais — também gitignored.
- **Use kebab-case** para nomes de pasta (`checado-de-validade-az`, não `CheadoDeValidade_AZ`).
