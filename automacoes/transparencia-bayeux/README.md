# Transparência Bayeux — Scraper de Contratos

Scraper do Portal de Transparência de Bayeux (https://transparencia.bayeux.pb.gov.br).

Pra cada contrato listado num ano, abre a página de detalhamento, **extrai todos os
campos** (número, empresa, CNPJ, datas, valor, objeto) e **baixa o PDF**. NÃO mexe no
Panorama Fiscal — só extrai dados. A fase 2 (cadastrar + anexar) é uma automação separada.

---

## Status

🚧 Em construção — só tem o `_dev/explore.py` por enquanto. Próximo passo é rodar o
explore pra capturar o HTML real do portal e construir o scraper baseado nele.

---

## Como rodar a exploração (faça isso primeiro)

```bash
cd /Users/henriqueroma/IdeaProjects/Panorama-Automocoes/automacoes/transparencia-bayeux
../../.venv/bin/python _dev/explore.py
```

Vai abrir o browser visualmente, navegar pro portal, filtrar pelo ano 2026, abrir
o detalhamento do 1º contrato e salvar 3 arquivos em `_dev/snapshots/`:

- `01-listagem.html` — HTML da listagem com tabela
- `02-detalhamento.html` — HTML da página de detalhamento
- `03-resumo.txt` — qtd de linhas e link do PDF encontrado

Depois disso, me passa esses arquivos (ou só me diz "rodei") que eu construo o scraper.

---

## Como vai funcionar (depois de pronto)

```bash
# Baixa todos os contratos de 2026 + gera contratos-2026.csv
../../.venv/bin/python main.py --ano 2026

# Teste com 5 primeiros
../../.venv/bin/python main.py --ano 2026 --limit 5

# Múltiplos anos
../../.venv/bin/python main.py --ano 2026 2025 2024
```

### Saídas

- `~/Downloads/CONTRATOS TRANSPARENCIA/{ano}/*.pdf` — PDFs baixados
- `out/contratos-{ano}.csv` — metadados de cada contrato (pra alimentar a fase 2)
- `out/logs/run-*.log`
- `out/screenshots/erro-*.png`
