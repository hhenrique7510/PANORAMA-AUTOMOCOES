# Transparência → Panorama (orquestrador)

**Fase 2** do pipeline. Lê o `report.csv` gerado pela automação `transparencia-bayeux`
(Fase 1) e, pra cada contrato baixado, executa no Panorama Fiscal:

1. **Cadastra a empresa** se o CNPJ/CPF ainda não estiver no sistema (reusa
   o worker de `SVD/cadastro-empresas-excel`)
2. **Cadastra o contrato** se o número ainda não estiver no sistema (reusa
   `SVD/cadastro-contratos-excel`)
3. **Anexa o PDF** baixado pela Fase 1 no contrato existente (reusa
   `SVD/anexar-contratos`)

Cada passo é registrado com status `criada/criado/anexado/ja_existia/erro`.

---

## Pré-requisito

Rodar a Fase 1 antes — isto é, ter o arquivo:
```
automacoes/transparencia-bayeux/out/report.csv
```

E os PDFs em `~/Downloads/CONTRATOS TRANSPARENCIA/{ano}/`.

---

## Setup

```bash
cd automacoes/transparencia-orquestra
cp .env.example .env
# edita .env com PANORAMA_USER e PANORAMA_PASS
```

---

## Como rodar

```bash
# Dry-run nos 3 primeiros (não cadastra nada — só simula)
../../.venv/bin/python main.py --dry-run --headed --limit 3

# Pra valer (1 contrato por vez, sequencial)
../../.venv/bin/python main.py

# Filtrar contratos específicos
../../.venv/bin/python main.py --only 00189/2026

# Apontar pra outro CSV
../../.venv/bin/python main.py --csv /caminho/para/outro.csv
```

### Flags

| Flag | O que faz |
|---|---|
| `--csv PATH` | CSV de entrada (default: `../transparencia-bayeux/out/report.csv`) |
| `--dry-run` | Não salva nada — só relata o que faria |
| `--limit N` | Processa só os N primeiros |
| `--only TEXTO` | Filtra por número ou empresa que contenha esse texto |
| `--headed` | Abre janela do browser (default: headless) |
| `--debug` | Logs verbosos |
| `--skip-empresa` | Pula passo de criar empresa (assume que já existem) |
| `--skip-contrato` | Pula passo de criar contrato (assume que já existem) |
| `--skip-anexar` | Pula passo de anexar PDF |

---

## Status no relatório

| Status | Significa |
|---|---|
| `completo` | Empresa+contrato existiam (ou foram criados) e PDF foi anexado |
| `criou_tudo` | Criou empresa, criou contrato e anexou PDF |
| `criou_contrato` | Empresa já existia; criou contrato e anexou PDF |
| `criou_empresa` | Criou empresa, mas contrato falhou |
| `pdf_ja_anexado` | Contrato já tinha PDF; nada feito |
| `dry_run` | Modo simulação |
| `erro` | Falhou em algum passo (ver `motivo`) |
