(() => {
  "use strict";

  const MAX_POINTS = 120;
  const STOP_COLORS = ["#38bdf8", "#f97316", "#a855f7"];

  const state = {
    stops: new Map(),
    stopOrder: [],
    individualCharts: new Map(),
    comparativeChart: null,
    events: [],
    eventSource: null,
    toastTimer: null,
  };

  const els = {
    connectionPill: document.getElementById("connection-pill"),
    connectionText: document.getElementById("connection-text"),
    csvStopSelect: document.getElementById("csv-stop-select"),
    btnExportCsv: document.getElementById("btn-export-csv"),
    kpiMode: document.getElementById("kpi-mode"),
    kpiStreamStatus: document.getElementById("kpi-stream-status"),
    kpiAvgTemp: document.getElementById("kpi-avg-temp"),
    kpiMaxTemp: document.getElementById("kpi-max-temp"),
    kpiVentCount: document.getElementById("kpi-vent-count"),
    stopsGrid: document.getElementById("stops-grid"),
    eventsList: document.getElementById("events-list"),
    comparativeCanvas: document.getElementById("comparative-chart"),
    toast: document.getElementById("toast"),
  };

  function formatTime(isoString) {
    if (!isoString) return "--:--:--";
    const date = new Date(isoString);
    if (Number.isNaN(date.getTime())) return "--:--:--";
    return date.toLocaleTimeString("pt-BR", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  }

  function formatTemp(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) {
      return "-- °C";
    }
    return `${Number(value).toFixed(1)} °C`;
  }

  function showToast(message, isError = false) {
    els.toast.textContent = message;
    els.toast.classList.remove("hidden", "toast-error");
    if (isError) {
      els.toast.classList.add("toast-error");
    }
    if (state.toastTimer) {
      clearTimeout(state.toastTimer);
    }
    state.toastTimer = setTimeout(() => {
      els.toast.classList.add("hidden");
    }, 3600);
  }

  function renderSummary(summary) {
    if (!summary) return;
    const isSim = summary.operation_mode === "simulator";
    els.kpiMode.textContent = isSim ? "SIMULADOR" : "ARDUINO SERIAL";
    els.kpiAvgTemp.textContent = formatTemp(summary.average_temperature);
    els.kpiMaxTemp.textContent = formatTemp(summary.max_temperature);
    els.kpiVentCount.textContent = `${summary.ventilating_count} / ${summary.total_stops}`;

    els.connectionPill.classList.remove("status-connected", "status-disconnected");
    if (summary.connected) {
      els.connectionPill.classList.add("status-connected");
      els.connectionText.textContent = isSim
        ? "Simulador Ativo (3 paradas)"
        : "Arduino Conectado (Serial)";
    } else {
      els.connectionPill.classList.add("status-disconnected");
      els.connectionText.textContent = "DESCONECTADO — Sem sinal";
    }
  }

  function ensureStopCards(stops, historyMap) {
    if (state.stopOrder.length > 0) return;

    els.stopsGrid.innerHTML = "";
    els.csvStopSelect.innerHTML = '<option value="">Todas as paradas</option>';

    stops.forEach((stop, index) => {
      state.stopOrder.push(stop.stop_id);
      state.stops.set(stop.stop_id, stop);

      const option = document.createElement("option");
      option.value = stop.stop_id;
      option.textContent = stop.name;
      els.csvStopSelect.appendChild(option);

      const card = buildStopCardElement(stop);
      els.stopsGrid.appendChild(card);

      const canvas = card.querySelector("canvas");
      const initialHistory = (historyMap && historyMap[stop.stop_id]) || [];
      const chart = createIndividualChart(canvas, stop, initialHistory, STOP_COLORS[index % STOP_COLORS.length]);
      state.individualCharts.set(stop.stop_id, chart);

      updateStopCard(stop);
    });

    createComparativeChart(stops, historyMap || {});
  }

  function buildStopCardElement(stop) {
    const isReal = stop.origin === "REAL";
    const originText = isReal
      ? "REAL · ARDUINO"
      : `SIMULADA · ×${Number(stop.derivation_factor).toFixed(2)}`;

    const card = document.createElement("article");
    card.className = `stop-card state-${stop.status}`;
    card.dataset.stopId = stop.stop_id;
    card.innerHTML = `
      <div class="stop-header">
        <div>
          <h3 class="stop-title">${escapeHtml(stop.name)}</h3>
          <div class="stop-badges">
            <span class="badge ${isReal ? "badge-real" : "badge-sim"}">${originText}</span>
            <span class="badge badge-mode" data-role="mode-badge">AUTO</span>
          </div>
        </div>
        <span class="status-pill state-${stop.status}" data-role="status-pill">${stop.status}</span>
      </div>

      <div class="stop-metrics">
        <div class="temp-display" data-role="temp-value">-- °C</div>
        <div class="temp-meta">
          <span>Limite: <strong data-role="limit-value">${Number(stop.temp_limit).toFixed(1)} °C</strong></span>
          <span>Ventilação: <strong data-role="fan-value">DESLIGADA</strong></span>
          <span>Atualizado: <strong data-role="updated-value">--:--:--</strong></span>
        </div>
      </div>

      <div class="chart-container">
        <canvas aria-label="Gráfico de temperatura de ${escapeHtml(stop.name)}"></canvas>
      </div>

      <div class="stop-controls">
        <form class="limit-form" data-role="limit-form">
          <label for="limit-${stop.stop_id}">Limite (°C):</label>
          <input
            id="limit-${stop.stop_id}"
            type="number"
            min="15"
            max="50"
            step="0.5"
            value="${Number(stop.temp_limit).toFixed(1)}"
            data-role="limit-input"
            required
          />
          <button type="submit" class="btn btn-primary">Aplicar</button>
        </form>
        <div class="vent-actions">
          <button type="button" class="btn btn-danger" data-role="btn-off">
            Desligar Ventilação
          </button>
          <button type="button" class="btn btn-auto" data-role="btn-auto">
            Voltar ao Automático
          </button>
        </div>
      </div>
    `;

    const limitForm = card.querySelector('[data-role="limit-form"]');
    const limitInput = card.querySelector('[data-role="limit-input"]');
    const btnOff = card.querySelector('[data-role="btn-off"]');
    const btnAuto = card.querySelector('[data-role="btn-auto"]');

    limitForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const parsed = Number(limitInput.value);
      if (Number.isNaN(parsed)) {
        showToast("Informe um limite numérico válido.", true);
        return;
      }
      await sendLimitCommand(stop.stop_id, parsed);
    });

    btnOff.addEventListener("click", async () => {
      await sendVentilationCommand(stop.stop_id, "turn_off");
    });

    btnAuto.addEventListener("click", async () => {
      await sendVentilationCommand(stop.stop_id, "auto");
    });

    return card;
  }

  function updateStopCard(stop) {
    state.stops.set(stop.stop_id, stop);
    const card = els.stopsGrid.querySelector(`[data-stop-id="${stop.stop_id}"]`);
    if (!card) return;

    card.className = `stop-card state-${stop.status}`;

    const statusPill = card.querySelector('[data-role="status-pill"]');
    statusPill.className = `status-pill state-${stop.status}`;
    statusPill.textContent = stop.status === "DESCONECTADO" ? "SEM SINAL" : stop.status;

    const modeBadge = card.querySelector('[data-role="mode-badge"]');
    modeBadge.textContent = stop.mode === "MANUAL" ? "MANUAL" : "AUTOMÁTICO";
    modeBadge.className = `badge ${stop.mode === "MANUAL" ? "badge-mode-manual" : "badge-mode"}`;

    const tempEl = card.querySelector('[data-role="temp-value"]');
    tempEl.textContent = formatTemp(stop.temperature);
    tempEl.classList.toggle(
      "hot",
      stop.temperature !== null && Number(stop.temperature) >= Number(stop.temp_limit)
    );

    card.querySelector('[data-role="limit-value"]').textContent = `${Number(stop.temp_limit).toFixed(1)} °C`;
    card.querySelector('[data-role="fan-value"]').textContent = stop.ventilation ? "LIGADA" : "DESLIGADA";
    card.querySelector('[data-role="updated-value"]').textContent = formatTime(stop.last_updated);

    const limitInput = card.querySelector('[data-role="limit-input"]');
    if (document.activeElement !== limitInput) {
      limitInput.value = Number(stop.temp_limit).toFixed(1);
    }

    const btnAuto = card.querySelector('[data-role="btn-auto"]');
    btnAuto.disabled = stop.mode === "AUTO";

    // Atualiza a linha de limite no gráfico individual imediatamente
    const chart = state.individualCharts.get(stop.stop_id);
    if (chart && chart.data.datasets[1]) {
      const limitData = chart.data.datasets[1].data;
      if (limitData.length > 0) {
        limitData[limitData.length - 1] = Number(stop.temp_limit);
        chart.update("none");
      }
    }
  }

  function createIndividualChart(canvas, stop, history, lineColor) {
    const labels = history.map((r) => formatTime(r.timestamp));
    const temps = history.map((r) => Number(r.temperature));
    const limits = history.map((r) => Number(r.temp_limit));

    return new Chart(canvas.getContext("2d"), {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "Temperatura (°C)",
            data: temps,
            borderColor: lineColor,
            backgroundColor: `${lineColor}26`,
            borderWidth: 2.2,
            fill: true,
            tension: 0.25,
            pointRadius: 0,
            pointHoverRadius: 4,
          },
          {
            label: "Limite (°C)",
            data: limits,
            borderColor: "#ef4444",
            borderWidth: 1.5,
            borderDash: [6, 4],
            fill: false,
            tension: 0,
            pointRadius: 0,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: {
            display: true,
            labels: { color: "#cbd5e1", boxWidth: 12, font: { size: 11 } },
          },
        },
        scales: {
          x: {
            ticks: { color: "#94a3b8", maxTicksLimit: 5, font: { size: 10 } },
            grid: { color: "rgba(148,163,184,0.08)" },
          },
          y: {
            suggestedMin: 22,
            suggestedMax: 38,
            ticks: { color: "#94a3b8", font: { size: 10 } },
            grid: { color: "rgba(148,163,184,0.12)" },
          },
        },
      },
    });
  }

  function createComparativeChart(stops, historyMap) {
    if (state.comparativeChart) {
      state.comparativeChart.destroy();
    }

    const firstId = stops[0]?.stop_id;
    const baseHistory = (firstId && historyMap[firstId]) || [];
    const labels = baseHistory.map((r) => formatTime(r.timestamp));

    const datasets = stops.map((stop, idx) => {
      const series = historyMap[stop.stop_id] || [];
      return {
        label: stop.name,
        data: series.map((r) => Number(r.temperature)),
        borderColor: STOP_COLORS[idx % STOP_COLORS.length],
        borderWidth: 2.2,
        fill: false,
        tension: 0.25,
        pointRadius: 0,
      };
    });

    state.comparativeChart = new Chart(els.comparativeCanvas.getContext("2d"), {
      type: "line",
      data: { labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: {
            position: "top",
            labels: { color: "#e2e8f0", boxWidth: 14, font: { size: 12 } },
          },
        },
        scales: {
          x: {
            ticks: { color: "#94a3b8", maxTicksLimit: 8, font: { size: 11 } },
            grid: { color: "rgba(148,163,184,0.08)" },
          },
          y: {
            suggestedMin: 22,
            suggestedMax: 38,
            ticks: { color: "#94a3b8", font: { size: 11 } },
            grid: { color: "rgba(148,163,184,0.12)" },
          },
        },
      },
    });
  }

  function appendReadingsToCharts(readings) {
    if (!Array.isArray(readings) || readings.length === 0) return;

    const label = formatTime(readings[0].timestamp);

    readings.forEach((reading) => {
      const chart = state.individualCharts.get(reading.stop_id);
      if (!chart) return;
      chart.data.labels.push(formatTime(reading.timestamp));
      chart.data.datasets[0].data.push(Number(reading.temperature));
      chart.data.datasets[1].data.push(Number(reading.temp_limit));

      if (chart.data.labels.length > MAX_POINTS) {
        chart.data.labels.shift();
        chart.data.datasets[0].data.shift();
        chart.data.datasets[1].data.shift();
      }
      chart.update("none");
    });

    if (state.comparativeChart) {
      const comp = state.comparativeChart;
      const byId = new Map(readings.map((r) => [r.stop_id, r]));
      comp.data.labels.push(label);
      state.stopOrder.forEach((stopId, idx) => {
        const rec = byId.get(stopId);
        comp.data.datasets[idx].data.push(rec ? Number(rec.temperature) : null);
      });
      if (comp.data.labels.length > MAX_POINTS) {
        comp.data.labels.shift();
        comp.data.datasets.forEach((ds) => ds.data.shift());
      }
      comp.update("none");
    }
  }

  function renderEvents(events) {
    state.events = Array.isArray(events) ? events.slice(0, 40) : [];
    if (state.events.length === 0) {
      els.eventsList.innerHTML = '<li class="events-empty">Nenhum evento registrado ainda.</li>';
      return;
    }

    els.eventsList.innerHTML = state.events
      .map(
        (ev) => `
          <li class="event-item">
            <div class="event-top">
              <span class="event-tag">${escapeHtml(ev.event_type)}</span>
              <span>${formatTime(ev.timestamp)}</span>
            </div>
            <div>${escapeHtml(ev.message)}</div>
          </li>
        `
      )
      .join("");
  }

  function prependEvent(ev) {
    state.events.unshift(ev);
    if (state.events.length > 40) {
      state.events.pop();
    }
    renderEvents(state.events);
  }

  async function sendLimitCommand(stopId, limit) {
    try {
      const res = await fetch(`/api/stops/${encodeURIComponent(stopId)}/limit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ limit }),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        showToast(data.message || "Não foi possível alterar o limite.", true);
        return;
      }
      if (data.state) {
        updateStopCard(data.state);
      }
      showToast(data.message || "Limite atualizado com sucesso.");
    } catch {
      showToast("Erro de comunicação com o servidor na Raspberry Pi.", true);
    }
  }

  async function sendVentilationCommand(stopId, action) {
    try {
      const res = await fetch(`/api/stops/${encodeURIComponent(stopId)}/ventilation`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action }),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        showToast(data.message || "Não foi possível enviar o comando.", true);
        return;
      }
      if (data.state) {
        updateStopCard(data.state);
      }
      showToast(data.message || "Comando aplicado com sucesso.");
    } catch {
      showToast("Erro de comunicação com o servidor na Raspberry Pi.", true);
    }
  }

  function applySnapshot(snapshot) {
    if (!snapshot) return;
    ensureStopCards(snapshot.stops || [], snapshot.history || {});
    renderSummary(snapshot.summary);
    (snapshot.stops || []).forEach(updateStopCard);
    if (snapshot.events) {
      renderEvents(snapshot.events);
    }
  }

  function connectSSE() {
    if (state.eventSource) {
      state.eventSource.close();
    }

    const source = new EventSource("/api/stream");
    state.eventSource = source;

    source.onopen = () => {
      els.kpiStreamStatus.textContent = "Stream SSE ativo em tempo real";
    };

    source.onerror = () => {
      els.kpiStreamStatus.textContent = "Reconectando stream SSE...";
    };

    source.addEventListener("snapshot", (ev) => {
      applySnapshot(JSON.parse(ev.data));
    });

    source.addEventListener("reading", (ev) => {
      const payload = JSON.parse(ev.data);
      renderSummary(payload.summary);
      (payload.stops || []).forEach(updateStopCard);
      appendReadingsToCharts(payload.readings || []);
    });

    source.addEventListener("status", (ev) => {
      const payload = JSON.parse(ev.data);
      renderSummary(payload.summary);
      (payload.stops || []).forEach(updateStopCard);
    });

    source.addEventListener("event_log", (ev) => {
      prependEvent(JSON.parse(ev.data));
    });
  }

  function escapeHtml(text) {
    return String(text ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  els.btnExportCsv.addEventListener("click", () => {
    const selected = els.csvStopSelect.value;
    const url = selected
      ? `/api/export/csv?stop_id=${encodeURIComponent(selected)}`
      : "/api/export/csv";
    window.location.href = url;
  });

  async function bootstrap() {
    try {
      const res = await fetch("/api/state");
      if (res.ok) {
        const snapshot = await res.json();
        applySnapshot(snapshot);
      }
    } catch {
      showToast("Não foi possível carregar o estado inicial.", true);
    }
    connectSSE();
  }

  bootstrap();
})();
