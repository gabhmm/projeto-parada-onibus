# Central de Monitoramento e Controle — Paradas de Ônibus (Raspberry Pi)

Software embarcado em **Python 3.12+** para a **Raspberry Pi** monitorar em tempo real uma rede de **3 paradas de ônibus** — **1 parada física (Arduino via USB Serial NDJSON)** + **2 paradas simuladas derivadas** (`× 1.05` e `× 0.95`) — com persistência em **SQLite**, exportação em **CSV** e **Dashboard Web 100% offline** acessível na rede local (`http://<IP-DA-RASPI>:5000`).

---

## ✨ Recursos Principais
- **Comunicação Serial Bidirecional (NDJSON):** recebe telemetria contínua do Arduino autônomo (Parada 1) e envia comandos sob demanda (`set_limit`, `set_fan`, `set_mode`).
- **Derivação de Paradas Virtuais:** gera automaticamente as leituras das Paradas 2 e 3 com fatores configuráveis e regras independentes de limite e modo (`AUTOMÁTICO` / `MANUAL`).
- **Dashboard Offline-First (`FastAPI` + `SSE` + `Chart.js` local):**
  - Resumo consolidado da rede (status Serial, média, máxima, paradas ventilando).
  - Cards individuais das 3 paradas com **gráfico individual de temperatura visível por padrão** (com linha horizontal do limite) e paleta visual de estados (**Verde** Normal, **Azul/Vermelho** Ventilando, **Amarelo** Sem Sinal, **Vermelho** Falha no Sensor).
  - Controles individuais por parada: alterar temperatura limite, desligar ventilação (override manual) e retornar ao modo automático.
  - Gráfico comparativo das 3 paradas, histórico recente de eventos e exportação CSV (geral ou por parada).
- **Resiliência:**
  - Descarte seguro de JSON malformado e watchdog de desconexão (6 s sem telemetria) que pausa a derivação virtual sem gerar dados falsos.
  - Reconexão Serial automática com backoff exponencial; a porta também é reaberta se ficar 10 s aberta sem receber nenhum byte (Arduino travado, mau contato no cabo).
  - **Falha do sensor** (`"temp": null`): a parada continua conectada e aparece como `FALHA_SENSOR`, com o cooler ligado por segurança; nenhuma leitura falsa é gravada.
  - **Ressincronização:** o Arduino volta para `30.0 °C / AUTO` a cada reinício (inclusive ao abrir a porta Serial, por causa do reset via DTR). A Raspberry guarda o limite e o modo no SQLite e os reenvia automaticamente após cada (re)conexão.
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

> **Com `uv`:** dispensa criar a venv manualmente — `uv run python -m parada_onibus --serial-port /dev/ttyACM0`.

### 3. Testando no PC com WSL2 (Arduino no Windows)
O WSL não enxerga USB sozinho; use o [usbipd-win](https://github.com/dorssel/usbipd-win) para repassar o Arduino:

```powershell
# PowerShell (bind exige administrador, apenas na 1ª vez por porta USB)
usbipd list                       # anote o BUSID do "Arduino Uno"
usbipd bind --busid <BUSID>
usbipd attach --wsl --busid <BUSID>
```
No WSL o Arduino aparece como `/dev/ttyACM0` (ou `ACM1`, ...). Observações:
- Enquanto repassado, o Arduino **some do Windows** (a Arduino IDE não vê a porta COM). Para gravar o firmware: pare o servidor, rode `usbipd detach --busid <BUSID>`, grave e repasse de novo.
- O repasse do usbipd pode cair (o painel mostra **Sem Sinal** e o `usbipd list` volta a exibir `Shared`). Reconecte o cabo e refaça o `attach`. Esse problema é do WSL; na Raspberry, com USB nativo, ele não ocorre.
- Se aparecer uma porta "fantasma" (ex.: `ACM0` que não responde), use a nova (`ACM1`) ou reinicie o WSL com `wsl --shutdown`.

### Variáveis de Ambiente Suportadas
Todas as opções do `config.toml` podem ser sobrescritas via ambiente:
- `PARADA_CONFIG` (caminho do `config.toml`)
- `PARADA_HOST` (padrão `0.0.0.0`), `PARADA_PORT` (padrão `5000`)
- `PARADA_MODE` (`serial` ou `simulator`)
- `PARADA_SERIAL_PORT` (padrão `/dev/ttyACM0`), `PARADA_BAUD_RATE` (padrão `9600`)
- `PARADA_DB_PATH` (padrão `data/parada_onibus.db`)

---

## 🔌 Integração com o Arduino
O protocolo completo (telemetria, comandos, ACK, falha do sensor e ressincronização) está em [`docs/CONTRATO_SERIAL_ARDUINO.md`](docs/CONTRATO_SERIAL_ARDUINO.md).

### Firmware da Parada 1
O sketch de referência fica em [`firmware/parada1_ventilacao/parada1_ventilacao.ino`](firmware/parada1_ventilacao/parada1_ventilacao.ino). Para gravar: Arduino IDE → placa **Arduino Uno** → instale a biblioteca **"DHT sensor library" (Adafruit)** → upload.

| Componente | Pino | Observação |
| --- | --- | --- |
| Sensor DHT11 (dados) | D2 | Leitura a cada 2 s; resolução de 1 °C |
| Botão | D3 | Entre o pino e o GND (usa `INPUT_PULLUP`) |
| Relé do cooler | D7 | Ventoinha nos bornes **COM** e **NO** (normalmente aberto) |
| LED RGB (R / G / B) | D9 / D10 / D11 | Com resistor em cada cor |

Ajustes no topo do sketch, conforme o hardware:
- `RELE_ATIVO_EM_LOW = true`: módulos de relé comuns (com optoacoplador, pinos `IN/VCC/GND`) ligam com o pino em LOW. Sintoma de valor errado: a ventoinha liga junto com o Arduino e o painel mostra "DESLIGADA".
- `RGB_ANODO_COMUM`: `true` se o LED RGB for de ânodo comum (perna comum no 5V).

### Comportamento da Parada 1
- **LED:** vermelho = ventoinha desligada; verde = ventoinha ligada.
- **Regra automática com histerese:** liga em `temp >= limite` e só desliga em `temp <= limite - 1.0` (`HISTERESE`). Exemplo com limite 27 °C: liga em 27, desliga em 26; entre os dois, mantém o estado anterior. Sem essa faixa, a temperatura oscilando em volta do limite faria o relé ligar e desligar a cada leitura.
- **Botão físico:** clique curto → modo `MANUAL` e inverte a ventoinha; clique longo (≥ 1,5 s) → volta ao `AUTO`. A Raspberry adota essas mudanças como a configuração salva.
- **Falha do sensor:** o DHT11 falha leituras isoladas; o firmware só declara falha após 3 erros seguidos (~6 s). Em falha, liga a ventoinha por segurança e envia `"temp": null` com `"err": "dht"`.

---

## 🧪 Testes e Verificação de Qualidade

```bash
pytest
mypy src
ruff check src tests
```
