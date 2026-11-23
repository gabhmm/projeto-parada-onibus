# Central de Monitoramento e Controle — Paradas de Ônibus (Raspberry Pi)

Software embarcado em **Python 3.12+** para a **Raspberry Pi** monitorar em tempo real uma rede de **3 paradas de ônibus** — **1 parada física (Arduino via USB Serial NDJSON)** + **2 paradas simuladas derivadas** (`× 1.05` e `× 0.95`) — com persistência em **SQLite**, exportação em **CSV** e **Dashboard Web 100% offline** acessível na rede local (`http://<IP-DA-RASPI>:5000`).

---

## ✨ Recursos Principais
- **Comunicação Serial Bidirecional (NDJSON):** recebe telemetria contínua do Arduino autônomo (Parada 1) e envia comandos sob demanda (`set_limit`, `set_fan`, `set_mode`).
- **Derivação de Paradas Virtuais:** gera automaticamente as leituras das Paradas 2 e 3 com fatores configuráveis e regras independentes de limite e modo (`AUTOMÁTICO` / `MANUAL`).
- **Dashboard Offline-First (`FastAPI` + `SSE` + `Chart.js` local):**
  - Resumo consolidado da rede (status Serial, média, máxima, paradas ventilando).
  - Cards individuais das 3 paradas com **gráfico individual de temperatura visível por padrão** (com linha horizontal do limite) e paleta visual de estados (**Verde** Normal, **Azul/Vermelho** Ventilando, **Amarelo/Cinza** Sem Sinal).
  - Controles individuais por parada: alterar temperatura limite, desligar ventilação (override manual) e retornar ao modo automático.
  - Gráfico comparativo das 3 paradas, histórico recente de eventos e exportação CSV (geral ou por parada).
- **Resiliência:** descarte seguro de JSON malformado, reconexão Serial automática com backoff exponencial e watchdog de desconexão que pausa a derivação virtual sem gerar dados falsos.
- **Modo Simulador Completo (`--simulator`):** demonstração e desenvolvimento ponta a ponta sem Arduino conectado.

---

## 🚀 Instalação

Requisitos: **Python 3.12+** (testado em Linux/Raspberry Pi OS e Windows).

```bash
python3 -m venv .venv
# Linux / Raspberry Pi:
source .venv/bin/activate
# Windows (PowerShell):
# .\.venv\Scripts\Activate.ps1

pip install -e ".[dev]"
```

---

## ▶️ Como Executar

### 1. Modo Simulador Completo (sem Arduino)
```bash
python -m parada_onibus --simulator --port 5000
```
Acesse no navegador: `http://localhost:5000` (ou `http://<IP-DA-RASPI>:5000` pela rede local).

### 2. Modo Real (Raspberry Pi + Arduino via USB Serial)
```bash
# Copie o exemplo de configuração e ajuste a porta serial se necessário
cp config.example.toml config.toml

python -m parada_onibus --serial-port /dev/ttyACM0 --baud-rate 9600 --port 5000
```

> **Dica na Raspberry Pi:** garanta que o usuário tenha permissão na porta Serial (`sudo usermod -a -G dialout $USER`).

### Variáveis de Ambiente Suportadas
Todas as opções do `config.toml` podem ser sobrescritas via ambiente:
- `PARADA_CONFIG` (caminho do `config.toml`)
- `PARADA_HOST` (padrão `0.0.0.0`), `PARADA_PORT` (padrão `5000`)
- `PARADA_MODE` (`serial` ou `simulator`)
- `PARADA_SERIAL_PORT` (padrão `/dev/ttyACM0`), `PARADA_BAUD_RATE` (padrão `9600`)
- `PARADA_DB_PATH` (padrão `data/parada_onibus.db`)

---

## 🔌 Integração com o Arduino
Consulte o contrato completo em [`docs/CONTRATO_SERIAL_ARDUINO.md`](docs/CONTRATO_SERIAL_ARDUINO.md) para enviar ao responsável pelo firmware `.ino`.

---

## 🧪 Testes e Verificação de Qualidade

```bash
pytest
mypy src
ruff check src tests
```
