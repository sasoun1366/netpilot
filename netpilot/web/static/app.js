/* netpilot dashboard — vanilla JS, no build step, no CDN.
   Live updates arrive over Server-Sent Events; everything else is plain fetch(). */

'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  meta: null,
  overview: null,
  devices: [],
  templates: [],
  credentials: [],
  deviceFilter: '',
  tagFilter: '',
  deploySelection: new Set(),
  deployTemplateId: null,
  deployTargetsFilter: '',
  autoDeploySelection: true,
  currentDeviceId: null,
  es: null,
};

/* ── helpers ──────────────────────────────────────────────────────── */

const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));

const fmtTime = (ts) => {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const now = Date.now();
  const diff = (now - d.getTime()) / 1000;
  if (diff < 60) return `${Math.max(0, Math.round(diff))}s ago`;
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
  return d.toLocaleString();
};

const fmtDuration = (sec) => {
  if (sec === null || sec === undefined) return '—';
  if (sec < 1) return `${Math.round(sec * 1000)} ms`;
  if (sec < 60) return `${sec.toFixed(1)} s`;
  const m = Math.floor(sec / 60);
  return `${m}m ${Math.round(sec - m * 60)}s`;
};

const fmtBytes = (n) => {
  if (!n && n !== 0) return '—';
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KiB`;
  return `${(n / 1048576).toFixed(2)} MiB`;
};

const fmtUptime = (sec) => {
  if (!sec && sec !== 0) return '—';
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
};

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    const detail = (data && data.detail) ? data.detail : `${res.status} ${res.statusText}`;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return data;
}

function toast(message, kind = '') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.innerHTML = esc(message);
  $('#toasts').appendChild(el);
  setTimeout(() => {
    el.style.transition = 'opacity .3s';
    el.style.opacity = '0';
    setTimeout(() => el.remove(), 320);
  }, kind === 'err' ? 6500 : 3800);
}

/* ── sparkline renderer (inline SVG, no library) ──────────────────── */

function sparkline(points, width = 240, height = 30) {
  if (!points || !points.length) {
    return `<svg class="spark" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
      <line x1="0" y1="${height - 2}" x2="${width}" y2="${height - 2}"
            stroke="#253040" stroke-width="1"/></svg>`;
  }
  const values = points.map((p) => (p.ok ? (p.latency_ms ?? 1) : null));
  const known = values.filter((v) => v !== null);
  const max = known.length ? Math.max(...known) * 1.25 : 1;
  const step = points.length > 1 ? width / (points.length - 1) : width;

  let path = '';
  let started = false;
  points.forEach((p, i) => {
    const x = i * step;
    if (p.ok) {
      const v = p.latency_ms ?? max * 0.2;
      const y = height - 3 - (v / max) * (height - 8);
      path += `${started ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)} `;
      started = true;
    } else {
      path += `${started ? 'L' : 'M'}${x.toFixed(1)},${(height - 1.5).toFixed(1)} `;
      started = true;
    }
  });

  const failures = points.map((p, i) => (p.ok ? '' :
    `<rect x="${(i * step - 1).toFixed(1)}" y="0" width="2.4" height="${height}"
           fill="#e0574f" opacity=".55"/>`)).join('');

  return `<svg class="spark" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
    <line x1="0" y1="${height - 2}" x2="${width}" y2="${height - 2}" stroke="#253040" stroke-width="1"/>
    ${failures}
    <path d="${path}" fill="none" stroke="#4fd1a3" stroke-width="1.7"
          stroke-linejoin="round" stroke-linecap="round"/>
  </svg>`;
}

/* ── views / navigation ───────────────────────────────────────────── */

function showView(name) {
  $$('.view').forEach((v) => v.classList.remove('active'));
  $$('.nav-item').forEach((n) => n.classList.toggle('active', n.dataset.view === name));
  const view = $(`#view-${name}`);
  if (view) view.classList.add('active');
  if (name === 'templates') loadTemplates();
  if (name === 'backups') loadBackups();
  if (name === 'events') loadEvents();
  if (name === 'settings') loadSettings();
  if (name === 'deploy') loadDeployView();
  if (name === 'inventory') loadDevices();
}

$('#nav').addEventListener('click', (e) => {
  const btn = e.target.closest('.nav-item');
  if (btn) showView(btn.dataset.view);
});

/* ── dashboard ────────────────────────────────────────────────────── */

function renderStats() {
  const o = state.overview;
  if (!o) return;
  const s = o.states || {};
  const cards = [
    { k: 'Devices', v: o.devices_total, d: `${o.devices_enabled} monitored`, cls: 'accent' },
    { k: 'Up', v: s.up || 0, d: 'responding', cls: 'up' },
    { k: 'Down', v: s.down || 0, d: 'not responding', cls: 'down' },
    { k: 'Degraded', v: s.degraded || 0, d: 'partial failures', cls: 'degraded' },
    { k: 'Avg latency', v: o.avg_latency_ms !== null && o.avg_latency_ms !== undefined ? `${o.avg_latency_ms}` : '—', d: 'milliseconds', cls: '' },
    { k: 'Checks / 24h', v: (o.checks_24h || 0).toLocaleString(), d: `${o.deploys_24h || 0} deployments`, cls: '' },
    { k: 'Backups', v: o.backups_total || 0, d: 'configs stored', cls: '' },
    { k: 'Open alerts', v: o.unacknowledged_events || 0, d: 'unacknowledged', cls: o.unacknowledged_events ? 'down' : '' },
  ];
  $('#stat-grid').innerHTML = cards.map((c) => `
    <div class="stat ${c.cls}">
      <div class="k">${esc(c.k)}</div>
      <div class="v ${String(c.v).length > 6 ? 'small' : ''}">${esc(c.v)}</div>
      <div class="d">${esc(c.d)}</div>
    </div>`).join('');

  $('#nav-device-count').textContent = o.devices_total;
  const alerts = $('#nav-alert-count');
  alerts.textContent = o.unacknowledged_events || 0;
  alerts.classList.toggle('zero', !o.unacknowledged_events);
}

function renderTags() {
  const tags = (state.overview && state.overview.tags) || [];
  const rows = [''].concat(tags);
  const html = rows.map((t) => `
    <button class="chip ${state.tagFilter === t ? 'on' : ''}" data-tag="${esc(t)}">
      ${t ? esc(t) : 'all tags'}
    </button>`).join('');
  const target = $('#tag-filter');
  if (target) target.innerHTML = html;
  const deployRow = $('#deploy-tag-row');
  if (deployRow) deployRow.innerHTML = html;
}

function deviceMatchesFilter(card) {
  const needle = state.deviceFilter.toLowerCase();
  if (state.tagFilter && !(card.tags || []).some((t) => t.toLowerCase() === state.tagFilter.toLowerCase())) {
    return false;
  }
  if (!needle) return true;
  return [card.name, card.host, card.site].filter(Boolean).some((v) => String(v).toLowerCase().includes(needle));
}

function renderDeviceGrid() {
  const grid = $('#device-grid');
  if (!grid) return;
  const cards = (state.devices || []).filter(deviceMatchesFilter);
  if (!cards.length) {
    grid.innerHTML = `<div class="empty" style="grid-column:1/-1">
      ${state.devices.length ? 'No device matches this filter.' : 'No devices yet — add one and it starts being monitored immediately.'}
    </div>`;
    return;
  }
  grid.innerHTML = cards.map((c) => {
    const st = c.state || {};
    const latency = st.last_latency_ms !== null && st.last_latency_ms !== undefined
      ? `${st.last_latency_ms.toFixed(1)} ms` : '—';
    const uptime = c.availability_24h !== null && c.availability_24h !== undefined
      ? `${c.availability_24h}%` : '—';
    return `
    <div class="dev-card s-${esc(st.state || 'unknown')}" data-device="${c.id}">
      <div class="top">
        <div>
          <div class="name">${esc(c.name)}</div>
          <div class="host">${esc(c.host)}:${esc(c.ssh_port)}</div>
        </div>
        <span class="state ${esc(st.state || 'unknown')}">${esc(st.state || 'unknown')}</span>
      </div>
      <div class="meta">
        <span>${esc(latency)}</span>
        <span>24h ${esc(uptime)}</span>
        <span>${esc(c.vendor)}</span>
      </div>
      ${sparkline(c.sparkline)}
      <div class="tags">
        ${(c.tags || []).map((t) => `<span class="tag">${esc(t)}</span>`).join('')}
        ${c.site ? `<span class="tag">${esc(c.site)}</span>` : ''}
      </div>
    </div>`;
  }).join('');
}

function renderEvents(listEl, events, options = {}) {
  if (!listEl) return;
  if (!events || !events.length) {
    listEl.innerHTML = '<div class="empty">No events yet.</div>';
    return;
  }
  listEl.innerHTML = events.map((e) => {
    const det = e.details && Object.keys(e.details).length
      ? Object.entries(e.details).slice(0, 6).map(([k, v]) => `${esc(k)}=${esc(v)}`).join('  ')
      : '';
    return `
    <div class="event ${esc(e.severity)} ${e.acknowledged ? 'acked' : ''}" data-event="${e.id}">
      <div class="row">
        <span class="msg">${esc(e.message)}</span>
        <span class="when">${esc(fmtTime(e.ts))}</span>
      </div>
      ${det ? `<div class="det">${det}</div>` : ''}
      ${options.acks && !e.acknowledged
        ? '<button class="mini" data-ack="' + e.id + '">acknowledge</button>' : ''}
    </div>`;
  }).join('');
}

function renderJobs(listEl, jobs) {
  if (!listEl) return;
  if (!jobs || !jobs.length) {
    listEl.innerHTML = '<div class="empty">No deployments yet.</div>';
    return;
  }
  listEl.innerHTML = jobs.map((j) => `
    <div class="event ${j.failed ? 'warning' : 'info'}" data-job="${j.id}">
      <div class="row">
        <span class="msg">#${j.id} ${esc(j.template_name)} — ${j.succeeded}/${j.total} ok${j.failed ? `, ${j.failed} failed` : ''}</span>
        <span class="when">${esc(fmtTime(j.created_at))}</span>
      </div>
      <div class="det">${esc(j.status)} · ${esc(fmtDuration(j.duration_sec))}${(j.options && j.options.dry_run) ? ' · dry run' : ''}</div>
    </div>`).join('');
}

async function refreshOverview() {
  try {
    state.overview = await api('/api/overview');
    state.devices = state.overview.devices || [];
    renderStats();
    renderTags();
    renderDeviceGrid();
    renderEvents($('#dash-events'), state.overview.events);
    renderJobs($('#dash-jobs'), state.overview.recent_jobs);
    $('#dash-sub').textContent =
      `${state.overview.devices_total} device(s) · ${state.overview.monitor_running ? 'monitor running' : 'monitor stopped'} · updated ${new Date().toLocaleTimeString()}`;
    $('#brand-tag').textContent = `v${state.meta ? state.meta.version : '?'}`;
  } catch (err) {
    toast(`Could not load dashboard: ${err.message}`, 'err');
  }
}

/* ── inventory ────────────────────────────────────────────────────── */

async function loadDevices() {
  try {
    state.devices = await api('/api/devices?details=true');
    renderDeviceTable();
    renderDeviceGrid();
  } catch (err) { toast(err.message, 'err'); }
}

function renderDeviceTable() {
  const tbody = $('#device-table tbody');
  if (!tbody) return;
  const devices = state.devices.filter(deviceMatchesFilter);
  if (!devices.length) {
    tbody.innerHTML = '<tr><td colspan="9" class="empty">No devices.</td></tr>';
    return;
  }
  tbody.innerHTML = devices.map((d) => {
    const st = d.state || {};
    const latency = st.last_latency_ms !== null && st.last_latency_ms !== undefined ? `${st.last_latency_ms.toFixed(1)} ms` : '—';
    const uptime = d.availability_24h !== null && d.availability_24h !== undefined ? `${d.availability_24h}%` : '—';
    return `
    <tr data-device="${d.id}">
      <td><b>${esc(d.name)}</b></td>
      <td class="mono">${esc(d.host)}</td>
      <td>${esc(d.vendor)}</td>
      <td>${esc(d.site || '—')}</td>
      <td><span class="state ${esc(st.state || 'unknown')}">${esc(st.state || 'unknown')}</span></td>
      <td class="mono">${esc(latency)}</td>
      <td class="mono">${esc(uptime)}</td>
      <td>${(d.tags || []).map((t) => `<span class="tag">${esc(t)}</span>`).join(' ')}</td>
      <td class="actions">
        <button class="btn small" data-act="open" data-id="${d.id}">Open</button>
        <button class="btn small" data-act="probe" data-id="${d.id}">Check</button>
      </td>
    </tr>`;
  }).join('');
}

/* ── device drawer ────────────────────────────────────────────────── */

async function openDevice(deviceId) {
  state.currentDeviceId = deviceId;
  $('#drawer-backdrop').classList.add('open');
  $('#device-drawer').classList.add('open');
  $('#dd-body').innerHTML = '<div class="empty"><span class="spinner"></span> loading…</div>';
  try {
    const d = await api(`/api/devices/${deviceId}`);
    renderDeviceDrawer(d);
  } catch (err) {
    $('#dd-body').innerHTML = `<div class="empty err-text">${esc(err.message)}</div>`;
  }
}

function closeDrawer() {
  $('#drawer-backdrop').classList.remove('open');
  $('#device-drawer').classList.remove('open');
  state.currentDeviceId = null;
}

function renderDeviceDrawer(d) {
  const st = d.state || {};
  $('#dd-name').textContent = d.name;
  $('#dd-sub').innerHTML = `${esc(d.host)}:${esc(d.ssh_port)} · ${esc(d.vendor)} · <span class="state ${esc(st.state)}">${esc(st.state)}</span>`;

  const history = d.history || [];
  const results = d.results || [];
  const checks = d.checks || [];

  const checkRows = checks.map((c) => {
    const cs = (d.check_states || {})[c.id] || {};
    const last = cs.last_ts ? fmtTime(cs.last_ts) : 'never';
    return `
    <tr>
      <td><b>${esc(c.kind)}</b><div class="mono" style="font-size:11px">${esc(c.label || '')}</div></td>
      <td class="mono">${esc(JSON.stringify(c.params))}</td>
      <td class="mono">${esc(c.interval_sec)}s</td>
      <td><span class="state ${esc(cs.state || 'unknown')}">${esc(cs.state || 'unknown')}</span></td>
      <td class="mono">${esc(last)}</td>
      <td class="actions">
        <button class="btn small" data-act="check-edit" data-id="${c.id}">edit</button>
        <button class="btn small danger" data-act="check-del" data-id="${c.id}">✕</button>
      </td>
    </tr>`;
  }).join('') || '<tr><td colspan="6" class="empty">No checks configured.</td></tr>';

  const recent = results.slice(0, 14).map((r) => `
    <tr>
      <td class="mono">${esc(new Date(r.ts * 1000).toLocaleTimeString())}</td>
      <td class="mono">${esc(r.kind)}</td>
      <td>${r.ok ? '<span class="ok-text">ok</span>' : '<span class="err-text">fail</span>'}</td>
      <td class="mono">${r.latency_ms !== null && r.latency_ms !== undefined ? esc(r.latency_ms.toFixed(1)) + ' ms' : '—'}</td>
      <td class="mono">${esc((r.message || '').slice(0, 90))}</td>
    </tr>`).join('') || '<tr><td colspan="5" class="empty">No probe results yet.</td></tr>';

  const backups = (d.backups || []).map((b) => `
    <tr>
      <td class="mono">#${b.id}</td>
      <td class="mono">${esc(fmtTime(b.created_at))}</td>
      <td class="mono">${esc(fmtBytes(b.byte_size))}</td>
      <td class="mono">${esc(b.source)}</td>
      <td class="actions"><button class="btn small" data-act="backup-view" data-id="${b.id}">view</button></td>
    </tr>`).join('') || '<tr><td colspan="5" class="empty">No backups yet.</td></tr>';

  // Built-in templates have no database id, so options are keyed by array index.
  window._ddTemplates = d.templates || [];
  const templates = window._ddTemplates.map((t, index) => `
    <option value="${index}">${esc(t.name)} · ${esc(t.vendor)}</option>`).join('');

  $('#dd-body').innerHTML = `
    <div class="head-actions" style="margin-bottom:16px">
      <button class="btn primary" data-act="test">Test SSH connection</button>
      <button class="btn" data-act="probe">Run all checks now</button>
      <button class="btn" data-act="backup">Capture config</button>
      <button class="btn ghost" data-act="deploy-one">Deploy config…</button>
      <button class="btn ghost" data-act="edit">Edit</button>
      <button class="btn danger" data-act="delete">Delete</button>
    </div>

    <h3>State</h3>
    <dl class="kv">
      <dt>state</dt><dd>${esc(st.state)} since ${esc(fmtTime(st.since))}</dd>
      <dt>last check</dt><dd>${esc(st.last_check_ts ? fmtTime(st.last_check_ts) : 'never')}</dd>
      <dt>latency</dt><dd>${st.last_latency_ms !== null && st.last_latency_ms !== undefined ? esc(st.last_latency_ms.toFixed(1)) + ' ms' : '—'}</dd>
      <dt>checks</dt><dd>${esc(st.total_checks || 0)} run, ${esc(st.up_checks || 0)} ok</dd>
      <dt>availability</dt><dd>24h ${esc(d.availability_24h ?? '—')}% · 7d ${esc(d.availability_7d ?? '—')}%</dd>
      <dt>last error</dt><dd>${esc(st.last_error || '—')}</dd>
      <dt>tags</dt><dd>${(d.tags || []).map((t) => esc(t)).join(', ') || '—'}</dd>
      <dt>credential</dt><dd>${esc((state.credentials.find((c) => c.id === d.credential_id) || {}).name || '—')}</dd>
      <dt>notes</dt><dd>${esc(d.notes || '—')}</dd>
    </dl>

    <h3>Latency &amp; availability (last ${history.length} probes)</h3>
    ${sparkline(history, 620, 64)}

    <h3>Monitors <button class="mini" data-act="check-add">+ add check</button></h3>
    <div class="table-wrap"><table class="table">
      <thead><tr><th>Kind</th><th>Params</th><th>Every</th><th>State</th><th>Last</th><th></th></tr></thead>
      <tbody>${checkRows}</tbody>
    </table></div>

    <h3>Recent probe results</h3>
    <div class="table-wrap"><table class="table">
      <thead><tr><th>Time</th><th>Kind</th><th>Result</th><th>Latency</th><th>Detail</th></tr></thead>
      <tbody>${recent}</tbody>
    </table></div>

    <h3>Backups of this device</h3>
    <div class="table-wrap"><table class="table">
      <thead><tr><th>#</th><th>When</th><th>Size</th><th>Source</th><th></th></tr></thead>
      <tbody>${backups}</tbody>
    </table></div>

    <h3>Push a template to this device</h3>
    <div class="panel">
      <select id="dd-template">${templates}</select>
      <div id="dd-vars" class="var-grid"></div>
      <div class="preview" id="dd-preview"><span class="muted">Pick a template to preview its commands.</span></div>
      <label class="switch"><input type="checkbox" id="dd-dry" checked> dry run (change nothing)</label>
      <button class="btn primary wide" data-act="deploy-one-run">Run on ${esc(d.name)}</button>
    </div>

    <h3>Event history</h3>
    <div class="event-list">${(d.events || []).map((e) => `
      <div class="event ${esc(e.severity)}">
        <div class="row"><span class="msg">${esc(e.message)}</span><span class="when">${esc(fmtTime(e.ts))}</span></div>
      </div>`).join('') || '<div class="empty">No events.</div>'}
    </div>
  `;

  const sel = $('#dd-template');
  if (sel) {
    sel.addEventListener('change', () => renderDrawerTemplate(d));
    renderDrawerTemplate(d);
  }
}

function currentDrawerTemplate() {
  const sel = $('#dd-template');
  if (!sel || sel.value === '') return null;
  return (window._ddTemplates || [])[Number(sel.value)] || null;
}

function renderDrawerTemplate(d) {
  const template = currentDrawerTemplate();
  const vars = template ? (template.variables || {}) : {};
  $('#dd-vars').innerHTML = Object.entries(vars).map(([k, v]) => `
    <label>${esc(k)}<input type="text" data-var="${esc(k)}" value="${esc(v)}"></label>`).join('');
  updateDrawerPreview(d);
}

async function updateDrawerPreview(d) {
  const template = currentDrawerTemplate();
  if (!template) { $('#dd-preview').innerHTML = '<span class="muted">Pick a template.</span>'; return; }
  const variables = {};
  $$('#dd-vars input[data-var]').forEach((i) => { variables[i.dataset.var] = i.value; });
  try {
    const pv = await api('/api/templates/preview', {
      method: 'POST',
      body: { template_id: template.id, body: template.body, vendor: d.vendor, variables },
    });
    $('#dd-preview').innerHTML = pv.commands.length
      ? esc(pv.commands.map((c, i) => `${String(i + 1).padStart(2)}  ${c}`).join('\n'))
      : '<span class="muted">Template renders to zero commands.</span>';
  } catch (err) {
    $('#dd-preview').innerHTML = `<span class="err-text">${esc(err.message)}</span>`;
  }
}

/* ── deploy view ──────────────────────────────────────────────────── */

async function loadDeployView() {
  if (!state.templates.length) await loadTemplates();
  if (!state.devices.length) state.devices = await api('/api/devices?details=true');
  const sel = $('#deploy-template');
  // Built-in templates have no database id, so the option value is the array index and
  // the template object itself is kept alongside — never re-looked-up by id.
  window._deployTemplates = state.templates.filter((t) => t.vendor !== 'generic');
  sel.innerHTML = window._deployTemplates.map((t, index) => `
      <option value="${index}">${esc(t.name)} · ${esc(t.vendor)}</option>`).join('')
    + '<option value="">— ad-hoc (write your own) —</option>';
  const previous = state.deployTemplateName;
  if (previous) {
    const index = window._deployTemplates.findIndex((t) => t.name === previous);
    if (index >= 0) sel.value = String(index);
  }
  if (!sel.dataset.bound) {
    sel.addEventListener('change', onDeployTemplateChange);
    $('#deploy-body').addEventListener('input', () => { renderDeployVars(); updateDeployPreview(); });
    sel.dataset.bound = '1';
  }
  onDeployTemplateChange();
  renderDeployTargets();
  loadJobs();
}

function currentDeployTemplate() {
  const sel = $('#deploy-template');
  if (!sel || sel.value === '') return null;
  return (window._deployTemplates || [])[Number(sel.value)] || null;
}

function onDeployTemplateChange() {
  const template = currentDeployTemplate();
  state.deployTemplateId = template ? template.id ?? null : null;
  state.deployTemplateName = template ? template.name : null;
  $('#deploy-body').value = template ? template.body : '';
  renderDeployVars();
  state.autoDeploySelection = true;
  renderDeployTargets();
  updateDeployPreview();
}

function renderDeployVars() {
  const template = currentDeployTemplate();
  const vars = template ? (template.variables || {}) : {};
  $('#deploy-vars').innerHTML = Object.entries(vars).map(([k, v]) => `
    <label>${esc(k)}<input type="text" data-var="${esc(k)}" value="${esc(v)}"></label>`).join('');
  $$('#deploy-vars input[data-var]').forEach((input) => {
    if (!input.dataset.bound) {
      input.addEventListener('input', updateDeployPreview);
      input.dataset.bound = '1';
    }
  });
}

function deployVariables() {
  const out = {};
  $$('#deploy-vars input[data-var]').forEach((i) => { out[i.dataset.var] = i.value; });
  return out;
}

function renderDeployTargets() {
  const list = $('#deploy-targets');
  if (!list) return;
  const template = currentDeployTemplate();
  const vendor = template ? template.vendor : ($('#deploy-body') ? null : 'mikrotik');
  const needle = state.deployTargetsFilter.toLowerCase();

  if (state.autoDeploySelection && template) {
    state.deploySelection = new Set(
      state.devices.filter((d) => d.vendor === template.vendor).map((d) => d.id)
    );
  }

  const visible = state.devices.filter((d) => {
    if (state.tagFilter && !(d.tags || []).some((t) => t.toLowerCase() === state.tagFilter.toLowerCase())) return false;
    if (!needle) return true;
    return [d.name, d.host, d.site].filter(Boolean).some((v) => String(v).toLowerCase().includes(needle));
  });

  if (!visible.length) {
    list.innerHTML = '<div class="empty">No devices match.</div>';
    return;
  }

  list.innerHTML = visible.map((d) => {
    const unsupported = d.vendor === 'generic';
    const checked = state.deploySelection.has(d.id) ? 'checked' : '';
    return `
    <label class="target-item ${unsupported ? 'no-deploy' : ''}">
      <input type="checkbox" data-target="${d.id}" ${checked} ${unsupported ? 'disabled' : ''}>
      <span class="who">
        <span class="n">${esc(d.name)}</span><br>
        <span class="h">${esc(d.host)} · ${esc(d.vendor)}${unsupported ? ' · monitoring only' : ''}</span>
      </span>
      <span class="state ${esc((d.state || {}).state || 'unknown')}">${esc((d.state || {}).state || '?')}</span>
    </label>`;
  }).join('');

  const warn = $('#deploy-warning');
  if (vendor === 'generic') {
    warn.innerHTML = '<span class="warn-text">This template targets a generic SSH shell, which has no modelled config push. Pick a vendor template.</span>';
  } else {
    warn.textContent = '';
  }
  updateTargetCount();
}

function updateTargetCount() {
  const n = state.deploySelection.size;
  const el = $('#deploy-target-count');
  if (el) el.textContent = `${n} device(s) selected`;
}

async function updateDeployPreview() {
  const body = $('#deploy-body').value;
  const template = currentDeployTemplate();
  if (!body.trim()) {
    $('#deploy-preview').innerHTML = '<span class="muted">Nothing to render.</span>';
    return;
  }
  const vendor = template ? template.vendor : (state.devices.find((d) => state.deploySelection.has(d.id)) || {}).vendor || 'mikrotik';
  try {
    const pv = await api('/api/templates/preview', {
      method: 'POST',
      body: { template_id: state.deployTemplateId, body, vendor, variables: deployVariables() },
    });
    let html = esc(pv.commands.map((c, i) => `${String(i + 1).padStart(2)}  ${c}`).join('\n'));
    if (pv.unresolved && pv.unresolved.length) {
      html += `\n\n<span class="warn-text">Unresolved variables: ${esc(pv.unresolved.join(', '))}</span>`;
    }
    html += `\n\n<span class="muted">${pv.command_count} command(s) · rollback: ${esc(pv.rollback_support)}</span>`;
    $('#deploy-preview').innerHTML = html;
  } catch (err) {
    $('#deploy-preview').innerHTML = `<span class="err-text">${esc(err.message)}</span>`;
  }
}

async function runDeployment() {
  const body = $('#deploy-body').value;
  if (!body.trim()) return toast('Nothing to deploy.', 'err');
  if (!state.deploySelection.size) return toast('Select at least one target device.', 'err');
  const options = {
    dry_run: $('#opt-dry').checked,
    backup_before: $('#opt-backup').checked,
    auto_rollback: $('#opt-rollback').checked,
    stop_on_first_failure: $('#opt-stop').checked,
    max_parallel: Number($('#opt-parallel').value || 5),
    save_config: $('#deploy-save').checked,
  };
  const template = currentDeployTemplate();
  const btn = $('#btn-deploy-run');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> running…';
  try {
    const job = await api('/api/deploys', {
      method: 'POST',
      body: {
        body,
        vendor: template ? template.vendor : (state.devices.find((d) => state.deploySelection.has(d.id)) || {}).vendor,
        device_ids: Array.from(state.deploySelection),
        variables: deployVariables(),
        options,
        template_id: state.deployTemplateId,
        template_name: state.deployTemplateName,
      },
    });
    toast(`Deployment #${job.id} started on ${job.total} device(s)${options.dry_run ? ' (dry run)' : ''}`, options.dry_run ? 'ok' : 'warn');
    pollJob(job.id);
  } catch (err) {
    toast(err.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Run deployment';
  }
}

function pollJob(jobId) {
  const tick = async () => {
    try {
      const job = await api(`/api/deploys/${jobId}`);
      if (job.status === 'running' || job.status === 'pending') {
        setTimeout(tick, 1200);
      } else {
        loadJobs();
        refreshOverview();
        showJobModal(job);
      }
    } catch { /* ignore */ }
  };
  setTimeout(tick, 900);
}

async function loadJobs() {
  try {
    const jobs = await api('/api/deploys?limit=25');
    const tbody = $('#job-table tbody');
    if (!tbody) return;
    if (!jobs.length) {
      tbody.innerHTML = '<tr><td colspan="9" class="empty">No deployments yet.</td></tr>';
      return;
    }
    tbody.innerHTML = jobs.map((j) => `
      <tr>
        <td class="mono">#${j.id}</td>
        <td>${esc(j.template_name)} <span class="tag">${esc(j.vendor)}</span></td>
        <td><span class="state ${j.status === 'done' ? 'up' : j.status === 'failed' ? 'down' : j.status === 'running' ? 'degraded' : 'unknown'}">${esc(j.status)}</span>${(j.options && j.options.dry_run) ? ' <span class="tag">dry run</span>' : ''}</td>
        <td class="mono">${j.total}</td>
        <td class="mono ok-text">${j.succeeded}</td>
        <td class="mono ${j.failed ? 'err-text' : ''}">${j.failed}</td>
        <td class="mono">${esc(fmtDuration(j.duration_sec))}</td>
        <td class="mono">${esc(fmtTime(j.created_at))}</td>
        <td class="actions"><button class="btn small" data-job-open="${j.id}">detail</button></td>
      </tr>`).join('');
  } catch (err) { toast(err.message, 'err'); }
}

async function showJobModal(job) {
  const targets = (job.targets || []).map((t) => `
    <tr>
      <td>${esc(t.device_name)}<div class="mono" style="font-size:11px">${esc(t.host)}</div></td>
      <td><span class="state ${t.status === 'ok' ? 'up' : t.status === 'failed' ? 'down' : t.status === 'rolled-back' ? 'degraded' : 'unknown'}">${esc(t.status)}</span></td>
      <td class="mono">${esc(t.duration_ms !== null && t.duration_ms !== undefined ? t.duration_ms + ' ms' : '—')}</td>
      <td class="mono">${t.commands ? t.commands.length + ' cmd' : '—'}${t.has_backup ? ' · backup' : ''}</td>
      <td class="mono" style="max-width:320px;overflow:hidden;text-overflow:ellipsis">${esc((t.error || '').slice(0, 120))}</td>
    </tr>`).join('');

  openModal(`Deployment #${job.id} — ${job.template_name}`, `
    <p class="sub">${esc(job.status)} · ${job.total} targets · ${job.succeeded} ok · ${job.failed} failed · ${esc(fmtDuration(job.duration_sec))}${(job.options && job.options.dry_run) ? ' · DRY RUN (nothing was written)' : ''}</p>
    <div class="table-wrap"><table class="table">
      <thead><tr><th>Device</th><th>Status</th><th>Time</th><th></th><th>Error</th></tr></thead>
      <tbody>${targets || '<tr><td colspan="5" class="empty">No targets.</td></tr>'}</tbody>
    </table></div>
    ${(job.targets || []).map((t) => t.output ? `
      <h4>${esc(t.device_name)} — output</h4>
      <div class="preview">${esc(t.output.slice(0, 4000))}</div>` : '').join('')}
  `);
}

/* ── templates ────────────────────────────────────────────────────── */

async function loadTemplates() {
  try {
    state.templates = await api('/api/templates');
    renderTemplateTable();
  } catch (err) { toast(err.message, 'err'); }
}

function renderTemplateTable() {
  const tbody = $('#template-table tbody');
  if (!tbody) return;
  if (!state.templates.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty">No templates.</td></tr>';
    return;
  }
  tbody.innerHTML = state.templates.map((t) => `
    <tr>
      <td><b>${esc(t.name)}</b></td>
      <td>${esc(t.vendor)}</td>
      <td>${t.builtin ? '<span class="tag">built-in</span>' : '<span class="tag">custom</span>'}</td>
      <td class="mono">${Object.keys(t.variables || {}).length}</td>
      <td>${esc(t.description)}</td>
      <td class="actions">
        <button class="btn small" data-tpl-view="${t.id ?? t.name}">view</button>
        <button class="btn small" data-tpl-edit="${t.id ?? t.name}">edit</button>
      </td>
    </tr>`).join('');
}

function findTemplate(key) {
  if (key === '' || key === undefined || key === null) return null;
  const asNum = Number(key);
  if (!Number.isNaN(asNum) && /^\d+$/.test(String(key))) {
    return state.templates.find((t) => t.id === asNum) || null;
  }
  return state.templates.find((t) => t.name === key) || null;
}

function openTemplateEditor(template) {
  const t = template || { name: '', vendor: 'mikrotik', description: '', body: '', variables: {}, save_config: true };
  const varsText = Object.entries(t.variables || {}).map(([k, v]) => `${k}=${v}`).join('\n');
  openModal(template ? `Edit template — ${t.name}` : 'New template', `
    <div class="form-grid">
      <label>Name<input type="text" id="tpl-name" value="${esc(t.name)}"></label>
      <label>Vendor
        <select id="tpl-vendor">
          ${(state.meta ? state.meta.vendors : []).filter((v) => v.supports_deploy).map((v) =>
            `<option value="${esc(v.name)}" ${v.name === t.vendor ? 'selected' : ''}>${esc(v.label)}</option>`).join('')}
        </select>
      </label>
      <label class="wide">Description<input type="text" id="tpl-desc" value="${esc(t.description)}"></label>
    </div>
    <h4>Configuration body</h4>
    <textarea id="tpl-body" rows="12" spellcheck="false">${esc(t.body)}</textarea>
    <h4>Variables (one <span class="mono">key=value</span> per line)</h4>
    <textarea id="tpl-vars" rows="5" spellcheck="false">${esc(varsText)}</textarea>
    <label class="switch" style="margin-top:12px"><input type="checkbox" id="tpl-save" ${t.save_config ? 'checked' : ''}> persist config on device after applying</label>
    <div id="tpl-preview" class="preview" style="margin-top:12px"><span class="muted">Rendered preview appears here.</span></div>
    <div class="head-actions" style="margin-top:12px">
      <button class="btn ghost" id="tpl-preview-btn">Preview</button>
    </div>
  `, [
    { label: 'Cancel', cls: 'ghost', action: closeModal },
    {
      label: 'Save',
      cls: 'primary',
      action: async () => {
        const name = $('#tpl-name').value.trim();
        if (!name) return toast('Name is required.', 'err');
        const variables = {};
        $('#tpl-vars').value.split('\n').forEach((line) => {
          const idx = line.indexOf('=');
          if (idx > 0) variables[line.slice(0, idx).trim()] = line.slice(idx + 1).trim();
        });
        const payload = {
          id: template && template.id ? template.id : undefined,
          name,
          vendor: $('#tpl-vendor').value,
          description: $('#tpl-desc').value,
          body: $('#tpl-body').value,
          variables,
          save_config: $('#tpl-save').checked,
        };
        try {
          await api('/api/templates', { method: 'POST', body: payload });
          toast(`Template "${name}" saved.`, 'ok');
          closeModal();
          await loadTemplates();
        } catch (err) { toast(err.message, 'err'); }
      },
    },
  ]);

  $('#tpl-preview-btn').addEventListener('click', async () => {
    const variables = {};
    $('#tpl-vars').value.split('\n').forEach((line) => {
      const idx = line.indexOf('=');
      if (idx > 0) variables[line.slice(0, idx).trim()] = line.slice(idx + 1).trim();
    });
    try {
      const pv = await api('/api/templates/preview', {
        method: 'POST',
        body: { template_id: null, body: $('#tpl-body').value, vendor: $('#tpl-vendor').value, variables },
      });
      $('#tpl-preview').innerHTML = esc(pv.commands.join('\n') || '(no commands)');
    } catch (err) { $('#tpl-preview').innerHTML = `<span class="err-text">${esc(err.message)}</span>`; }
  });
}

/* ── backups ──────────────────────────────────────────────────────── */

async function loadBackups() {
  try {
    const backups = await api('/api/backups');
    const tbody = $('#backup-table tbody');
    if (!backups.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="empty">No backups yet — every deploy stores one automatically.</td></tr>';
      return;
    }
    tbody.innerHTML = backups.map((b) => `
      <tr>
        <td class="mono">#${b.id}</td>
        <td><b>${esc(b.device_name)}</b></td>
        <td class="mono">${esc(b.host)}</td>
        <td class="mono">${esc(fmtTime(b.created_at))}</td>
        <td class="mono">${esc(fmtBytes(b.byte_size))}</td>
        <td class="mono">${esc(b.source)}</td>
        <td class="actions">
          <button class="btn small" data-bk-view="${b.id}">view</button>
          <button class="btn small" data-bk-restore="${b.id}">restore…</button>
          <button class="btn small danger" data-bk-del="${b.id}">✕</button>
        </td>
      </tr>`).join('');
  } catch (err) { toast(err.message, 'err'); }
}

async function viewBackup(id) {
  const b = await api(`/api/backups/${id}`);
  openModal(`Backup #${b.id} — ${b.device_name}`, `
    <p class="sub">${esc(b.host)} · ${esc(fmtTime(b.created_at))} · ${esc(fmtBytes(b.byte_size))} · ${esc(b.source)}</p>
    <div class="preview" style="max-height:52vh">${esc(b.config)}</div>
  `, [
    { label: 'Download', cls: 'ghost', action: () => {
        const blob = new Blob([b.config], { type: 'text/plain' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = `netpilot-backup-${b.id}-${b.device_name}.cfg`;
        a.click();
      } },
    { label: 'Close', cls: 'primary', action: closeModal },
  ]);
}

async function restoreBackup(id) {
  const dry = await api(`/api/backups/${id}/restore`, { method: 'POST', body: { dry_run: true } });
  if (!dry.ok) return toast(dry.error || 'preview failed', 'err');
  openModal('Restore configuration', `
    <p class="err-text"><b>This pushes a stored configuration back onto ${esc(dry.device)}.</b></p>
    <p class="sub">It will be applied through the same verified flow as a deployment — ${dry.command_count} command(s), with a fresh backup captured first.</p>
    <div class="preview">${esc((dry.preview || []).join('\n'))}</div>
  `, [
    { label: 'Cancel', cls: 'ghost', action: closeModal },
    {
      label: 'Restore now',
      cls: 'primary',
      action: async () => {
        closeModal();
        toast('Restoring — this can take a moment…');
        try {
          const res = await api(`/api/backups/${id}/restore`, { method: 'POST', body: { dry_run: false } });
          if (res.ok) toast(`Restored ${res.applied} command(s) on ${res.device}.`, 'ok');
          else toast(`Restore failed: ${res.error}`, 'err');
          refreshOverview();
        } catch (err) { toast(err.message, 'err'); }
      },
    },
  ]);
}

/* ── events ───────────────────────────────────────────────────────── */

async function loadEvents() {
  const params = new URLSearchParams({ limit: '200' });
  const sev = $('#events-severity').value;
  if (sev) params.set('severity', sev);
  if ($('#events-unacked').checked) params.set('unacked', 'true');
  try {
    const events = await api(`/api/events?${params}`);
    renderEvents($('#events-full'), events, { acks: true });
  } catch (err) { toast(err.message, 'err'); }
}

/* ── settings ─────────────────────────────────────────────────────── */

async function loadSettings() {
  try {
    const s = await api('/api/settings');
    const n = s.notify || {};
    $('#nt-enabled').checked = !!n.enabled;
    $('#nt-min').value = n.min_severity || 'warning';
    $('#nt-cooldown').value = n.cooldown_sec ?? 300;
    $('#nt-quiet').value = n.quiet_hours || '';
    $('#nt-recovery').checked = n.notify_on_recovery !== false;
    $('#nt-webhook').value = n.webhook_url || '';
    $('#nt-kind').value = n.webhook_kind || 'generic';
    $('#nt-smtp-host').value = n.smtp_host || '';
    $('#nt-smtp-port').value = n.smtp_port || 587;
    $('#nt-smtp-user').value = n.smtp_user || '';
    $('#nt-smtp-from').value = n.smtp_from || '';
    $('#nt-smtp-to').value = n.smtp_to || '';
    $('#nt-syslog-host').value = n.syslog_host || '';
    $('#nt-syslog-port').value = n.syslog_port || 514;

    const st = s.notify_status || {};
    $('#sink-status').innerHTML = (st.sinks || []).length
      ? (st.sinks || []).map((sink) => `
          <div class="event ${sink.last_error ? 'warning' : 'info'}">
            <div class="row">
              <span class="msg">${esc(sink.name)} → ${esc(sink.target)}</span>
              <span class="when">${sink.sent} sent · ${sink.failed} failed</span>
            </div>
            ${sink.last_error ? `<div class="det err-text">${esc(sink.last_error)}</div>` : ''}
          </div>`).join('')
      : '<div class="hint">No notification sinks configured — the dashboard and event feed work without them.</div>';

    const ms = s.monitor_stats || {};
    $('#runtime-kv').innerHTML = `
      <dt>version</dt><dd>${esc(state.meta ? state.meta.version : '?')}</dd>
      <dt>uptime</dt><dd>${esc(fmtUptime(s.uptime_sec))}</dd>
      <dt>monitor</dt><dd>${s.monitor_running ? 'running' : 'stopped'}</dd>
      <dt>concurrency</dt><dd>${esc(ms.concurrency)}</dd>
      <dt>checks run</dt><dd>${esc(ms.checks_run)}</dd>
      <dt>results written</dt><dd>${esc(ms.results_written)}</dd>
      <dt>vendors</dt><dd>${esc((s.vendors || []).map((v) => v.name).join(', '))}</dd>
      <dt>check kinds</dt><dd>${esc((s.check_kinds || []).join(', '))}</dd>
    `;
    $('#data-dir').textContent = s.db_path;
  } catch (err) { toast(err.message, 'err'); }
}

async function saveNotify() {
  const payload = {
    enabled: $('#nt-enabled').checked,
    min_severity: $('#nt-min').value,
    cooldown_sec: Number($('#nt-cooldown').value || 300),
    quiet_hours: $('#nt-quiet').value,
    notify_on_recovery: $('#nt-recovery').checked,
    webhook_url: $('#nt-webhook').value,
    webhook_kind: $('#nt-kind').value,
    smtp_host: $('#nt-smtp-host').value,
    smtp_port: Number($('#nt-smtp-port').value || 587),
    smtp_user: $('#nt-smtp-user').value,
    smtp_password: $('#nt-smtp-pass').value,
    smtp_from: $('#nt-smtp-from').value,
    smtp_to: $('#nt-smtp-to').value,
    syslog_host: $('#nt-syslog-host').value,
    syslog_port: Number($('#nt-syslog-port').value || 514),
  };
  try {
    await api('/api/settings/notify', { method: 'PUT', body: payload });
    toast('Notification settings saved.', 'ok');
    $('#nt-smtp-pass').value = '';
    loadSettings();
  } catch (err) { toast(err.message, 'err'); }
}

/* ── device add/edit modal ────────────────────────────────────────── */

function openDeviceEditor(device) {
  const d = device || { name: '', host: '', vendor: 'mikrotik', ssh_port: 22, tags: [], site: '', notes: '', enabled: true, mgmt_url: '' };
  const credOptions = state.credentials.map((c) =>
    `<option value="${c.id}" ${c.id === d.credential_id ? 'selected' : ''}>${esc(c.name)} (${esc(c.username || '—')})</option>`).join('');

  openModal(device ? `Edit ${d.name}` : 'Add device', `
    <div class="form-grid">
      <label>Host / IP *<input type="text" id="f-host" value="${esc(d.host)}" placeholder="10.0.0.1" ${device ? 'readonly' : ''}></label>
      <label>Display name<input type="text" id="f-name" value="${esc(d.name)}" placeholder="core-router-1"></label>
      <label>Vendor
        <select id="f-vendor">
          ${(state.meta ? state.meta.vendors : []).map((v) =>
            `<option value="${esc(v.name)}" ${v.name === d.vendor ? 'selected' : ''}>${esc(v.label)}${v.supports_deploy ? '' : ' (monitoring only)'}</option>`).join('')}
        </select>
      </label>
      <label>SSH port<input type="number" id="f-port" value="${esc(d.ssh_port || 22)}"></label>
      <label>Credential
        <select id="f-cred"><option value="">— none —</option>${credOptions}</select>
      </label>
      <label>Site<input type="text" id="f-site" value="${esc(d.site || '')}"></label>
      <label class="wide">Tags (comma separated)<input type="text" id="f-tags" value="${esc((d.tags || []).join(', '))}" placeholder="core, datacenter, cisco"></label>
      <label class="wide">Management URL (optional, monitored over HTTP/S)<input type="text" id="f-mgmt" value="${esc(d.mgmt_url || '')}" placeholder="https://10.0.0.1"></label>
      <label class="wide">Notes<textarea id="f-notes" rows="2">${esc(d.notes || '')}</textarea></label>
    </div>
    <label class="switch"><input type="checkbox" id="f-enabled" ${d.enabled !== false ? 'checked' : ''}> monitored</label>
    ${device ? '' : '<label class="switch"><input type="checkbox" id="f-autochecks" checked> attach default monitors (ping + SSH port) right away</label>'}
  `, [
    { label: 'Cancel', cls: 'ghost', action: closeModal },
    {
      label: device ? 'Save changes' : 'Add device',
      cls: 'primary',
      action: async () => {
        const payload = {
          host: $('#f-host').value.trim(),
          name: $('#f-name').value.trim(),
          vendor: $('#f-vendor').value,
          ssh_port: Number($('#f-port').value || 22),
          credential_id: $('#f-cred').value ? Number($('#f-cred').value) : null,
          site: $('#f-site').value,
          tags: $('#f-tags').value.split(',').map((t) => t.trim()).filter(Boolean),
          mgmt_url: $('#f-mgmt').value.trim() || null,
          notes: $('#f-notes').value,
          enabled: $('#f-enabled').checked,
        };
        if (!payload.host) return toast('Host is required.', 'err');
        if (!device) payload.auto_checks = $('#f-autochecks').checked;
        try {
          const saved = device
            ? await api(`/api/devices/${device.id}`, { method: 'PUT', body: payload })
            : await api('/api/devices', { method: 'POST', body: payload });
          toast(device ? 'Device updated.' : `${saved.name} added — monitoring starts now.`, 'ok');
          closeModal();
          await refreshOverview();
          await loadDevices();
        } catch (err) { toast(err.message, 'err'); }
      },
    },
  ]);
}

function openCredentialEditor() {
  openModal('Credentials', `
    <p class="sub">Passwords are encrypted at rest with a key stored next to the database (or taken from <span class="mono">NETPILOT_KEY</span>).</p>
    <div class="table-wrap" style="margin-bottom:16px"><table class="table">
      <thead><tr><th>Name</th><th>Username</th><th>SNMP</th><th></th></tr></thead>
      <tbody>${state.credentials.map((c) => `
        <tr>
          <td><b>${esc(c.name)}</b></td>
          <td class="mono">${esc(c.username)}</td>
          <td class="mono">${esc(c.snmp_version)}${c.snmp_community ? ' · community set' : ''}</td>
          <td class="actions">
            <button class="btn small" data-cred-edit="${c.id}">edit</button>
            <button class="btn small danger" data-cred-del="${c.id}">✕</button>
          </td>
        </tr>`).join('') || '<tr><td colspan="4" class="empty">No credentials stored.</td></tr>'}
      </tbody>
    </table></div>
    <h4>Add credential</h4>
    <div class="form-grid">
      <label>Name *<input type="text" id="c-name" placeholder="core-switches"></label>
      <label>Username<input type="text" id="c-user"></label>
      <label>Password<input type="password" id="c-pass" autocomplete="new-password"></label>
      <label>Enable password (Cisco)<input type="password" id="c-enable" autocomplete="new-password"></label>
      <label>SSH private key path<input type="text" id="c-key" placeholder="/home/you/.ssh/id_ed25519"></label>
      <label>Key passphrase<input type="password" id="c-keypass" autocomplete="new-password"></label>
      <label>SNMP version
        <select id="c-snmpver"><option>1</option><option selected>2c</option><option>3</option></select>
      </label>
      <label>SNMP community<input type="text" id="c-community" placeholder="public"></label>
    </div>
  `, [
    { label: 'Close', cls: 'ghost', action: closeModal },
    {
      label: 'Add credential',
      cls: 'primary',
      action: async () => {
        const payload = {
          name: $('#c-name').value.trim(),
          username: $('#c-user').value,
          password: $('#c-pass').value || null,
          enable_password: $('#c-enable').value || null,
          key_path: $('#c-key').value || null,
          key_passphrase: $('#c-keypass').value || null,
          snmp_version: $('#c-snmpver').value,
          snmp_community: $('#c-community').value || null,
        };
        if (!payload.name) return toast('Credential name is required.', 'err');
        try {
          await api('/api/credentials', { method: 'POST', body: payload });
          toast('Credential stored (encrypted).', 'ok');
          closeModal();
          await loadCredentials();
        } catch (err) { toast(err.message, 'err'); }
      },
    },
  ]);
}

async function loadCredentials() {
  try { state.credentials = await api('/api/credentials'); }
  catch { state.credentials = []; }
}

/* ── modal plumbing ───────────────────────────────────────────────── */

let modalButtons = [];
function openModal(title, bodyHtml, buttons = []) {
  $('#modal-title').textContent = title;
  $('#modal-body').innerHTML = bodyHtml;
  const foot = $('#modal-foot');
  foot.innerHTML = '';
  modalButtons = buttons;
  buttons.forEach((btn, index) => {
    const el = document.createElement('button');
    el.className = `btn ${btn.cls || ''}`;
    el.textContent = btn.label;
    el.addEventListener('click', () => btn.action());
    foot.appendChild(el);
  });
  $('#modal-backdrop').classList.add('open');
}

function closeModal() {
  $('#modal-backdrop').classList.remove('open');
  $('#modal-body').innerHTML = '';
}

/* ── live updates ─────────────────────────────────────────────────── */

function connectStream() {
  if (state.es) state.es.close();
  const es = new EventSource('/api/events/stream');
  state.es = es;

  es.onopen = () => {
    $('#live-dot').className = 'live-dot on';
    $('#live-label').textContent = 'live';
  };
  es.onerror = () => {
    $('#live-dot').className = 'live-dot off';
    $('#live-label').textContent = 'reconnecting…';
  };
  es.onmessage = (evt) => {
    let payload;
    try { payload = JSON.parse(evt.data); } catch { return; }
    if (payload.type === 'hello') return;
    if (payload.type === 'event') {
      const event = payload.data;
      const list = $('#dash-events');
      if (list) {
        const div = document.createElement('div');
        div.className = `event ${event.severity}`;
        div.innerHTML = `<div class="row"><span class="msg">${esc(event.message)}</span>
          <span class="when">now</span></div>`;
        list.prepend(div);
        while (list.children.length > 40) list.lastChild.remove();
      }
      if (event.severity === 'critical') toast(event.message, 'err');
      const badge = $('#nav-alert-count');
      badge.textContent = Number(badge.textContent || 0) + 1;
      badge.classList.remove('zero');
      if ($('#view-events').classList.contains('active')) loadEvents();
    }
    if (payload.type === 'result') {
      const card = document.querySelector(`.dev-card[data-device="${payload.data.device_id}"]`);
      if (card) card.style.transition = 'background .35s';
    }
    if (payload.type === 'job' && $('#view-deploy').classList.contains('active')) loadJobs();
  };
}

/* ── one-off event delegation ─────────────────────────────────────── */

document.addEventListener('click', async (e) => {
  const target = e.target;

  const devCard = target.closest('.dev-card');
  if (devCard && !target.closest('button')) return openDevice(Number(devCard.dataset.device));

  const chip = target.closest('.chip');
  if (chip) {
    state.tagFilter = chip.dataset.tag || '';
    renderTags();
    renderDeviceGrid();
    if ($('#view-inventory').classList.contains('active')) renderDeviceTable();
    if ($('#view-deploy').classList.contains('active')) renderDeployTargets();
    return;
  }

  const ackBtn = target.closest('[data-ack]');
  if (ackBtn) {
    await api(`/api/events/${ackBtn.dataset.ack}/ack`, { method: 'POST', body: { acknowledged: true } });
    loadEvents();
    refreshOverview();
    return;
  }

  const jobOpen = target.closest('[data-job-open]');
  if (jobOpen) {
    const job = await api(`/api/deploys/${jobOpen.dataset.jobOpen}`);
    showJobModal(job);
    return;
  }

  const tplView = target.closest('[data-tpl-view]');
  if (tplView) {
    const t = findTemplate(tplView.dataset.tplView);
    if (t) openModal(t.name, `
      <p class="sub">${esc(t.vendor)} · ${esc(t.description)}</p>
      <div class="preview" style="max-height:50vh">${esc(t.body)}</div>
      <h4>Variables</h4>
      <dl class="kv">${Object.entries(t.variables || {}).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('') || '<dt>—</dt><dd></dd>'}</dl>
    `, [{ label: 'Close', cls: 'primary', action: closeModal }]);
    return;
  }

  const tplEdit = target.closest('[data-tpl-edit]');
  if (tplEdit) {
    const t = findTemplate(tplEdit.dataset.tplEdit);
    openTemplateEditor(t ? { ...t, id: t.builtin ? undefined : t.id } : null);
    return;
  }

  const bkView = target.closest('[data-bk-view]');
  if (bkView) return viewBackup(Number(bkView.dataset.bkView));

  const bkRestore = target.closest('[data-bk-restore]');
  if (bkRestore) return restoreBackup(Number(bkRestore.dataset.bkRestore));

  const bkDel = target.closest('[data-bk-del]');
  if (bkDel) {
    if (!confirm('Delete this stored configuration?')) return;
    await api(`/api/backups/${bkDel.dataset.bkDel}`, { method: 'DELETE' });
    toast('Backup deleted.');
    loadBackups();
    return;
  }

  const credEdit = target.closest('[data-cred-edit]');
  if (credEdit) return toast('Edit credentials from the API for now — open /api/credentials', 'warn');

  const credDel = target.closest('[data-cred-del]');
  if (credDel) {
    if (!confirm('Delete this credential? Devices using it will lose their login.')) return;
    await api(`/api/credentials/${credDel.dataset.credDel}`, { method: 'DELETE' });
    await loadCredentials();
    openCredentialEditor();
    return;
  }

  const action = target.closest('[data-act]');
  if (action) return handleDrawerAction(action.dataset.act, action.dataset.id);

  const row = target.closest('#device-table tbody tr');
  if (row && target.closest('button')) {
    const act = target.closest('button').dataset.act;
    if (act === 'open') return openDevice(Number(row.dataset.device));
    if (act === 'probe') {
      toast('Running checks…');
      try {
        const res = await api(`/api/devices/${row.dataset.device}/probe`, { method: 'POST' });
        const okCount = res.results.filter((r) => r.ok).length;
        toast(`${okCount}/${res.results.length} checks passed.`, okCount === res.results.length ? 'ok' : 'warn');
        refreshOverview();
      } catch (err) { toast(err.message, 'err'); }
      return;
    }
  }
  if (row && !target.closest('button')) return openDevice(Number(row.dataset.device));
});

async function handleDrawerAction(act, id) {
  const deviceId = state.currentDeviceId;
  const device = state.devices.find((d) => d.id === deviceId);

  if (act === 'test') {
    toast('Connecting over SSH…');
    try {
      const res = await api(`/api/devices/${deviceId}/test`, { method: 'POST' });
      if (res.ok) toast(`Connected in ${res.elapsed_ms} ms — ${res.summary}`, 'ok');
      else toast(`Connection failed: ${res.error}`, 'err');
    } catch (err) { toast(err.message, 'err'); }
    return;
  }
  if (act === 'probe') {
    toast('Running all monitors…');
    const res = await api(`/api/devices/${deviceId}/probe`, { method: 'POST' });
    const okCount = res.results.filter((r) => r.ok).length;
    toast(`${okCount}/${res.results.length} checks passed.`, okCount === res.results.length ? 'ok' : 'warn');
    await openDevice(deviceId);
    refreshOverview();
    return;
  }
  if (act === 'backup') {
    toast('Capturing configuration…');
    const res = await api(`/api/devices/${deviceId}/backup`, { method: 'POST' });
    if (res.ok) toast(`Captured ${res.bytes} bytes.`, 'ok');
    else toast(`Backup failed: ${res.error}`, 'err');
    await openDevice(deviceId);
    return;
  }
  if (act === 'edit') { closeDrawer(); return openDeviceEditor(device); }
  if (act === 'delete') {
    if (!confirm(`Delete ${device ? device.name : 'this device'} and all of its history?`)) return;
    await api(`/api/devices/${deviceId}`, { method: 'DELETE' });
    toast('Device deleted.');
    closeDrawer();
    await refreshOverview();
    await loadDevices();
    return;
  }
  if (act === 'deploy-one') {
    showView('deploy');
    state.deploySelection = new Set([deviceId]);
    renderDeployTargets();
    closeDrawer();
    return;
  }
  if (act === 'deploy-one-run') {
    const template = currentDrawerTemplate();
    if (!template) return toast('Pick a template first.', 'err');
    const variables = {};
    $$('#dd-vars input[data-var]').forEach((i) => { variables[i.dataset.var] = i.value; });
    try {
      const job = await api('/api/deploys', {
        method: 'POST',
        body: {
          template_id: template.id,
          template_name: template.name,
          body: template.body,
          vendor: device ? device.vendor : 'mikrotik',
          device_ids: [deviceId],
          variables,
          options: { dry_run: $('#dd-dry').checked, backup_before: true, auto_rollback: true, max_parallel: 1 },
        },
      });
      toast(`Deployment #${job.id} started.`, 'ok');
      pollJob(job.id);
    } catch (err) { toast(err.message, 'err'); }
    return;
  }
  if (act === 'check-del') {
    if (!confirm('Remove this monitor?')) return;
    await api(`/api/checks/${id}`, { method: 'DELETE' });
    await openDevice(deviceId);
    return;
  }
  if (act === 'check-add') return openCheckEditor(deviceId, null);
  if (act === 'check-edit') return openCheckEditor(deviceId, Number(id));
  if (act === 'backup-view') return viewBackup(Number(id));
}

function openCheckEditor(deviceId, checkId) {
  const device = state.devices.find((d) => d.id === deviceId);
  const kinds = state.meta ? state.meta.check_kinds : ['icmp', 'tcp', 'http', 'https', 'snmp', 'ssh'];
  openModal(checkId ? 'Edit monitor' : 'Add monitor', `
    <div class="form-grid">
      <label>Kind
        <select id="k-kind">${kinds.map((k) => `<option ${checkId ? '' : ''}>${k}</option>`).join('')}</select>
      </label>
      <label>Label<input type="text" id="k-label" placeholder="Ping"></label>
      <label>Interval (seconds)<input type="number" id="k-interval" value="60" min="5"></label>
      <label>Timeout (seconds)<input type="number" id="k-timeout" value="5" step="0.5" min="0.5"></label>
      <label>Failures before down<input type="number" id="k-fail" value="2" min="1"></label>
      <label>Successes before up<input type="number" id="k-up" value="1" min="1"></label>
      <label class="wide">Parameters (JSON)<textarea id="k-params" rows="3" spellcheck="false">{}</textarea></label>
    </div>
    <p class="hint">Examples — <span class="mono">{"count": 3}</span> ·
      <span class="mono">{"port": 443}</span> ·
      <span class="mono">{"url": "https://10.0.0.1/status"}</span> ·
      <span class="mono">{"oids": "1.3.6.1.2.1.1.3.0"}</span></p>
  `, [
    { label: 'Cancel', cls: 'ghost', action: closeModal },
    {
      label: 'Save monitor',
      cls: 'primary',
      action: async () => {
        let params;
        try { params = JSON.parse($('#k-params').value || '{}'); }
        catch { return toast('Parameters must be valid JSON.', 'err'); }
        const payload = {
          device_id: deviceId,
          kind: $('#k-kind').value,
          label: $('#k-label').value,
          interval_sec: Number($('#k-interval').value || 60),
          timeout_sec: Number($('#k-timeout').value || 5),
          failures_to_down: Number($('#k-fail').value || 2),
          successes_to_up: Number($('#k-up').value || 1),
          params,
        };
        try {
          if (checkId) await api(`/api/checks/${checkId}`, { method: 'PUT', body: payload });
          else await api('/api/checks', { method: 'POST', body: payload });
          toast('Monitor saved.', 'ok');
          closeModal();
          await openDevice(deviceId);
        } catch (err) { toast(err.message, 'err'); }
      },
    },
  ]);
  const sel = $('#k-kind');
  sel.addEventListener('change', () => {
    const examples = {
      icmp: { count: 3 },
      tcp: { port: 22 },
      http: { path: '/', scheme: 'http' },
      https: { path: '/', insecure: true },
      snmp: { oids: '1.3.6.1.2.1.1.3.0', port: 161 },
      ssh: { port: 22 },
    };
    $('#k-params').value = JSON.stringify(examples[sel.value] || {}, null, 0);
  });
  if (checkId) {
    const check = ((state.overview && state.overview.devices) || []).flatMap((d) => d.checks || []).find((c) => c.id === checkId);
    if (check) {
      $('#k-kind').value = check.kind;
      $('#k-label').value = check.label || '';
      $('#k-interval').value = check.interval_sec;
      $('#k-timeout').value = check.timeout_sec;
      $('#k-fail').value = check.failures_to_down;
      $('#k-up').value = check.successes_to_up;
      $('#k-params').value = JSON.stringify(check.params || {});
    }
  } else if (device) {
    $('#k-label').value = 'Ping';
    $('#k-params').value = JSON.stringify({ count: 3 });
  }
}

/* ── header wiring ────────────────────────────────────────────────── */

$('#btn-refresh').addEventListener('click', refreshOverview);
$('#btn-add-device').addEventListener('click', () => openDeviceEditor(null));
$('#btn-quick-add').addEventListener('click', () => openDeviceEditor(null));
$('#btn-new-template').addEventListener('click', () => openTemplateEditor(null));
$('#btn-credentials').addEventListener('click', () => { loadCredentials().then(openCredentialEditor); });
$('#dd-close').addEventListener('click', closeDrawer);
$('#drawer-backdrop').addEventListener('click', closeDrawer);
$('#modal-close').addEventListener('click', closeModal);
$('#modal-backdrop').addEventListener('click', (e) => { if (e.target.id === 'modal-backdrop') closeModal(); });
$('#btn-deploy-run').addEventListener('click', runDeployment);
$('#deploy-body').addEventListener('input', () => { clearTimeout(window._pvT); window._pvT = setTimeout(updateDeployPreview, 350); });
$('#deploy-vars').addEventListener('input', () => { clearTimeout(window._pvT); window._pvT = setTimeout(updateDeployPreview, 350); });
$('#deploy-vars').addEventListener('change', updateDeployPreview);
$('#dd-vars').addEventListener('input', () => {
  const d = state.devices.find((x) => x.id === state.currentDeviceId);
  if (d) { clearTimeout(window._ddT); window._ddT = setTimeout(() => updateDrawerPreview(d), 350); }
});
$('#dd-vars').addEventListener('change', () => {
  const d = state.devices.find((x) => x.id === state.currentDeviceId);
  if (d) updateDrawerPreview(d);
});
$('#dash-search').addEventListener('input', (e) => { state.deviceFilter = e.target.value; renderDeviceGrid(); });
$('#deploy-search').addEventListener('input', (e) => { state.deployTargetsFilter = e.target.value; renderDeployTargets(); });
$('#deploy-targets').addEventListener('change', (e) => {
  const box = e.target.closest('input[data-target]');
  if (!box) return;
  const id = Number(box.dataset.target);
  if (box.checked) state.deploySelection.add(id); else state.deploySelection.delete(id);
  state.autoDeploySelection = false;
  updateTargetCount();
});
$('#btn-ack-all').addEventListener('click', async () => { await api('/api/events/ack-all', { method: 'POST' }); refreshOverview(); });
$('#btn-ack-all-2').addEventListener('click', async () => { await api('/api/events/ack-all', { method: 'POST' }); loadEvents(); refreshOverview(); });
$('#btn-save-notify').addEventListener('click', saveNotify);
$('#btn-test-notify').addEventListener('click', async () => {
  try {
    const res = await api('/api/settings/notify/test', { method: 'POST' });
    $('#notify-result').innerHTML = res.sent
      ? `<span class="ok-text">Test alert delivered to ${res.sinks.filter((s) => s.ok).length} sink(s).</span>`
      : `<span class="warn-text">Not delivered: ${esc(res.reason)}${res.sinks.length ? ' — ' + esc(res.sinks.map((s) => s.error).join('; ')) : ''}</span>`;
  } catch (err) { toast(err.message, 'err'); }
});
$('#events-severity').addEventListener('change', loadEvents);
$('#events-unacked').addEventListener('change', loadEvents);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { closeModal(); closeDrawer(); }
});

/* ── boot ─────────────────────────────────────────────────────────── */

(async function boot() {
  try {
    state.meta = await api('/api/meta');
  } catch (err) {
    toast('netpilot API unreachable', 'err');
    return;
  }
  await loadCredentials();
  await refreshOverview();
  connectStream();
  setInterval(refreshOverview, 20000);
})();
