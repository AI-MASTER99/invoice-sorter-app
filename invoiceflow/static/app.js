/* ============================================================
   Invoice Sorter — frontend app
   ============================================================ */

const API = '';
let currentPage = 'dashboard';
let currentTab  = 'verified';
let currentUser = null;
let currentRole = null;

/* ── Utility ─────────────────────────────────────────────── */
async function api(method, path, body) {
  const opts = { method, credentials: 'include' };
  if (body instanceof FormData) {
    opts.body = body;
  } else if (body) {
    opts.headers = { 'Content-Type': 'application/json' };
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(API + path, opts);
  if (r.status === 401) {
    window.location.href = '/login';
    throw new Error('Not authenticated');
  }
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.detail || `${r.status} ${r.statusText}`);
  }
  return r.json();
}

function fmtDate(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleDateString('en-GB', { day:'2-digit', month:'short', year:'numeric' });
}

function escHtml(str) {
  return String(str ?? '')
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

/* ── Auth ─────────────────────────────────────────────────── */
async function checkAuth() {
  try {
    const me = await fetch('/api/me', { credentials: 'include' });
    if (!me.ok) { window.location.href = '/login'; return; }
    const data = await me.json();
    currentUser = data.user;
    currentRole = data.role;
    const label = data.company ? `${currentUser} · ${data.company}` : currentUser;
    document.getElementById('sidebar-username').textContent = label;
    // Show admin panel nav item only for super_admin
    const navAdmin = document.getElementById('nav-admin');
    if (navAdmin) {
      navAdmin.classList.toggle('hidden', currentRole !== 'super_admin');
    }
  } catch {
    window.location.href = '/login';
  }
}

document.getElementById('btn-logout').addEventListener('click', async () => {
  await fetch('/api/logout', { method: 'POST', credentials: 'include' });
  window.location.href = '/login';
});

/* ── Navigation ──────────────────────────────────────────── */
function navigateTo(page) {
  currentPage = page;

  document.querySelectorAll('.nav-item').forEach(el => {
    el.classList.toggle('active', el.dataset.page === page);
  });

  document.querySelectorAll('.page-content').forEach(el => {
    el.classList.toggle('hidden', el.id !== `page-${page}`);
  });

  const titles = {
    dashboard: 'Dashboard',
    invoices:  'Invoices',
    memory:    'Product Memory',
    tariff:    'UK Tariff Lookup',
    settings:  'Settings',
    admin:     'Admin panel',
  };
  document.getElementById('topbar-title').textContent = titles[page] || page;

  if (page === 'invoices') refreshInvoicesPage();
  if (page === 'memory')   refreshMemoryPage();
  if (page === 'settings') refreshSettingsPage();
  if (page === 'admin')    refreshAdminPage();
}

document.querySelectorAll('.nav-item[data-page]').forEach(el => {
  el.addEventListener('click', () => navigateTo(el.dataset.page));
});

/* ── Tabs (Invoices page) ─────────────────────────────────── */
function switchTab(tab) {
  currentTab = tab;
  document.querySelectorAll('.tab-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === tab);
  });
  document.querySelectorAll('.tab-pane').forEach(p => {
    p.classList.toggle('hidden', p.id !== `pane-${tab}`);
  });
}

/* ── Toast ────────────────────────────────────────────────── */
function toast(msg, type = 'info') {
  const icons = { success: '✅', error: '❌', info: 'ℹ️' };
  const container = document.getElementById('toast-container');
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.innerHTML = `<span>${icons[type]}</span><span>${escHtml(msg)}</span>`;
  container.appendChild(el);
  setTimeout(() => {
    el.classList.add('removing');
    setTimeout(() => el.remove(), 350);
  }, 3800);
}

/* ── Stats ────────────────────────────────────────────────── */
async function refreshStats() {
  try {
    const s = await api('GET', '/stats');
    document.getElementById('stat-processed').textContent = s.processed_today;
    document.getElementById('stat-rate').textContent = s.verification_rate + '%';
    document.getElementById('stat-rate-bar').style.width = s.verification_rate + '%';
    document.getElementById('stat-memory').textContent = s.memory_count;
    const pendingEl = document.getElementById('stat-pending');
    const memBadge  = document.getElementById('nav-badge-memory');
    if (s.memory_pending > 0) {
      pendingEl.textContent = s.memory_pending + ' pending';
      pendingEl.style.display = 'inline-block';
      memBadge.textContent = s.memory_pending;
      memBadge.style.display = 'inline';
    } else {
      pendingEl.style.display = 'none';
      memBadge.style.display  = 'none';
    }
  } catch (e) { /* silent */ }
}

/* ── Jobs ─────────────────────────────────────────────────── */
const knownJobs = {};

async function refreshJobs() {
  try {
    const jobs = await api('GET', '/jobs');
    // Show running, queued AND failed jobs (so failed ones can be retried)
    const visible = jobs.filter(j => ['running', 'queued', 'failed'].includes(j.status));

    jobs.forEach(j => {
      const prev = knownJobs[j.id];
      if (prev && (prev.status === 'running' || prev.status === 'queued')) {
        if (j.status === 'done') {
          toast('Invoice processed successfully!', 'success');
          refreshInvoices();
          refreshInvoicesPage();
          refreshStats();
        } else if (j.status === 'failed') {
          toast('Processing failed: ' + (j.step || 'Unknown error'), 'error');
        }
      }
      knownJobs[j.id] = { ...j };
    });

    const section   = document.getElementById('jobs-section');
    const container = document.getElementById('jobs-container');

    if (visible.length === 0) { section.classList.remove('visible'); return; }
    section.classList.add('visible');

    // Sort: running → queued → failed
    const rank = { running: 0, queued: 1, failed: 2 };
    const sorted = [...visible].sort((a, b) => {
      const d = (rank[a.status] ?? 9) - (rank[b.status] ?? 9);
      if (d !== 0) return d;
      return (a.queue_position || 0) - (b.queue_position || 0);
    });

    container.innerHTML = '';
    sorted.forEach(job => {
      const isQueued = job.status === 'queued';
      const isFailed = job.status === 'failed';
      const card = document.createElement('div');
      let cls = 'job-card';
      if (isQueued) cls += ' job-queued';
      if (isFailed) cls += ' job-failed';
      card.className = cls;
      const iconHtml = isFailed
        ? '<div class="fail-icon">❌</div>'
        : isQueued
          ? '<div class="queue-icon">⏳</div>'
          : '<div class="spinner"></div>';
      const stepText = isFailed
        ? (job.step || job.error || 'Processing failed')
        : (job.step || 'Processing…');
      const actionBtn = isFailed
        ? `<button class="btn-retry-job" onclick="retryFailedJob('${job.id}')">↻ Retry</button>
           <button class="btn-cancel" onclick="dismissFailedJob('${job.id}')">Dismiss</button>`
        : `<button class="btn-cancel" onclick="cancelJob('${job.id}')">Cancel</button>`;
      card.innerHTML = `
        ${iconHtml}
        <div class="job-info">
          <div class="job-filename">${escHtml(job.filename)}</div>
          <div class="job-step">${escHtml(stepText)}</div>
          <div class="job-progress-track">
            <div class="job-progress-fill${isQueued ? ' queued' : ''}${isFailed ? ' failed' : ''}" style="width:${isFailed ? 100 : (isQueued ? 0 : job.progress)}%"></div>
          </div>
        </div>
        <div class="job-actions">${actionBtn}</div>`;
      container.appendChild(card);
    });
  } catch (e) { /* silent */ }
}

async function retryFailedJob(jobId) {
  try {
    const r = await api('POST', `/jobs/${jobId}/retry`);
    toast('Retrying…', 'info');
    delete knownJobs[jobId];
    knownJobs[r.job_id] = { id: r.job_id, status: 'queued' };
    refreshJobs(); refreshStats();
  } catch (e) {
    toast('Retry failed: ' + e.message, 'error');
  }
}

async function dismissFailedJob(id) {
  try {
    await api('DELETE', `/jobs/${id}`);
    delete knownJobs[id];
    toast('Dismissed', 'info');
    refreshJobs();
  } catch (e) {
    toast('Failed to dismiss: ' + e.message, 'error');
  }
}

async function deleteInvoice(invoiceId, supplier) {
  const label = supplier ? `"${supplier}"` : 'this invoice';
  if (!confirm(`Permanently delete ${label}?\n\nThis removes the invoice, its Excel exports and the original PDF. This cannot be undone.`)) return;
  try {
    await api('DELETE', `/invoices/${invoiceId}`);
    toast('Invoice deleted', 'success');
    refreshInvoices();
    refreshInvoicesPage();
    refreshStats();
  } catch (e) {
    toast('Delete failed: ' + e.message, 'error');
  }
}

function cancelJob(id) {
  delete knownJobs[id];
  refreshJobs();
  toast('Job removed from view.', 'info');
}

/* ── Dashboard invoice preview ────────────────────────────── */
async function refreshInvoices() {
  try {
    const list = await api('GET', '/invoices');
    const tbody = document.getElementById('invoices-tbody');

    const subcode = list.filter(i => i.status === 'subcode_needed').length;
    const badge = document.getElementById('nav-badge-invoices');
    if (subcode > 0) {
      badge.textContent = subcode;
      badge.style.display = 'inline';
      badge.className = 'nav-badge amber';
    } else {
      badge.style.display = 'none';
    }

    if (!list || list.length === 0) {
      tbody.innerHTML = `<tr><td colspan="5"><div class="empty-state"><div class="empty-icon">📄</div>No invoices yet. Upload one to get started.</div></td></tr>`;
      return;
    }
    tbody.innerHTML = list.slice(0, 8).map(inv => `
      <tr>
        <td><div class="supplier-cell">${escHtml(inv.supplier)}</div><div class="filename-sub">${escHtml(inv.filename)}</div></td>
        <td>${fmtDate(inv.date)}</td>
        <td><strong>${escHtml(inv.value)}</strong></td>
        <td>${badgeHtml(inv.status)}</td>
        <td class="actions-cell">${actionsHtml(inv)}</td>
      </tr>`).join('');
  } catch (e) { /* silent */ }
}

/* ── Full invoices page (3 tabs) ──────────────────────────── */
async function refreshInvoicesPage() {
  try {
    const list = await api('GET', '/invoices');
    const groups = { verified: [], subcode_needed: [], failed: [] };
    list.forEach(inv => { (groups[inv.status] || groups.failed).push(inv); });

    document.getElementById('tab-count-verified').textContent = groups.verified.length;
    document.getElementById('tab-count-subcode').textContent  = groups.subcode_needed.length;
    document.getElementById('tab-count-failed').textContent   = groups.failed.length;

    renderTabTable('tbody-verified',       groups.verified);
    renderTabTable('tbody-subcode_needed', groups.subcode_needed);
    renderTabTable('tbody-failed',         groups.failed);
  } catch (e) { /* silent */ }
}

function renderTabTable(tbodyId, list) {
  const tbody = document.getElementById(tbodyId);
  const status = tbodyId.replace('tbody-', '');
  const emptyMsgs = {
    verified:       '✅  No verified invoices yet.',
    subcode_needed: '⚠️  No invoices need review.',
    failed:         '❌  No failed invoices.',
  };
  if (!list || list.length === 0) {
    tbody.innerHTML = `<tr><td colspan="5"><div class="empty-state">${emptyMsgs[status]||'No invoices.'}</div></td></tr>`;
    return;
  }
  tbody.innerHTML = list.map(inv => `
    <tr>
      <td><div class="supplier-cell">${escHtml(inv.supplier)}</div><div class="filename-sub">${escHtml(inv.filename)}</div></td>
      <td>${fmtDate(inv.date)}</td>
      <td><strong>${escHtml(inv.value)}</strong></td>
      <td>${badgeHtml(inv.status)}</td>
      <td class="actions-cell">${actionsHtml(inv)}</td>
    </tr>`).join('');
}

/* ── Badge & actions helpers ──────────────────────────────── */
function badgeHtml(status) {
  const map = {
    verified:      ['badge-verified', '✓', 'Verified'],
    subcode_needed:['badge-subcode',  '⚠', 'Needs review'],
    failed:        ['badge-failed',   '✕', 'Failed'],
  };
  const [cls, icon, label] = map[status] || ['badge-processing', '…', 'Processing'];
  return `<span class="status-badge ${cls}">${icon} ${label}</span>`;
}

function actionsHtml(inv) {
  const sup = escHtml(inv.supplier || '');
  const del = `<button class="btn-export btn-delete" title="Delete permanently" onclick="deleteInvoice('${inv.id}', '${sup}')">🗑</button>`;
  if (inv.status === 'verified') {
    return `
      <button class="btn-export btn-full"   onclick="exportFull('${inv.id}')">Full Excel</button>
      <button class="btn-export btn-raw"    onclick="exportRaw('${inv.id}')">Raw only</button>
      ${del}`;
  }
  if (inv.status === 'subcode_needed') {
    return `
      <button class="btn-export btn-resolve" onclick="openResolve('${inv.id}', '${sup}')">Resolve</button>
      <button class="btn-export btn-retry"   onclick="retryInvoice('${inv.id}')">↻ Retry</button>
      <button class="btn-export btn-raw"     onclick="exportRaw('${inv.id}')">Raw only</button>
      ${del}`;
  }
  if (inv.status === 'failed') {
    return `
      <button class="btn-export btn-retry" onclick="retryInvoice('${inv.id}')">Retry</button>
      ${del}`;
  }
  return del;
}

function exportFull(id)  { window.open(`/invoices/${id}/export/full`, '_blank'); }
function exportRaw(id)   { window.open(`/invoices/${id}/export/raw`,  '_blank'); }

async function retryInvoice(id) {
  try {
    const r = await api('POST', `/invoices/${id}/retry`);
    toast('Retrying invoice…', 'info');
    knownJobs[r.job_id] = { id: r.job_id, status: 'running' };
    refreshJobs(); refreshInvoices(); refreshInvoicesPage();
  } catch (e) { toast('Retry failed: ' + e.message, 'error'); }
}

/* ── Resolve modal ────────────────────────────────────────── */
let resolveInvoiceId = null;

function openResolve(invoiceId, supplierName) {
  resolveInvoiceId = invoiceId;
  document.getElementById('modal-invoice-id').textContent = supplierName || invoiceId;
  document.getElementById('modal-subcode-input').value = '';
  document.getElementById('modal-overlay').classList.remove('hidden');
}

document.getElementById('modal-cancel').addEventListener('click', () => {
  document.getElementById('modal-overlay').classList.add('hidden');
  resolveInvoiceId = null;
});

document.getElementById('modal-confirm').addEventListener('click', async () => {
  if (!resolveInvoiceId) return;
  const subcode = document.getElementById('modal-subcode-input').value.trim();
  try {
    await api('POST', `/invoices/${resolveInvoiceId}/resolve`, { subcode });
    toast('Invoice moved to Verified! ✅', 'success');
  } catch (e) {
    toast('Subcode saved.', 'info');
  }
  document.getElementById('modal-overlay').classList.add('hidden');
  resolveInvoiceId = null;
  await refreshInvoices();
  await refreshInvoicesPage();
  refreshStats();
});

/* ── Memory page ──────────────────────────────────────────── */
let _memoryItems = [];

async function refreshMemoryPage() {
  try {
    const items = await api('GET', '/memory');
    _memoryItems = items || [];
    renderMemoryTable(_memoryItems);

    const count = _memoryItems.length;
    document.getElementById('memory-count').textContent = count > 0 ? `(${count})` : '';

    // Silent tariff refresh for entries with empty tariff
    const needsRefresh = _memoryItems.some(m => !m.tariff || Object.keys(m.tariff).length === 0);
    if (needsRefresh) {
      api('POST', '/memory/refresh-tariff').then(() => {
        setTimeout(() => api('GET', '/memory').then(updated => {
          _memoryItems = updated || [];
          renderMemoryTable(_memoryItems);
          document.getElementById('memory-count').textContent = `(${_memoryItems.length})`;
        }), 500);
      }).catch(() => {});
    }
  } catch (e) { /* silent */ }
}

function renderMemoryTable(items) {
  const container = document.getElementById('memory-list');
  const query = (document.getElementById('memory-search')?.value || '').toLowerCase();
  const filtered = query
    ? items.filter(m => m.description?.toLowerCase().includes(query) || m.code?.includes(query))
    : items;

  if (!filtered || filtered.length === 0) {
    container.innerHTML = `<div class="empty-state" style="padding:32px 0"><div class="empty-icon">🧠</div>${query ? 'No products match your search.' : 'No products in memory yet.'}</div>`;
    return;
  }

  let rows = '';
  for (const m of filtered) {
    const subs = m.tariff?.subcodes || [];
    const matchedCode = m.matched_code || '';
    let subsHtml = '<span style="color:var(--muted)">—</span>';
    if (subs.length > 0) {
      subsHtml = subs.map(s => {
        const isMatch = matchedCode && s.code === matchedCode;
        return `<div class="subcode-row${isMatch ? ' subcode-matched' : ''}">`
          + `<span class="code-mono">${escHtml(s.code)}</span> `
          + `<span class="subcode-desc">${escHtml(s.description)}</span> `
          + `<span class="subcode-duty">${escHtml(s.duty || '')}</span>`
          + (isMatch ? ' <span class="match-badge">✓ match</span>' : '')
          + `</div>`;
      }).join('');
    }
    // Show matched code prominently if available
    const matchHtml = matchedCode
      ? `<span class="code-mono matched-code">${escHtml(matchedCode)}</span>`
      : '<span style="color:var(--muted)">—</span>';
    const statusBadge = m.confirmed
      ? '<span class="status-badge badge-verified">✓ Confirmed</span>'
      : '<span class="status-badge badge-subcode">⚠ Pending</span>';
    rows += `
      <tr>
        <td><span class="code-mono">${escHtml(m.code)}</span></td>
        <td>${escHtml(m.description)}</td>
        <td class="tariff-cell">${matchHtml}</td>
        <td class="tariff-cell">${escHtml(m.tariff?.duty || '—')}</td>
        <td class="tariff-cell">${escHtml(m.tariff?.vat || '—')}</td>
        <td class="subcodes-cell">${subsHtml}</td>
        <td>${statusBadge}</td>
      </tr>`;
  }

  container.innerHTML = `
    <table class="memory-table">
      <thead><tr>
        <th>Commodity code</th>
        <th>Description</th>
        <th>Matched sub-code</th>
        <th>Duty</th>
        <th>VAT</th>
        <th>All possible sub-codes</th>
        <th>Status</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

document.getElementById('memory-search')?.addEventListener('input', () => {
  renderMemoryTable(_memoryItems);
});

document.getElementById('btn-refresh-tariff')?.addEventListener('click', async () => {
  const btn = document.getElementById('btn-refresh-tariff');
  btn.disabled = true;
  btn.textContent = '↻ Refreshing…';
  try {
    const r = await api('POST', '/memory/refresh-tariff');
    toast(`Updated ${r.updated} tariff entries`, 'success');
    await refreshMemoryPage();
  } catch (e) {
    toast('Tariff refresh failed', 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = '↻ Refresh tariff';
  }
});

/* ── Tariff lookup page ───────────────────────────────────── */
document.getElementById('btn-tariff-search')?.addEventListener('click', doTariffSearch);
document.getElementById('tariff-input')?.addEventListener('keydown', e => {
  if (e.key === 'Enter') doTariffSearch();
});

async function doTariffSearch() {
  const input = document.getElementById('tariff-input');
  const results = document.getElementById('tariff-results');
  const q = input.value.trim();
  if (!q) return;

  results.innerHTML = `<div style="padding:24px 0;text-align:center;color:var(--muted)"><div class="spinner" style="margin:0 auto 8px"></div>Searching…</div>`;
  try {
    const data = await api('GET', `/tariff/search?q=${encodeURIComponent(q)}`);
    if (!data || data.length === 0) {
      results.innerHTML = `<div class="empty-state" style="padding:32px 0"><div class="empty-icon">🔍</div>No results for "${escHtml(q)}".</div>`;
      return;
    }
    results.innerHTML = `
      <table class="memory-table" style="margin-top:16px">
        <thead><tr>
          <th>Code</th>
          <th>Description</th>
          <th>Duty</th>
          <th>VAT</th>
          <th></th>
        </tr></thead>
        <tbody>
          ${data.map(item => `
            <tr>
              <td><span class="code-mono">${escHtml(item.code)}</span></td>
              <td>${escHtml(item.description)}</td>
              <td class="tariff-cell">${escHtml(item.duty || '—')}</td>
              <td class="tariff-cell">${escHtml(item.vat || '—')}</td>
              <td><button class="btn-copy-code" onclick="copyCode('${escHtml(item.code)}')" title="Copy code">📋</button></td>
            </tr>`).join('')}
        </tbody>
      </table>`;
  } catch (e) {
    results.innerHTML = `<div class="empty-state" style="padding:32px 0;color:var(--red)">Search failed: ${escHtml(e.message)}</div>`;
  }
}

function copyCode(code) {
  navigator.clipboard.writeText(code).then(() => toast(`Copied ${code}`, 'success'));
}

/* ── Settings page ────────────────────────────────────────── */
async function refreshSettingsPage() {
  await loadUsersList();
}

async function loadUsersList() {
  try {
    const users = await api('GET', '/api/users');
    const container = document.getElementById('users-list');
    if (!users || users.length === 0) {
      container.innerHTML = `<div style="color:var(--muted);font-size:13px">No users found.</div>`;
      return;
    }
    container.innerHTML = `
      <table class="memory-table">
        <thead><tr><th>Username</th><th>Role</th><th></th></tr></thead>
        <tbody>
          ${users.map(u => `
            <tr>
              <td>${escHtml(u.username)}${u.username === currentUser ? ' <span style="color:var(--muted);font-size:11px">(you)</span>' : ''}</td>
              <td><span class="role-badge role-${escHtml(u.role)}">${escHtml(u.role)}</span></td>
              <td style="text-align:right">
                <div style="display:flex;gap:6px;justify-content:flex-end">
                  <button class="btn-export btn-raw" onclick="openChpwModal('${escHtml(u.username)}')">Password</button>
                  ${u.username !== currentUser
                    ? `<button class="btn-export btn-retry" onclick="deleteUser('${escHtml(u.username)}')">Delete</button>`
                    : ''}
                </div>
              </td>
            </tr>`).join('')}
        </tbody>
      </table>`;
  } catch (e) {
    if (e.message.includes('403') || e.message.includes('Admin')) {
      document.getElementById('users-list').innerHTML = `<div style="color:var(--muted);font-size:13px">Admin access required to manage users.</div>`;
      document.getElementById('btn-add-user').style.display = 'none';
    }
  }
}

document.getElementById('btn-add-user')?.addEventListener('click', () => {
  document.getElementById('add-user-form').classList.remove('hidden');
  document.getElementById('btn-add-user').style.display = 'none';
});

document.getElementById('btn-cancel-user')?.addEventListener('click', () => {
  document.getElementById('add-user-form').classList.add('hidden');
  document.getElementById('btn-add-user').style.display = '';
  document.getElementById('new-username').value = '';
  document.getElementById('new-password').value = '';
});

document.getElementById('btn-save-user')?.addEventListener('click', async () => {
  const username = document.getElementById('new-username').value.trim();
  const password = document.getElementById('new-password').value;
  const role = document.getElementById('new-role').value;
  if (!username || !password) { toast('Username and password required', 'error'); return; }
  try {
    await api('POST', '/api/users', { username, password, role });
    toast(`User "${username}" created`, 'success');
    document.getElementById('add-user-form').classList.add('hidden');
    document.getElementById('btn-add-user').style.display = '';
    document.getElementById('new-username').value = '';
    document.getElementById('new-password').value = '';
    await loadUsersList();
  } catch (e) { toast('Error: ' + e.message, 'error'); }
});

async function deleteUser(username) {
  if (!confirm(`Delete user "${username}"?`)) return;
  try {
    await api('DELETE', `/api/users/${encodeURIComponent(username)}`);
    toast(`User "${username}" deleted`, 'success');
    await loadUsersList();
  } catch (e) { toast('Error: ' + e.message, 'error'); }
}

document.getElementById('btn-chpw')?.addEventListener('click', async () => {
  const np = document.getElementById('chpw-new').value;
  const cp = document.getElementById('chpw-confirm').value;
  const msg = document.getElementById('chpw-msg');
  if (!np) { msg.textContent = 'Enter a new password.'; msg.className = 'settings-msg error show'; return; }
  if (np !== cp) { msg.textContent = 'Passwords do not match.'; msg.className = 'settings-msg error show'; return; }
  try {
    await api('PUT', `/api/users/${encodeURIComponent(currentUser)}/password`, { password: np });
    msg.textContent = 'Password updated successfully.';
    msg.className = 'settings-msg success show';
    document.getElementById('chpw-new').value = '';
    document.getElementById('chpw-confirm').value = '';
  } catch (e) {
    msg.textContent = 'Error: ' + e.message;
    msg.className = 'settings-msg error show';
  }
  setTimeout(() => msg.classList.remove('show'), 3000);
});

let chpwTargetUser = null;

function openChpwModal(username) {
  chpwTargetUser = username;
  document.getElementById('modal-chpw-user').textContent = username;
  document.getElementById('modal-chpw-input').value = '';
  document.getElementById('modal-chpw-overlay').classList.remove('hidden');
}

document.getElementById('modal-chpw-cancel')?.addEventListener('click', () => {
  document.getElementById('modal-chpw-overlay').classList.add('hidden');
  chpwTargetUser = null;
});

document.getElementById('modal-chpw-confirm')?.addEventListener('click', async () => {
  if (!chpwTargetUser) return;
  const pw = document.getElementById('modal-chpw-input').value;
  if (!pw) { toast('Enter a password', 'error'); return; }
  try {
    await api('PUT', `/api/users/${encodeURIComponent(chpwTargetUser)}/password`, { password: pw });
    toast(`Password updated for "${chpwTargetUser}"`, 'success');
  } catch (e) { toast('Error: ' + e.message, 'error'); }
  document.getElementById('modal-chpw-overlay').classList.add('hidden');
  chpwTargetUser = null;
});

/* ── Upload ───────────────────────────────────────────────── */
const dropZone = document.getElementById('drop-zone');
const fileInput = document.getElementById('file-input');

document.getElementById('btn-upload-topbar').addEventListener('click', () => fileInput.click());
document.getElementById('btn-choose').addEventListener('click', () => fileInput.click());

fileInput.addEventListener('change', e => {
  handleBulkUpload([...e.target.files]);
  fileInput.value = '';
});

dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('drag-over'); });
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
dropZone.addEventListener('drop', e => {
  e.preventDefault();
  dropZone.classList.remove('drag-over');
  handleBulkUpload([...e.dataTransfer.files]);
});

async function handleBulkUpload(files) {
  const allowed = ['.pdf','.jpg','.jpeg','.png','.docx'];
  const valid = files.filter(f => {
    const ext = f.name.substring(f.name.lastIndexOf('.')).toLowerCase();
    if (!allowed.includes(ext)) { toast(`Skipped ${f.name} — unsupported type`, 'error'); return false; }
    return true;
  });
  if (valid.length === 0) return;

  if (valid.length > 1) {
    toast(`Uploading ${valid.length} invoices — they'll be processed one at a time`, 'info');
  }

  let queued = 0;
  for (const file of valid) {
    try {
      const fd = new FormData();
      fd.append('file', file);
      const r = await api('POST', '/upload', fd);
      knownJobs[r.job_id] = { id: r.job_id, status: 'queued' };
      queued++;
    } catch (e) { toast(`Upload failed for ${file.name}: ${e.message}`, 'error'); }
  }

  if (queued === 1) {
    toast(`Processing ${valid[0].name}…`, 'info');
  } else if (queued > 1) {
    toast(`${queued} invoices queued for processing`, 'success');
  }
  refreshJobs();
}

/* ── Admin panel (super_admin only) ──────────────────────── */
async function refreshAdminPage() {
  if (currentRole !== 'super_admin') return;
  try {
    const companies = await api('GET', '/api/admin/companies');
    document.getElementById('admin-company-count').textContent =
      companies.length > 0 ? `(${companies.length})` : '';
    const container = document.getElementById('admin-companies-list');
    if (!companies || companies.length === 0) {
      container.innerHTML = `<div style="color:var(--muted);font-size:13px">No companies yet.</div>`;
      return;
    }
    let html = '<table class="memory-table"><thead><tr>'
      + '<th>Company</th><th>Users</th><th>Created</th><th></th></tr></thead><tbody>';
    for (const c of companies) {
      const usersList = c.users.map(u => `${escHtml(u.username)} (${escHtml(u.role)})`).join(', ');
      const created   = fmtDate(c.created_at);
      const isSelf    = c.users.some(u => u.role === 'super_admin' && u.username === currentUser);
      const delBtn    = isSelf
        ? '<span style="color:var(--muted);font-size:11px">(your company)</span>'
        : `<button class="btn-export btn-retry" onclick="deleteCompany('${c.id}', '${escHtml(c.name)}')">Delete</button>`;
      html += `<tr>
        <td><strong>${escHtml(c.name)}</strong></td>
        <td style="color:var(--muted);font-size:12px">${usersList || '—'}</td>
        <td>${created}</td>
        <td style="text-align:right">${delBtn}</td>
      </tr>`;
    }
    html += '</tbody></table>';
    container.innerHTML = html;
  } catch (e) {
    console.error(e);
  }
}

async function deleteCompany(companyId, companyName) {
  if (!confirm(`Permanently delete "${companyName}" and ALL their data (invoices, memory, users)?\n\nThis cannot be undone.`)) return;
  try {
    await api('DELETE', `/api/admin/companies/${companyId}`);
    toast(`Deleted ${companyName}`, 'success');
    refreshAdminPage();
  } catch (e) { toast('Delete failed: ' + e.message, 'error'); }
}

document.getElementById('btn-create-company')?.addEventListener('click', async () => {
  const company  = document.getElementById('admin-new-company').value.trim();
  const username = document.getElementById('admin-new-username').value.trim();
  const password = document.getElementById('admin-new-password').value;
  const msg      = document.getElementById('admin-msg');
  if (!company || !username || !password) {
    msg.textContent = 'All fields are required.';
    msg.className = 'settings-msg error show'; return;
  }
  if (password.length < 6) {
    msg.textContent = 'Password must be at least 6 characters.';
    msg.className = 'settings-msg error show'; return;
  }
  try {
    await api('POST', '/api/admin/companies', { company, username, password });
    msg.textContent = `✓ Company "${company}" created. Share credentials with the customer.`;
    msg.className = 'settings-msg success show';
    document.getElementById('admin-new-company').value = '';
    document.getElementById('admin-new-username').value = '';
    document.getElementById('admin-new-password').value = '';
    refreshAdminPage();
  } catch (e) {
    msg.textContent = 'Error: ' + e.message;
    msg.className = 'settings-msg error show';
  }
  setTimeout(() => msg.classList.remove('show'), 5000);
});

/* ── Polling ─────────────────────────────────────────────── */
function startPolling() {
  refreshJobs(); refreshStats(); refreshInvoices();
  setInterval(refreshJobs,      2000);
  setInterval(refreshStats,    10000);
  setInterval(refreshInvoices,  5000);
}

/* ── Init ─────────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', async () => {
  await checkAuth();
  const now = new Date();
  document.getElementById('topbar-date').textContent =
    now.toLocaleDateString('en-GB', { weekday:'long', day:'numeric', month:'long', year:'numeric' });
  startPolling();
});
