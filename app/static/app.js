/* 多约束无障碍调色台前端
 * 颜色转换函数与后端 app/color.py 保持逐行等价。
 */
"use strict";

// ------------------------------------------------------------ 颜色工具

function normalizeHex(value) {
  let s = String(value ?? "").trim().toLowerCase();
  if (s.startsWith("#")) s = s.slice(1);
  if (s.length === 3) s = s.split("").map((c) => c + c).join("");
  if (!/^[0-9a-f]{6}$/.test(s)) throw new Error(`无法识别的十六进制颜色：${value}`);
  return "#" + s;
}

function hexToRgb(hex) {
  const s = normalizeHex(hex).slice(1);
  return [parseInt(s.slice(0, 2), 16), parseInt(s.slice(2, 4), 16), parseInt(s.slice(4, 6), 16)];
}

function hueToChannel(p, q, t) {
  if (t < 0) t += 1;
  if (t > 1) t -= 1;
  if (t < 1 / 6) return p + (q - p) * 6 * t;
  if (t < 1 / 2) return q;
  if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
  return p;
}

function hslToRgb(h, s, l) {
  h = ((h % 1) + 1) % 1;
  if (s === 0) return [l * 255, l * 255, l * 255];
  const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
  const p = 2 * l - q;
  return [
    hueToChannel(p, q, h + 1 / 3) * 255,
    hueToChannel(p, q, h) * 255,
    hueToChannel(p, q, h - 1 / 3) * 255,
  ];
}

function rgbToHsl(r, g, b) {
  r /= 255; g /= 255; b /= 255;
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b);
  const l = (mx + mn) / 2;
  if (mx === mn) return [0, 0, l];
  const d = mx - mn;
  const s = l > 0.5 ? d / (2 - mx - mn) : d / (mx + mn);
  let h;
  if (mx === r) h = (g - b) / d + (g < b ? 6 : 0);
  else if (mx === g) h = (b - r) / d + 2;
  else h = (r - g) / d + 4;
  return [h / 6, s, l];
}

function hexToHslInt(hex) {
  const [h, s, l] = rgbToHsl(...hexToRgb(hex));
  // 与 Python 端一致的 floor(v+0.5) 取整（数值均为非负）
  return [
    Math.min(359, Math.floor(h * 360 + 0.5)),
    Math.min(100, Math.floor(s * 100 + 0.5)),
    Math.min(100, Math.floor(l * 100 + 0.5)),
  ];
}

function hslIntToHex(h, s, l) {
  const [r, g, b] = hslToRgb(h / 360, s / 100, l / 100);
  const ch = (x) => Math.max(0, Math.min(255, Math.round(x))).toString(16).padStart(2, "0");
  return `#${ch(r)}${ch(g)}${ch(b)}`;
}

function srgbToLinear(c) {
  c /= 255;
  return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
}

function relativeLuminance(rgb) {
  const [r, g, b] = rgb.map(srgbToLinear);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrastHex(c1, c2) {
  const l1 = relativeLuminance(hexToRgb(c1));
  const l2 = relativeLuminance(hexToRgb(c2));
  const hi = Math.max(l1, l2), lo = Math.min(l1, l2);
  return (hi + 0.05) / (lo + 0.05);
}

// ------------------------------------------------------------ 状态

const DEFAULT_PALETTE = {
  roles: [
    { name: "background", hex: "#ffffff", locked: true },
    { name: "text", hex: "#6b7280", locked: false },
    { name: "primary", hex: "#2563eb", locked: false },
    { name: "muted", hex: "#9aa1ab", locked: false },
    { name: "danger", hex: "#dc2626", locked: false },
  ],
  relations: [
    { fg: "text", bg: "background", min_contrast: 4.5 },
    { fg: "primary", bg: "background", min_contrast: 4.5 },
    { fg: "muted", bg: "background", min_contrast: 3 },
    { fg: "danger", bg: "background", min_contrast: 3 },
  ],
};

let state = structuredClone(DEFAULT_PALETTE);
let lastResult = null;

const $ = (id) => document.getElementById(id);

function toast(message, kind = "") {
  const el = $("toast");
  el.textContent = message;
  el.className = `toast ${kind}`;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, 2600);
}

// ------------------------------------------------------------ 渲染：角色

function renderRoles() {
  const list = $("roles-list");
  list.innerHTML = "";
  state.roles.forEach((role, i) => {
    let hsl;
    try { hsl = hexToHslInt(role.hex); } catch { hsl = [0, 0, 0]; }

    const row = document.createElement("div");
    row.className = "role-row" + (role.locked ? " locked" : "");

    const colorWrap = document.createElement("label");
    colorWrap.className = "role-color";
    const picker = document.createElement("input");
    picker.type = "color";
    picker.value = /^#[0-9a-f]{6}$/i.test(role.hex) ? role.hex : "#000000";
    picker.title = "选择颜色";
    picker.addEventListener("input", () => {
      role.hex = picker.value.toLowerCase();
      renderRoles();
    });
    colorWrap.appendChild(picker);

    const main = document.createElement("div");
    main.className = "role-main";
    const nameRow = document.createElement("div");
    nameRow.className = "role-name-row";
    const nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.value = role.name;
    nameInput.placeholder = "角色名，如 text / primary";
    nameInput.addEventListener("input", () => {
      const oldName = role.name;
      role.name = nameInput.value;
      hexCode.textContent = role.hex;
      // 同步关系中的角色引用，避免重命名后出现悬空角色
      if (oldName !== role.name) {
        for (const rel of state.relations) {
          if (rel.fg === oldName) rel.fg = role.name;
          if (rel.bg === oldName) rel.bg = role.name;
        }
        renderRelations();
      }
    });
    const hexCode = document.createElement("span");
    hexCode.className = "role-hex-code";
    hexCode.textContent = role.hex;
    nameRow.append(nameInput, hexCode);
    const hslLine = document.createElement("div");
    hslLine.className = "role-hsl";
    hslLine.textContent = `H ${hsl[0]}  S ${hsl[1]}%  L ${hsl[2]}%`;
    main.append(nameRow, hslLine);

    const lock = document.createElement("label");
    lock.className = "role-lock";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = role.locked;
    cb.title = "锁定后该角色明度不参与求解";
    cb.addEventListener("change", () => {
      role.locked = cb.checked;
      renderRoles();
    });
    lock.append(cb, document.createTextNode(role.locked ? "🔒 锁定" : "锁定"));

    const del = document.createElement("button");
    del.className = "icon-btn";
    del.textContent = "🗑";
    del.title = "删除角色（相关关系会一并移除）";
    del.addEventListener("click", () => {
      const name = role.name;
      state.roles.splice(i, 1);
      state.relations = state.relations.filter((r) => r.fg !== name && r.bg !== name);
      renderAll();
    });

    row.append(colorWrap, main, lock, del);
    list.appendChild(row);
  });
}

// ------------------------------------------------------------ 渲染：关系

function roleOptions(selected) {
  return state.roles
    .map((r) => `<option value="${escapeHtml(r.name)}"${r.name === selected ? " selected" : ""}>${escapeHtml(r.name)}</option>`)
    .join("");
}

function renderRelations() {
  const list = $("relations-list");
  list.innerHTML = "";
  const conflictSet = new Set(
    lastResult?.status === "infeasible"
      ? (lastResult.groups?.flatMap((g) => g.indices) ?? lastResult.conflict_indices ?? [])
      : []
  );

  state.relations.forEach((rel, i) => {
    const row = document.createElement("div");
    row.className = "relation-row" + (conflictSet.has(i) ? " conflict" : "");

    const fg = document.createElement("select");
    fg.innerHTML = roleOptions(rel.fg);
    fg.addEventListener("change", () => { rel.fg = fg.value; });

    const arrow = document.createElement("div");
    arrow.className = "rel-arrow";
    arrow.textContent = "on";

    const bg = document.createElement("select");
    bg.innerHTML = roleOptions(rel.bg);
    bg.addEventListener("change", () => { rel.bg = bg.value; });

    const minWrap = document.createElement("div");
    minWrap.className = "rel-min";
    const num = document.createElement("input");
    num.type = "number";
    num.min = "1"; num.max = "21"; num.step = "0.1";
    num.value = rel.min_contrast;
    num.addEventListener("input", () => { rel.min_contrast = parseFloat(num.value) || 0; });
    minWrap.append(num, document.createElement("span"));
    minWrap.lastChild.textContent = ":1";

    const del = document.createElement("button");
    del.className = "icon-btn";
    del.textContent = "✕";
    del.title = "删除关系";
    del.addEventListener("click", () => {
      state.relations.splice(i, 1);
      renderRelations();
    });

    row.append(fg, arrow, bg, minWrap, del);
    list.appendChild(row);
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ------------------------------------------------------------ 求解

async function runSolve(showSuccess = true) {
  const banner = $("status-banner");
  banner.className = "banner hidden";

  // 本地预检
  const names = new Set();
  for (const r of state.roles) {
    const name = r.name.trim();
    if (!name) return showBanner("error", "存在没有名称的角色。");
    if (names.has(name)) return showBanner("error", `角色名「${name}」重复。`);
    names.add(name);
    try { normalizeHex(r.hex); } catch (e) { return showBanner("error", `角色「${name}」：${e.message}`); }
  }
  for (const rel of state.relations) {
    if (!(rel.min_contrast >= 1 && rel.min_contrast <= 21)) {
      return showBanner("error", `关系「${rel.fg} on ${rel.bg}」的最低对比度需在 1~21 之间。`);
    }
  }

  setBusy(true);
  try {
    const res = await fetch("/api/solve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state),
    });
    const data = await res.json();
    if (!res.ok) {
      showBanner("error", data.error || "求解失败");
      return;
    }
    lastResult = data;
    if (data.status === "optimal") {
      renderResult(data);
      renderPreview(data);
      hideConflicts();
      if (showSuccess) {
        const proven = data.proven_optimal ? "" : "（搜索预算内最优）";
        showBanner("success",
          `✅ 已找到满足全部 ${data.per_relation.length} 条关系的调色方案，` +
          `总明度改变量 ΣΔL = ${data.total_delta}${proven}。`);
      }
      $("btn-save-version").disabled = false;
      $("btn-export").disabled = false;
    } else if (data.status === "infeasible") {
      renderConflicts(data);
      clearResult();
      showBanner("error", "❌ 没有能同时满足全部约束的配色，下面列出冲突约束。");
      $("btn-save-version").disabled = true;
      $("btn-export").disabled = true;
    } else {
      showBanner("warn", "搜索空间过大，预算耗尽未完成求解。可减少角色数或锁定更多角色。");
      clearResult();
      hideConflicts();
    }
  } catch (err) {
    showBanner("error", "网络错误：" + err.message);
  } finally {
    setBusy(false);
    renderRelations();
  }
}

function setBusy(busy) {
  const btn = $("btn-solve");
  btn.disabled = busy;
  btn.textContent = busy ? "求解中…" : "求解调色";
}

function showBanner(kind, text) {
  const banner = $("status-banner");
  banner.textContent = text;
  banner.className = `banner ${kind}`;
}

function clearResult() {
  $("result-card").hidden = true;
  $("preview-card").hidden = true;
  $("result-meta").innerHTML = "";
  $("per-role-table").innerHTML = "";
  $("rel-results").innerHTML = "";
  $("preview-area").innerHTML = "";
  $("export-preview").hidden = true;
}

function hideConflicts() {
  $("conflict-card").hidden = true;
  $("conflict-area").innerHTML = "";
}

// ------------------------------------------------------------ 渲染：结果表

function renderResult(data) {
  $("result-card").hidden = false;
  $("result-meta").innerHTML =
    `ΣΔL <strong>${data.total_delta}</strong> · 搜索节点 ${data.nodes}`;

  const table = $("per-role-table");
  table.innerHTML = "";
  for (const r of data.per_role) {
    const row = document.createElement("div");
    row.className = "pr-row";
    const dClass = r.delta > 0 ? "pos" : r.delta < 0 ? "neg" : "zero";
    const dText = r.delta > 0 ? `+${r.delta}` : `${r.delta}`;
    row.innerHTML = `
      <span class="pr-swatch" style="background:${r.new_hex}"></span>
      <span class="pr-name">${escapeHtml(r.name)}${r.locked ? '<span class="lock-tag">🔒</span>' : ""}</span>
      <span class="pr-cell">${r.old_hex}</span>
      <span class="pr-cell">${r.new_hex}</span>
      <span class="pr-delta ${dClass}">L ${r.l_before}→${r.l_after} (${dText})</span>
    `;
    table.appendChild(row);
  }

  const relBox = $("rel-results");
  relBox.innerHTML = "";
  for (const rel of data.per_relation) {
    const row = document.createElement("div");
    row.className = "rr-row";
    row.innerHTML = `
      <span class="rr-pair">
        <span class="rr-swatch" style="background:${data.new_hex[rel.fg]}"></span>
        <span>${escapeHtml(rel.fg)}</span>
        <span class="rr-meta">on</span>
        <span class="rr-swatch" style="background:${data.new_hex[rel.bg]}"></span>
        <span>${escapeHtml(rel.bg)}</span>
      </span>
      <span class="rr-badges">
        <span class="ratio-badge ${rel.before >= rel.min_contrast ? "pass" : "fail"}">${rel.before.toFixed(2)}:1</span>
        <span class="ratio-arrow">→</span>
        <span class="ratio-badge ${rel.passes ? "pass" : "fail"}">${rel.after.toFixed(2)}:1</span>
        <span class="rr-meta">≥ ${rel.min_contrast}:1</span>
      </span>`;
    relBox.appendChild(row);
  }
}

// ------------------------------------------------------------ 渲染：样张

function renderPreview(data) {
  $("preview-card").hidden = false;
  const area = $("preview-area");
  area.innerHTML = "";

  data.per_relation.forEach((rel, idx) => {
    const fg = data.new_hex[rel.fg];
    const bg = data.new_hex[rel.bg];
    const sample = document.createElement("div");
    sample.className = "sample";
    sample.style.background = bg;
    sample.style.color = fg;
    sample.innerHTML = `
      <h3>「${escapeHtml(rel.fg)}」置于「${escapeHtml(rel.bg)}」之上</h3>
      <p>无障碍设计要求前景文字与其背景之间具备足够的明度对比，
         以便弱视用户在不同光照环境下都能阅读内容。</p>
      <p class="sample-small">小号正文样例 · The quick brown fox jumps over 0123456789</p>
      <div class="chips">
        <span class="chip" style="color:${fg};border-color:${fg}55">按钮 / Button</span>
        <span class="chip" style="color:${fg};border-color:${fg}55">链接文字</span>
        <span class="chip sample-tag" style="color:${fg};border-color:${fg}44">
          ${rel.before.toFixed(2)}:1 → ${rel.after.toFixed(2)}:1（要求 ≥ ${rel.min_contrast}:1）
        </span>
      </div>`;
    area.appendChild(sample);
  });
}

// ------------------------------------------------------------ 渲染：冲突

function renderConflicts(data) {
  $("conflict-card").hidden = false;
  const area = $("conflict-area");
  area.innerHTML = "";

  const groups = data.groups ?? [{ indices: data.conflict_indices, relations: data.conflicts, reasons: data.reasons }];
  groups.forEach((group, gi) => {
    const card = document.createElement("div");
    card.className = "conflict-group";
    const title = document.createElement("h3");
    title.textContent = groups.length > 1 ? `冲突簇 ${gi + 1}（${group.indices.length} 条关系）`
      : `极小冲突集（${group.indices.length} 条关系）`;
    card.appendChild(title);

    group.relations.forEach((rel) => {
      const row = document.createElement("div");
      row.className = "conflict-rel";
      row.innerHTML = `<span class="rr-swatch" style="background:${roleHex(rel.fg)}"></span>
        <strong>${escapeHtml(rel.fg)}</strong> on
        <span class="rr-swatch" style="background:${roleHex(rel.bg)}"></span>
        <strong>${escapeHtml(rel.bg)}</strong>
        <span class="rr-meta">要求 ≥ ${rel.min_contrast}:1</span>`;
      card.appendChild(row);
      const reason = group.reasons?.[String(rel.index)] ?? data.reasons?.[String(rel.index)];
      if (reason) {
        const rs = document.createElement("div");
        rs.className = "conflict-reason";
        rs.textContent = "⚠ " + reason;
        card.appendChild(rs);
      }
    });

    const advice = document.createElement("div");
    advice.className = "conflict-advice";
    advice.textContent = "建议：降低其中某条对比度门槛、解锁相关角色，或调整其初始色相/饱和度后重试。";
    card.appendChild(advice);
    area.appendChild(card);
  });
}

function roleHex(name) {
  const r = state.roles.find((x) => x.name === name);
  return r ? r.hex : "#888888";
}

// ------------------------------------------------------------ 配置与版本

async function saveConfig(silent = false) {
  try {
    const res = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "保存失败");
    if (!silent) toast("配置已保存", "success");
  } catch (err) {
    toast(err.message, "error");
  }
}

async function saveVersion() {
  if (!lastResult || lastResult.status !== "optimal") return;
  const label = $("version-label").value.trim() ||
    `版本 ${new Date().toLocaleString("zh-CN", { hour12: false })}`;
  try {
    const res = await fetch("/api/versions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label, payload: state, result: lastResult }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "保存版本失败");
    $("version-label").value = "";
    toast("已存为版本", "success");
    loadVersions();
  } catch (err) {
    toast(err.message, "error");
  }
}

async function loadVersions() {
  const res = await fetch("/api/versions");
  const versions = await res.json();
  const ul = $("versions-list");
  ul.innerHTML = "";
  if (!versions.length) {
    const li = document.createElement("li");
    li.className = "hint";
    li.textContent = "暂无保存的版本。";
    ul.appendChild(li);
    return;
  }
  for (const v of versions) {
    const li = document.createElement("li");
    li.className = "version-item";
    const info = document.createElement("div");
    const label = document.createElement("div");
    label.className = "v-label";
    label.textContent = v.label;
    label.title = "点击恢复此版本";
    label.addEventListener("click", () => restoreVersion(v.id));
    const time = document.createElement("time");
    time.textContent = new Date(v.created_at).toLocaleString("zh-CN", { hour12: false });
    info.append(label, time);

    const actions = document.createElement("div");
    actions.className = "version-actions";
    const restore = document.createElement("button");
    restore.className = "icon-btn";
    restore.textContent = "↺";
    restore.title = "恢复";
    restore.addEventListener("click", () => restoreVersion(v.id));
    const del = document.createElement("button");
    del.className = "icon-btn";
    del.textContent = "🗑";
    del.title = "删除版本";
    del.addEventListener("click", () => deleteVersion(v.id));
    actions.append(restore, del);

    li.append(info, actions);
    ul.appendChild(li);
  }
}

async function restoreVersion(vid) {
  const res = await fetch(`/api/versions/${vid}`);
  if (!res.ok) return toast("版本不存在", "error");
  const v = await res.json();
  state = v.payload;
  lastResult = v.result;
  renderAll();
  if (lastResult?.status === "optimal") {
    renderResult(lastResult);
    renderPreview(lastResult);
    $("btn-save-version").disabled = false;
    $("btn-export").disabled = false;
    showBanner("success", `已恢复版本「${v.label}」的配置与结果。`);
  } else {
    clearResult();
    hideConflicts();
    showBanner("success", `已恢复版本「${v.label}」的配置，请重新求解。`);
  }
  toast(`已恢复「${v.label}」`, "success");
}

async function deleteVersion(vid) {
  if (!confirm("确定删除这个版本？")) return;
  const res = await fetch(`/api/versions/${vid}`, { method: "DELETE" });
  if (!res.ok) return toast("删除失败", "error");
  loadVersions();
  toast("版本已删除");
}

// ------------------------------------------------------------ 导出 CSS

async function exportCss() {
  if (!lastResult || lastResult.status !== "optimal") return;
  const res = await fetch("/api/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ result: lastResult }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    return toast(data.error || "导出失败", "error");
  }
  const css = await res.text();
  const preview = $("export-preview");
  preview.textContent = css;
  preview.hidden = false;

  const blob = new Blob([css], { type: "text/css" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "variables.css";
  a.click();
  URL.revokeObjectURL(url);
  toast("variables.css 已下载", "success");
}

// ------------------------------------------------------------ 初始化

function renderAll() {
  renderRoles();
  renderRelations();
}

function bindEvents() {
  $("btn-add-role").addEventListener("click", () => {
    state.roles.push({ name: `role-${state.roles.length + 1}`, hex: "#888888", locked: false });
    renderRoles();
  });
  $("btn-add-relation").addEventListener("click", () => {
    if (state.roles.length < 2) return toast("请先创建至少两个角色", "error");
    state.relations.push({
      fg: state.roles[0].name,
      bg: state.roles[1]?.name || state.roles[0].name,
      min_contrast: 4.5,
    });
    renderRelations();
  });
  $("btn-solve").addEventListener("click", () => runSolve(true));
  $("btn-save-config").addEventListener("click", () => saveConfig(false));
  $("btn-save-version").addEventListener("click", saveVersion);
  $("btn-export").addEventListener("click", exportCss);
}

async function init() {
  bindEvents();
  try {
    const res = await fetch("/api/config");
    const saved = await res.json();
    if (saved && Array.isArray(saved.roles) && saved.roles.length) {
      state = saved;
    }
  } catch { /* 首次启动使用默认样例 */ }
  renderAll();
  loadVersions();
  runSolve(false);
}

init();
