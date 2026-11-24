// Parada 1 (maquete física): DHT11 + relé do cooler + botão + LED RGB.
// Protocolo: docs/CONTRATO_SERIAL_ARDUINO.md (NDJSON a 9600 bps).

#include <DHT.h>

const uint8_t PINO_DHT    = 2;
const uint8_t PINO_BOTAO  = 3;
const uint8_t PINO_RELE   = 7;
const uint8_t PINO_RGB_R  = 10;
const uint8_t PINO_RGB_G  = 9;
const uint8_t PINO_RGB_B  = 8;

const long  BAUD_RATE     = 9600;

const float LIMITE_PADRAO = 30.0;
const float LIMITE_MIN    = -20.0;
const float LIMITE_MAX    = 100.0;
const float HISTERESE     = 1.0;
const unsigned long TELEMETRIA_MS      = 1000;
const unsigned long LEITURA_DHT_MS     = 2000;
const unsigned long PRIMEIRA_LEITURA_MS = 1000; 
const unsigned long DEBOUNCE_MS        = 50;
const unsigned long CLIQUE_LONGO_MS    = 1500;


const uint8_t FALHAS_PARA_ERRO = 3;

// Módulos de relé comuns (optoacoplador, pinos IN/VCC/GND) ligam com o pino em LOW.
const bool RELE_ATIVO_EM_LOW = true;
const bool RGB_ANODO_COMUM   = false;

DHT dht(PINO_DHT, DHT11);

float tempLimit   = LIMITE_PADRAO;
bool  manualMode  = false;
bool  fanOn       = false;
float ultimaTemp  = NAN;
bool  sensorFalha = true;
uint8_t falhasSeguidas = 0;
bool  leituraFeita = false;  // só envia telemetria após a 1ª tentativa de leitura

unsigned long ultimaTelemetriaMs = 0;
unsigned long ultimaLeituraMs    = 0;

const uint8_t TAM_BUFFER = 96;
char bufferSerial[TAM_BUFFER];
uint8_t posBuffer = 0;
bool descartandoLinha = false;

bool estadoBotaoEstavel = HIGH;
bool ultimaLeituraBotao = HIGH;
unsigned long ultimaMudancaBotaoMs = 0;
unsigned long inicioPressaoMs = 0;

void escreverCanal(uint8_t pino, bool aceso) {
  bool nivel = RGB_ANODO_COMUM ? !aceso : aceso;
  digitalWrite(pino, nivel ? HIGH : LOW);
}

void atualizarRgb() {
  escreverCanal(PINO_RGB_R, !fanOn);
  escreverCanal(PINO_RGB_G, fanOn);
  escreverCanal(PINO_RGB_B, false);
}

void setFan(bool ligar) {
  fanOn = ligar;
  bool nivel = RELE_ATIVO_EM_LOW ? !ligar : ligar;
  digitalWrite(PINO_RELE, nivel ? HIGH : LOW);
  atualizarRgb();
}

void aplicarRegra() {
  if (manualMode) return;

  if (sensorFalha) {
    setFan(true);
    return;
  }
  if (!fanOn && ultimaTemp >= tempLimit) {
    setFan(true);
  } else if (fanOn && ultimaTemp <= tempLimit - HISTERESE) {
    setFan(false);
  }
}

void lerSensor() {
  float t = dht.readTemperature();
  if (isnan(t) || t < LIMITE_MIN || t > LIMITE_MAX) {
    if (falhasSeguidas < 255) falhasSeguidas++;
    // Sem nenhuma leitura válida ainda, ou falha persistente: entra em falha.
    if (isnan(ultimaTemp) || falhasSeguidas >= FALHAS_PARA_ERRO) {
      sensorFalha = true;
      ultimaTemp = NAN;
    }
  } else {
    falhasSeguidas = 0;
    sensorFalha = false;
    ultimaTemp = t;
  }
  leituraFeita = true;
  aplicarRegra();
}

void enviarTelemetria() {
  Serial.print(F("{\"temp\":"));
  if (sensorFalha) {
    Serial.print(F("null"));
  } else {
    Serial.print(ultimaTemp, 1);
  }
  Serial.print(F(",\"fan\":"));
  Serial.print(fanOn ? F("true") : F("false"));
  Serial.print(F(",\"limit\":"));
  Serial.print(tempLimit, 1);
  Serial.print(F(",\"mode\":\""));
  Serial.print(manualMode ? F("MANUAL") : F("AUTO"));
  Serial.print(F("\""));
  if (sensorFalha) {
    Serial.print(F(",\"err\":\"dht\""));
  }
  Serial.println(F("}"));
}

void enviarAck(const char* cmd, bool ok) {
  Serial.print(F("{\"ack\":\""));
  Serial.print(cmd);
  Serial.print(F("\",\"ok\":"));
  Serial.print(ok ? F("true") : F("false"));
  Serial.println(F("}"));
}

const char* acharValor(const char* json, const char* chave) {
  char padrao[16];
  snprintf(padrao, sizeof(padrao), "\"%s\"", chave);
  const char* p = strstr(json, padrao);
  if (p == nullptr) return nullptr;
  p = strchr(p + strlen(padrao), ':');
  if (p == nullptr) return nullptr;
  p++;
  while (*p == ' ') p++;
  return p;
}

bool extrairString(const char* json, const char* chave, char* saida, size_t tamanho) {
  const char* p = acharValor(json, chave);
  if (p == nullptr || *p != '"') return false;
  p++;
  size_t i = 0;
  while (*p != '"' && *p != '\0' && i < tamanho - 1) {
    saida[i++] = *p++;
  }
  saida[i] = '\0';
  return *p == '"';
}

void processarComando(const char* linha) {
  char cmd[16];
  if (!extrairString(linha, "cmd", cmd, sizeof(cmd))) return;

  const char* valor = acharValor(linha, "value");
  bool ok = false;

  if (strcmp(cmd, "set_limit") == 0 && valor != nullptr &&
      (isdigit(*valor) || *valor == '-')) {
    float v = atof(valor);
    if (v >= LIMITE_MIN && v <= LIMITE_MAX) {
      tempLimit = v;
      ok = true;
    }
  } else if (strcmp(cmd, "set_fan") == 0 && valor != nullptr) {
    if (strncmp(valor, "true", 4) == 0) {
      manualMode = true;
      setFan(true);
      ok = true;
    } else if (strncmp(valor, "false", 5) == 0) {
      manualMode = true;
      setFan(false);
      ok = true;
    }
  } else if (strcmp(cmd, "set_mode") == 0) {
    char modo[8];
    if (extrairString(linha, "value", modo, sizeof(modo))) {
      if (strcmp(modo, "AUTO") == 0) {
        manualMode = false;
        ok = true;
      } else if (strcmp(modo, "MANUAL") == 0) {
        manualMode = true;
        ok = true;
      }
    }
  }

  enviarAck(cmd, ok);
  if (ok) {
    aplicarRegra();
    enviarTelemetria();
    ultimaTelemetriaMs = millis();
  }
}

void lerSerial() {
  while (Serial.available() > 0) {
    char c = Serial.read();

    if (c == '\r') continue;
    if (c == '\n') {
      if (!descartandoLinha && posBuffer > 0) {
        bufferSerial[posBuffer] = '\0';
        processarComando(bufferSerial);
      }
      posBuffer = 0;
      descartandoLinha = false;
      continue;
    }
    if (descartandoLinha) continue;

    if (posBuffer < TAM_BUFFER - 1) {
      bufferSerial[posBuffer++] = c;
    } else {
      descartandoLinha = true;
      posBuffer = 0;
    }
  }
}

void lerBotao() {
  bool leitura = digitalRead(PINO_BOTAO);
  unsigned long agora = millis();

  if (leitura != ultimaLeituraBotao) {
    ultimaMudancaBotaoMs = agora;
    ultimaLeituraBotao = leitura;
  }
  if (agora - ultimaMudancaBotaoMs < DEBOUNCE_MS) return;
  if (leitura == estadoBotaoEstavel) return;

  estadoBotaoEstavel = leitura;

  if (estadoBotaoEstavel == LOW) {
    inicioPressaoMs = agora;
    return;
  }

  if (agora - inicioPressaoMs >= CLIQUE_LONGO_MS) {
    manualMode = false;
    aplicarRegra();
  } else {
    manualMode = true;
    setFan(!fanOn);
  }
  enviarTelemetria();
  ultimaTelemetriaMs = agora;
}

void setup() {
  Serial.begin(BAUD_RATE);

  // Define o nível "desligado" ANTES de virar saída: pinMode(OUTPUT) sozinho
  // começa em LOW e acionaria um relé ativo em LOW por um instante no boot.
  digitalWrite(PINO_RELE, RELE_ATIVO_EM_LOW ? HIGH : LOW);
  pinMode(PINO_RELE, OUTPUT);
  pinMode(PINO_RGB_R, OUTPUT);
  pinMode(PINO_RGB_G, OUTPUT);
  pinMode(PINO_RGB_B, OUTPUT);
  pinMode(PINO_BOTAO, INPUT_PULLUP);

  setFan(false);
  dht.begin();

  // Agenda a 1ª leitura para PRIMEIRA_LEITURA_MS após o boot (aritmética unsigned).
  ultimaLeituraMs = millis() - (LEITURA_DHT_MS - PRIMEIRA_LEITURA_MS);
}

void loop() {
  unsigned long agora = millis();

  lerSerial();
  lerBotao();

  if (agora - ultimaLeituraMs >= LEITURA_DHT_MS) {
    ultimaLeituraMs = agora;
    lerSensor();
  }

  if (leituraFeita && agora - ultimaTelemetriaMs >= TELEMETRIA_MS) {
    ultimaTelemetriaMs = agora;
    enviarTelemetria();
  }
}
