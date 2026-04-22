/* ── Utils ── */
const $   = id => document.getElementById(id);
const esc = s  => { const d = document.createElement('div'); d.appendChild(document.createTextNode(s || '')); return d.innerHTML; };

function fmtDate(s) {
  if (!s) return '—';
  // SQLite stores without timezone; treat as local
  const d = new Date(s.includes('T') ? s : s.replace(' ', 'T'));
  return isNaN(d) ? s : d.toLocaleString();
}

function toast(msg, ms = 3200) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(t._tid);
  t._tid = setTimeout(() => t.classList.remove('show'), ms);
}

/* ── Tabs ── */
document.querySelectorAll('.nav-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab').forEach(t => t.classList.add('hidden'));
    btn.classList.add('active');
    $(`tab-${btn.dataset.tab}`).classList.remove('hidden');
    if (btn.dataset.tab === 'history') loadHistory();
  });
});

/* ── Check App ── */
$('btn-check-app').addEventListener('click', () => {
  const url = $('app-url').value.trim();
  if (!url) { toast('Please enter an app URL'); return; }
  startJob('/api/check-app', { url });
});
$('app-url').addEventListener('keydown', e => { if (e.key === 'Enter') $('btn-check-app').click(); });

/* ── Check Developer ── */
$('btn-check-dev').addEventListener('click', () => {
  const url = $('dev-url').value.trim();
  if (!url) { toast('Please enter a developer URL'); return; }
  startJob('/api/check-developer', { url });
});
$('dev-url').addEventListener('keydown', e => { if (e.key === 'Enter') $('btn-check-dev').click(); });

/* ── Back ── */
$('btn-new-check').addEventListener('click', () => {
  $('results-section').classList.add('hidden');
});

/* ── Job state ── */
let activeJobId = null;
let pollTimer   = null;

// 76 countries, 47 languages
const N_COUNTRIES = 76;
const N_LANGUAGES = 47;
const N_TOTAL     = N_COUNTRIES + N_LANGUAGES;

async function startJob(endpoint, body) {
  setInputsDisabled(true);
  $('results-section').classList.add('hidden');
  $('progress-card').classList.remove('hidden');
  resetBars();
  updatePhaseDisplay('Starting…', '', 0, 0);

  try {
    const res = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    const { job_id } = await res.json();
    activeJobId = job_id;
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(() => pollJob(job_id), 2000);
  } catch (e) {
    toast(`Error: ${e.message}`, 6000);
    $('progress-card').classList.add('hidden');
    setInputsDisabled(false);
  }
}

async function pollJob(jobId) {
  try {
    const job = await fetch(`/api/jobs/${jobId}`).then(r => r.json());

    // progress is total across all apps; normalise per-app for bars
    const prog  = job.progress || 0;
    const total = job.total    || N_TOTAL;
    // per-app position inside a single app's 123-step cycle
    const posInApp = prog % N_TOTAL;

    updatePhaseDisplay(
      job.phase   || 'Checking…',
      job.message || '',
      prog, total,
    );
    updateBars(posInApp);

    if (job.status === 'completed') {
      clearInterval(pollTimer);
      setInputsDisabled(false);
      $('progress-card').classList.add('hidden');
      updateBars(N_TOTAL); // fill both bars
      const ids = JSON.parse(job.result_app_ids || '[]');
      await renderResults(ids);
    } else if (job.status === 'failed') {
      clearInterval(pollTimer);
      setInputsDisabled(false);
      $('progress-card').classList.add('hidden');
      toast(`Job failed: ${job.error || 'Unknown error'}`, 7000);
    } else if (job.status === 'cancelled') {
      clearInterval(pollTimer);
      setInputsDisabled(false);
      $('progress-card').classList.add('hidden');
      toast('Job cancelled.');
    }
  } catch (e) {
    console.error('poll:', e);
  }
}

/* ── Cancel ── */
$('btn-cancel').addEventListener('click', async () => {
  if (!activeJobId) return;
  await fetch(`/api/jobs/${activeJobId}/cancel`, { method: 'POST' });
  toast('Cancelling…');
});

/* ── Progress UI ── */
function resetBars() {
  $('bar-country').style.width = '0%';
  $('bar-lang').style.width    = '0%';
}

function updateBars(posInApp) {
  const cPct = Math.min(100, Math.round((Math.min(posInApp, N_COUNTRIES) / N_COUNTRIES) * 100));
  const lPos = Math.max(0, posInApp - N_COUNTRIES);
  const lPct = Math.min(100, Math.round((lPos / N_LANGUAGES) * 100));
  $('bar-country').style.width = `${cPct}%`;
  $('bar-lang').style.width    = `${lPct}%`;
}

function updatePhaseDisplay(phase, msg, done, total) {
  $('progress-phase').textContent   = phase;
  $('progress-msg').textContent     = msg;
  $('progress-counter').textContent = total ? `${done} / ${total}` : '';
}

function setInputsDisabled(dis) {
  ['btn-check-app', 'btn-check-dev'].forEach(id => $(id).disabled = dis);
}

/* ── Render results ── */
async function renderResults(appIds) {
  const list = $('results-list');
  list.innerHTML = '';

  for (const id of appIds) {
    const app = await fetch(`/api/apps/${id}`).then(r => r.json());
    const countries = app.designs.filter(d => d.scan_type === 'country');
    const languages = app.designs.filter(d => d.scan_type === 'language');

    const block = document.createElement('div');
    block.className = 'app-block';
    block.innerHTML = `
      <div class="app-block-pkg">
        <span>${esc(app.package_name)}</span>
        <span class="h-pill pill-country">${countries.length} country design${countries.length !== 1 ? 's' : ''}</span>
        <span class="h-pill pill-lang">${languages.length} language design${languages.length !== 1 ? 's' : ''}</span>
      </div>
      ${phaseSection('country', 'Countries', countries)}
      ${phaseSection('language', 'Languages', languages)}
    `;
    list.appendChild(block);
  }

  $('results-section').classList.remove('hidden');
  $('results-title').textContent = appIds.length > 1
    ? `Results (${appIds.length} apps)`
    : 'Results';
}

function phaseSection(type, label, designs) {
  if (!designs.length) return '';
  const pillClass = type === 'country' ? 'pill-country' : 'pill-lang';
  const cards = designs.map(d => designCard(d, type)).join('');
  return `
    <div class="phase-section">
      <div class="phase-section-title">
        <span class="phase-pill ${pillClass}">${label}</span>
        <span class="phase-count">${designs.length} unique name${designs.length !== 1 ? 's' : ''}</span>
      </div>
      <div class="designs-row">${cards}</div>
    </div>`;
}

function designCard(d, type) {
  const tagClass = type === 'country' ? 'country-tag' : 'lang-tag';
  const tags = d.entries
    .map(e => `<span class="entry-tag ${tagClass}" title="${esc(e.name)}">${esc(e.code)}</span>`)
    .join('');
  const shotSrc = d.screenshot_path
    ? `/screenshots/${encodeURIComponent(d.screenshot_path)}`
    : null;
  return `
    <div class="design-card">
      <div class="design-shot"${shotSrc ? ` onclick="openLightbox('${shotSrc}')"` : ''}>
        ${shotSrc
          ? `<img src="${shotSrc}" alt="${esc(d.app_name)}" loading="lazy"/><span class="zoom-hint">🔍 zoom</span>`
          : `<div class="no-shot">No screenshot</div>`}
      </div>
      <div class="design-info">
        <div class="design-name">${esc(d.app_name)}</div>
        <div class="design-entries">${tags}</div>
      </div>
    </div>`;
}

/* ── History ── */
async function loadHistory() {
  const list = $('history-list');
  list.innerHTML = '<div class="empty-state">Loading…</div>';
  try {
    const apps = await fetch('/api/apps').then(r => r.json());
    if (!apps.length) {
      list.innerHTML = '<div class="empty-state">No apps checked yet.</div>';
      return;
    }
    list.innerHTML = apps.map(a => `
      <div class="card history-item" id="hitem-${a.id}">
        <div class="history-row">
          <div>
            <div class="h-pkg">${esc(a.package_name)}</div>
            <div class="h-meta">Checked: ${fmtDate(a.checked_at)}</div>
            <div class="h-pills">
              <span class="h-pill pill-country">${a.country_designs} country design${a.country_designs !== 1 ? 's' : ''}</span>
              <span class="h-pill pill-lang">${a.language_designs} language design${a.language_designs !== 1 ? 's' : ''}</span>
            </div>
          </div>
          <div class="h-actions">
            <button class="btn-view"    onclick="openAppModal(${a.id})">View</button>
            <button class="btn-recheck" onclick="recheckApp('${esc(a.original_url)}')">Re-check</button>
            <button class="btn-delete"  onclick="deleteApp(${a.id})">Delete</button>
          </div>
        </div>
      </div>`).join('');
  } catch (e) {
    list.innerHTML = `<div class="empty-state">Failed to load: ${e.message}</div>`;
  }
}

$('btn-refresh').addEventListener('click', loadHistory);

async function openAppModal(appId) {
  const app = await fetch(`/api/apps/${appId}`).then(r => r.json());
  const countries = app.designs.filter(d => d.scan_type === 'country');
  const languages = app.designs.filter(d => d.scan_type === 'language');

  $('modal-body').innerHTML = `
    <div class="modal-pkg">${esc(app.package_name)}</div>
    <div class="modal-meta">Checked: ${fmtDate(app.checked_at)}</div>
    ${phaseSection('country', 'Countries', countries)}
    ${phaseSection('language', 'Languages', languages)}`;
  $('modal').classList.remove('hidden');
}

function recheckApp(originalUrl) {
  document.querySelector('[data-tab="check"]').click();
  $('app-url').value = originalUrl;
  startJob('/api/check-app', { url: originalUrl });
}

async function deleteApp(appId) {
  if (!confirm('Delete this app and all its screenshots?')) return;
  const res = await fetch(`/api/apps/${appId}`, { method: 'DELETE' });
  if (res.ok) {
    const el = $(`hitem-${appId}`);
    if (el) el.remove();
    toast('Deleted.');
  } else {
    toast('Delete failed.');
  }
}

/* ── Lightbox ── */
function openLightbox(src) {
  $('lightbox-img').src = src;
  $('lightbox').classList.remove('hidden');
}
function closeLightbox() { $('lightbox').classList.add('hidden'); }
function closeModal()    { $('modal').classList.add('hidden'); }

document.querySelector('.lightbox-backdrop').addEventListener('click', closeLightbox);
document.querySelector('.lightbox-close').addEventListener('click', closeLightbox);
document.querySelector('.modal-backdrop').addEventListener('click', closeModal);
document.querySelector('.modal-close').addEventListener('click', closeModal);
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { closeLightbox(); closeModal(); }
});
