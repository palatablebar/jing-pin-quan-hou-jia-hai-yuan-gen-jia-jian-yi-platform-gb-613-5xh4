import { api } from '../api.js';
import { ENUMS, can, claimTone, couponTone, optionList } from '../config.js';
import { drawer, errorBox, errorMessage, esc, field, filterBar, kv, table, toast } from '../ui.js';
import { couponText, money, priceCompare, signedPct } from '../components/price.js';

const state = { capturedOn: '', competitorId: '', category: '', q: '', basisStatus: '' };

function unitTable(effective) {
  const units = effective.units || [];
  if (!units.length) return '<p class="muted">这条快照的券配置非法，算法 A 无法还原。</p>';
  return `<div class="table-wrap"><table class="data compact" style="min-width:0">
    <thead><tr><th>件数</th><th>小计</th><th>满减</th><th>店铺券</th><th>平台券</th><th>会员</th><th>应付</th><th>单价</th></tr></thead>
    <tbody>${units
      .map(
        (unit) => `<tr class="${unit.units === effective.best_units ? 'best-row' : ''}">
          <td class="num">${unit.units}</td>
          <td class="num money">${money(unit.subtotal)}</td>
          <td class="num">${unit.full_reduction.applied ? `−${money(unit.full_reduction.minus)}` : '未触发'}</td>
          <td class="num">${unit.shop_coupon.applied ? `−${money(unit.shop_coupon.minus)}` : '未触发'}</td>
          <td class="num">${unit.platform_coupon.applied ? `−${money(unit.platform_coupon.minus)} ×${unit.platform_coupon.times}` : '未触发'}</td>
          <td class="num">${unit.member_discount < 1 ? `${Math.round(unit.member_discount * 100)}%` : '—'}</td>
          <td class="num money">${money(unit.payable)}</td>
          <td class="num money">${money(unit.unit_price)}</td>
        </tr>`,
      )
      .join('')}</tbody></table></div>
    <p class="muted tiny">命中组合按「满减单档 → 店铺券 → 平台跨店券（每满可重复）→ 会员折扣」顺序结算；最优件数 = 单件到手价最低。</p>`;
}

function couponTable(coupons) {
  if (!coupons || !coupons.length) {
    return '<p class="muted tiny">这条快照的券配置没有挂到限量券台账（无券，或该平台券未登记）。</p>';
  }
  return `<div class="table-wrap"><table class="data compact" style="min-width:0">
    <thead><tr><th>券</th><th>抓价时状态</th><th>当前状态</th><th>账号领取权</th><th>名额（抓价时 → 现在）</th><th>当前可用</th><th>原因</th></tr></thead>
    <tbody>${coupons
      .map(
        (item) => `<tr class="${item.claimable ? '' : 'row-bad'}">
          <td><b class="mono">${esc(item.code || item.kind)}</b><div class="muted tiny">${esc(item.title || '')}</div></td>
          <td><span class="badge ${couponTone(item.status_at_capture)}">${esc(ENUMS.couponStatus[item.status_at_capture] || item.status_at_capture)}</span></td>
          <td><span class="badge ${couponTone(item.status_now)}">${esc(item.status_now_label || item.status_now)}</span></td>
          <td><span class="badge ${claimTone(item.claim_status)}">${esc(item.claim_status_label || item.claim_status)}</span></td>
          <td class="num">${esc(item.remaining_at_capture)} → <b>${esc(item.remaining_now)}</b></td>
          <td>${item.claimable ? '<span class="badge ok">可领</span>' : '<span class="badge high">不可领</span>'}</td>
          <td class="muted tiny">${esc(item.reason || '—')}</td>
        </tr>`,
      )
      .join('')}</tbody></table></div>`;
}

function openSnapshot(view, ctx, item) {
  const canCheck = can(ctx.user.role, 'snapshot:write');
  const invalidated = item.basis_status === 'invalidated';
  const { root, close } = drawer({
    title: `${item.sku_name} · ${item.captured_on}`,
    subtitle: `${item.competitor_name} · 快照 #${item.id}`,
    body: `${priceCompare(item)}
      ${
        invalidated
          ? `<div class="note-strip danger"><b>依据已失效。</b>${esc(item.basis_reason || '券当前不可领')}。
             下方「早班券后」仅作历史留档；跟价建议已按<b>当前可用价 ${money(item.visible_unit_price_1)}</b> 重算或作废。</div>`
          : ''
      }
      ${kv([
        ['页面价', `<span class="money">${money(item.page_price)}</span>`],
        ['券配置', esc(couponText(item))],
        ['会员折扣', item.member_discount < 1 ? `${Math.round(item.member_discount * 100)}%` : '非会员价'],
        ['活动', esc(item.campaign || '—')],
        ['库存', esc(item.stock)],
        ['算法 A 结论（抓价口径）', esc(item.note || '—')],
        ['当前可见价（不靠失效券）', `<span class="money">${money(item.visible_unit_price_1)}</span>`],
        ['依据状态', `<span class="badge ${invalidated ? 'high' : 'ok'}">${invalidated ? '依据已失效' : '依据有效'}</span>`],
        ['置信度', `${item.valid ? '0.99' : '0.00'}${(item.warnings || []).length ? ` · ${esc(item.warnings.join('；'))}` : ''}`],
      ])}
      <div class="section-label">限量券台账 · 每次券状态 / 名额 / 账号领取权</div>
      ${couponTable(item.coupons)}
      <div class="section-label">按件数拆解（算法 A：抓价口径）</div>
      ${unitTable(item.effective || {})}
      ${
        item.effective_visible && item.effective_visible.degraded
          ? `<div class="section-label">按当前可用券重算（可见价）</div>
             ${kv([
               ['可见价 1 件', `<span class="money">${money(item.visible_unit_price_1)}</span>`],
               ['可见价 2 / 3 件', `${money(item.visible_unit_price_2)} / ${money(item.visible_unit_price_3)}`],
               ['命中组合', esc(item.visible_note || '—')],
               ['剔除的券', esc(Object.keys(item.effective_visible.excluded || {}).join('、') || '无')],
             ])}`
          : ''
      }`,
    footer: canCheck
      ? `<div class="form-grid">
          ${field({ label: '核对备注（可留空）', name: 'check-note', placeholder: '例如：午间复核，店铺券已抢空' })}
          <button class="btn accent" id="check-coupons" type="button">核对当前可用价</button>
          <span class="mono muted tiny">核对会刷新券状态与名额；同一抓价重跑不会重复领券。</span>
        </div>`
      : '<span class="mono muted">只有定价主管 / 价格运营可以核对券状态；店长只读。</span>',
  });
  root.querySelector('#check-coupons')?.addEventListener('click', async () => {
    const button = root.querySelector('#check-coupons');
    button.disabled = true;
    button.textContent = '核对中…';
    try {
      const detail = await api.post(`/api/snapshots/${item.id}/coupon-check`, {
        reason: root.querySelector('[name=check-note]').value.trim() || null,
      });
      const check = detail.coupon_check || {};
      const rerun = check.rerun;
      toast(
        check.invalidated
          ? `核对完成：${check.basis_reason || '券不可领'}；可见价 ¥${check.visible_unit_price_1}` +
            (rerun ? `；已按可见价重算 ${rerun.run_no}，新建议 ${rerun.advices} 条，作废旧依据 ${rerun.voided_advices} 条` : '')
          : `核对完成：券均可领，可见价 ¥${check.visible_unit_price_1}`,
      );
      close();
      openSnapshot(view, ctx, detail);
      await snapshotsPage.render(view, ctx);
    } catch (err) {
      toast(errorMessage(err), 'err');
      button.disabled = false;
      button.textContent = '核对当前可用价';
    }
  });
}

export const snapshotsPage = {
  async render(view, ctx) {
    let data;
    try {
      data = await api.get('/api/snapshots', {
        captured_on: state.capturedOn,
        competitor_id: state.competitorId,
        category: state.category,
        q: state.q,
        basis_status: state.basisStatus,
        limit: 50,
      });
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => snapshotsPage.render(view, ctx));
      return;
    }
    const days = data.capture_days || [];
    if (!state.capturedOn && days.length) state.capturedOn = days[0];
    const competitors = await api.get('/api/competitors', { limit: 20 }).catch(() => ({ items: [] }));
    view.innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <h2>抓价快照 · 页面价 vs 券后价 vs 当前可用价</h2>
          <div class="head-actions">
            <span class="meta">${data.total} 条快照 · 快照日 ${days.length} 天 · 依据失效 ${data.invalidated_total || 0} 条</span>
            ${can(ctx.user.role, 'run:create') ? '<button class="btn sm accent" id="run-calc">重跑今天测算</button>' : ''}
          </div>
        </div>
        <div class="panel-body">
          ${filterBar('snap-filters', [
            field({ label: '快照日期', name: 'captured_on', value: state.capturedOn, options: days.map((day) => ({ value: day, label: day })) }),
            field({ label: '类目', name: 'category', value: state.category, options: optionList(ENUMS.category) }),
            field({
              label: '竞品店铺',
              name: 'competitor_id',
              value: state.competitorId,
              options: [{ value: '', label: '全部' }, ...(competitors.items || []).map((item) => ({ value: item.id, label: item.name }))],
            }),
            field({ label: '券依据', name: 'basis_status', value: state.basisStatus, options: optionList(ENUMS.basisStatus) }),
            field({ label: '搜索 SKU', name: 'q', value: state.q, placeholder: '名称 / 编码' }),
          ])}
        </div>
        ${table({
          rows: data.items,
          rowClass: (row) => (row.basis_status === 'invalidated' ? 'row-bad' : ''),
          empty: { title: '这个筛选下没有快照', hint: '换一天，或先跑一次抓价回放。' },
          columns: [
            {
              key: 'sku_name',
              label: 'SKU / 竞品',
              render: (row) =>
                `<b>${esc(row.sku_name)}</b><div class="mono muted">${esc(row.sku_code)} · ${esc(row.competitor_name || '—')}</div>`,
            },
            { key: 'page_price', label: '页面价', className: 'num money', render: (row) => money(row.page_price) },
            {
              key: 'unit_price_1',
              label: '早班券后',
              className: 'num',
              render: (row) =>
                row.basis_status === 'invalidated'
                  ? `<span class="money muted" style="text-decoration:line-through">${money(row.unit_price_1)}</span>`
                  : `<span class="money">${money(row.unit_price_1)}</span>`,
            },
            {
              key: 'visible_unit_price_1',
              label: '当前可用价',
              className: 'num',
              render: (row) => `<b class="money">${money(row.visible_unit_price_1)}</b>`,
            },
            {
              key: 'basis_status',
              label: '券依据',
              render: (row) =>
                row.basis_status === 'invalidated'
                  ? `<span class="badge high" title="${esc(row.basis_reason || '')}">依据已失效</span>
                     <div class="muted tiny ellipsis">${esc(row.basis_reason || '')}</div>`
                  : '<span class="badge ok">依据有效</span>',
            },
            {
              key: 'discount_pct',
              label: '券后折扣',
              className: 'num',
              render: (row) => `<span class="badge info">${signedPct(-Math.abs(Number(row.discount_pct)))}</span>`,
            },
            {
              key: 'note',
              label: '命中组合',
              render: (row) => `<span class="mono muted ellipsis">${esc(row.note || '—')}</span>`,
            },
          ],
        })}
      </section>`;

    const filters = view.querySelector('#snap-filters');
    filters?.addEventListener('change', () => {
      state.capturedOn = filters.querySelector('[name=captured_on]').value;
      state.category = filters.querySelector('[name=category]').value;
      state.competitorId = filters.querySelector('[name=competitor_id]').value;
      state.basisStatus = filters.querySelector('[name=basis_status]').value;
      state.q = filters.querySelector('[name=q]').value.trim();
      snapshotsPage.render(view, ctx);
    });
    filters?.querySelector('[name=q]')?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        state.q = event.target.value.trim();
        snapshotsPage.render(view, ctx);
      }
    });
    view.querySelectorAll('table.data tbody tr').forEach((row) => {
      row.addEventListener('click', async () => {
        try {
          const detail = await api.get(`/api/snapshots/${row.dataset.id}`);
          openSnapshot(view, ctx, detail);
        } catch (err) {
          toast(errorMessage(err), 'err');
        }
      });
    });
    view.querySelector('#run-calc')?.addEventListener('click', async () => {
      const button = view.querySelector('#run-calc');
      button.disabled = true;
      button.textContent = '测算中…';
      try {
        const run = await api.post('/api/runs', { captured_on: state.capturedOn || undefined, trigger: 'manual' });
        const metrics = run.metrics || {};
        toast(
          `测算完成 ${run.run_no}：${metrics.snapshots} 条快照 → ${metrics.advices} 条建议；` +
            `依据失效 ${metrics.invalidated_snapshots || 0} 条，作废旧建议 ${metrics.voided_advices || 0} 条；` +
            `领券新领 ${metrics.claims_new || 0} / 复用 ${metrics.claims_reused || 0} / 被拦 ${metrics.claims_blocked || 0}`,
        );
        await snapshotsPage.render(view, ctx);
      } catch (err) {
        toast(errorMessage(err), 'err');
        button.disabled = false;
        button.textContent = '重跑今天测算';
      }
    });
  },
};

export default snapshotsPage;
