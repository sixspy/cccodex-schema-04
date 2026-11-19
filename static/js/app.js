const msg = document.getElementById('msg');
const view = document.getElementById('view');
let contracts = [];
let card = null;
let currentCid = null;

async function api(path, opts={}) {
  const r = await fetch(path, {
    headers: {'Content-Type': 'application/json', ...(opts.headers||{})},
    ...opts
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || ('HTTP '+r.status));
  return j;
}

function setMsg(t, isErr=false) {
  msg.textContent = t || '';
  msg.className = isErr ? 'err' : 'muted';
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
  ));
}

function chip(state) {
  const cls = state === '缺边' ? 'chip bad'
    : state === '已作废' ? 'chip void'
    : state === '未冻结' ? 'chip idle'
    : 'chip';
  return `<span class="${cls}">${esc(state)}</span>`;
}

async function loadContracts() {
  const data = await api('/api/contracts');
  contracts = data.items || [];
}

function renderContracts() {
  view.innerHTML = `<div class="grid">${contracts.map(c => `
    <article class="card">
      <div class="row">
        <h3>${esc(c.table_name)} · v${c.version_no}</h3>
        <span class="chip">${esc(c.status)}</span>
        ${c.lineage_edge_count ? `<span class="chip idle">血缘边 ${c.lineage_edge_count}</span>` : ''}
      </div>
      <p class="muted">${esc(c.note || '')} · 字段 ${c.field_count}</p>
    </article>`).join('')}</div>`;
}

function draftOptions() {
  return contracts.filter(c => c.status === '草稿')
    .map(c => `<option value="${c.id}">${esc(c.table_name)} v${c.version_no}</option>`).join('');
}

function childDraftOptions() {
  return contracts.filter(c => c.status === '草稿' && c.lineage_edge_count > 0)
    .map(c => `<option value="${c.id}">${esc(c.table_name)} v${c.version_no}</option>`).join('');
}

function renderCheck() {
  view.innerHTML = `
    <div class="card grid">
      <label>草稿契约
        <select id="cid">${draftOptions()}</select>
      </label>
      <button class="primary" id="btn-check" type="button">运行破坏性检查</button>
      <pre id="out" class="muted"></pre>
    </div>`;
  document.getElementById('btn-check').onclick = async () => {
    try {
      const cid = document.getElementById('cid').value;
      const j = await api(`/api/contracts/${cid}/breaking-check`, {method:'POST', body:'{}'});
      document.getElementById('out').textContent = JSON.stringify(j.check, null, 2);
      setMsg(`检查结论：${j.check.verdict}`);
    } catch (e) { setMsg(e.message, true); }
  };
}

/* ---------- 依赖目录：每条边只亮快照芯片 ---------- */
async function renderDirectory() {
  const items = (await api('/api/lineage/edges')).items || [];
  const groups = {};
  for (const e of items) {
    (groups[e.child_table] = groups[e.child_table] || []).push(e);
  }
  view.innerHTML = `
    <div class="card">
      <div class="row">
        <strong>依赖目录</strong>
        <span class="chip">芯片：快照裁决</span>
        <span class="muted small">齐套门槛已定：打开卡片即冻结，改边只作废快照，不回头重扫父表</span>
      </div>
      <p class="muted small">芯片口径：${esc(items[0] ? items[0].chip : '快照')}；状态取自各子表最近一张快照，与升版卡片、水线、对账回看同源。</p>
    </div>
    <div class="card grid" style="margin-top:0.8rem">
      <strong>改边（改完需到升版卡片重新冻结）</strong>
      <div class="row">
        <label class="grow">子表<input id="e-child" placeholder="dw_payments_d" /></label>
        <label class="grow">子表列<input id="e-cfield" placeholder="pay_channel" /></label>
        <label class="grow">父表<input id="e-parent" placeholder="dim_customer_d" /></label>
        <label class="grow">父表列<input id="e-pfield" placeholder="channel_code" /></label>
      </div>
      <div class="row">
        <button class="primary" id="btn-add-edge" type="button">接上这条边</button>
        <button id="btn-del-edge" type="button">撤掉这条边</button>
      </div>
      <p class="muted small">改边只作废冻结快照、记一条水线，不会当场重扫父表。</p>
    </div>
    <div class="grid" style="margin-top:0.8rem">
      ${Object.keys(groups).sort().map(child => `
        <article class="card">
          <h3>${esc(child)}</h3>
          <table>
            <thead><tr><th>子表列</th><th>父表</th><th>父表列</th><th>父表现行</th><th>快照芯片</th><th>凭证号</th></tr></thead>
            <tbody>
              ${groups[child].map(e => `
                <tr>
                  <td>${esc(e.child_field)}</td>
                  <td>${esc(e.parent_table)}</td>
                  <td>${esc(e.parent_field)}</td>
                  <td>v${esc(e.parent_version == null ? '—' : e.parent_version)}</td>
                  <td>${chip(e.snapshot_state)}</td>
                  <td class="mono">${esc(e.snapshot_voucher || '—')}</td>
                </tr>`).join('')}
            </tbody>
          </table>
          <p class="muted small">${esc(groups[child][0].snapshot_status ? '快照状态：' + groups[child][0].snapshot_status : '')}</p>
        </article>`).join('')}
    </div>`;

  const edgeBody = () => JSON.stringify({
    child_table: document.getElementById('e-child').value.trim(),
    child_field: document.getElementById('e-cfield').value.trim(),
    parent_table: document.getElementById('e-parent').value.trim(),
    parent_field: document.getElementById('e-pfield').value.trim(),
  });
  document.getElementById('btn-add-edge').onclick = async () => {
    try {
      const j = await api('/api/lineage/edges', {method:'POST', body: edgeBody()});
      setMsg(j.result.need_refreeze
        ? `已接边；原快照已作废，请到升版卡片重新冻结`
        : '已接边；尚未冻结过快照，打开卡片时会按新边冻结');
      await renderDirectory();
    } catch (e) { setMsg(e.message, true); }
  };
  document.getElementById('btn-del-edge').onclick = async () => {
    try {
      const j = await api('/api/lineage/edges', {method:'DELETE', body: edgeBody()});
      setMsg(j.result.need_refreeze
        ? `已撤边；原快照已作废，请到升版卡片重新冻结`
        : '已撤边');
      await renderDirectory();
    } catch (e) { setMsg(e.message, true); }
  };
}

/* ---------- 子表升版卡片：打开即冻结快照 ---------- */
function renderCardPicker() {
  view.innerHTML = `
    <div class="card grid">
      <label>子表草稿（有父表血缘边）
        <select id="cid">${childDraftOptions()}</select>
      </label>
      <button class="primary" id="btn-open" type="button">打开升版卡片（冻结缺边快照）</button>
      <p class="muted small">打开瞬间先冻住父表缺边快照，再在快照上裁决；提交时不重扫父表。</p>
    </div>`;
  document.getElementById('btn-open').onclick = async () => {
    try {
      const cid = document.getElementById('cid').value;
      const j = await api(`/api/contracts/${cid}/upgrade-card`);
      card = j.card; currentCid = cid;
      setMsg(`已冻结：${card.snapshot.voucher}`);
      renderCard();
      await loadContracts();
    } catch (e) { setMsg(e.message, true); }
  };
}

function renderCard() {
  if (!card) { renderCardPicker(); return; }
  const s = card.snapshot;
  const gate = s.gate;
  const decided = s.status === '已裁决' && gate;
  view.innerHTML = `
    <div class="card">
      <div class="row">
        <h3>${esc(card.contract.table_name)} · 升版卡片</h3>
        ${chip(s.verdict)}
        <span class="chip idle">门槛：${esc(card.gate_chip)}</span>
        <span class="mono">${esc(s.voucher)}</span>
      </div>
      <p class="muted small">${esc(card.gate_note)}</p>
      ${card.stale ? `<p class="err">${esc(card.refreeze_hint)}</p>` : ''}
      <div class="row">
        <button id="btn-refreeze" type="button">改边后重新冻结</button>
        <button class="primary" id="btn-decide" type="button" ${s.status !== '冻结' ? 'disabled' : ''}>点齐套</button>
        <button id="btn-apply" type="button" ${decided && gate.decision === '齐套放行' && card.contract.status === '草稿' ? '' : 'disabled'}>子表升现行</button>
        <button class="ghost" id="btn-close" type="button">换一张</button>
      </div>
      <p id="card-result" class="${decided && gate.decision === '隔离' ? 'err' : 'muted'}">
        ${decided ? `齐套结果：${esc(gate.decision)} · 凭证 ${esc(gate.voucher)} · ${esc(gate.detail)}` : `快照已冻结（${esc(s.status)}），缺边 ${s.missing_count} 条`}
      </p>
    </div>
    <div class="grid two" style="margin-top:0.8rem">
      <article class="card">
        <h3>依赖目录（本卡片）</h3>
        <table>
          <thead><tr><th>子表列</th><th>父表</th><th>父表列</th><th>芯片</th></tr></thead>
          <tbody>${card.directory.map(e => `
            <tr>
              <td>${esc(e.child_field)}</td>
              <td>${esc(e.parent_table)}</td>
              <td>${esc(e.parent_field)}</td>
              <td>${chip(e.state)}</td>
            </tr>`).join('')}</tbody>
        </table>
        <p class="muted small">缺边 ${s.missing_count} 条：${s.missing_edges.map(m => esc(`${m.parent_table}.${m.parent_field}（${m.reason}）`)).join('；') || '无'}</p>
      </article>
      <article class="card">
        <h3>父表详情</h3>
        ${card.parent_details.map(p => `
          <div class="sub">
            <div class="row"><strong>${esc(p.table_name)}</strong><span class="chip idle">现行 v${esc(p.version == null ? '—' : p.version)}</span></div>
            <p class="muted small">现行字段：${p.fields.map(esc).join('、') || '（无）'}</p>
            <p class="muted small">被引用：${p.used_edges.map(u => `${esc(u.child_field)}→${esc(u.parent_field)} ${chip(u.state)}`).join('　') || '无'}</p>
          </div>`).join('') || '<p class="muted">无父表</p>'}
      </article>
    </div>
    <article class="card" style="margin-top:0.8rem">
      <h3>进度水线（本卡片）</h3>
      <div class="waterline">
        ${card.watermarks.map(w => `
          <div class="wm">
            <span class="wm-stage">${esc(w.stage)}</span>
            <span class="mono">${esc(w.voucher)}</span>
            <span class="muted small">${esc(w.detail || '')}</span>
          </div>`).join('') || '<p class="muted">暂无</p>'}
      </div>
    </article>`;

  document.getElementById('btn-refreeze').onclick = async () => {
    try {
      const j = await api(`/api/contracts/${currentCid}/lineage/refreeze`, {method:'POST', body:'{}'});
      card = j.card;
      setMsg(`重新冻结：${card.snapshot.voucher}`);
      renderCard();
    } catch (e) { setMsg(e.message, true); }
  };
  document.getElementById('btn-decide').onclick = async () => {
    try {
      const j = await api(`/api/contracts/${currentCid}/lineage/decide`, {method:'POST', body:'{}'});
      card = j.card;
      setMsg(j.replayed
        ? `已有裁决（另一名血缘员先到）：${j.gate.decision} · ${j.gate.voucher}`
        : `齐套结果：${j.gate.decision} · ${j.gate.voucher}`);
      renderCard();
    } catch (e) { setMsg(e.message, true); }
  };
  document.getElementById('btn-apply').onclick = async () => {
    try {
      const j = await api(`/api/contracts/${currentCid}/apply`, {method:'POST', body:'{}'});
      setMsg(`已升现行 ${j.contract.table_name} v${j.contract.version_no}（齐套凭证 ${j.contract.gate_voucher}）`);
      await loadContracts();
      const c = await api(`/api/contracts/${currentCid}/card`);
      card = c.card; renderCard();
    } catch (e) { setMsg(e.message, true); }
  };
  document.getElementById('btn-close').onclick = () => {
    card = null; currentCid = null; renderCardPicker();
  };
}

/* ---------- 进度水线：总览，隔离亮凭证号 ---------- */
async function renderWaterline() {
  const items = (await api('/api/lineage/waterline')).items || [];
  view.innerHTML = `
    <div class="card">
      <div class="row">
        <strong>进度水线</strong>
        <span class="muted small">冻结 → 齐套结果 → 升版；隔离亮隔离凭证号</span>
      </div>
    </div>
    <div class="grid" style="margin-top:0.8rem">
      ${items.map(w => `
        <article class="card">
          <div class="row">
            <h3>${esc(w.child_table)}</h3>
            ${w.verdict ? chip(w.verdict) : '<span class="chip idle">未冻结</span>'}
            ${w.decision ? `<span class="${w.decision === '隔离' ? 'chip bad' : 'chip'}">${esc(w.decision)}</span>` : ''}
            ${w.live_version ? `<span class="chip idle">现行 v${esc(w.live_version)}</span>` : ''}
          </div>
          <div class="waterline">
            <div class="wm"><span class="wm-stage">冻结快照</span><span class="mono">${esc(w.voucher || '—')}</span></div>
            <div class="wm"><span class="wm-stage">齐套裁决</span><span class="mono">${esc(w.gate_voucher || '—')}</span><span class="muted small">${esc(w.decision || '待裁决')}</span></div>
            <div class="wm"><span class="wm-stage">目录凭证</span><span class="mono">${esc(w.catalog_voucher || '—')}</span></div>
          </div>
          ${w.decision === '隔离' ? `<p class="err">隔离凭证号 ${esc(w.gate_voucher)}：父表缺边未清，子表不得升现行</p>` : ''}
        </article>`).join('') || '<p class="muted">暂无血缘子表</p>'}
    </div>`;
}

/* ---------- 目录对账 ---------- */
async function renderReconcile() {
  const reports = (await api('/api/reconcile-reports')).items || [];
  view.innerHTML = `
    <div class="card grid">
      <div class="row">
        <button class="primary" id="btn-rec" type="button">跑一轮对账</button>
        <span class="muted small">对账只回看冻结快照与凭证留证，不重扫父表</span>
      </div>
      <div class="grid" id="reps">${reports.map(r => `
        <article class="card">
          <div class="row">
            <strong>${esc(r.as_of)}</strong>
            <span class="${r.lineage_aligned ? 'chip' : 'chip bad'}">${r.lineage_aligned ? '齐套对齐' : '齐套未对齐'}</span>
          </div>
          <p>${esc(r.summary)}</p>
          <p class="muted small">${esc(r.lineage_summary || '')}</p>
        </article>`).join('') || '<p class="muted">暂无对账</p>'}
      </div>
    </div>`;
  document.getElementById('btn-rec').onclick = async () => {
    try {
      const j = await api('/api/reconcile-reports', {
        method:'POST', body: JSON.stringify({as_of: '2026-11-18'})
      });
      setMsg(`${j.report.summary}｜${j.report.lineage_summary}`);
      await renderReconcile();
    } catch (e) { setMsg(e.message, true); }
  };
}

async function show(tab) {
  setMsg('');
  await loadContracts();
  if (tab === 'contracts') renderContracts();
  if (tab === 'check') renderCheck();
  if (tab === 'directory') await renderDirectory();
  if (tab === 'card') { card = null; currentCid = null; renderCardPicker(); }
  if (tab === 'waterline') await renderWaterline();
  if (tab === 'reconcile') await renderReconcile();
}

document.querySelectorAll('.pills button').forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll('.pills button').forEach(b => b.classList.remove('on'));
    btn.classList.add('on');
    show(btn.dataset.tab);
  };
});
document.getElementById('btn-logout').onclick = async () => {
  await api('/api/logout', {method:'POST', body:'{}'});
  location.href = '/login';
};
show('contracts');
