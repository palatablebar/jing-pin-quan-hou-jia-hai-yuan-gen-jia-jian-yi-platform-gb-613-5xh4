import { api } from '../api.js';
import { ENUMS, can, claimTone, couponTone, optionList } from '../config.js';
import { drawer, errorBox, errorMessage, esc, field, filterBar, fmtTime, kv, table, toast } from '../ui.js';

const state = { status: '', competitorId: '', q: '' };

function quotaMeter(item) {
  const total = Number(item.total_quota) || 0;
  const claimed = Number(item.claimed_count) || 0;
  const pct = total ? Math.min(100, Math.round((claimed / total) * 100)) : 100;
  const bad = item.status !== 'active';
  return `<div class="meter">
    <div class="mono tiny">已领 ${esc(claimed)} / ${esc(total)} · 剩 <b>${esc(item.remaining)}</b></div>
    <div class="meter-track"><div class="meter-fill ${bad ? 'bad' : ''}" style="width:${pct}%"></div></div>
  </div>`;
}

function snapshotRows(items) {
  if (!items || !items.length) return '<p class="muted tiny">还没有抓价快照引用这张券。</p>';
  return `<div class="table-wrap"><table class="data compact" style="min-width:0">
    <thead><tr><th>快照</th><th>抓价时状态</th><th>当前状态</th><th>账号领取权</th><th>名额（抓价时 → 现在）</th><th>当前可用</th><th>原因</th></tr></thead>
    <tbody>${items
      .map(
        (row) => `<tr class="${row.claimable ? '' : 'row-bad'}">
          <td class="mono">#${esc(row.snapshot_id)}<div class="muted tiny">${esc(row.captured_on || '—')}</div></td>
          <td><span class="badge ${couponTone(row.status_at_capture)}">${esc(ENUMS.couponStatus[row.status_at_capture] || row.status_at_capture)}</span></td>
          <td><span class="badge ${couponTone(row.status_now)}">${esc(ENUMS.couponStatus[row.status_now] || row.status_now)}</span></td>
          <td><span class="badge ${claimTone(row.claim_status)}">${esc(row.claim_status_label || row.claim_status)}</span></td>
          <td class="num">${esc(row.remaining_at_capture)} → <b>${esc(row.remaining_now)}</b></td>
          <td>${row.claimable ? '<span class="badge ok">可领</span>' : '<span class="badge high">不可领</span>'}</td>
          <td class="muted tiny">${esc(row.reason || '—')}</td>
        </tr>`,
      )
      .join('')}</tbody></table></div>`;
}

function claimRows(items) {
  if (!items || !items.length) return '<p class="muted tiny">抓价账号在这张券上还没有领取记录。</p>';
  return `<div class="table-wrap"><table class="data compact" style="min-width:0">
    <thead><tr><th>抓价账号</th><th>抓价日</th><th>领取结果</th><th>尝试次数</th><th>最近尝试</th><th>原因</th></tr></thead>
    <tbody>${items
      .map(
        (row) => `<tr>
          <td class="mono">${esc(row.account)}</td>
          <td class="mono">${esc(row.captured_on)}</td>
          <td><span class="badge ${claimTone(row.result)}">${esc(row.result_label || row.result)}</span></td>
          <td class="num">${esc(row.attempts)}</td>
          <td class="mono muted">${fmtTime(row.last_attempt_at)}</td>
          <td class="muted tiny">${esc(row.reason || '—')}</td>
        </tr>`,
      )
      .join('')}</tbody></table></div>`;
}

function eventRows(items) {
  if (!items || !items.length) return '<p class="muted tiny">还没有状态 / 名额 / 资格变更记录。</p>';
  return `<ul class="timeline">${items
    .map(
      (row) => `<li>
        <span class="tl-time">${fmtTime(row.created_at)}</span>
        <span class="tl-body">
          <b>${esc(row.event)}</b>
          <span class="mono muted">状态 ${esc(row.before_status || '—')} → ${esc(row.after_status || '—')} ·
            名额 ${esc(row.before_remaining ?? '—')} → ${esc(row.after_remaining ?? '—')} ·
            资格 ${esc(row.before_eligible ?? '—')} → ${esc(row.after_eligible ?? '—')}</span>
          <span>${esc(row.reason || '')} <span class="muted tiny">· ${esc(row.actor)}</span></span>
        </span>
      </li>`,
    )
    .join('')}</ul>`;
}

function adviceRows(items) {
  if (!items || !items.length) return '<p class="muted tiny">还没有跟价建议引用这张券所在快照。</p>';
  return `<div class="table-wrap"><table class="data compact" style="min-width:0">
    <thead><tr><th>运行号</th><th>SKU</th><th>建议价</th><th>建议状态</th><th>依据状态</th><th>去向</th></tr></thead>
    <tbody>${items
      .map(
        (row) => `<tr class="${row.basis_status === 'invalidated' ? 'row-bad' : ''}">
          <td class="mono">${esc(row.run_no)}</td>
          <td>${esc(row.sku_name || '')}<div class="mono muted tiny">${esc(row.sku_code || '')}</div></td>
          <td class="num money">¥${Number(row.suggested_price).toFixed(2)}</td>
          <td><span class="badge ${row.status === 'voided' ? 'high' : row.status === 'pending' ? 'pending warn' : 'ok'}">${esc(ENUMS.adviceStatus[row.status] || row.status)}</span></td>
          <td><span class="badge ${row.basis_status === 'invalidated' ? 'high' : 'ok'}">${esc(ENUMS.basisStatus[row.basis_status] || row.basis_status)}</span></td>
          <td class="mono muted">${row.superseded_by_id ? `→ 建议 #${esc(row.superseded_by_id)}` : '—'}</td>
        </tr>`,
      )
      .join('')}</tbody></table></div>`;
}

function openCoupon(view, ctx, item) {
  const editable = can(ctx.user.role, 'coupon:write');
  const { root, close } = drawer({
    title: item.title || item.code,
    subtitle: `${item.code} · ${item.competitor_name || '—'} · ${item.kind_label}`,
    body: `${kv([
      ['券面', `满 ${esc(item.threshold)} 减 ${esc(item.minus)}${item.per ? `（平台每满 ${esc(item.per)} 减 ${esc(item.minus)}）` : ''}`],
      ['当前状态', `<span class="badge ${couponTone(item.status)}">${esc(item.status_label)}</span>`],
      ['名额', `已领 ${esc(item.claimed_count)} / ${esc(item.total_quota)}，剩 ${esc(item.remaining)}`],
      ['账号领取权', `每账号上限 ${esc(item.per_account_limit)} 次 · 抓价账号资格：${item.eligible ? '具备' : '不具备'}`],
      ['有效期', `${esc(item.valid_from || '—')} ~ ${esc(item.valid_until || '—')}`],
      ['引用快照', `${esc(item.snapshot_links)} 条（已领到 ${esc(item.claimed_links)} 条）`],
      ['备注', esc(item.note || '—')],
    ])}
    ${quotaMeter(item)}
    <div class="section-label">抓价快照引用（抓价时 vs 现在的状态、名额、领取权）</div>
    ${snapshotRows(item.snapshots)}
    <div class="section-label">抓价账号领取记录（重跑只累加尝试次数）</div>
    ${claimRows(item.claims)}
    <div class="section-label">依据这张券的跟价建议与去向</div>
    ${adviceRows(item.advices)}
    <div class="section-label">状态 / 名额 / 资格变更留痕</div>
    ${eventRows(item.events)}`,
    footer: editable
      ? `<form id="coupon-form" class="form-grid">
          ${field({
            label: '券状态',
            name: 'status',
            value: item.status,
            options: Object.entries(ENUMS.couponStatus).map(([value, label]) => ({ value, label })),
          })}
          ${field({ label: '总名额', name: 'total_quota', type: 'number', value: item.total_quota, attrs: 'step="1" min="0"' })}
          ${field({ label: '已领数', name: 'claimed_count', type: 'number', value: item.claimed_count, attrs: 'step="1" min="0"' })}
          ${field({ label: '每账号可领次数', name: 'per_account_limit', type: 'number', value: item.per_account_limit, attrs: 'step="1" min="1"' })}
          ${field({
            label: '抓价账号领取资格',
            name: 'eligible',
            value: item.eligible ? 'true' : 'false',
            options: [
              { value: 'true', label: '具备资格' },
              { value: 'false', label: '不具备资格' },
            ],
          })}
          ${field({ label: '变更原因（留痕）', name: 'reason', placeholder: '例如：午间复核，券被抢空' })}
          <div class="form-error" id="coupon-error"></div>
          <button class="btn accent" type="submit">保存并留痕</button>
          <span class="mono muted tiny">状态 / 名额变化会让相关快照的依据失效；重跑测算后按可见价给新建议。</span>
        </form>`
      : '<span class="mono muted">只有定价主管可以调整券状态与名额；其他角色只读。</span>',
  });
  root.querySelector('#coupon-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.target;
    try {
      const payload = {
        status: form.status.value,
        total_quota: Number(form.total_quota.value),
        claimed_count: Number(form.claimed_count.value),
        per_account_limit: Number(form.per_account_limit.value),
        eligible: form.eligible.value === 'true',
        reason: form.reason.value.trim() || null,
      };
      await api.patch(`/api/coupons/${item.id}`, payload);
      toast('券状态已更新并留痕');
      close();
      await couponsPage.render(view, ctx);
    } catch (err) {
      root.querySelector('#coupon-error').textContent = errorMessage(err);
    }
  });
}

export const couponsPage = {
  async render(view, ctx) {
    let data;
    try {
      data = await api.get('/api/coupons', { status: state.status, competitor_id: state.competitorId, q: state.q, limit: 60 });
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => couponsPage.render(view, ctx));
      return;
    }
    const competitors = await api.get('/api/competitors', { limit: 20 }).catch(() => ({ items: [] }));
    const blocked = data.items.filter((item) => item.status !== 'active' || !item.eligible).length;
    view.innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <h2>限量券台账 · 名额与抓价账号领取权</h2>
          <div class="head-actions">
            <span class="meta">${data.total} 张券 · 不可领 ${blocked} 张</span>
          </div>
        </div>
        <div class="panel-body">
          ${filterBar('coupon-filters', [
            field({ label: '券状态', name: 'status', value: state.status, options: optionList(ENUMS.couponStatus) }),
            field({
              label: '竞品店铺',
              name: 'competitor_id',
              value: state.competitorId,
              options: [{ value: '', label: '全部' }, ...(competitors.items || []).map((item) => ({ value: item.id, label: item.name }))],
            }),
            field({ label: '搜索券', name: 'q', value: state.q, placeholder: '券编码 / 名称' }),
          ])}
        </div>
        ${table({
          rows: data.items,
          rowClass: (row) => (row.status !== 'active' || !row.eligible ? 'row-bad' : ''),
          empty: { title: '还没有登记限量券', hint: '跑一次抓价回放后会按快照里的券配置建台账。' },
          columns: [
            {
              key: 'title',
              label: '券 / 店铺',
              render: (row) =>
                `<b>${esc(row.title || row.code)}</b>
                 <div class="mono muted">${esc(row.code)} · ${esc(row.competitor_name || '—')}</div>`,
            },
            { key: 'kind_label', label: '类型', render: (row) => esc(row.kind_label) },
            { key: 'threshold', label: '券面', className: 'num', render: (row) => `满${esc(row.threshold)}减${esc(row.minus)}` },
            {
              key: 'remaining',
              label: '名额',
              render: (row) => quotaMeter(row),
            },
            {
              key: 'status',
              label: '状态',
              render: (row) =>
                `<span class="badge ${couponTone(row.status)}">${esc(row.status_label)}</span>
                 ${row.eligible ? '' : '<div class="badge high" style="margin-top:4px">无领取资格</div>'}`,
            },
            {
              key: 'claimed_links',
              label: '快照引用',
              className: 'num',
              render: (row) => `${esc(row.snapshot_links)} 条<div class="muted tiny">已领 ${esc(row.claimed_links)}</div>`,
            },
            {
              key: 'updated_at',
              label: '最近变更',
              render: (row) => `<span class="mono muted tiny">${fmtTime(row.updated_at)}</span>`,
            },
          ],
        })}
      </section>`;

    const filters = view.querySelector('#coupon-filters');
    filters?.addEventListener('change', () => {
      state.status = filters.querySelector('[name=status]').value;
      state.competitorId = filters.querySelector('[name=competitor_id]').value;
      state.q = filters.querySelector('[name=q]').value.trim();
      couponsPage.render(view, ctx);
    });
    filters?.querySelector('[name=q]')?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        state.q = event.target.value.trim();
        couponsPage.render(view, ctx);
      }
    });
    view.querySelectorAll('table.data tbody tr').forEach((row) => {
      row.addEventListener('click', async () => {
        try {
          const detail = await api.get(`/api/coupons/${row.dataset.id}`);
          openCoupon(view, ctx, detail);
        } catch (err) {
          toast(errorMessage(err), 'err');
        }
      });
    });
  },
};

export default couponsPage;
