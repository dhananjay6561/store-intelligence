'use strict';

const API_BASE       = window.API_BASE || 'http://localhost:8000';
const CHART_MAX_PTS  = 30;
const QUEUE_WARN     = 5;
const QUEUE_CRITICAL = 8;
const POLL_INTERVAL  = 10_000;   // heatmap + anomalies + funnel

// ─── Chart ────────────────────────────────────────────────────────

let chart = null;
const history = { labels: [], data: [] };

function initChart() {
  const ctx = document.getElementById('conversionChart').getContext('2d');
  chart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: history.labels,
      datasets: [{
        label: 'Conversion Rate',
        data: history.data,
        borderColor: '#2563EB',
        backgroundColor: 'rgba(37,99,235,0.06)',
        pointBackgroundColor: '#2563EB',
        pointRadius: 2.5,
        pointHoverRadius: 5,
        tension: 0.45,
        borderWidth: 2,
        fill: true,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 400 },
      interaction: { mode: 'index', intersect: false },
      scales: {
        y: {
          min: 0, max: 1,
          ticks: {
            color: '#9CA3AF',
            font: { size: 11 },
            callback: v => (v * 100).toFixed(0) + '%',
          },
          grid: { color: '#F3F4F6' },
          border: { dash: [4, 4] },
        },
        x: {
          ticks: { color: '#9CA3AF', font: { size: 10 }, maxRotation: 0, autoSkip: true, maxTicksLimit: 6 },
          grid: { display: false },
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#1F2937',
          titleColor: '#F9FAFB',
          bodyColor: '#D1D5DB',
          padding: 10,
          cornerRadius: 8,
          callbacks: { label: ctx => ' ' + (ctx.parsed.y * 100).toFixed(1) + '%' },
        },
      },
    },
  });
}

function pushChartPoint(rate) {
  const now = new Date();
  const label = now.toLocaleTimeString('en-IN', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
  history.labels.push(label);
  history.data.push(rate);
  if (history.labels.length > CHART_MAX_PTS) {
    history.labels.shift();
    history.data.shift();
  }
  chart && chart.update('none');
}

// ─── KPI cards ────────────────────────────────────────────────────

function updateKpis(m) {
  setText('valVisitors',  m.unique_visitors ?? '—');

  const rate = m.conversion_rate ?? 0;
  setText('valConversion', fmtPct(rate));
  setClass('cardConversion', rate < 0.1 ? 'kpi-card state-warn' : 'kpi-card');

  const qd = m.queue_depth_current ?? 0;
  setText('valQueue', qd);
  setClass('cardQueue',
    qd >= QUEUE_CRITICAL ? 'kpi-card state-alert' :
    qd >= QUEUE_WARN     ? 'kpi-card state-warn'  : 'kpi-card');
  const pct = Math.min(100, (qd / QUEUE_CRITICAL) * 100);
  const fill = document.getElementById('queueBar');
  if (fill) {
    fill.style.width = pct + '%';
    fill.style.background = qd >= QUEUE_CRITICAL ? '#DC2626' : qd >= QUEUE_WARN ? '#D97706' : '#059669';
  }

  const ar = m.abandonment_rate ?? 0;
  setText('valAbandonment', fmtPct(ar));
  setClass('cardAbandonment', ar > 0.4 ? 'kpi-card state-alert' : ar > 0.2 ? 'kpi-card state-warn' : 'kpi-card');

  pushChartPoint(rate);

  const ts = new Date();
  setText('lastUpdated', 'Updated ' + ts.toLocaleTimeString('en-IN', { hour12: true }));
  setText('footerTs', ts.toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' }));
}

// ─── Zone heatmap ─────────────────────────────────────────────────

function renderHeatmap(zones) {
  if (!zones || !zones.length) return;
  const grid = document.getElementById('zoneGrid');
  const chip = document.getElementById('confidenceChip');

  const confidence = zones[0]?.data_confidence ?? 'LOW';
  if (chip) {
    chip.textContent = confidence;
    chip.className = 'confidence-chip' + (confidence === 'LOW' ? ' low' : '');
  }

  grid.innerHTML = zones.map(z => {
    const { bg, text, border } = zoneColour(z.score);
    const dwell = z.avg_dwell_ms > 0 ? Math.round(z.avg_dwell_ms / 1000) + 's dwell' : 'no dwell data';
    return `
      <div class="zone-cell" style="background:${bg};border-color:${border}">
        <div class="zone-name" style="color:${text}">${z.zone_id.replace(/_/g,' ')}</div>
        <div class="zone-score" style="color:${text}">${z.score}</div>
        <div class="zone-meta" style="color:${text}">${z.visit_count} visits · ${dwell}</div>
      </div>`;
  }).join('');
}

function zoneColour(score) {
  if (score >= 80) return { bg: '#EFF6FF', text: '#1D4ED8', border: '#BFDBFE' };
  if (score >= 50) return { bg: '#F0F9FF', text: '#0369A1', border: '#BAE6FD' };
  if (score >= 20) return { bg: '#F8FAFC', text: '#475569', border: '#E2E8F0' };
  return               { bg: '#F9FAFB', text: '#9CA3AF', border: '#F3F4F6' };
}

// ─── Funnel ───────────────────────────────────────────────────────

function renderFunnel(funnel) {
  if (!funnel || !funnel.length) return;
  const container = document.getElementById('funnelStages');
  const top = funnel[0].sessions || 1;

  container.innerHTML = funnel.map(stage => {
    const pct = Math.round((stage.sessions / top) * 100);
    const hasDrop = stage.drop_off_pct > 0;
    return `
      <div class="funnel-row">
        <div class="funnel-stage-name">${fmtStageName(stage.stage)}</div>
        <div class="funnel-bar-wrap">
          <div class="funnel-track">
            <div class="funnel-fill" style="width:${pct}%"></div>
          </div>
        </div>
        <div class="funnel-right">
          <div class="funnel-count">${stage.sessions.toLocaleString()}</div>
          <div class="funnel-drop ${hasDrop ? 'has-drop' : ''}">${hasDrop ? '▼ ' + stage.drop_off_pct + '%' : '—'}</div>
        </div>
      </div>`;
  }).join('');
}

function fmtStageName(s) {
  const map = { ENTRY: 'Entry', ZONE_VISIT: 'Zone Visit', BILLING_QUEUE: 'Billing Queue', PURCHASE: 'Purchase' };
  return map[s] || s;
}

// ─── Anomalies ────────────────────────────────────────────────────

function renderAnomalies(anomalies) {
  const list  = document.getElementById('anomalyList');
  const count = document.getElementById('anomalyCount');

  count.textContent = anomalies.length;
  const hasCrit = anomalies.some(a => a.severity === 'CRITICAL');
  const hasWarn = anomalies.some(a => a.severity === 'WARN');
  count.className = 'anomaly-count' + (hasCrit ? ' has-critical' : hasWarn ? ' has-warn' : '');

  if (!anomalies.length) {
    list.innerHTML = `
      <div class="anomaly-empty">
        <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#059669" stroke-width="1.5">
          <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>
        </svg>
        <span>All systems normal</span>
      </div>`;
    return;
  }

  list.innerHTML = anomalies.map(a => `
    <div class="anomaly-item ${a.severity}">
      <span class="anomaly-badge ${a.severity}">${a.severity}</span>
      <div class="anomaly-body">
        <div class="anomaly-type">${a.type.replace(/_/g,' ')}</div>
        <div class="anomaly-desc">${a.description}</div>
        <div class="anomaly-action">→ ${a.suggested_action}</div>
      </div>
    </div>`).join('');
}

// ─── Feed status ──────────────────────────────────────────────────

let _lastFetchOk = false;

function setFeedStatus(ok) {
  const badge = document.getElementById('feedBadge');
  const label = document.getElementById('feedLabel');
  if (!badge || !label) return;
  _lastFetchOk = ok;
  if (ok) {
    badge.className = 'feed-badge live';
    label.textContent = 'Live';
  } else {
    badge.className = 'feed-badge stale';
    label.textContent = 'Disconnected';
  }
}

// ─── Initial load — populate everything immediately on page load ───

async function fetchMetrics(storeId) {
  try {
    const r = await fetch(`${API_BASE}/stores/${storeId}/metrics?window=all`);
    if (r.ok) {
      updateKpis(await r.json());
      setFeedStatus(true);
    } else {
      setFeedStatus(false);
    }
  } catch (_) {
    setFeedStatus(false);
  }
}

async function initialLoad(storeId) {
  await Promise.all([
    fetchMetrics(storeId),
    fetchHeatmap(storeId),
    fetchFunnel(storeId),
    fetchAnomalies(storeId),
  ]);
}

// ─── Polling ──────────────────────────────────────────────────────

async function fetchHeatmap(storeId) {
  try {
    const r = await fetch(`${API_BASE}/stores/${storeId}/heatmap?window=all`);
    if (r.ok) renderHeatmap((await r.json()).zones);
  } catch (_) {}
}

async function fetchAnomalies(storeId) {
  try {
    const r = await fetch(`${API_BASE}/stores/${storeId}/anomalies`);
    if (r.ok) renderAnomalies((await r.json()).anomalies);
  } catch (_) {}
}

async function fetchFunnel(storeId) {
  try {
    const r = await fetch(`${API_BASE}/stores/${storeId}/funnel?window=all`);
    if (r.ok) renderFunnel((await r.json()).funnel);
  } catch (_) {}
}


// ─── Helpers ──────────────────────────────────────────────────────

function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}
function setClass(id, cls) {
  const el = document.getElementById(id);
  if (el) el.className = cls;
}
function fmtPct(v) { return (v * 100).toFixed(1) + '%'; }

// ─── Boot ─────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  const storeId = 'ST1008';
  initChart();
  initialLoad(storeId);                          // populate instantly on load

  setInterval(() => fetchMetrics(storeId), 4000); // refresh KPIs every 4s
  setInterval(() => {                             // refresh rest every 10s
    fetchHeatmap(storeId);
    fetchFunnel(storeId);
    fetchAnomalies(storeId);
  }, POLL_INTERVAL);
});
