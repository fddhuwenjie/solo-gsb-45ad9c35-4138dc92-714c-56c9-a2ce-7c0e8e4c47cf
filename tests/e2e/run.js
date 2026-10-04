/*
 * 端到端回归测试：jsdom 加载真实页面与 app.js，后端跑真实 Flask 服务。
 * 覆盖：求解后连续编辑（颜色/名称/锁定/增删角色/关系与门槛）立即清结果、
 * 禁用「存为版本」与导出；重新求解后恢复；版本保存/删除/恢复成对生效；
 * 服务端拒绝旧 result；导出 CSS 与当前配置一致。
 *
 * 运行：node tests/e2e/run.js
 */
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawn } = require("child_process");
const { JSDOM } = require("jsdom");

const ROOT = path.resolve(__dirname, "..", "..");
const net = require("net");

function pickFreePort() {
  return new Promise((resolve) => {
    const srv = net.createServer();
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

let PORT;
let BASE;
const DB = path.join(os.tmpdir(), `palette-e2e-${process.pid}-${Date.now()}.db`);

let failures = 0;
function assert(cond, msg) {
  if (!cond) {
    failures++;
    console.error(`  ✗ ${msg}`);
  } else {
    console.log(`  ✓ ${msg}`);
  }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitFor(fn, { timeout = 15000, label = "condition" } = {}) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    try {
      if (await fn()) return true;
    } catch { /* retry */ }
    await sleep(50);
  }
  throw new Error(`等待超时：${label}`);
}

async function api(pathname, opts) {
  const res = await fetch(BASE + pathname, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  return { status: res.status, data, headers: res.headers };
}

// ------------------------------------------------------------ 启动后端

// 容器/CI 里 Flask 通常在系统 site-packages；本地沙箱可能装在自定义前缀
const EXTRA_PY = process.env.FLASK_SITE_PACKAGES ||
  "/tmp/pyroot/local/lib/python3.11/dist-packages";

function startServer() {
  const env = {
    ...process.env,
    DATABASE_PATH: DB,
    PYTHONPATH: [ROOT, EXTRA_PY, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
  };
  const child = spawn("python3", ["-c", `
from app import server, storage
storage.init_db()
server.app.run(host="127.0.0.1", port=${PORT}, threaded=True)
`], { cwd: ROOT, env });
  child.stdout.on("data", () => {});
  child.stderr.on("data", (d) => process.stderr.write(`[server] ${d}`));
  return child;
}

async function waitForServer() {
  for (let i = 0; i < 100; i++) {
    try {
      const res = await fetch(BASE + "/api/versions");
      if (res.ok) return;
    } catch { /* not up yet */ }
    await sleep(100);
  }
  throw new Error("后端服务未启动");
}

// ------------------------------------------------------------ 加载页面

async function loadDom() {
  const html = fs.readFileSync(path.join(ROOT, "app", "static", "index.html"), "utf8");
  const appJs = fs.readFileSync(path.join(ROOT, "app", "static", "app.js"), "utf8");

  const dom = new JSDOM(html, {
    url: BASE + "/",
    runScripts: "outside-only",
    pretendToBeVisual: true,
    beforeParse(window) {
      // jsdom 无 fetch / structuredClone / URL 对象支持，用 Node 实现补齐
      window.fetch = (u, opts) => fetch(new URL(String(u), BASE).href, opts);
      window.structuredClone = (v) => JSON.parse(JSON.stringify(v));
      if (window.URL) {
        window.URL.createObjectURL = () => "blob:mock";
        window.URL.revokeObjectURL = () => {};
      }
      window.confirm = () => true;
      window.HTMLElement.prototype.click = function click() {
        this.dispatchEvent(new window.Event("click", { bubbles: true }));
      };
      window.onerror = (msg) => { failures++; console.error(`  ✗ window error: ${msg}`); };
    },
  });

  const { window } = dom;
  window.eval(appJs);
  return dom;
}

// ------------------------------------------------------------ DOM 辅助

const fire = (el, type, Ctor, win) =>
  el.dispatchEvent(new (Ctor || win.Event)(type, { bubbles: true }));

function roleRows(win) {
  return [...win.document.querySelectorAll("#roles-list .role-row")];
}
function relationRows(win) {
  return [...win.document.querySelectorAll("#relations-list .relation-row")];
}
function buttons(win) {
  return {
    solve: win.document.getElementById("btn-solve"),
    saveVersion: win.document.getElementById("btn-save-version"),
    export: win.document.getElementById("btn-export"),
  };
}
function resultVisible(win) {
  return !win.document.getElementById("result-card").hidden;
}
function previewVisible(win) {
  return !win.document.getElementById("preview-card").hidden;
}

function setColor(win, row, hex) {
  const picker = row.querySelector('input[type="color"]');
  picker.value = hex;
  fire(picker, "input", win.Event, win);
}
function setLock(win, row, checked) {
  const cb = row.querySelector('input[type="checkbox"]');
  cb.checked = checked;
  fire(cb, "change", win.Event, win);
}
function setRoleName(win, row, name) {
  const input = row.querySelector('input[type="text"]');
  input.value = name;
  fire(input, "input", win.Event, win);
}
function setThreshold(win, relRow, value) {
  const num = relRow.querySelector('input[type="number"]');
  num.value = String(value);
  fire(num, "input", win.Event, win);
}
function selectByLabel(win, select, value) {
  select.value = value;
  fire(select, "change", win.Event, win);
}

async function clickSolveAndWait(win) {
  buttons(win).solve.click();
  await waitFor(() => !buttons(win).solve.disabled, { label: "求解结束" });
  await sleep(50);
}

// ------------------------------------------------------------ 测试主体

async function run() {
  const createdVersionIds = [];

  console.log("初始自动求解（默认配置可行）");
  const dom = await loadDom();
  const { window } = dom;
  const $ = (id) => window.document.getElementById(id);

  await waitFor(() => resultVisible(window) && !buttons(window).saveVersion.disabled,
    { label: "初始求解结果渲染" });
  assert(resultVisible(window), "初始求解后展示结果表");
  assert(previewVisible(window), "初始求解后展示样张");
  assert(!buttons(window).saveVersion.disabled, "初始求解后「存为版本」可用");
  assert(!buttons(window).export.disabled, "初始求解后 CSS 导出可用");

  let rows = roleRows(window);
  const initialRoleCount = rows.length;
  let rels = relationRows(window);
  assert(initialRoleCount === 5, `默认 5 个角色（实际 ${initialRoleCount}）`);
  assert(rels.length === 4, `默认 4 条关系（实际 ${rels.length}）`);

  console.log("\n① 求解成功后连续编辑 → 立即清结果、禁用按钮");

  // 改颜色
  setColor(window, rows[1], "#000000");
  await sleep(0);
  assert(!resultVisible(window), "修改角色颜色后结果表立即清除");
  assert(!previewVisible(window), "修改角色颜色后样张立即清除");
  assert(buttons(window).saveVersion.disabled, "改颜色后「存为版本」禁用");
  assert(buttons(window).export.disabled, "改颜色后 CSS 导出禁用");
  assert($("status-banner").classList.contains("hidden"), "旧成功横幅隐藏");

  // 切锁定
  setLock(window, rows[2], true);
  assert(!resultVisible(window), "切换锁定状态后结果保持清除");
  assert(buttons(window).saveVersion.disabled, "切锁定后「存为版本」禁用");

  // 改名称（关系引用同步）
  const textNewName = "body-copy";
  setRoleName(window, rows[1], textNewName);
  assert(!resultVisible(window), "修改角色名称后结果清除");
  const fgSelect = relationRows(window)[0].querySelectorAll("select")[0];
  assert(fgSelect.value === textNewName, "重命名后前景引用同步更新");
  assert(buttons(window).export.disabled, "改名称后导出禁用");

  // 改门槛（第 0 条此刻已是 body-copy on background，#000 可达 7:1）
  setThreshold(window, relationRows(window)[0], 7);
  assert(!resultVisible(window), "调整对比度门槛后结果清除");

  // 改前景-背景关系（选择仍可行的重连）：
  //  - 第 2 条 primary on background 的前景换成 danger（danger 对白底已过 4.5）
  //  - 第 3 条 muted on background 的背景换成 body-copy（浅灰在黑底上过 3）
  const rel1 = relationRows(window)[1].querySelectorAll("select");
  assert(rel1[0].value === "primary" && rel1[1].value === "background",
    "第 2 条关系初始为 primary on background");
  selectByLabel(window, rel1[0], "danger");
  assert(!resultVisible(window), "调整前景角色后结果清除");
  assert(buttons(window).saveVersion.disabled, "调整前景角色后「存为版本」禁用");

  const rel2 = relationRows(window)[2].querySelectorAll("select");
  assert(rel2[0].value === "muted" && rel2[1].value === "background",
    "第 3 条关系初始为 muted on background");
  selectByLabel(window, rel2[1], textNewName);
  assert(!resultVisible(window), "调整背景角色后结果清除");
  assert(buttons(window).saveVersion.disabled, "调整背景角色后「存为版本」禁用");

  // 增/删角色
  $("btn-add-role").click();
  assert(roleRows(window).length === initialRoleCount + 1, "新增角色出现");
  assert(!resultVisible(window), "新增角色后结果清除");
  const addedRow = roleRows(window)[roleRows(window).length - 1];
  addedRow.querySelector(".icon-btn").click(); // 删除新角色
  assert(roleRows(window).length === initialRoleCount, "删除角色后行消失，连带关系处理");
  assert(buttons(window).export.disabled, "删除角色后导出禁用");

  // 增/删关系
  $("btn-add-relation").click();
  assert(relationRows(window).length === 5, "新增关系出现");
  assert(!resultVisible(window), "新增关系后结果清除");
  relationRows(window)[relationRows(window).length - 1].querySelector(".icon-btn").click();
  assert(relationRows(window).length === 4, "删除关系后行消失");
  assert(buttons(window).saveVersion.disabled, "删除关系后「存为版本」禁用");

  console.log("\n② 重新求解 → 结果恢复、按钮重新启用");
  await clickSolveAndWait(window);
  assert(resultVisible(window), "重新求解后结果表恢复");
  assert(previewVisible(window), "重新求解后样张恢复");
  assert(!buttons(window).saveVersion.disabled, "重新求解后「存为版本」恢复可用");
  assert(!buttons(window).export.disabled, "重新求解后导出恢复可用");
  const relResultBadges = [...$("rel-results").querySelectorAll(".ratio-badge.pass")];
  assert(relResultBadges.length >= 4, "新结果中各关系对比度通过标记显示");

  console.log("\n③ 存为版本：服务端绑定校验，SQLite 存权威结果");
  // 先抓一份当前页面配置对应的求解结果（通过服务端 API 模拟页面提交）
  $("version-label").value = "e2e-可行版本";
  buttons(window).saveVersion.click();
  await waitFor(async () => {
    const list = await api("/api/versions");
    return list.data.some((v) => v.label === "e2e-可行版本");
  }, { label: "版本出现在列表" });
  let versions = (await api("/api/versions")).data;
  const goodVersion = versions.find((v) => v.label === "e2e-可行版本");
  createdVersionIds.push(goodVersion.id);
  assert(!!goodVersion, "可行求解结果成功存为版本");

  // 直接打 API：连续编辑后的旧 result 必须被服务端拒绝（所有编辑类型抽样）
  const full = await api(`/api/versions/${goodVersion.id}`);
  const savedPayload = full.data.payload;
  const savedResult = full.data.result;

  const staleAttempts = [
    ["改颜色", (p) => { p.roles[1].hex = "#202020"; }],
    ["改名称", (p) => {
      const old = p.roles[1].name;
      p.roles[1].name = "renamed";
      p.relations.forEach((r) => { if (r.fg === old) r.fg = "renamed"; });
    }],
    ["切锁定", (p) => { p.roles[0].locked = !p.roles[0].locked; }],
    ["增角色", (p) => p.roles.push({ name: "extra", hex: "#999999", locked: false })],
    ["删角色", (p) => {
      const n = p.roles.pop().name;
      p.relations = p.relations.filter((r) => r.fg !== n && r.bg !== n);
    }],
    ["改门槛", (p) => { p.relations[0].min_contrast = 4.5; }],
    ["删关系", (p) => p.relations.pop()],
  ];
  for (const [tag, mutate] of staleAttempts) {
    const payload = JSON.parse(JSON.stringify(savedPayload));
    mutate(payload);
    const rv = await api("/api/versions", {
      method: "POST",
      body: JSON.stringify({ label: `stale-${tag}`, payload, result: savedResult }),
    });
    assert(rv.status === 400, `服务端拒绝${tag}后的旧 result`);
  }
  // 篡改结果字段
  const tampered = JSON.parse(JSON.stringify(savedResult));
  tampered.total_delta += 3;
  const rvTamper = await api("/api/versions", {
    method: "POST",
    body: JSON.stringify({ label: "tampered", payload: savedPayload, result: tampered }),
  });
  assert(rvTamper.status === 400, "服务端拒绝篡改 total_delta 的 result");

  // 导出同样拒绝旧 result（快照里 rel0 门槛为 7，改成 4.5 即错配）
  const stalePayload = JSON.parse(JSON.stringify(savedPayload));
  stalePayload.relations[0].min_contrast = 4.5;
  const rvStaleExport = await api("/api/export", {
    method: "POST",
    body: JSON.stringify({ payload: stalePayload, result: savedResult }),
  });
  assert(rvStaleExport.status === 400, "导出接口拒绝旧 result");

  console.log("\n④ 导出 CSS：与当前配置/快照一致");
  buttons(window).export.click();
  await waitFor(() => !$("export-preview").hidden, { label: "CSS 导出预览出现" });
  const cssText = $("export-preview").textContent;
  assert(cssText.includes(":root"), "导出内容为 CSS :root 块");
  for (const [, hexv] of Object.entries(savedResult.new_hex)) {
    assert(cssText.includes(hexv), `导出 CSS 含快照颜色 ${hexv}`);
  }
  assert(cssText.includes(`ΣΔL = ${savedResult.total_delta}`),
    "导出注释中的总改变量与快照一致");

  console.log("\n⑤ 编辑后未重新求解时导出按钮禁用（不能导出旧方案）");
  setColor(window, roleRows(window)[1], "#101010");
  assert(buttons(window).export.disabled, "编辑后导出立即禁用");
  assert(!resultVisible(window), "编辑后旧结果不展示");

  console.log("\n⑥ 恢复版本：配置与结果成对生效");
  await clickSolveAndWait(window); // 先回到一份与版本不同的当前状态
  window.document.body.dataset.restoredVersionId = "";
  // 点击版本列表中的恢复按钮（第一项即最新版本）
  const restoreBtn = $("versions-list").querySelector(".version-item .version-actions .icon-btn");
  restoreBtn.click();
  await waitFor(() => window.document.body.dataset.restoredVersionId === goodVersion.id,
    { label: "恢复动作完成" });
  assert(roleRows(window).length === savedPayload.roles.length,
    "恢复后角色配置与快照一致");
  assert(relationRows(window).length === savedPayload.relations.length,
    "恢复后关系配置与快照一致");
  const perRoleText = $("per-role-table").textContent;
  for (const [, hexv] of Object.entries(savedResult.new_hex)) {
    // 结果表直接使用 new_hex（含完整 6 位 HEX）
    assert(perRoleText.includes(hexv), `恢复后结果表含快照颜色 ${hexv}`);
  }
  assert(!buttons(window).saveVersion.disabled, "恢复成对版本后「存为版本」可用");
  assert(!buttons(window).export.disabled, "恢复成对版本后导出可用");

  console.log("\n⑦ 恢复不成对（旧/被改）版本：只恢复配置，结果作废");
  // 正常 API 写不进错配对：先确认拒绝
  const rvBad = await api("/api/versions", {
    method: "POST",
    body: JSON.stringify({ label: "e2e-错配版本", payload: stalePayload, result: savedResult }),
  });
  assert(rvBad.status === 400, "绑定校验阻止错配版本写入");

  // 直接绕过 API 往 SQLite 插一条配置与结果错配的历史脏快照（门槛 7 配旧结果）
  const dirtyInsert = await new Promise((resolve) => {
    const py = spawn("python3", ["-c", `
import json, sys
sys.path.insert(0, ${JSON.stringify(ROOT)})
from app import storage
payload = json.loads(${JSON.stringify(JSON.stringify(stalePayload))})
result = json.loads(${JSON.stringify(JSON.stringify(savedResult))})
meta = storage.create_version("e2e-错配版本", payload, result)
print(meta["id"])
`], {
      cwd: ROOT,
      env: {
        ...process.env,
        DATABASE_PATH: DB,
        PYTHONPATH: [ROOT, EXTRA_PY, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
      },
    });
    let out = "";
    py.stdout.on("data", (d) => { out += d; });
    py.stderr.on("data", (d) => process.stderr.write(`[py] ${d}`));
    py.on("close", () => resolve(out.trim()));
  });
  assert(/^[0-9a-f]{12}$/.test(dirtyInsert), "脏快照已直接写入 SQLite");
  createdVersionIds.push(dirtyInsert);

  // 页面上没有版本刷新按钮：重新加载页面以拉取最新列表，再走 UI 恢复
  const dom2 = await loadDom();
  const w2 = dom2.window;
  await waitFor(() => resultVisible(w2), { label: "第二页面初始结果渲染" });
  await waitFor(async () =>
    [...w2.document.querySelectorAll("#versions-list .v-label")]
      .some((el) => el.textContent === "e2e-错配版本"),
  { label: "脏快照出现在版本列表" });
  const dirtyLabel = [...w2.document.querySelectorAll("#versions-list .v-label")]
    .find((el) => el.textContent === "e2e-错配版本");
  w2.document.body.dataset.restoredVersionId = "";
  dirtyLabel.click();
  await waitFor(() => w2.document.body.dataset.restoredVersionId === dirtyInsert,
    { label: "错配版本恢复动作完成" });
  const dRows = w2.document.querySelectorAll("#roles-list .role-row").length;
  assert(dRows === stalePayload.roles.length, "恢复错配版本时配置仍生效（角色数正确）");
  assert(!resultVisible(w2), "恢复错配版本后旧结果不展示");
  assert(!previewVisible(w2), "恢复错配版本后旧样张不展示");
  assert(w2.document.getElementById("btn-save-version").disabled,
    "恢复错配版本后「存为版本」禁用");
  assert(w2.document.getElementById("btn-export").disabled,
    "恢复错配版本后导出禁用");
  assert(w2.document.getElementById("status-banner").textContent.includes("重新求解"),
    "横幅提示配置与结果不匹配、需重新求解");
  w2.close();

  console.log("\n⑧ 删除版本后列表刷新");
  const before = (await api("/api/versions")).data.length;
  const delRes = await api(`/api/versions/${goodVersion.id}`, { method: "DELETE" });
  assert(delRes.status === 200, "删除版本返回 200");
  const after = (await api("/api/versions")).data.length;
  assert(after === before - 1, "版本列表中该版本消失");
  for (const id of createdVersionIds) {
    await api(`/api/versions/${id}`, { method: "DELETE" });
  }

  window.close();
}

async function main() {
  PORT = process.env.TEST_PORT || String(await pickFreePort());
  BASE = `http://127.0.0.1:${PORT}`;
  const serverProc = startServer();
  let code = 0;
  try {
    await waitForServer();
    await run();
  } catch (err) {
    failures++;
    console.error(err);
  } finally {
    serverProc.kill("SIGTERM");
    for (const ext of ["", "-wal", "-shm"]) {
      try { fs.unlinkSync(DB + ext); } catch { /* ignore */ }
    }
  }
  if (failures) {
    console.error(`\n${failures} 项断言失败`);
    code = 1;
  } else {
    console.log("\n全部通过 ✅");
  }
  process.exit(code);
}

main();
