import { api, chip, escapeHtml } from '../api.js';

export async function renderVisits(el) {
  const [batches, sites, protocols, ledger] = await Promise.all([
    api('/api/batches'),
    api('/api/sites'),
    api('/api/protocols?status=' + encodeURIComponent('生效')),
    api('/api/idem_key/board'),
  ]);
  const board = ledger || {};
  const siteOpts = (sites.items || [])
    .filter((s) => s.status !== '停用')
    .map((s) => `<option value="${s.id}">${escapeHtml(s.site_code + ' · ' + s.name)}</option>`)
    .join('');
  const protoOpts = (protocols.items || [])
    .map((p) => `<option value="${p.id}">${escapeHtml(p.protocol_code + ' · ' + p.title)}</option>`)
    .join('');

  el.innerHTML = `
    <div class="card">
      <h2>访视清单抓取批次</h2>
      <p class="muted">按试验中心开批次，模拟抓取访视行，合格行可入库，缺口行进入质控与补采。</p>
      <div class="row">
        <span class="chip ${board.aligned ? 'ok' : 'bad'}" data-idem-board-chip>${escapeHtml(board.chip || '幂等已亮')}</span>
        <span class="muted">幂等账册 ${board.side || 'bypass_ledger'} · 凭证 ${board.voucher_total || 0} · 钉定 ${board.pinned_total || 0} · 释放 ${board.released_total || 0} · 未对齐批次 ${board.mismatch_count || 0}</span>
      </div>
      <div class="row">
        <select id="siteId">${siteOpts}</select>
        <select id="protocolId"><option value="">默认生效方案</option>${protoOpts}</select>
        <input id="bnote" placeholder="批次备注" />
        <button id="createBatch">开新批次</button>
      </div>
    </div>
    <div class="card">
      <div class="row">
        <select id="statusFilter">
          <option value="">全部批次状态</option>
          <option>采集中</option>
          <option>已收齐</option>
          <option>已关闭</option>
        </select>
        <button id="reload" class="ghost">刷新列表</button>
      </div>
      <table>
        <thead>
          <tr>
            <th>批次编码</th><th>中心</th><th>方案</th><th>状态</th>
            <th>行数</th><th>缺口</th><th>幂等徽标</th><th>追加次数</th><th>备注</th><th>操作</th>
          </tr>
        </thead>
        <tbody id="batchBody">
          ${(batches.items || []).map(batchRow).join('')}
        </tbody>
      </table>
    </div>
    <div class="card">
      <h2>批次详情与访视行</h2>
      <div id="batchDetail" class="muted">点击「查看」加载批次内访视行与质量分布。</div>
    </div>`;

  function batchRow(b) {
    const aligned = b.idem_aligned !== false;
    const chipHtml = `<span class="chip ${aligned ? 'ok' : 'bad'}">${escapeHtml(b.idem_chip || (aligned ? '幂等已亮' : '重复追加'))}</span>`;
    const countHtml = `<span${aligned ? '' : ' class="err" title="账册凭证数与访视行数对不上，已挡重复追加"'}>${b.append_count ?? 0} / ${b.item_count ?? 0}</span>`;
    return `
      <tr>
        <td>${escapeHtml(b.batch_code)}</td>
        <td>${escapeHtml(b.site_name || '')}</td>
        <td>${escapeHtml((b.protocol_code || '-') + (b.protocol_version ? '@' + b.protocol_version : ''))}</td>
        <td>${chip(b.status)}</td>
        <td>${b.item_count}</td>
        <td>${b.gap_count}</td>
        <td>${chipHtml}</td>
        <td>${countHtml}</td>
        <td class="muted">${escapeHtml(b.note || '')}</td>
        <td class="row">
          <button data-view="${b.id}">查看</button>
          <button data-crawl="${b.id}" class="ghost" ${aligned ? '' : 'disabled'}>模拟抓取</button>
          <button data-ingest="${b.id}" class="ghost">合格入库</button>
          <button data-complete="${b.id}" class="warn">标已收齐</button>
          <button data-close="${b.id}" class="danger">关闭</button>
        </td>
      </tr>`;
  }

  const detail = el.querySelector('#batchDetail');
  // 非破坏式提示：只替换上一条 flash，不整块重绘详情，避免把刚发生的拒绝原因冲掉。
  const flash = (msg, isErr) => {
    detail.querySelectorAll('.flash').forEach((n) => n.remove());
    detail.insertAdjacentHTML(
      'afterbegin',
      `<p class="${isErr ? 'err' : 'muted'} flash">${escapeHtml(msg)}</p>`,
    );
  };
  const showErr = (msg) => flash(msg, true);

  async function loadDetail(id) {
    const b = await api(`/api/batches/${id}`);
    const qs = b.quality_stats || {};
    detail.innerHTML = `
      <div class="row">
        <strong>${escapeHtml(b.batch_code)}</strong>
        ${chip(b.status)}
        <span class="chip ${b.idem_aligned ? 'ok' : 'bad'}">${escapeHtml(b.idem_chip || '幂等已亮')}</span>
        <span class="muted">${escapeHtml(b.site_name)} · 方案 ${escapeHtml(b.protocol_title || '-')}</span>
      </div>
      ${b.idem_aligned ? '' : `<p class="err">幂等徽标与追加次数对不上（账册 ${b.append_count} / 访视行 ${b.item_count}），已挡重复追加。可提交回刷申请，甲路账册会拒绝并留痕。</p>
      <div class="row"><button id="requestRefresh" class="warn">申请回刷账册</button></div>`}
      <div class="grid">
        <div class="stat"><div class="n">${b.item_count}</div><div class="l">访视行</div></div>
        <div class="stat"><div class="n">${b.append_count ?? 0}</div><div class="l">追加次数（账册凭证）</div></div>
        <div class="stat"><div class="n">${b.gap_count}</div><div class="l">缺口行</div></div>
        <div class="stat"><div class="n">${qs['合格'] || 0}</div><div class="l">合格</div></div>
        <div class="stat"><div class="n">${(qs['缺字段'] || 0) + (qs['编码空'] || 0) + (qs['重复键'] || 0)}</div><div class="l">异常质量</div></div>
      </div>
      <div class="row">
        <input id="subjectKey" placeholder="受试者键" />
        <input id="visitCode" placeholder="访视编码" value="V1" style="width:90px" />
        <input id="payload" placeholder='载荷 JSON，如 {"visit_date":"2026-11-20","investigator":"王医生","weight_kg":60}' style="min-width:320px" />
        <select id="qualityForce">
          <option value="">自动推断质量</option>
          <option>合格</option>
          <option>缺字段</option>
          <option>编码空</option>
          <option>重复键</option>
        </select>
        <button id="addRow">追加访视行</button>
        <button id="runQc" class="warn">对本批质控扫描</button>
        <button id="enqueueGaps">缺口排队补采</button>
      </div>
      <table>
        <thead><tr><th>ID</th><th>受试者</th><th>访视</th><th>质量</th><th>状态</th><th>载荷摘要</th><th>操作</th></tr></thead>
        <tbody>
          ${(b.items || []).map((r) => `
            <tr>
              <td>${r.id}</td>
              <td>${escapeHtml(r.subject_key)}</td>
              <td>${escapeHtml(r.visit_code)}</td>
              <td>${chip(r.quality)}</td>
              <td>${chip(r.status)}</td>
              <td class="muted">${escapeHtml((r.payload || '').slice(0, 80))}</td>
              <td class="row">
                <button data-enqueue-row="${r.id}" ${r.quality === '合格' && r.status === '已入库' ? 'disabled' : ''}>排队补采</button>
                <button data-mark="${r.id}" data-st="待补采" class="ghost">标待补采</button>
              </td>
            </tr>`).join('') || '<tr><td colspan="7" class="muted">暂无访视行</td></tr>'}
        </tbody>
      </table>`;

    detail.querySelector('#addRow').onclick = async () => {
      try {
        const q = detail.querySelector('#qualityForce').value;
        await api(`/api/batches/${id}/items`, {
          method: 'POST',
          body: JSON.stringify({
            subject_key: detail.querySelector('#subjectKey').value,
            visit_code: detail.querySelector('#visitCode').value,
            payload: detail.querySelector('#payload').value,
            quality: q || null,
          }),
        });
        await loadDetail(id);
        await softReload();
      } catch (e) { showErr(e.message); }
    };
    const refreshBtn = detail.querySelector('#requestRefresh');
    if (refreshBtn) refreshBtn.onclick = async () => {
      try {
        const r = await api('/api/idem_key/refresh', {
          method: 'POST',
          body: JSON.stringify({ batch_id: id, note: '页面申请回刷账册' }),
        });
        await loadDetail(id);
        showErr(`${r.message}（已拒绝，凭证仍为 ${r.append_count}）`);
      } catch (e) { showErr(e.message); }
    };
    detail.querySelector('#runQc').onclick = async () => {
      try {
        const r = await api(`/api/batches/${id}/qc`, { method: 'POST', body: '{}' });
        await loadDetail(id);
        flash(`质控新建发现 ${r.created} 条`, false);
      } catch (e) { showErr(e.message); }
    };
    detail.querySelector('#enqueueGaps').onclick = async () => {
      try {
        const r = await api(`/api/batches/${id}/backfill`, { method: 'POST', body: '{}' });
        await loadDetail(id);
        flash(`已排队 ${r.enqueued}，跳过 ${r.skipped}`, false);
      } catch (e) { showErr(e.message); }
    };
    detail.querySelectorAll('[data-enqueue-row]').forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/rows/${btn.dataset.enqueueRow}/enqueue`, { method: 'POST', body: '{}' });
          await loadDetail(id);
        } catch (e) { showErr(e.message); }
      };
    });
    detail.querySelectorAll('[data-mark]').forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/rows/${btn.dataset.mark}/status`, {
            method: 'POST',
            body: JSON.stringify({ status: btn.dataset.st }),
          });
          await loadDetail(id);
        } catch (e) { showErr(e.message); }
      };
    });
  }

  async function softReload() {
    const status = el.querySelector('#statusFilter').value;
    const url = status ? `/api/batches?status=${encodeURIComponent(status)}` : '/api/batches';
    const data = await api(url);
    el.querySelector('#batchBody').innerHTML = (data.items || []).map(batchRow).join('');
    bindBatchActions();
  }

  function bindBatchActions() {
    el.querySelectorAll('[data-view]').forEach((btn) => {
      btn.onclick = () => loadDetail(btn.dataset.view).catch((e) => showErr(e.message));
    });
    el.querySelectorAll('[data-crawl]').forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/batches/${btn.dataset.crawl}/crawl`, {
            method: 'POST',
            body: JSON.stringify({ count: 5, mix_bad: true }),
          });
          await softReload();
          await loadDetail(btn.dataset.crawl);
        } catch (e) { showErr(e.message); }
      };
    });
    el.querySelectorAll('[data-ingest]').forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/batches/${btn.dataset.ingest}/ingest`, { method: 'POST', body: '{}' });
          await softReload();
          await loadDetail(btn.dataset.ingest);
        } catch (e) { showErr(e.message); }
      };
    });
    el.querySelectorAll('[data-complete]').forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/batches/${btn.dataset.complete}/complete`, { method: 'POST', body: '{}' });
          await softReload();
        } catch (e) { showErr(e.message); }
      };
    });
    el.querySelectorAll('[data-close]').forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/batches/${btn.dataset.close}/close`, { method: 'POST', body: '{}' });
          await softReload();
        } catch (e) { showErr(e.message); }
      };
    });
  }

  el.querySelector('#createBatch').onclick = async () => {
    try {
      const body = {
        site_id: Number(el.querySelector('#siteId').value),
        note: el.querySelector('#bnote').value,
      };
      const pid = el.querySelector('#protocolId').value;
      if (pid) body.protocol_id = Number(pid);
      const created = await api('/api/batches', { method: 'POST', body: JSON.stringify(body) });
      await softReload();
      await loadDetail(created.id);
    } catch (e) { showErr(e.message); }
  };
  el.querySelector('#reload').onclick = () => softReload().catch((e) => showErr(e.message));
  el.querySelector('#statusFilter').onchange = () => softReload().catch((e) => showErr(e.message));
  bindBatchActions();
}
