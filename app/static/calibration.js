/* 锚点校准抽屉模块 —— 在双来源对账结果中勾选 1～20 对人工确认的同一动作
   事件，指定待校准方，调用 /api/anchor-calibration 求 offset 增量范围交集。

   复用 app.js 的 state / overrides / el / esc / toast / focusEvent /
   shortId / recompute，复用 reconciliation.js 的 reconState / anchorSelection /
   reconTime / reconClose。只读分析；「采用中点」仅写左栏未保存 clock_offset
   假设并触发重算，仍可在左栏放弃调整。 */
"use strict";

const anchorState = {
  result: null,
};
const anchor$ = (sel) => document.querySelector(sel);

/* ------------------------------------------------------------ 面板同步 */

/* 对账结果重渲染后：同步勾选状态、计数与面板可见性 */
document.addEventListener("recon:rendered", (e) => {
  anchorSync(e.detail);
});

function anchorPanel() {
  return anchor$("#anchor-panel");
}

function anchorSide() {
  const checked = document.querySelector('input[name="anchor-cal-side"]:checked');
  return checked ? checked.value : "b";
}

function anchorSync(d) {
  const panel = anchorPanel();
  if (!d || !d.pairs.length) {
    panel.classList.add("hidden");
    anchorState.result = null;
    anchor$("#anchor-result").innerHTML = "";
    anchorRefreshCount();
    return;
  }
  panel.classList.remove("hidden");
  anchorRefreshCount();
}

/* 勾选框位于 #recon-result（anchor-panel 的兄弟节点），故委托到整个对账抽屉。
   reconRender 每次重建 DOM，事件委托无需重新绑定。 */
anchor$("#recon-overlay").addEventListener("change", (ev) => {
  const cb = ev.target.closest(".anchor-cb");
  if (!cb) return;
  const key = `${cb.dataset.a}|${cb.dataset.b}`;
  if (cb.checked) {
    if (anchorSelection.size >= 20 && !anchorSelection.has(key)) {
      cb.checked = false;
      toast("锚点最多选择 20 对", "error");
      return;
    }
    anchorSelection.add(key);
  } else {
    anchorSelection.delete(key);
  }
  // 调整勾选后旧校准结果不再对应同一组锚点，清掉避免误采
  anchorState.result = null;
  anchor$("#anchor-result").innerHTML = "";
  anchorRefreshCount();
});

/* 切换待校准方后旧结果标签作废，提示重新计算 */
document.querySelectorAll('input[name="anchor-cal-side"]').forEach((radio) =>
  radio.addEventListener("change", () => {
    anchorState.result = null;
    anchor$("#anchor-result").innerHTML = "";
  }));

function anchorRefreshCount() {
  const cnt = anchor$("#anchor-count");
  if (cnt) cnt.textContent = String(anchorSelection.size);
}

/* ------------------------------------------------------------ 计算 */

async function anchorRun() {
  const d = reconState.result;
  if (!d) return toast("请先完成一次双来源对账", "error");
  if (!anchorSelection.size) return toast("请先在「已配对事件」中勾选至少 1 对锚点", "error");
  if (anchorSelection.size > 20) return toast("锚点最多 20 对", "error");

  const side = anchorSide();              // a | b：哪一侧作待校准方
  const calSrc = side === "a" ? d.sides.a.source_id : d.sides.b.source_id;
  const refSrc = side === "a" ? d.sides.b.source_id : d.sides.a.source_id;
  // 配对键方向固定为 A|B：待校准侧为 A 时取 a/b，为 B 时取 b/a
  const anchors = [...anchorSelection].map((k) => {
    const [aid, bid] = k.split("|");
    return side === "a"
      ? { cal_event_id: aid, ref_event_id: bid }
      : { cal_event_id: bid, ref_event_id: aid };
  });

  const box = anchor$("#anchor-result");
  box.innerHTML = "";
  box.appendChild(el("div", "cmp-empty", "求各锚点 offset 增量范围交集中…"));

  const res = await fetch("/api/anchor-calibration", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      calibrated_source: calSrc,
      reference_source: refSrc,
      anchors,
      overrides: state.overrides || {},
    }),
  });
  if (!res.ok) {
    let msg = await res.text();
    try { msg = JSON.parse(msg).detail || msg; } catch { /* 保留原文 */ }
    anchorState.result = null;
    box.innerHTML = "";
    const err = el("div", "anchor-error");
    err.textContent = "锚点校准失败: " + msg;
    box.appendChild(err);
    toast("锚点校准失败: " + msg, "error");
    return;
  }
  anchorState.result = await res.json();
  anchorRender();
}

/* 采用中点：只更新左栏未保存 clock_offset 假设并重算时间线 */
async function anchorAdopt() {
  const r = anchorState.result;
  if (!r || !r.feasible) return;
  const sid = r.calibrated_source.id;
  state.overrides[sid] = state.overrides[sid] || {};
  state.overrides[sid].clock_offset = r.midpoint_seconds;
  reconClose();
  await recompute();
  toast(`已把 ${sid} 的未保存 offset 设为范围中点 ${r.midpoint_seconds}s，时间线已重算；`
        + "仍可在左栏「放弃调整」撤销", "ok");
}

/* ------------------------------------------------------------ 渲染 */

function anchorMsText(v) { return `${v >= 0 ? "+" : ""}${v} ms`; }

function anchorRender() {
  const r = anchorState.result;
  const box = anchor$("#anchor-result");
  box.innerHTML = "";
  if (!r) return;
  box.appendChild(anchorSummary(r));
  if (!r.feasible) box.appendChild(anchorConflict(r));
  box.appendChild(anchorAnchorList(r));
  box.querySelectorAll("[data-eid]").forEach((n) =>
    n.addEventListener("click", () => { reconClose(); focusEvent(n.dataset.eid); }));
  const adopt = box.querySelector(".anchor-adopt");
  if (adopt) adopt.addEventListener("click", anchorAdopt);
}

function anchorSummary(r) {
  const blk = el("div", "cmp-block anchor-summary" + (r.feasible ? " ok" : " bad"));
  const f = r.fixed_assumptions;
  const dirty = Object.keys(state.overrides || {}).length > 0;
  const feasibleHtml = `
    <div class="anchor-verdict ok">✓ ${r.anchor_count} 对锚点的 offset 增量范围交集非空</div>
    <div class="anchor-range-row">
      <span class="anchor-range-label">允许的绝对 offset（${esc(r.calibrated_source.name || r.calibrated_source.id)}）</span>
      <span class="anchor-range-val ok">${esc(r.absolute_offset_text)}</span>
      <span class="anchor-range-sub">整数毫秒 [${r.absolute_offset_ms[0]}, ${r.absolute_offset_ms[1]}]</span>
    </div>
    <div class="anchor-range-row">
      <span class="anchor-range-label">相对当前 offset（${esc(r.current_offset_text)}）的增量</span>
      <span class="anchor-range-val">${esc(r.offset_delta_text)}</span>
    </div>
    <button class="btn tiny primary anchor-adopt">✍ 采用中点 ${r.midpoint_seconds}s（写入左栏未保存假设并重算）</button>`;
  const infeasibleHtml = `
    <div class="anchor-verdict bad">✕ ${r.anchor_count} 对锚点的 offset 增量范围交集为空</div>
    <div class="anchor-range-row">
      <span class="anchor-range-label">各锚点增量范围的 max 下界</span>
      <span class="anchor-range-val bad">${esc(r.conflict.lower_bound_text)}</span>
    </div>
    <div class="anchor-range-row">
      <span class="anchor-range-label">各锚点增量范围的 min 上界</span>
      <span class="anchor-range-val bad">${esc(r.conflict.upper_bound_text)}</span>
    </div>
    <div class="anchor-range-sub">下界高出上界 ${esc(r.conflict.gap_text)}——不存在任何单一 offset 可让全部锚点闭区间同时相交</div>`;
  blk.innerHTML = `
    <h3>锚点校准结果
      <span class="cmp-count">待校准 ${esc(r.calibrated_source.name || r.calibrated_source.id)}
        · 参照 ${esc(r.reference_source.name || r.reference_source.id)} · ${r.anchor_count} 对锚点</span></h3>
    ${r.feasible ? feasibleHtml : infeasibleHtml}
    <div class="sens-meta" style="margin-top:8px">
      <span class="stat">固定 drift <b>${f.drift_ppm} ppm</b></span>
      <span class="stat">固定不确定度 <b>±${f.uncertainty}s</b></span>
      <span class="stat">固定可靠度 <b>${f.reliability}</b></span>
      ${dirty ? '<span class="stat warn">含未保存假设</span>' : ""}
    </div>
    <div class="sens-notice" style="margin-top:8px">${esc(r.calibration.rule)}；${esc(r.calibration.rounding)}。</div>`;
  return blk;
}

function anchorConflict(r) {
  const c = r.conflict;
  const blk = el("div", "cmp-block anchor-conflict");
  blk.innerHTML = `
    <h3>冲突锚点
      <span class="cmp-count">分别决定交集下界与上界的两对锚点${c.lower_tie || c.upper_tie ? "（含并列）" : ""}</span></h3>
    <div class="anchor-conflict-explain">${esc(c.explanation)}</div>
    <div class="anchor-conflict-cols">
      ${anchorConflictSide("决定下界 max", c.lower_anchor, "lower", c.lower_determiner_indices)}
      ${anchorConflictSide("决定上界 min", c.upper_anchor, "upper", c.upper_determiner_indices)}
    </div>`;
  return blk;
}

function anchorConflictSide(label, a, cls, indices) {
  return `
    <div class="anchor-conf-side ${cls}">
      <div class="anchor-conf-head"><b>${esc(label)}</b>
        <span class="anchor-conf-range">δ ∈ ${esc(a.delta_range_text)}</span>
        <span class="tag amb">排序后锚点 #${a.anchor_index}</span>
        <span class="tag amb">并列决定者 ${indices.join(" / ")}</span>
      </div>
      <div class="anchor-conf-pair">
        <span class="chain-node" data-eid="${esc(a.calibrated.id)}" title="待校准事件，点击定位">${esc(shortId(a.calibrated.id))}</span>
        <span class="chain-arrow">≈</span>
        <span class="chain-node" data-eid="${esc(a.reference.id)}" title="参照事件，点击定位">${esc(shortId(a.reference.id))}</span>
        <span class="tag actor">👤 ${esc(a.actor)}</span>
        <span class="recon-um-type">${esc(a.event_type)}</span>
      </div>
      <div class="anchor-conf-ranges">
        待校准 [${esc(reconTime(a.calibrated.lo_ms))} ~ ${esc(reconTime(a.calibrated.hi_ms))}]
        · 参照 [${esc(reconTime(a.reference.lo_ms))} ~ ${esc(reconTime(a.reference.hi_ms))}]
      </div>
    </div>`;
}

function anchorAnchorList(r) {
  const blk = el("div", "cmp-block");
  blk.innerHTML = `<h3>逐锚点 offset 增量范围
    <span class="cmp-count">δ ∈ [lo_待校准 − hi_参照, hi_待校准 − lo_参照]，整数毫秒；按待校准事件 ID 排序</span></h3>`;
  const wrap = el("div", "anchor-rows");
  for (const a of r.anchors) {
    const det = a.determines
      ? `<span class="tag ${a.determines === "lower" ? "amb" : "conf"} anchor-det"
           title="该锚点决定交集的${a.determines === "lower" ? "下界" : "上界"}">
           ${a.determines === "lower" ? "⤒ 决定下界" : "⤓ 决定上界"}</span>`
      : "";
    const row = el("div", "anchor-row" + (a.determines ? " " + a.determines : ""));
    row.innerHTML = `
      <span class="anchor-idx">#${a.index}</span>
      <span class="chain-node" data-eid="${esc(a.calibrated.id)}" title="待校准事件">${esc(shortId(a.calibrated.id))}</span>
      <span class="chain-arrow">≈</span>
      <span class="chain-node" data-eid="${esc(a.reference.id)}" title="参照事件">${esc(shortId(a.reference.id))}</span>
      <span class="tag actor">👤 ${esc(a.actor)}</span>
      <span class="recon-um-type">${esc(a.event_type)}</span>
      <span class="anchor-dr" title="使两闭区间相交的 offset 增量范围">
        δ ${esc(a.delta_range_text)} <em>[${a.delta_lo_ms}, ${a.delta_hi_ms}] ms</em>
      </span>
      <span class="anchor-gap" title="当前（校准前）闭区间间距 / 待校准中点 − 参照中点">
        当前间距 ${a.current_closed_gap_ms} ms · 中点差 ${esc(anchorMsText(a.current_mid_delta_ms))}
      </span>
      ${det}`;
    wrap.appendChild(row);
  }
  blk.appendChild(wrap);
  return blk;
}

/* ------------------------------------------------------------ 绑定 */

anchor$("#anchor-run").addEventListener("click", anchorRun);
