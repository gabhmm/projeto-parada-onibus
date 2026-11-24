# Contrato de Comunicação Serial (NDJSON) — Arduino ↔ Raspberry Pi

Este documento define o protocolo oficial entre o **Arduino (Parada 1 — Maquete Física)** e o servidor da **Raspberry Pi**.

---

## 1. Parâmetros da Porta Serial
- **Conexão:** Cabo USB (`Arduino` → `Raspberry Pi`, normalmente `/dev/ttyACM0` ou `/dev/ttyUSB0`).
- **Velocidade (Baud Rate):** `9600` bps *(padrão; configurável na Raspi se necessário)*.
- **Framing:** `8N1` (padrão do `Serial.begin(9600)`).
- **Formato de Mensagem:** **NDJSON** — **1 objeto JSON completo por linha**, terminado obrigatoriamente por quebra de linha (`\n`, ou seja, `Serial.println(...)`).

---

## 2. Telemetria: Arduino → Raspberry Pi

O Arduino executa localmente e de forma autônoma a regra de ventilação com histerese (`liga cooler e LED em temp >= limite; desliga em temp <= limite - 1.0`) e envia **1 linha JSON a cada ~1 segundo** (recomendado entre `1s` e `2s`; acima de `6s` sem envio a Raspi marcará `DESCONECTADO`).

### Payload de Telemetria
```json
{"temp": 31.4, "fan": true, "limit": 30.0, "mode": "AUTO"}
```

| Campo | Tipo | Obrigatório | Descrição |
| --- | --- | --- | --- |
| `temp` | `float` ou `null` | **Sim** | Temperatura medida pelo sensor em °C (valores válidos: `-20.0` a `100.0`). `null` indica falha do sensor (ver 2.1). |
| `fan` | `bool` | **Sim** | `true` se a ventilação/cooler está ligada; `false` se desligada. |
| `limit` | `float` | Não | Temperatura limite ativa no Arduino em °C (padrão inicial: `30.0`). |
| `mode` | `string` | Não | `"AUTO"` (regra automática ativa) ou `"MANUAL"` (override via painel ou botão físico). |
| `err` | `string` | Não | Código da falha do sensor quando `temp` é `null` (ex.: `"dht"`). |

### 2.1 Falha do sensor
Se a leitura do sensor falhar (NaN ou fora da faixa), o Arduino **continua enviando telemetria** com `temp: null`, liga o cooler por segurança (em `AUTO`) e informa o motivo em `err`:
```json
{"temp": null, "fan": true, "limit": 30.0, "mode": "AUTO", "err": "dht"}
```
A Raspberry Pi mantém a conexão como ativa, exibe o status `FALHA_SENSOR` na Parada 1, não grava leituras sem temperatura e pausa a derivação das Paradas 2 e 3 (que também passam a `FALHA_SENSOR`, ventilando em `AUTO`). Os eventos `SENSOR_FAULT` / `SENSOR_RECOVERED` são registrados no histórico.

### 2.2 Botão físico
- **Clique curto:** entra em `MANUAL` e inverte o cooler.
- **Clique longo (≥ 1,5 s):** volta para `AUTO`.

A Raspberry Pi adota essas mudanças de modo/cooler (vindas da telemetria) como a nova configuração salva da Parada 1.

> **Observação:** Linhas soltas de debug que não sejam JSON válido são ignoradas com segurança pela Raspberry Pi, mas evite enviar texto fora de JSON em produção.

---

## 3. Comandos: Raspberry Pi → Arduino

Somente quando o operador clica em um controle da **Parada 1** no dashboard Web, a Raspberry Pi envia 1 linha JSON terminada com `\n`:

### 3.1 Alterar temperatura limite
```json
{"cmd": "set_limit", "value": 28.5}
```
- **Ação esperada no Arduino:** atualizar a variável `tempLimit = 28.5` (em °C). Se o Arduino estiver em modo `AUTO`, passa a usar o novo limite imediatamente.

### 3.2 Desligar ventilação (Override Manual)
```json
{"cmd": "set_fan", "value": false}
```
- **Ação esperada no Arduino:** entrar em modo `MANUAL` e desligar o(s) cooler(s) (e atualizar o LED RGB), mantendo a ventilação desligada até receber o comando de retorno ao automático.

### 3.3 Voltar ao modo Automático
```json
{"cmd": "set_mode", "value": "AUTO"}
```
- **Ação esperada no Arduino:** sair do override manual e voltar a aplicar automaticamente a regra `temp >= tempLimit`.

### Confirmação (ACK)
Não é obrigatório implementar handshake extra: o próprio próximo envio de telemetria (~1s) com os novos valores de `limit`, `fan` e `mode` já confirma a aplicação do comando. Se desejar, o Arduino pode enviar opcionalmente:
```json
{"ack": "set_limit", "ok": true}
```
Um ACK com `"ok": false` é registrado no histórico como `COMMAND_REJECTED`.

### 3.4 Ressincronização da configuração salva
O Arduino **não guarda** o limite nem o modo: a cada boot volta para `30.0` / `AUTO`. Abrir a porta Serial reinicia o Arduino Uno (reset via DTR), então isso acontece toda vez que o servidor inicia ou o cabo é reconectado.

A Raspberry Pi é a fonte da verdade (configuração persistida no SQLite):
1. Na primeira telemetria válida após cada (re)conexão — ou seja, quando o Arduino já saiu do bootloader — compara `limit`/`mode`/`fan` com a configuração salva.
2. Se divergir, reenvia `set_limit` e/ou `set_mode`/`set_fan`, repetindo a cada 3 s, até 3 tentativas.
3. Se o Arduino não confirmar, adota os valores do Arduino (evento `STATE_RESYNC`) para não entrar em laço.
4. Com a conexão ativa, um `limit` diferente do salvo (o Arduino não altera o limite sozinho) indica reinício do Arduino (ex.: queda de tensão ao chavear o relé) e dispara nova ressincronização.

Por isso o Arduino deve sempre incluir `limit` e `mode` na telemetria.

---

## 4. Exemplo Mínimo de Referência no Arduino (Sketch)

```cpp
// Exemplo didático usando Serial nativo (para parsing completo recomenda-se ArduinoJson)
float tempLimit = 30.0;
bool manualMode = false;
bool fanOn = false;

void enviarTelemetria(float tempC) {
  Serial.print("{\"temp\":");
  Serial.print(tempC, 2);
  Serial.print(",\"fan\":");
  Serial.print(fanOn ? "true" : "false");
  Serial.print(",\"limit\":");
  Serial.print(tempLimit, 1);
  Serial.print(",\"mode\":\"");
  Serial.print(manualMode ? "MANUAL" : "AUTO");
  Serial.println("\"}");
}
```
