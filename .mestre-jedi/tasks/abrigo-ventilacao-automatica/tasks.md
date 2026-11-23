# Tasks — Dashboard Central de Monitoramento e Controle de Paradas de Ônibus (Raspberry Pi)

**Demanda:** `abrigo-ventilacao-automatica`  
**Versão:** `1.0`  
**Status:** ✅ Concluído (Todas as 20 tarefas implementadas e validadas com `pytest`, `mypy --strict` e `ruff`)  
**Referências:** [prd.md](./prd.md) (✅ Aprovado — v1.3) | [tech_spec.md](./tech_spec.md) (✅ Aprovada — v1.0)

> **Regra:** Nenhuma linha de código pode ser implementada antes da aprovação formal do PRD, da TechSpec e deste checklist de Tasks.

---

## Fase 1 — Setup do Projeto e Contrato Externo
- [x] **T-01:** Criar `pyproject.toml` (Python `>=3.12`, dependências `fastapi`, `uvicorn[standard]`, `pyserial`, ferramentas de dev `pytest`, `pytest-asyncio`, `httpx`, `ruff`, `mypy` com configuração estrita), `.gitignore` e a árvore inicial de pacotes em `src/parada_onibus/` e `tests/`.
- [x] **T-02:** Criar `config.example.toml` com todas as configurações externas (porta HTTP `5000`, porta Serial, baud rate `9600`, timeout `6.0s`, caminho SQLite, intervalo de persistência, limite inicial `30.0 °C`, nomes e fatores das 3 paradas — `1.00`, `1.05`, `0.95`) — *(RNF-06)*.
- [x] **T-03:** Criar `docs/CONTRATO_SERIAL_ARDUINO.md` documentando o contrato bidirecional NDJSON (telemetria Arduino → Raspi, comandos Raspi → Arduino, exemplos e boas práticas) pronto para entrega ao colega do Arduino — *(RF-01, RF-04)*.

## Fase 2 — Domínio e Contratos (Core Puro)
- [x] **T-04:** Implementar `src/parada_onibus/domain/models.py` com os `StrEnum`s (`StopOrigin`, `ControlMode`, `StopStatus`, `CommandType`, `EventType`) e `@dataclass(frozen=True, slots=True)` (`StopConfig`, `AppConfig`, `ArduinoTelemetry`, `ArduinoCommand`, `StopReading`, `StopState`, `SystemEvent`, `NetworkSummary`) — *(RNF-05)*.
- [x] **T-05:** Implementar `src/parada_onibus/domain/ports.py` definindo os `Protocol`s (`TelemetryTransport`, `DataRepository`) para inversão de dependência entre Serial real, Simulador e SQLite — *(RNF-05)*.
- [x] **T-06:** Implementar `src/parada_onibus/domain/parser.py` com parsing/validação defensiva de NDJSON (faixas finitas, campos obrigatórios/opcionais, reconhecimento de `ack`, descarte seguro de linhas corrompidas) e serialização de comandos JSON (`set_limit`, `set_fan`, `set_mode`) — *(RF-02, RNF-03)*.
- [x] **T-07:** Implementar `src/parada_onibus/domain/deriver.py` com o motor de derivação das Paradas 2 e 3 (`temp_real * fator`), avaliação independente de `temp >= limite`, override manual (`turn_off`) e retorno ao modo `AUTO` — *(RF-03, RF-04)*.

## Fase 3 — Infraestrutura e Aplicação
- [x] **T-08:** Implementar `src/parada_onibus/infrastructure/config.py` carregando `config.toml` via `tomllib` (stdlib) com sobrescrita por variáveis de ambiente e validação de faixas/paradas — *(RNF-06)*.
- [x] **T-09:** Implementar `src/parada_onibus/infrastructure/sqlite_repository.py` com modo `WAL` (preservação do cartão SD), schema das tabelas `readings`, `events` e `stop_settings`, gravação em transação única por ciclo das 3 paradas, consultas de histórico e restauração de ajustes após reinicialização — *(RF-06, RNF-01)*.
- [x] **T-10:** Implementar `src/parada_onibus/infrastructure/csv_exporter.py` com exportação em streaming no formato `timestamp,parada_id,nome,origem,temperatura,limite,ventilacao,modo` (geral ou filtrada por `stop_id`) — *(RF-07)*.
- [x] **T-11:** Implementar `src/parada_onibus/infrastructure/serial_transport.py` com leitura/escrita `pyserial` em thread dedicada, reconexão automática com backoff exponencial (`1s → 2s → 5s`) e encerramento limpo — *(RF-01, RF-08, RNF-03)*.
- [x] **T-12:** Implementar `src/parada_onibus/infrastructure/simulator_transport.py` para o Modo Simulador Completo (sem hardware), gerando curva térmica realista da Parada 1 cruzando `30 °C` e respondendo aos comandos `set_limit`, `set_fan` e `set_mode` — *(RF-09)*.
- [x] **T-13:** Implementar `src/parada_onibus/application/broadcaster.py` (`SSEBroadcaster`) com filas assíncronas por cliente (`maxsize=32`), proteção contra backpressure e keep-alive periódico — *(RNF-01, RNF-02)*.
- [x] **T-14:** Implementar `src/parada_onibus/application/manager.py` (`StopNetworkManager`) integrando transporte, parser, derivador, SQLite e broadcaster, incluindo watchdog de perda de sinal (`6.0s`) que marca `DESCONECTADO` e pausa derivações virtuais, log de transições de ventilação e despacho de comandos — *(RF-03, RF-04, RF-08)*.

## Fase 4 — Interface Web, API REST/SSE e Entrypoint
- [x] **T-15:** Implementar `src/parada_onibus/web/routes.py`, `src/parada_onibus/web/app.py` (FastAPI + `lifespan` + rotas REST, CSV e SSE `/api/stream`) e `src/parada_onibus/__main__.py` (`python -m parada_onibus [--simulator] [--port 5000]`) — *(RF-04, RF-05, RF-07, RNF-02)*.
- [x] **T-16:** Baixar/empacotar `Chart.js` localmente em `src/parada_onibus/web/static/vendor/chart.umd.min.js` para operação 100% offline na rede local — *(RNF-04)*.
- [x] **T-17:** Implementar o frontend responsivo em `src/parada_onibus/web/static/index.html`, `styles.css` e `app.js`:
  - Resumo consolidado da rede (status Serial, média/máxima, contagem ventilando, exportação CSV geral e por parada).
  - Cards individuais das 3 paradas (`REAL` vs `SIMULADA`), paleta de cores de status (**Verde**, **Azul/Vermelho**, **Amarelo/Cinza**), temperatura, modo, limite e **gráfico individual `Chart.js` visível por padrão em cada parada** com linha horizontal do limite.
  - Controles individuais por parada (alterar limite, desligar ventilação / voltar ao automático).
  - Painel analítico complementar com gráfico comparativo das 3 paradas e histórico recente de eventos, atualizados via SSE — *(RF-04, RF-05, RF-07, RNF-04)*.

## Fase 5 — Testes Automatizados, Validação e Documentação Final
- [x] **T-18:** Implementar testes unitários em `tests/unit/` (`test_parser.py`, `test_deriver.py`, `test_config.py`) — *(RNF-05)*.
- [x] **T-19:** Implementar testes de integração em `tests/integration/` (`test_sqlite_repository.py`, `test_api_and_sse.py`, `test_simulator_e2e.py`) cobrindo SQLite, API, SSE, CSV e simulação ponta a ponta — *(RF-06, RF-07, RF-09, RNF-05)*.
- [x] **T-20:** Executar suíte completa de verificação (`pytest`, `mypy src`, `ruff check src tests`), validar execução real em modo simulador e atualizar o `README.md` raiz com guia prático de instalação e uso na Raspberry Pi e em ambiente local — *(RNF-05, RNF-06)*.
