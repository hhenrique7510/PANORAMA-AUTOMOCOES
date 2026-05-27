# cadastro-empresas-excel

Cadastra **empresas/pessoas** no Panorama Fiscal (`/svd/empresas`) a partir da
planilha de gestão de contratos. É o **pré-requisito** do
[`cadastro-contratos-excel`](../cadastro-contratos-excel/): um contrato só pode
ser criado se a empresa já estiver cadastrada. Esta automação fecha essa lacuna.

## O que faz

1. Lê a mesma planilha de contratos e extrai as empresas únicas (dedup por CNPJ/CPF).
2. Lista as empresas já cadastradas no Panorama e **pula as que já existem** (idempotente).
3. Para cada empresa que falta, abre o form `/svd/empresa/salvar` e cadastra:
   - **CNPJ (PJ):** digita o número e usa a **lupa (`buscarCnpj`)** pra puxar
     razão social/cidade/UF da Receita; se a Receita não achar, cai pro nome da planilha.
   - **CPF (pessoa física):** preenche a razão social com o nome da planilha.
   - Defaults: Situação=ATIVA, Tributação=LUCRO PRESUMIDO (só PJ), Matriz="Pessoa Física" (PF).
4. Gera relatórios em `out/` (CSV + Markdown + histórico).

## Setup

```bash
cp .env.example .env   # preencha PANORAMA_USER (CPF) e PANORAMA_PASS; o XLSX_PATH já vem apontado
# (mesmos valores do .env de cadastro-contratos-excel)
```

Depende do `parser.py` de `../cadastro-contratos-excel` (reusa o `parse_xlsx`) — as
duas pastas precisam coexistir no repo. Venv compartilhado na raiz.

## Uso

```bash
cd automacoes/cadastro-empresas-excel

# 1) Dry-run de UMA (confere preenchimento, não salva):
../../.venv/bin/python main.py --dry-run --headed --only KENKO

# 2) Cadastrar UMA de verdade (valida o salvar + lupa):
../../.venv/bin/python main.py --headed --limit 1

# 3) Cadastrar todas as que faltam:
../../.venv/bin/python main.py
```

Flags: `--dry-run`, `--headed`, `--debug`, `--limit N`, `--only <texto>`,
`--exclude <textos…>`, `--all-status` (todas as linhas, não só ADICIONAR),
`--xlsx/--sheet/--header-row`.

## Detalhes técnicos (peculiaridades do form)

- Campo do documento (`#cnpj`) tem máscara que **embaralha** quando digitado →
  o valor é setado **já formatado via JS**.
- A **lupa** `buscarCnpj()` consulta a Receita e auto-preenche; é o caminho
  preferido pra PJ (dados oficiais). Pode ser lenta — valide com `--headed`.
- Salvar dispara `confirm` + `alert`; ambos são aceitos e a mensagem é usada pra
  detectar sucesso (`"...Sucesso"`) vs falha (`"...não realizado"`).
- Login usa o CPF com jQuery Mask (digitado, não `fill`).

Depois de rodar, re-rode o `cadastro-contratos-excel` nos contratos antes pulados.
