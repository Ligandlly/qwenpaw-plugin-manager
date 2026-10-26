/* QwenPaw Fleet console — no build step, no framework, no inline script.
   Everything the server returns is treated as text; only local templates
   create markup. */

'use strict';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const TOKEN_KEY = 'qpm-console-token';
const POLL_MS = 700;

const state = {
  token: '',
  meta: null,
  runtimes: [],
  selected: new Set(),
  knownIds: new Set(),
  selectionReady: false,
  view: 'inventory',
  plan: null,
  jobId: '',
  pollTimer: 0,
  jobs: [],
  activeJob: null,
};

/* ── tiny DOM helpers ─────────────────────────────────────────────────── */

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2), value);
    } else if (key in node && key !== 'list') node[key] = value;
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function clear(node) {
  node.replaceChildren();
}

/* ── API ──────────────────────────────────────────────────────────────── */

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

async function request(path, options = {}) {
  const { method = 'GET', body } = options;
  const headers = {};
  if (state.token) headers['X-QPM-Token'] = state.token;
  let payload = body;
  if (body !== undefined && !(body instanceof FormData)) {
    headers['Content-Type'] = 'application/json';
    payload = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: payload,
      credentials: 'same-origin',
      cache: 'no-store',
    });
  } catch (error) {
    throw new ApiError('Cannot reach the local console server.', 0);
  }
  if (response.status === 401) {
    showTokenBlock();
    throw new ApiError('Console token rejected. Paste the token printed by `qpm serve`.', 401);
  }
  let data = null;
  const type = response.headers.get('Content-Type') || '';
  if (type.includes('application/json')) {
    try {
      data = await response.json();
    } catch (error) {
      data = null;
    }
  }
  if (!response.ok) {
    const message = (data && data.error) || `HTTP ${response.status}`;
    throw new ApiError(message, response.status);
  }
  return data;
}

/* ── boot ─────────────────────────────────────────────────────────────── */

document.addEventListener('DOMContentLoaded', () => {
  bindControls();
  readTokenFromHash();
  console.info('[qpm] boot, token', state.token ? 'present' : 'missing');
  if (!state.token) {
    showTokenBlock();
    setConn('idle', 'token required');
    return;
  }
  connect();
});

function readTokenFromHash() {
  const hash = location.hash.replace(/^#/, '');
  if (hash) {
    const params = new URLSearchParams(hash);
    const token = params.get('token');
    if (token) {
      state.token = token;
      sessionStorage.setItem(TOKEN_KEY, token);
      history.replaceState(null, '', location.pathname + location.search);
      return;
    }
  }
  state.token = sessionStorage.getItem(TOKEN_KEY) || '';
}

function showTokenBlock() {
  $('#token-block').hidden = false;
}

function hideTokenBlock() {
  $('#token-block').hidden = true;
}

async function connect() {
  setConn('busy', 'connecting');
  try {
    state.meta = await request('/api/meta');
    console.info('[qpm] meta loaded', state.meta.version);
    hideTokenBlock();
    renderFooter();
    await loadFleet();
    console.info('[qpm] fleet loaded', state.runtimes.length);
    await resumeActiveJob();
  } catch (error) {
    if (error.status === 401) {
      setConn('bad', 'token required');
    } else {
      setConn('bad', 'offline');
      toast(error.message, 'bad');
    }
  }
}

/* ── fleet loading ────────────────────────────────────────────────────── */

function includeStopped() {
  return $('#include-stopped').checked;
}

function inventoryUrl() {
  return `/api/inventory?include_stopped=${includeStopped() ? 1 : 0}`;
}

async function loadFleet() {
  setConn('busy', 'reading fleet');
  const data = await request(inventoryUrl());
  state.runtimes = data.runtimes || [];
  const known = new Set(state.runtimes.map((runtime) => runtime.runtime_id));
  const knownBefore = state.knownIds;
  if (!state.selectionReady) {
    state.selected = new Set(
      state.runtimes
        .filter((runtime) => runtime.state === 'running')
        .map((runtime) => runtime.runtime_id),
    );
    state.selectionReady = true;
  } else {
    // Drop runtimes that disappeared, keep deliberate deselections, and
    // select runtimes that appeared since the previous refresh.
    state.selected = new Set([...state.selected].filter((id) => known.has(id)));
    for (const runtime of state.runtimes) {
      if (runtime.state === 'running' && !knownBefore.has(runtime.runtime_id)) {
        state.selected.add(runtime.runtime_id);
      }
    }
  }
  state.knownIds = known;
  if (!state.selected.size) {
    for (const runtime of state.runtimes) state.selected.add(runtime.runtime_id);
  }
  state.warnings = data.warnings || [];
  renderTargets();
  renderStats();
  renderInventory();
  renderChips();
  renderFreezeOptions();
  const running = state.runtimes.filter((runtime) => runtime.state === 'running').length;
  const dryRun =
    state.meta && state.meta.execution && state.meta.execution.dry_run;
  setConn('live', `${state.runtimes.length} runtimes · ${running} live${dryRun ? ' · dry run' : ''}`);
  if (state.warnings.length && state.warnings.length <= 3) {
    for (const warning of state.warnings) toast(warning, 'info');
  }
}

async function refreshFleet() {
  try {
    await loadFleet();
    toast('Fleet refreshed.', 'good');
  } catch (error) {
    toast(error.message, 'bad');
  }
}

function renderFooter() {
  const meta = state.meta || {};
  const execution = meta.execution || {};
  const discovery = meta.discovery || {};
  const bits = [
    `qpm ${meta.version || ''}`.trim(),
    `source ${discovery.source || 'docker'}`,
    `backups ${execution.backup ? 'on' : 'off'}`,
    `concurrency ${execution.concurrency || '?'}`,
  ];
  if (execution.dry_run) bits.push('DRY RUN');
  $('#footer-meta').textContent = bits.join(' · ');
  $('.footer__hint').textContent = meta.token_required
    ? 'loopback only · token required'
    : 'loopback only · token disabled';
}

function setConn(kind, text) {
  $('#conn-lamp').dataset.state = kind;
  $('#conn-text').textContent = text;
}

/* ── rail: targets, stats ─────────────────────────────────────────────── */

function visibleRuntimes() {
  const needle = $('#target-filter').value.trim().toLowerCase();
  return state.runtimes.filter((runtime) => {
    if (!needle) return true;
    return (
      runtime.runtime_id.toLowerCase().includes(needle) ||
      (runtime.tenant_id || '').toLowerCase().includes(needle)
    );
  });
}

function renderTargets() {
  const list = $('#target-list');
  clear(list);
  const runtimes = visibleRuntimes();
  if (!runtimes.length) {
    list.append(el('p', { class: 'empty', text: 'No runtime matches that filter.' }));
  }
  for (const runtime of runtimes) {
    const checkbox = el('input', {
      type: 'checkbox',
      checked: state.selected.has(runtime.runtime_id),
      onchange: (event) => {
        if (event.target.checked) state.selected.add(runtime.runtime_id);
        else state.selected.delete(runtime.runtime_id);
        renderSelectionHint();
        invalidatePlans();
      },
    });
    const port = portOf(runtime.base_url);
    const row = el(
      'label',
      {
        class: 'target',
        dataset: { state: runtime.state },
        title: `${runtime.base_url}${runtime.tenant_id ? ` · ${runtime.tenant_id}` : ''}`,
      },
      [
        checkbox,
        el('span', { class: 'lamp', dataset: { state: lampFor(runtime) } }),
        el('span', {}, [
          el('span', { class: 'target__id', text: runtime.runtime_id }),
          el('span', {
            class: 'target__tenant',
            text: runtime.tenant_id || 'no tenant',
          }),
        ]),
        el('span', { class: 'target__port', text: port ? `:${port}` : '—' }),
      ],
    );
    list.append(row);
  }
  renderSelectionHint();
}

function renderSelectionHint() {
  const total = state.selected.size;
  const plugins = state.runtimes
    .filter((runtime) => state.selected.has(runtime.runtime_id))
    .reduce((sum, runtime) => sum + (runtime.plugins ? runtime.plugins.length : 0), 0);
  $('#selection-hint').textContent = total
    ? `${total} target${total === 1 ? '' : 's'} selected · ${plugins} plugin slot${plugins === 1 ? '' : 's'}`
    : 'No targets selected. Operations will refuse to run.';
}

function renderStats() {
  const container = $('#fleet-stats');
  clear(container);
  const running = state.runtimes.filter((runtime) => runtime.state === 'running').length;
  const failed = state.runtimes.filter((runtime) => runtime.error).length;
  const plugins = state.runtimes.reduce(
    (sum, runtime) => sum + (runtime.plugins ? runtime.plugins.length : 0),
    0,
  );
  const disabled = state.runtimes.reduce(
    (sum, runtime) =>
      sum + (runtime.plugins || []).filter((plugin) => !plugin.enabled).length,
    0,
  );
  const rows = [
    ['runtimes', state.runtimes.length],
    ['running', running],
    ['plugin slots', plugins],
    ['disabled', disabled],
    ['inventory errors', failed],
  ];
  for (const [label, value] of rows) {
    container.append(
      el('div', { class: label === 'inventory errors' && value ? 'stat stat--warn' : 'stat' }, [
        el('span', { text: label }),
        el('b', { text: String(value) }),
      ]),
    );
  }
}

/* ── inventory ────────────────────────────────────────────────────────── */

function lampFor(runtime) {
  if (runtime.error) return 'bad';
  if (runtime.state === 'running') return 'live';
  return 'idle';
}

function portOf(url) {
  try {
    return new URL(url).port;
  } catch (error) {
    return '';
  }
}

function renderInventory() {
  const container = $('#inventory');
  clear(container);
  const needle = $('#plugin-filter').value.trim().toLowerCase();
  const runtimes = state.runtimes.filter((runtime) => {
    if (!needle) return true;
    return (runtime.plugins || []).some((plugin) =>
      plugin.id.toLowerCase().includes(needle),
    );
  });
  if (!runtimes.length) {
    container.append(el('p', { class: 'empty', text: 'Nothing to show.' }));
    return;
  }
  for (const runtime of runtimes) {
    container.append(renderUnit(runtime, needle));
  }
}

function renderUnit(runtime, needle) {
  const unit = el('article', {
    class: 'unit',
    dataset: { state: runtime.state },
  });
  const plugins = (runtime.plugins || []).filter(
    (plugin) => !needle || plugin.id.toLowerCase().includes(needle),
  );
  const head = el('header', { class: 'unit__head' }, [
    el('span', { class: 'lamp', dataset: { state: lampFor(runtime) } }),
    el('span', { class: 'unit__id', text: runtime.runtime_id }),
    el('span', { class: 'unit__port', text: portOf(runtime.base_url) ? `:${portOf(runtime.base_url)}` : 'no port' }),
    el('span', { class: 'unit__meta' }, [
      'qwenpaw ',
      el('b', { text: runtime.qwenpaw_version || 'unknown' }),
    ]),
    el('span', { class: 'unit__meta' }, [
      'tenant ',
      el('b', { text: runtime.tenant_id || '—' }),
    ]),
    el('span', { class: 'unit__meta' }, [
      'plugins ',
      el('b', { text: String((runtime.plugins || []).length) }),
    ]),
    el('span', { class: 'unit__spacer' }),
    el('span', {
      class: 'pill',
      dataset: { tone: runtime.state === 'running' ? 'live' : 'muted' },
      text: runtime.state,
    }),
  ]);
  unit.append(head);

  if (runtime.error) {
    unit.append(el('p', { class: 'unit__error', text: runtime.error }));
    return unit;
  }
  if (!plugins.length) {
    unit.append(
      el('p', {
        class: 'empty',
        text: needle ? 'No plugin matches that filter.' : 'No plugins installed.',
      }),
    );
    return unit;
  }

  const slots = el('div', { class: 'slots' });
  for (const plugin of plugins) {
    slots.append(renderSlot(runtime, plugin));
  }
  unit.append(slots);
  return unit;
}

function renderSlot(runtime, plugin) {
  const running = runtime.state === 'running';
  const status = (plugin.status || '').toLowerCase();
  const failed = status === 'failed' || status === 'error';
  const badges = el('div', { class: 'slot__badges' }, [
    el('span', {
      class: 'pill',
      dataset: { tone: plugin.enabled ? 'live' : 'muted' },
      text: plugin.enabled ? 'enabled' : 'disabled',
    }),
    el('span', {
      class: 'pill',
      dataset: { tone: plugin.loaded ? 'signal' : 'muted' },
      text: plugin.loaded ? 'loaded' : 'unloaded',
    }),
  ]);
  if (failed) {
    badges.append(el('span', { class: 'pill', dataset: { tone: 'bad' }, text: status }));
  }

  const toggle = el('button', {
    class: 'btn btn--small btn--ghost',
    type: 'button',
    text: plugin.enabled ? 'Disable' : 'Enable',
    disabled: !running,
    title: running ? '' : `runtime is ${runtime.state}`,
    onclick: () =>
      quickOperation(plugin.enabled ? 'disable' : 'enable', runtime, plugin.id),
  });
  const remove = el('button', {
    class: 'btn btn--small btn--danger',
    type: 'button',
    text: 'Uninstall',
    disabled: !running,
    title: running ? '' : `runtime is ${runtime.state}`,
    onclick: () => quickOperation('uninstall', runtime, plugin.id),
  });

  return el(
    'div',
    {
      class: 'slot',
      dataset: {
        state: failed ? 'failed' : 'ok',
        enabled: String(plugin.enabled),
      },
    },
    [
      el('span', { class: 'slot__id' }, [
        plugin.id,
        el('span', {
          class: 'slot__sub',
          text: plugin.type ? `${plugin.type} plugin` : 'plugin',
        }),
      ]),
      el('span', { class: 'slot__version', text: plugin.version || '—' }),
      badges,
      el('div', { class: 'slot__actions' }, [toggle, remove]),
    ],
  );
}

/* ── plan + run flow ──────────────────────────────────────────────────── */

function currentFilters() {
  return {
    runtime: [...state.selected],
    include_stopped: includeStopped(),
  };
}

function renderPlan(container, plan) {
  container.hidden = false;
  clear(container);
  const mutating = plan.summary ? plan.summary.mutating : 0;
  container.append(
    el('div', { class: 'plan__head' }, [
      el('div', {}, [
        el('p', { class: 'eyebrow', text: 'Plan' }),
        el('p', { class: 'plan__title', text: `${plan.actions.length} decisions` }),
      ]),
      el('div', { class: 'plan__counts' }, [
        el('span', { text: `${mutating} change${mutating === 1 ? '' : 's'}` }),
        ...Object.entries((plan.summary && plan.summary.kinds) || {})
          .sort((a, b) => b[1] - a[1])
          .map(([kind, count]) => el('span', { text: `${count} ${kind}` })),
      ]),
    ]),
  );
  if (!plan.actions.length) {
    container.append(
      el('p', { class: 'empty', text: 'No action matches the current sources and targets.' }),
    );
    return;
  }
  const table = el('div', { class: 'plan__table' });
  table.append(
    el('div', { class: 'plan__row plan__row--head' }, [
      el('span', { text: 'action' }),
      el('span', { text: 'runtime' }),
      el('span', { text: 'plugin' }),
      el('span', { text: 'version' }),
      el('span', { text: 'why' }),
    ]),
  );
  for (const action of plan.actions) {
    table.append(
      el('div', { class: 'plan__row' }, [
        el('span', {}, [
          el('span', {
            class: 'action-chip',
            dataset: { kind: action.action },
            text: action.action,
          }),
        ]),
        el('span', { class: 'mono', text: action.runtime_id }),
        el('span', { class: 'mono', text: action.plugin }),
        el('span', { class: 'mono dim', text: transition(action) }),
        el('span', {
          class: 'dim',
          text: action.reason || action.warning || '',
        }),
      ]),
    );
  }
  container.append(table);
  if (plan.warnings && plan.warnings.length) {
    const list = el('div', { class: 'plan__head' }, [
      el('span', { class: 'faint', text: plan.warnings.join(' · ') }),
    ]);
    container.append(list);
  }
}

function transition(action) {
  const from = action.current_version || '—';
  const to = action.target_version || '—';
  if (['install', 'upgrade', 'downgrade', 'reinstall'].includes(action.action)) {
    return `${from} → ${to}`;
  }
  if (['uninstall', 'unload'].includes(action.action)) return `${from} → removed`;
  return from;
}

async function preview(operation, params, container, runButton, hint) {
  const payload = { operation, filters: currentFilters(), params };
  if (!state.selected.size) {
    toast('Select at least one target in the rail.', 'bad');
    return null;
  }
  runButton.disabled = true;
  if (hint) hint.textContent = 'Reading the live fleet…';
  try {
    const plan = await request('/api/plan', { method: 'POST', body: payload });
    state.plan = { payload, plan };
    renderPlan(container, plan);
    const mutating = plan.summary ? plan.summary.mutating : 0;
    runButton.disabled = mutating === 0;
    if (hint) {
      hint.textContent = mutating
        ? `${mutating} change${mutating === 1 ? '' : 's'} ready — nothing has run yet.`
        : 'Nothing to change on the selected targets.';
    }
    return state.plan;
  } catch (error) {
    toast(error.message, 'bad');
    if (hint) hint.textContent = 'Plan failed: ' + error.message;
    return null;
  }
}

async function execute(payload, plan) {
  if (state.activeJob) {
    toast('A run is already in progress. Wait for it to finish.', 'info');
    return;
  }
  const kinds = (plan.summary && plan.summary.kinds) || {};
  const destructive = ['uninstall', 'downgrade'].some((kind) => (kinds[kind] || 0) > 0);
  const list = el('ul', {});
  for (const [kind, count] of Object.entries(kinds).sort()) {
    list.append(el('li', { text: `${count} × ${kind}` }));
  }
  const approved = await confirmDialog({
    title: `Run ${plan.summary.mutating} change${plan.summary.mutating === 1 ? '' : 's'}?`,
    body: [
      el('p', {
        text: `Across ${state.selected.size} selected runtime${state.selected.size === 1 ? '' : 's'}. The plan is re-read from the live fleet before it runs.`,
      }),
      list,
    ],
    confirmLabel: destructive ? 'Run and remove' : 'Run changes',
    danger: destructive,
  });
  if (!approved) return;
  try {
    const response = await request('/api/run', { method: 'POST', body: payload });
    state.jobId = response.job_id;
    state.activeJob = { job_id: response.job_id };
    switchView('jobs');
    await refreshJobs();
    startPolling();
  } catch (error) {
    toast(error.message, 'bad');
  }
}

async function quickOperation(operation, runtime, pluginId) {
  const params = { plugins: [pluginId] };
  if (operation === 'uninstall') params.keep_files = false;
  const payload = {
    operation,
    filters: { runtime: [runtime.runtime_id], include_stopped: true },
    params,
  };
  try {
    const plan = await request('/api/plan', { method: 'POST', body: payload });
    if (!plan.summary.mutating) {
      toast(`Nothing to do: ${plan.actions[0] ? plan.actions[0].reason : 'no action'}.`, 'info');
      return;
    }
    const approved = await confirmDialog({
      title: `${operation === 'uninstall' ? 'Uninstall' : operation === 'enable' ? 'Enable' : 'Disable'} ${pluginId}?`,
      body: [
        el('p', {
          text: `Runtime ${runtime.runtime_id}, currently at ${pluginVersion(runtime, pluginId) || 'unknown version'}.`,
        }),
        operation === 'uninstall'
          ? el('p', { text: 'The plugin directory is snapshotted on the host first, so a failed removal can be restored.' })
          : null,
      ],
      confirmLabel: operation === 'uninstall' ? 'Uninstall' : 'Apply',
      danger: operation === 'uninstall',
    });
    if (!approved) return;
    const response = await request('/api/run', { method: 'POST', body: payload });
    state.jobId = response.job_id;
    state.activeJob = { job_id: response.job_id };
    switchView('jobs');
    await refreshJobs();
    startPolling();
  } catch (error) {
    toast(error.message, 'bad');
  }
}

function pluginVersion(runtime, pluginId) {
  const plugin = (runtime.plugins || []).find((item) => item.id === pluginId);
  return plugin ? plugin.version : '';
}

/* ── plan freshness ───────────────────────────────────────────────────── */

const PLAN_SLOTS = [
  {
    stateKey: 'lastInstall',
    formId: 'install-form',
    runId: 'install-run',
    planId: 'install-plan',
    hintId: 'install-hint',
    idleHint: 'Nothing runs until you confirm the plan.',
  },
  {
    stateKey: 'lastUninstall',
    formId: 'uninstall-form',
    runId: 'uninstall-run',
    planId: 'uninstall-plan',
    hintId: null,
    idleHint: '',
  },
  {
    stateKey: 'lastApply',
    formId: 'apply-form',
    runId: 'apply-run',
    planId: 'apply-plan',
    hintId: 'apply-status',
    idleHint: '',
  },
];

function invalidatePlans(message = 'Inputs changed — preview the plan again.') {
  for (const slot of PLAN_SLOTS) {
    state[slot.stateKey] = null;
    const run = document.getElementById(slot.runId);
    if (run) run.disabled = true;
    const plan = document.getElementById(slot.planId);
    if (plan) plan.hidden = true;
    const hint = slot.hintId ? document.getElementById(slot.hintId) : null;
    if (hint && message) hint.textContent = message;
  }
}

/* ── install / uninstall / apply forms ────────────────────────────────── */

function splitList(value) {
  return String(value || '')
    .split(/[\s,]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function radioValue(name) {
  const checked = $(`input[name="${name}"]:checked`);
  return checked ? checked.value : '';
}

function installParams() {
  const enableValue = radioValue('enable');
  return {
    sources: $$('#install-sources [data-source]')
      .map((input) => input.value.trim())
      .filter(Boolean),
    mode: radioValue('mode') || 'sync',
    enable: enableValue === 'keep' ? null : enableValue === 'enable',
    reinstall: $('#install-form [name="reinstall"]').checked,
    allow_downgrade: $('#install-form [name="allow_downgrade"]').checked,
    install_missing: $('#install-form [name="install_missing"]').checked,
    plugins: splitList($('#install-form [name="plugins"]').value),
    protect: splitList($('#install-form [name="protect"]').value),
  };
}

function uninstallParams() {
  return {
    plugins: splitList($('#uninstall-plugins').value),
    keep_files: $('#uninstall-form [name="keep_files"]').checked,
    protect: splitList($('#uninstall-form [name="protect"]').value),
  };
}

function applyParams() {
  const text = $('#state-text').value.trim();
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (error) {
    throw new Error(`State document is not valid JSON: ${error.message}`);
  }
  return {
    state: parsed,
    mode: radioValue('apply-mode') || 'sync',
    reinstall: $('#apply-form [name="apply_reinstall"]').checked,
    allow_downgrade: $('#apply-form [name="apply_allow_downgrade"]').checked,
    install_missing: $('#apply-form [name="apply_install_missing"]').checked,
    plugins: splitList($('#apply-form [name="apply_plugins"]').value),
    protect: splitList($('#apply-form [name="apply_protect"]').value),
  };
}

function addSourceRow(value = '', label = '') {
  const list = $('#install-sources');
  const input = el('input', {
    class: 'field',
    type: 'text',
    'data-source': '',
    value,
    spellcheck: false,
    placeholder: 'Server path, https:// URL, or uploaded bundle',
  });
  const row = el('div', { class: 'source-row' }, [
    input,
    label
      ? el('span', { class: 'pill', dataset: { tone: 'signal' }, text: label })
      : null,
    el('button', {
      class: 'btn btn--ghost btn--icon',
      type: 'button',
      text: '×',
      'aria-label': 'Remove source',
      onclick: () => row.remove(),
    }),
  ]);
  list.append(row);
  input.focus();
}

function renderChips() {
  const container = $('#installed-chips');
  clear(container);
  const input = $('#uninstall-plugins');
  const selected = new Set(splitList(input.value));
  const ids = new Set();
  for (const runtime of state.runtimes) {
    if (!state.selected.has(runtime.runtime_id)) continue;
    for (const plugin of runtime.plugins || []) ids.add(plugin.id);
  }
  if (!ids.size) {
    container.append(el('p', { class: 'hint', text: 'No plugins on the selected targets.' }));
    return;
  }
  for (const id of [...ids].sort()) {
    container.append(
      el('button', {
        class: 'chip',
        type: 'button',
        dataset: { selected: String(selected.has(id)) },
        text: id,
        onclick: (event) => {
          const current = new Set(splitList(input.value));
          if (current.has(id)) current.delete(id);
          else current.add(id);
          input.value = [...current].join(' ');
          event.currentTarget.dataset.selected = String(current.has(id));
        },
      }),
    );
  }
}

function renderFreezeOptions() {
  const select = $('#freeze-runtime');
  const previous = select.value;
  clear(select);
  for (const runtime of state.runtimes) {
    select.append(
      el('option', {
        value: runtime.runtime_id,
        text: `${runtime.runtime_id}${runtime.error ? ' (unreachable)' : ''}`,
      }),
    );
  }
  if (previous && state.runtimes.some((runtime) => runtime.runtime_id === previous)) {
    select.value = previous;
  }
}

/* ── jobs ─────────────────────────────────────────────────────────────── */

async function refreshJobs() {
  try {
    const data = await request('/api/jobs');
    state.jobs = data.jobs || [];
    renderJobs();
  } catch (error) {
    toast(error.message, 'bad');
  }
}

function renderJobs() {
  const list = $('#jobs-list');
  clear(list);
  if (!state.jobs.length) {
    list.append(el('p', { class: 'empty', text: 'No runs yet.' }));
    return;
  }
  for (const job of state.jobs) {
    const active = job.job_id === state.jobId;
    list.append(
      el(
        'button',
        {
          class: 'job-item',
          type: 'button',
          dataset: { active: String(active) },
          onclick: () => {
            state.jobId = job.job_id;
            renderJobs();
            loadJob(job.job_id);
          },
        },
        [
          el('span', {
            class: 'lamp',
            dataset: {
              state:
                job.status === 'running'
                  ? 'busy'
                  : job.status === 'failed'
                    ? 'bad'
                    : 'live',
            },
          }),
          el('span', {}, [
            el('span', { class: 'job-item__op', text: job.operation }),
            el('span', {
              class: 'job-item__meta',
              text: `${job.job_id} · ${job.created_at} · ${job.actions} planned`,
            }),
          ]),
          el('span', { class: 'job-item__state', text: job.status }),
        ],
      ),
    );
  }
}

async function loadJob(jobId) {
  try {
    const data = await request(`/api/jobs/${jobId}`);
    renderJob(data.job);
  } catch (error) {
    toast(error.message, 'bad');
  }
}

function renderJob(job) {
  const panel = $('#job-detail');
  clear(panel);
  const statusTone =
    job.status === 'failed' ? 'bad' : job.status === 'running' ? 'signal' : 'live';
  const head = el('div', { class: 'job-head' }, [
    el('div', {}, [
      el('p', { class: 'eyebrow', text: 'Run' }),
      el('h3', { text: `${job.operation} · ${job.job_id}` }),
      el('div', { class: 'job-meta' }, [
        el('span', {
          class: 'pill',
          dataset: { tone: statusTone },
          text: job.status,
        }),
        el('span', { text: `phase ${job.phase}` }),
        el('span', { text: `${job.runtimes.length} runtimes` }),
        el('span', { text: `${job.results.length} results` }),
        el('span', { text: job.started_at || job.created_at }),
      ]),
    ]),
  ]);
  panel.append(head);

  if (job.error) {
    panel.append(el('p', { class: 'error-box', text: job.error }));
  }

  if (job.results.length) {
    const counts = job.summary || {};
    panel.append(
      el('p', {
        class: 'hint',
        text: `${counts.ok || 0} ok · ${counts.skipped || 0} skipped · ${counts.failed || 0} failed · ${counts.rolled_back || 0} rolled back`,
      }),
    );
    const table = el('div', {});
    table.append(
      el('div', { class: 'result-row' }, [
        el('span', { class: 'faint', text: 'state' }),
        el('span', { class: 'faint', text: 'runtime' }),
        el('span', { class: 'faint', text: 'plugin' }),
        el('span', { class: 'faint', text: 'change' }),
        el('span', { class: 'faint', text: 'detail' }),
      ]),
    );
    const results = job.status === 'running'
      ? job.results
      : job.results.slice().sort(
          (a, b) =>
            a.runtime_id.localeCompare(b.runtime_id) ||
            a.plugin.localeCompare(b.plugin) ||
            a.action.localeCompare(b.action),
        );
    for (const result of results) {
      table.append(
        el('div', { class: 'result-row' }, [
          el('span', {}, [
            el('span', {
              class: 'action-chip',
              dataset: { kind: result.state },
              text: result.state,
            }),
          ]),
          el('span', { class: 'mono', text: result.runtime_id }),
          el('span', { class: 'mono', text: result.plugin }),
          el('span', { class: 'mono dim', text: resultChange(result) }),
          el('span', { class: 'dim', text: result.message || '' }),
        ]),
      );
    }
    panel.append(table);
  } else if (job.status === 'running') {
    panel.append(
      el('p', { class: 'empty', text: 'Planning against the live fleet…' }),
    );
  } else if (!job.error) {
    panel.append(el('p', { class: 'empty', text: 'No actions were planned.' }));
  }
}

function resultChange(result) {
  if (['install', 'upgrade', 'downgrade', 'reinstall'].includes(result.action)) {
    return `${result.current_version || '—'} → ${result.target_version || '—'}`;
  }
  if (['uninstall', 'unload'].includes(result.action)) {
    return `${result.current_version || '—'} → removed`;
  }
  return '—';
}

function startPolling() {
  stopPolling();
  if (!state.jobId) return;
  const tick = async () => {
    try {
      const data = await request(`/api/jobs/${state.jobId}`);
      const job = data.job;
      state.activeJob = job.status === 'running' ? job : null;
      renderJob(job);
      syncJobBrief(job);
      renderJobs();
      renderJobBadge();
      if (job.status === 'running') {
        setConn('busy', `${job.phase}…`);
        state.pollTimer = window.setTimeout(tick, POLL_MS);
      } else {
        setConn('live', 'connected');
        toast(
          job.status === 'success'
            ? `Run ${job.job_id} finished.`
            : `Run ${job.job_id} failed.`,
          job.status === 'success' ? 'good' : 'bad',
        );
        await refreshJobs().catch(() => {});
        await loadFleet().catch(() => {});
        invalidatePlans('');
      }
    } catch (error) {
      toast(error.message, 'bad');
      stopPolling();
    }
  };
  tick();
}

function stopPolling() {
  if (state.pollTimer) window.clearTimeout(state.pollTimer);
  state.pollTimer = 0;
}

function syncJobBrief(job) {
  const brief = {
    job_id: job.job_id,
    operation: job.operation,
    status: job.status,
    phase: job.phase,
    created_at: job.created_at,
    finished_at: job.finished_at,
    runtimes: job.runtimes.length,
    actions: job.plan.length,
    results: job.results.length,
    error: job.error,
  };
  const index = state.jobs.findIndex((item) => item.job_id === job.job_id);
  if (index >= 0) state.jobs[index] = brief;
  else state.jobs.unshift(brief);
}

function renderJobBadge() {
  const badge = $('#job-badge');
  const job = state.activeJob;
  if (!job) {
    badge.hidden = true;
    return;
  }
  badge.textContent = String(job.results.length);
  badge.hidden = false;
}

async function resumeActiveJob() {
  try {
    const data = await request('/api/jobs/active');
    if (data.job) {
      state.jobId = data.job.job_id;
      state.activeJob = data.job;
      renderJob(data.job);
      switchView('jobs');
      startPolling();
    }
  } catch (error) {
    /* a 404 or transient error must not block first paint */
  }
}

/* ── dialog ───────────────────────────────────────────────────────────── */

function confirmDialog({ title, body, confirmLabel, danger }) {
  const dialog = $('#confirm-dialog');
  const ok = $('#confirm-ok');
  $('#confirm-title').textContent = title;
  const container = $('#confirm-body');
  clear(container);
  for (const node of [].concat(body)) {
    if (node) container.append(node);
  }
  ok.textContent = confirmLabel || 'Run changes';
  ok.className = danger ? 'btn btn--danger' : 'btn btn--primary';
  return new Promise((resolve) => {
    const onClose = () => {
      dialog.removeEventListener('close', onClose);
      resolve(dialog.returnValue === 'confirm');
    };
    dialog.addEventListener('close', onClose);
    dialog.returnValue = 'cancel';
    dialog.showModal();
    $('.dialog__actions .btn--ghost', dialog).focus();
  });
}

/* ── toasts ───────────────────────────────────────────────────────────── */

function toast(message, tone = 'info') {
  const container = $('#toasts');
  const node = el('div', { class: 'toast', dataset: { tone }, text: message });
  container.append(node);
  window.setTimeout(() => {
    node.remove();
  }, 5600);
}

/* ── view switching ───────────────────────────────────────────────────── */

function switchView(view) {
  state.view = view;
  for (const tab of $$('.tab')) {
    const active = tab.dataset.view === view;
    tab.setAttribute('aria-selected', String(active));
    tab.tabIndex = active ? 0 : -1;
  }
  for (const section of $$('.view')) {
    section.hidden = section.id !== `view-${view}`;
  }
  if (view === 'jobs') refreshJobs();
  if (view === 'uninstall') renderChips();
}

/* ── bindings ─────────────────────────────────────────────────────────── */

function bindControls() {
  $('#btn-refresh').addEventListener('click', refreshFleet);

  $('#include-stopped').addEventListener('change', () => {
    invalidatePlans();
    loadFleet().catch((error) => toast(error.message, 'bad'));
  });

  $('#target-filter').addEventListener('input', renderTargets);
  $('#plugin-filter').addEventListener('input', renderInventory);

  for (const button of $$('[data-target]')) {
    button.addEventListener('click', () => {
      const mode = button.dataset.target;
      state.selected = new Set(
        mode === 'all'
          ? state.runtimes.map((runtime) => runtime.runtime_id)
          : [],
      );
      renderTargets();
      invalidatePlans();
    });
  }

  for (const tab of $$('.tab')) {
    tab.addEventListener('click', () => switchView(tab.dataset.view));
  }

  $('#token-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const value = $('#token-input').value.trim();
    if (!value) return;
    state.token = value;
    sessionStorage.setItem(TOKEN_KEY, value);
    $('#token-input').value = '';
    connect();
  });

  // Any edit invalidates a preview that is already on screen.
  for (const slot of PLAN_SLOTS) {
    const form = document.getElementById(slot.formId);
    if (!form) continue;
    form.addEventListener('input', () => invalidatePlans());
    form.addEventListener('change', () => invalidatePlans());
  }

  // install
  $('#btn-add-source').addEventListener('click', () => {
    invalidatePlans();
    addSourceRow();
  });
  $('#install-preview').addEventListener('click', async () => {
    const plan = await preview(
      'install',
      installParams(),
      $('#install-plan'),
      $('#install-run'),
      $('#install-hint'),
    );
    if (plan) state.lastInstall = plan;
  });
  $('#install-run').addEventListener('click', async () => {
    if (!state.lastInstall) return;
    await execute(state.lastInstall.payload, state.lastInstall.plan);
  });

  const drop = $('#dropzone');
  $('#btn-pick-files').addEventListener('click', () => $('#file-input').click());
  $('#file-input').addEventListener('change', (event) => {
    invalidatePlans();
    uploadFiles(event.target.files);
    event.target.value = '';
  });
  drop.addEventListener('dragover', (event) => {
    event.preventDefault();
    drop.dataset.over = 'true';
  });
  drop.addEventListener('dragleave', () => {
    drop.dataset.over = 'false';
  });
  drop.addEventListener('drop', (event) => {
    event.preventDefault();
    drop.dataset.over = 'false';
    invalidatePlans();
    uploadFiles(event.dataTransfer.files);
  });

  // uninstall
  $('#uninstall-preview').addEventListener('click', async () => {
    const plan = await preview(
      'uninstall',
      uninstallParams(),
      $('#uninstall-plan'),
      $('#uninstall-run'),
      null,
    );
    if (plan) state.lastUninstall = plan;
  });
  $('#uninstall-run').addEventListener('click', async () => {
    if (!state.lastUninstall) return;
    await execute(state.lastUninstall.payload, state.lastUninstall.plan);
  });
  $('#uninstall-plugins').addEventListener('input', renderChips);
  $('#installed-chips').addEventListener('click', () => invalidatePlans());

  // apply
  $('#apply-preview').addEventListener('click', async () => {
    let params;
    try {
      params = applyParams();
    } catch (error) {
      toast(error.message, 'bad');
      return;
    }
    const plan = await preview(
      'apply',
      params,
      $('#apply-plan'),
      $('#apply-run'),
      $('#apply-status'),
    );
    if (plan) state.lastApply = plan;
  });
  $('#apply-run').addEventListener('click', async () => {
    if (!state.lastApply) return;
    await execute(state.lastApply.payload, state.lastApply.plan);
  });
  $('#btn-freeze').addEventListener('click', async () => {
    const runtime = $('#freeze-runtime').value;
    if (!runtime) {
      toast('No runtime selected to freeze.', 'bad');
      return;
    }
    try {
      const text = await freezeText(runtime);
      invalidatePlans('');
      $('#state-text').value = text;
      $('#apply-status').textContent = `Loaded state from ${runtime}.`;
    } catch (error) {
      toast(error.message, 'bad');
    }
  });
  $('#btn-download-state').addEventListener('click', async () => {
    const runtime = $('#freeze-runtime').value;
    if (!runtime) return;
    try {
      const text = await freezeText(runtime);
      downloadText(`${runtime}-plugins.lock.json`, text);
    } catch (error) {
      toast(error.message, 'bad');
    }
  });
  $('#btn-upload-state').addEventListener('click', () => $('#state-file').click());
  $('#state-file').addEventListener('change', async (event) => {
    const file = event.target.files[0];
    event.target.value = '';
    if (!file) return;
    const text = await file.text();
    invalidatePlans('');
    $('#state-text').value = text;
    $('#apply-status').textContent = `Loaded ${file.name}.`;
  });
}

async function uploadFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;
  for (const file of files) {
    if (!file.name.toLowerCase().endsWith('.zip')) {
      toast(`${file.name} is not a .zip bundle.`, 'bad');
      continue;
    }
    try {
      const form = new FormData();
      form.append('file', file);
      const result = await request('/api/upload', { method: 'POST', body: form });
      addSourceRow(`@${result.upload_id}`, `uploaded ${result.name}`);
      toast(`Uploaded ${result.name} (${formatSize(result.size)}).`, 'good');
    } catch (error) {
      toast(error.message, 'bad');
    }
  }
}

async function freezeText(runtime) {
  const response = await fetch(`/api/freeze?runtime=${encodeURIComponent(runtime)}`, {
    headers: state.token ? { 'X-QPM-Token': state.token } : {},
    credentials: 'same-origin',
    cache: 'no-store',
  });
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try {
      const data = await response.json();
      if (data.error) message = data.error;
    } catch (error) {
      /* keep the status text */
    }
    throw new ApiError(message, response.status);
  }
  return response.text();
}

function downloadText(filename, text) {
  const blob = new Blob([text], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const anchor = el('a', { href: url, download: filename });
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}
