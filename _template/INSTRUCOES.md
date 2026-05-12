# Como usar este template

Esta pasta é um **esqueleto** pra criar uma nova automação. Ela **não roda** — você precisa copiar e adaptar.

## Passo a passo

1. **Copie a pasta inteira:**
   ```bash
   cp -r _template automacoes/nome-da-sua-automacao
   ```
   Use **kebab-case** no nome (`coleta-de-notas-fiscais`, não `ColetaDeNotasFiscais`).

2. **Renomeie os arquivos conforme fizer sentido:**
   - `main.py` → `audit.py`, `coleta.py`, ou o que descrever melhor
   - Adicione `worker.py` se for usar Playwright/Selenium

3. **Edite o `README.md`** copiado:
   - Tire o aviso do topo
   - Preencha "O que faz", arquitetura, setup, como rodar

4. **Configure credenciais:**
   ```bash
   cd automacoes/nome-da-sua-automacao
   cp .env.example .env
   # edite .env com as credenciais reais
   ```

5. **Adicione dependências** no `requirements.txt`.

6. **Implemente:**
   - `parser.py` — funções puras (decisão de "é anomalia?", normalização de dados)
   - `worker.py` — interação com sistema externo (navegador, API, banco)
   - `report.py` — geração de CSV/MD/JSON em `out/`
   - `main.py` — orquestração + CLI

7. **Atualize a tabela em `/README.md`** (raiz) com a nova automação.

8. **Apague este arquivo (`INSTRUCOES.md`)** da sua cópia — ele só faz sentido aqui no template.
