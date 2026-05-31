'use strict';

const API_BASE = window.API_BASE ||
  (window.location.protocol === 'file:' ? 'http://localhost:8000' : '');

const CHART_MAX  = 30;
const QUEUE_WARN = 5;
const QUEUE_CRIT = 8;
const STAGE_NAMES = { ENTRY: 'Entry', ZONE_VISIT: 'Zone Visit', BILLING_QUEUE: 'Billing Queue', PURCHASE: 'Purchase' };

// ── Chart ─────────────────────────────────────────────────────────
let chart = null;
const pts = { labels: [], data: [] };

function initChart() {
  const ctx = document.getElementById('convChart').getContext('2d');
  chart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: pts.labels,
      datasets: [{
        data: pts.data,
        borderColor: '#2563EB',
        backgroundColor: 'rgba(37,99,235,0.05)',
        pointRadius: 0,
        pointHoverRadius: 4,
        tension: 0.4,
        borderWidth: 1.5,
        fill: true,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      scales: {
        y: {
          min: 0, max: 1,
          ticks: { color: '#9CA3AF', font: { size: 10 }, callback: v => (v * 100).toFixed(0) + '%' },
          grid: { color: '#F3F4F6' },
          border: { display: false },
        },
        x: {
          ticks: { color: '#9CA3AF', font: { size: 10 }, maxRotation: 0, autoSkip: true, maxTicksLimit: 5 },
          grid: { display: false },
          border: { display: false },
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#1F2937',
          bodyColor: '#F9FAFB',
          padding: 8, cornerRadius: 6,
          callbacks: { label: c => (c.parsed.y * 100).toFixed(1) + '%' },
        },
      },
    },
  });
}

function addChartPoint(rate) {
  const t = new Date().toLocaleTimeString('en-IN', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
  pts.labels.push(t); pts.data.push(rate);
  if (pts.labels.length > CHART_MAX) { pts.labels.shift(); pts.data.shift(); }
  chart && chart.update('none');
}

// ── KPIs ──────────────────────────────────────────────────────────
function updateKpis(m) {
  set('valVisitors', m.unique_visitors ?? '—');

  const rate = m.conversion_rate ?? 0;
  const rateEl = document.getElementById('valConversion');
  if (rateEl) { rateEl.textContent = pct(rate); rateEl.className = 'kpi-value' + (rate < 0.1 ? ' warn' : ''); }

  const qd = m.queue_depth_current ?? 0;
  const qdEl = document.getElementById('valQueue');
  if (qdEl) { qdEl.textContent = qd; qdEl.className = 'kpi-value' + (qd >= QUEUE_CRIT ? ' alert' : qd >= QUEUE_WARN ? ' warn' : ''); }
  const fill = document.getElementById('queueFill');
  if (fill) {
    fill.style.width = Math.min(100, (qd / QUEUE_CRIT) * 100) + '%';
    fill.style.background = qd >= QUEUE_CRIT ? '#DC2626' : qd >= QUEUE_WARN ? '#D97706' : '#16A34A';
  }

  const ar = m.abandonment_rate ?? 0;
  const arEl = document.getElementById('valAbandonment');
  if (arEl) { arEl.textContent = pct(ar); arEl.className = 'kpi-value' + (ar > 0.4 ? ' alert' : ''); }

  addChartPoint(rate);

  const now = new Date();
  set('updatedAt', now.toLocaleTimeString('en-IN', { hour12: true }));
  set('footerTime', now.toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' }));
}

// ── Heatmap ───────────────────────────────────────────────────────
function renderHeatmap(zones) {
  if (!zones?.length) return;
  const grid = document.getElementById('zoneGrid');
  const tag  = document.getElementById('confidenceTag');
  const conf = zones[0]?.data_confidence ?? 'LOW';
  if (tag) tag.textContent = conf === 'HIGH' ? '' : 'Low data confidence';

  grid.innerHTML = zones.map(z => {
    const intensity = z.score >= 80 ? 'h-3' : z.score >= 50 ? 'h-2' : z.score >= 15 ? 'h-1' : 'h-0';
    return `<div class="zone-cell ${intensity}">
      <div class="zone-name">${z.zone_id.replace(/_/g,' ')}</div>
      <div class="zone-score">${z.score}</div>
      <div class="zone-visits">${z.visit_count} visits</div>
    </div>`;
  }).join('');
}

// ── Funnel ────────────────────────────────────────────────────────
function renderFunnel(funnel) {
  if (!funnel?.length) return;
  const tbody = document.getElementById('funnelBody');
  const top = funnel[0].sessions || 1;
  tbody.innerHTML = funnel.map(s => {
    const w = Math.round((s.sessions / top) * 100);
    const hasDrop = s.drop_off_pct > 0;
    return `<tr>
      <td><span class="stage-name">${STAGE_NAMES[s.stage] || s.stage}</span></td>
      <td class="stage-bar-cell">
        <div class="stage-bar-wrap"><div class="stage-bar-fill" style="width:${w}%"></div></div>
      </td>
      <td><span class="stage-count">${s.sessions.toLocaleString()}</span></td>
      <td><span class="stage-drop ${hasDrop ? 'has-drop' : ''}">${hasDrop ? '▼ ' + s.drop_off_pct + '%' : '—'}</span></td>
    </tr>`;
  }).join('');
}

// ── Anomalies ─────────────────────────────────────────────────────
function renderAnomalies(anomalies) {
  const list  = document.getElementById('anomalyList');
  const badge = document.getElementById('anomalyBadge');
  if (!list || !badge) return;

  badge.textContent = anomalies.length;
  const hasCrit = anomalies.some(a => a.severity === 'CRITICAL');
  const hasWarn = anomalies.some(a => a.severity === 'WARN');
  badge.className = 'anomaly-count-badge' + (hasCrit ? ' crit' : hasWarn ? ' warn' : '');

  if (!anomalies.length) {
    list.innerHTML = '<div class="anomaly-empty">✓ All clear</div>';
    return;
  }
  list.innerHTML = '<div class="anomaly-list">' + anomalies.map(a => `
    <div class="anomaly-row">
      <span class="sev-tag ${a.severity}">${a.severity}</span>
      <div>
        <div class="anomaly-type">${a.type.replace(/_/g,' ')}</div>
        <div class="anomaly-desc">${a.description}</div>
        <div class="anomaly-action">${a.suggested_action}</div>
      </div>
    </div>`).join('') + '</div>';
}

// ── Status ────────────────────────────────────────────────────────
function setStatus(ok) {
  const dot   = document.getElementById('statusDot');
  const label = document.getElementById('statusLabel');
  if (dot)   { dot.className   = 'status-dot ' + (ok ? 'live' : 'stale'); }
  if (label) { label.textContent = ok ? 'Live' : 'Disconnected'; }
}

// ── Fetchers ──────────────────────────────────────────────────────
async function fetchMetrics(storeId) {
  try {
    const r = await fetch(`${API_BASE}/stores/${storeId}/metrics?window=all`);
    if (r.ok) { updateKpis(await r.json()); setStatus(true); }
    else setStatus(false);
  } catch (_) { setStatus(false); }
}

async function fetchHeatmap(storeId) {
  try {
    const r = await fetch(`${API_BASE}/stores/${storeId}/heatmap?window=all`);
    if (r.ok) renderHeatmap((await r.json()).zones);
  } catch (_) {}
}

async function fetchFunnel(storeId) {
  try {
    const r = await fetch(`${API_BASE}/stores/${storeId}/funnel?window=all`);
    if (r.ok) renderFunnel((await r.json()).funnel);
  } catch (_) {}
}

async function fetchAnomalies(storeId) {
  try {
    const r = await fetch(`${API_BASE}/stores/${storeId}/anomalies`);
    if (r.ok) renderAnomalies((await r.json()).anomalies);
  } catch (_) {}
}

// ── Helpers ───────────────────────────────────────────────────────
function set(id, val) { const el = document.getElementById(id); if (el) el.textContent = val; }
function pct(v) { return (v * 100).toFixed(1) + '%'; }

// ── Boot ──────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  const store = 'ST1008';
  initChart();

  Promise.all([fetchMetrics(store), fetchHeatmap(store), fetchFunnel(store), fetchAnomalies(store)]);

  setInterval(() => fetchMetrics(store),    4_000);
  setInterval(() => fetchHeatmap(store),   10_000);
  setInterval(() => fetchFunnel(store),    10_000);
  setInterval(() => fetchAnomalies(store), 10_000);
});
