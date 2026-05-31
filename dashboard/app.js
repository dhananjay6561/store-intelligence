'use strict';

const API_BASE = window.API_BASE || 'http://localhost:8000';
const REFRESH_MS = 2000;
const CHART_MAX_POINTS = 30;
const QUEUE_WARN = 5;
const QUEUE_CRITICAL = 8;

// Conversion rate history for sparkline
const convHistory = { labels: [], data: [] };

// Chart.js instance
let convChart = null;

function initChart() {
  const ctx = document.getElementById('conversionChart').getContext('2d');
  convChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: convHistory.labels,
      datasets: [{
        label: 'Conversion Rate',
        data: convHistory.data,
        borderColor: '#6c63ff',
        backgroundColor: 'rgba(108,99,255,0.08)',
        tension: 0.4,
        pointRadius: 2,
        borderWidth: 2,
        fill: true,
      }],
    },
    options: {
      animation: { duration: 300 },
      scales: {
        y: {
          min: 0, max: 1,
          ticks: { color: '#8892a4', callback: v => (v * 100).toFixed(0) + '%' },
          grid: { color: '#2e3347' },
        },
        x: {
          ticks: { color: '#8892a4', maxRotation: 0, autoSkip: true, maxTicksLimit: 6 },
          grid: { color: '#2e3347' },
        },
      },
      plugins: { legend: { display: false } },
    },
  });
}

function pushConversionPoint(rate) {
  const label = new Date().toLocaleTimeString('en-IN', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
  convHistory.labels.push(label);
  convHistory.data.push(rate);
  if (convHistory.labels.length > CHART_MAX_POINTS) {
    convHistory.labels.shift();
    convHistory.data.shift();
  }
  if (convChart) {
    convChart.update('none');
  }
}

function updateKpis(metrics) {
  document.getElementById('valVisitors').textContent = metrics.unique_visitors ?? '—';

  const rate = metrics.conversion_rate ?? 0;
  document.getElementById('valConversion').textContent = (rate * 100).toFixed(1) + '%';
  document.getElementById('kvConversion').className = 'kpi-card' + (rate < 0.1 ? ' warn' : '');

  const qd = metrics.queue_depth_current ?? 0;
  document.getElementById('valQueue').textContent = qd;
  const qCard = document.getElementById('kvQueue');
  qCard.className = 'kpi-card' + (qd >= QUEUE_CRITICAL ? ' alert' : qd >= QUEUE_WARN ? ' warn' : '');

  const ar = metrics.abandonment_rate ?? 0;
  document.getElementById('valAbandonment').textContent = (ar * 100).toFixed(1) + '%';
  document.getElementById('kvAbandonment').className = 'kpi-card' + (ar > 0.4 ? ' alert' : ar > 0.2 ? ' warn' : '');

  pushConversionPoint(rate);
}

function zoneColour(score) {
  // Interpolate from cool blue → warm amber → hot red based on score 0-100
  if (score >= 80) return `rgba(239,68,68,${0.3 + score / 250})`;
  if (score >= 50) return `rgba(234,179,8,${0.2 + score / 250})`;
  if (score >= 20) return `rgba(59,130,246,${0.15 + score / 400})`;
  return 'rgba(46,51,71,0.9)';
}

function renderHeatmap(zones) {
  const grid = document.getElementById('zoneGrid');
  if (!zones || zones.length === 0) return;
  grid.innerHTML = '';
  zones.forEach(z => {
    const cell = document.createElement('div');
    cell.className = 'zone-cell';
    cell.style.background = zoneColour(z.score);
    cell.innerHTML = `
      <div class="zone-name">${z.zone_id.replace(/_/g, ' ')}</div>
      <div class="zone-score">${z.score}</div>
      <div class="zone-meta">${z.visit_count} visits · ${(z.avg_dwell_ms / 1000).toFixed(0)}s avg dwell</div>
      <div class="zone-meta" style="opacity:0.5">${z.data_confidence}</div>
    `;
    grid.appendChild(cell);
  });
}

function renderAnomalies(anomalies) {
  const list = document.getElementById('anomalyList');
  const badge = document.getElementById('anomalyBadge');

  badge.textContent = anomalies.length;
  badge.className = 'anomaly-badge' + (anomalies.length === 0 ? ' zero' : '');

  if (anomalies.length === 0) {
    list.innerHTML = '<li class="anomaly-empty">No anomalies detected</li>';
    return;
  }
  list.innerHTML = anomalies.map(a => `
    <li class="anomaly-item ${a.severity}">
      <span class="anomaly-severity ${a.severity}">${a.severity}</span>
      <div class="anomaly-body">
        <div class="anomaly-desc"><strong>${a.type.replace(/_/g, ' ')}</strong> — ${a.description}</div>
        <div class="anomaly-action">→ ${a.suggested_action}</div>
      </div>
    </li>
  `).join('');
}

function setFeedStatus(ok) {
  const el = document.getElementById('feedStatus');
  if (ok) {
    el.textContent = '● Live';
    el.className = 'feed-status ok';
  } else {
    el.textContent = '● Disconnected';
    el.className = 'feed-status stale';
  }
}

async function fetchHeatmap(storeId) {
  try {
    const resp = await fetch(`${API_BASE}/stores/${storeId}/heatmap`);
    if (!resp.ok) return;
    const data = await resp.json();
    renderHeatmap(data.zones);
  } catch (_) {}
}

async function fetchAnomalies(storeId) {
  try {
    const resp = await fetch(`${API_BASE}/stores/${storeId}/anomalies`);
    if (!resp.ok) return;
    const data = await resp.json();
    renderAnomalies(data.anomalies);
  } catch (_) {}
}

function startSSE(storeId) {
  const url = `${API_BASE}/events/stream?store_id=${encodeURIComponent(storeId)}`;
  const es = new EventSource(url);

  es.onopen = () => setFeedStatus(true);

  es.onmessage = (evt) => {
    try {
      const metrics = JSON.parse(evt.data);
      if (metrics.error) return;
      updateKpis(metrics);
      document.getElementById('lastUpdated').textContent = 'Last updated: ' + new Date().toLocaleTimeString('en-IN');
    } catch (_) {}
  };

  es.onerror = () => {
    setFeedStatus(false);
    es.close();
    // Reconnect after 5 seconds
    setTimeout(() => startSSE(storeId), 5000);
  };

  return es;
}

// Poll heatmap and anomalies every 10 seconds (they change slower)
function startPolling(storeId) {
  fetchHeatmap(storeId);
  fetchAnomalies(storeId);
  setInterval(() => {
    fetchHeatmap(storeId);
    fetchAnomalies(storeId);
  }, 10_000);
}

function main() {
  initChart();
  const select = document.getElementById('storeSelect');

  function launch(storeId) {
    startSSE(storeId);
    startPolling(storeId);
  }

  launch(select.value);
  select.addEventListener('change', () => launch(select.value));
}

document.addEventListener('DOMContentLoaded', main);
