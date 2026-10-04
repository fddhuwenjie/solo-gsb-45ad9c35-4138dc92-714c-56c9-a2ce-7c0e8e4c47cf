/*
 * 前端回归测试：连续编辑后旧结果立即作废、保存/恢复成对生效、导出与快照一致。
 *
 * 用 jsdom 加载真实的 index.html + app.js，后端是同进程里跑的真实 Flask
 * （临时 SQLite），通过全局 fetch 访问，不 mock 任何业务逻辑。
 *
 * 运行（仓库根目录）：
 *   node tests/js/frontend.test.js
 * 或：npm --prefix tests/js test
 */
"use strict";

const { spawn } = require("child_process");
const fs = require("fs");
const net = require("net");
const path = require("path");

const { JSDOM } = require("jsdom");

const ROOT = path.resolve(__dirname, "..", "..");
const DB_PATH = path.join(__dirname, "frontend-test.db");

// ------------------------------------------------------------ 启动真实后端

function freePort() {
  return new Promise((resolve) => {
    const srv = net.createServer();
    srv.listen(0, "127.0.0.1", () => {
      const port = srv.address().port;
      srv.close(() => resolve(port));
    });
  });
}

async function startServer() {
  for (const f of [DB_PATH, `${DB_PATH}-wal`, `${DB_PATH}-shm`]) {
    try { fs.unlinkSync(f); } catch { /* 不存在即忽略 */ }
  }
  const port = await freePort();
  const py = process.env.PYTHON || "python3";
  const userSite = process.env.PYTHON_USER_SITE || "";
  const env = {
    ...process.env,
    DATABASE_PATH: DB_PATH,
    FLASK_RUN_PORT: String(port),
    PYTHONPATH: [ROOT, userSite, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
  };
  const proc = spawn(py, ["-c", `
from app import server, storage
storage.init_db()
server.app.run(host="127.0.0.1", port=${port}, threaded=True, use_reloader=False)
`], { cwd: ROOT, env, stdio: ["ignore", "pipe", "pipe"] });

  let started = false;
  proc.stderr.on("data", (d) => { if (String(d).includes("Running on")) started = true; });
  for (let i = 0; i < 100; i++) {
    if (started) break;
    await new Promise((r) => setTimeout(r, 100));
  }
  // 再发一个请求确认服务就绪
  for (let i = 0; i < 50; i++) {
    try {
      const res = await fetch(`http://127.0.0.1:${port}/api/config`);
      if (res.ok) break;
    } catch { /* 尚未就绪 */ }
    await new Promise((r) => setTimeout(r, 100));
  }
  return { baseUrl: `http://127.0.0.1:${port}`, proc };
}

// ------------------------------------------------------------ 极简断言

let passed = 0;
const failures = [];

function check(name, cond, detail = "") {
  if (cond) {
    passed += 1;
    console.log(`  ✓ ${name}`);
  } else {
    failures.push(`${name}${detail ? ` — ${detail}` : ""}`);
    console.log(`  ✗ ${name} ${detail}`);
  }
}

// ------------------------------------------------------------ 加载页面

async function loadPage(baseUrl) {
  const indexHtml = fs.readFileSync(path.join(ROOT, "app", "static", "index.html"), "utf8");
  const appJs = fs.readFileSync(path.join(ROOT, "app", "static", "app.js"), "utf8");

  const dom = new JSDOM(indexHtml.replace(/<script[\s\S]*?<\/script>/, ""), {
    url: `${baseUrl}/`,
    runScripts: "outside-only",
    pretendToBeVisual: true,
  });
  const { window } = dom;
  window.fetch = (url, opts) => fetch(new URL(url, baseUrl).href, opts);
  window.structuredClone = (v) => JSON.parse(JSON.stringify(v));
  window.scrollTo = () => {};
  // jsdom 未实现这些 API，测试不触发下载/确认
  window.HTMLAnchorElement.prototype.click = function click() { this._clicked = true; };
  window.confirm = () => true;
  let downloadUrl = null;
  window.URL.createObjectURL = () => (downloadUrl = "blob:stub");
  window.URL.revokeObjectURL = () => { downloadUrl = null; };
  window.Blob = class Blob { constructor(parts) { this.parts = parts; } };

  window.eval(appJs);
  // 等待 init() 中的 /api/config 与 runSolve(false) 结束
  for (let i = 0; i < 100; i++) {
    if (!window.document.getElementById("result-card").hidden &&
        window.document.getElementById("per-role-table").children.length) break;
    await new Promise((r) => setTimeout(r, 50));
  }
  return { dom, window, document: window.document, getDownloaded: () => downloadUrl };
}

// ------------------------------------------------------------ 主流程

function setColor(document, rowIndex, hex) {
  const picker = document.querySelectorAll("#roles-list input[type=color]")[rowIndex];
  picker.value = hex;
  picker.dispatchEvent(new document.defaultView.Event("input", { bubbles: true }));
}
function setRoleName(document, rowIndex, value) {
  const input = document.querySelectorAll("#roles-list .role-name-row input[type=text]")[rowIndex];
  input.value = value;
  input.dispatchEvent(new document.defaultView.Event("input", { bubbles: true }));
}
function toggleLock(document, rowIndex, checked) {
  const cb = document.querySelectorAll("#roles-list input[type=checkbox]")[rowIndex];
  cb.checked = checked;
  cb.dispatchEvent(new document.defaultView.Event("change", { bubbles: true }));
}
function clickNth(elements, n) {
  const el = elements[n];
  el.dispatchEvent(new el.ownerDocument.defaultView.Event("click", { bubbles: true }));
}
function setRelationSelect(document, relIndex, which, value) {
  const row = document.querySelectorAll("#relations-list .relation-row")[relIndex];
  const selects = row.querySelectorAll("select");
  const sel = which === "fg" ? selects[0] : selects[1];
  sel.value = value;
  sel.dispatchEvent(new document.defaultView.Event("change", { bubbles: true }));
}
function setThreshold(document, relIndex, value) {
  const num = document.querySelectorAll("#relations-list input[type=number]")[relIndex];
  num.value = String(value);
  num.dispatchEvent(new document.defaultView.Event("input", { bubbles: true }));
}

async function waitFor(fn, tries = 100) {
  for (let i = 0; i < tries; i++) {
    if (fn()) return true;
    await new Promise((r) => setTimeout(r, 50));
  }
  return false;
}

// ------------------------------------------------------------ 主流程

async function main() {
  const { baseUrl, proc } = await startServer();

  try {
    // ============ 场景 1：求解成功后连续编辑，旧结果立即作废 ============
    console.log("场景 1：连续编辑使旧结果立即作废");
    let { document, window } = await loadPage(baseUrl);

    check("初始求解后展示结果卡片", !document.getElementById("result-card").hidden);
    check("初始求解后展示样张卡片", !document.getElementById("preview-card").hidden);
    check("初始后「存为版本」可用", !document.getElementById("btn-save-version").disabled);
    check("初始后「导出」可用", !document.getElementById("btn-export").disabled);

    function assertStale(label) {
      check(`[${label}] 结果卡片已隐藏`, document.getElementById("result-card").hidden);
      check(`[${label}] 样张卡片已隐藏`, document.getElementById("preview-card").hidden);
      check(`[${label}] 结果表已清空`, document.getElementById("per-role-table").children.length === 0);
      check(`[${label}] 「存为版本」已禁用`, document.getElementById("btn-save-version").disabled);
      check(`[${label}] 「导出 CSS」已禁用`, document.getElementById("btn-export").disabled);
      check(`[${label}] 提示重新求解`, /重新求解/.test(document.getElementById("status-banner").textContent));
    }

    // 改颜色
    setColor(document, 1, "#123456");
    assertStale("改颜色");

    // 重新求解 → 恢复
    document.getElementById("btn-solve").dispatchEvent(new document.defaultView.Event("click", { bubbles: true }));
    await waitFor(() => !document.getElementById("result-card").hidden &&
      document.getElementById("per-role-table").children.length);
    check("重新求解后结果恢复展示", !document.getElementById("result-card").hidden);
    check("重新求解后「存为版本」恢复可用", !document.getElementById("btn-save-version").disabled);

    // 改名称（关系引用会同步）
    setRoleName(document, 1, "body-text");
    assertStale("改名称");
    check("重命名后关系下拉同步更新",
      document.querySelectorAll("#relations-list select")[0].value === "body-text");

    // 改锁定
    toggleLock(document, 2, true);
    assertStale("改锁定");

    // 改前景（第一条关系）
    setRelationSelect(document, 0, "fg", "primary");
    assertStale("改前景");

    // 抬门槛
    setThreshold(document, 0, 7);
    assertStale("抬门槛");

    // 新增关系
    document.getElementById("btn-add-relation").dispatchEvent(new document.defaultView.Event("click", { bubbles: true }));
    assertStale("增关系");

    // 新增角色
    document.getElementById("btn-add-role").dispatchEvent(new document.defaultView.Event("click", { bubbles: true }));
    assertStale("增角色");

    // 删角色（最后一个，就是刚新增的）
    const roleDelBtns = document.querySelectorAll("#roles-list .icon-btn");
    clickNth(roleDelBtns, roleDelBtns.length - 1);
    assertStale("删角色");

    // 删关系
    const relDelBtns = document.querySelectorAll("#relations-list .icon-btn");
    clickNth(relDelBtns, relDelBtns.length - 1);
    assertStale("删关系");

    // ============ 场景 2：保存版本 → 恢复 → 配置与结果成对生效 ============
    console.log("场景 2：保存、恢复成对生效");
    ({ document, window } = await loadPage(baseUrl));
    await waitFor(() => !document.getElementById("result-card").hidden);

    document.getElementById("version-label").value = "回归基线";
    document.getElementById("btn-save-version").dispatchEvent(new document.defaultView.Event("click", { bubbles: true }));
    const versionSaved = await waitFor(() =>
      document.getElementById("versions-list").querySelectorAll(".v-label").length > 0);
    check("版本出现在列表中", versionSaved);
    const labelEl = document.querySelectorAll("#versions-list .v-label")[0];
    check("版本标签正确", labelEl.textContent === "回归基线", labelEl.textContent);

    // 记录保存时的结果
    const savedRows = [...document.querySelectorAll("#per-role-table .pr-name")]
      .map((e) => e.textContent.trim());

    // 编辑使结果作废
    setColor(document, 1, "#0f172a");
    check("编辑后结果已作废", document.getElementById("result-card").hidden);

    // 恢复版本：配置与结果一起回来
    document.querySelectorAll("#versions-list .v-label")[0]
      .dispatchEvent(new document.defaultView.Event("click", { bubbles: true }));
    await waitFor(() => !document.getElementById("result-card").hidden);
    check("恢复后结果卡片重新出现", !document.getElementById("result-card").hidden);
    check("恢复后样张重新出现", !document.getElementById("preview-card").hidden);
    check("恢复后「存为版本」可用", !document.getElementById("btn-save-version").disabled);
    check("恢复后「导出」可用", !document.getElementById("btn-export").disabled);
    const restoredColor = document.querySelectorAll("#roles-list input[type=color]")[1].value;
    check("恢复后角色颜色回到快照配置", restoredColor === "#6b7280", restoredColor);
    const restoredRows = [...document.querySelectorAll("#per-role-table .pr-name")]
      .map((e) => e.textContent.trim());
    check("恢复后结果表与保存时一致", JSON.stringify(restoredRows) === JSON.stringify(savedRows));
    check("恢复成功提示配置与结果成对恢复",
      /配置与结果/.test(document.getElementById("status-banner").textContent));

    // ============ 场景 3：恢复后继续编辑再次作废（回归闭环） ============
    console.log("场景 3：恢复后编辑再次作废，且服务端拒绝旧快照");
    setColor(document, 2, "#111111");
    check("恢复后编辑，结果立即作废", document.getElementById("result-card").hidden);
    check("恢复后编辑，按钮再次禁用", document.getElementById("btn-save-version").disabled);

    // 手工拿「恢复出的旧 result + 当前已编辑配置」打服务端，必须被拒
    const api = (p, body) => fetch(new URL(p, baseUrl).href, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    // 通过页面内部状态不易直接拿，改用 API 组合：取版本快照 + 一个改过的配置
    const versions = await (await fetch(new URL("/api/versions", baseUrl).href)).json();
    check("服务端已有 1 个版本", versions.length === 1, String(versions.length));
    const snap = await (await fetch(new URL(`/api/versions/${versions[0].id}`, baseUrl).href)).json();
    check("版本快照含成对 payload+result", snap.result && snap.payload &&
      snap.result.status === "optimal");

    const editedPayload = JSON.parse(JSON.stringify(snap.payload));
    editedPayload.roles[1].hex = "#111111";
    const vres = await api("/api/versions", { label: "x", payload: editedPayload, result: snap.result });
    check("服务端拒绝旧 result 保存版本", vres.status === 409, `got ${vres.status}`);
    const eres = await api("/api/export", { payload: editedPayload, result: snap.result });
    check("服务端拒绝旧 result 导出 CSS", eres.status === 409, `got ${eres.status}`);

    // 用快照原 payload + 快照 result 导出，必须成功且颜色与快照一致
    const ok = await api("/api/export", { payload: snap.payload, result: snap.result });
    check("同一份 payload 导出成功", ok.status === 200, `got ${ok.status}`);
    const css = await ok.text();
    check("导出 CSS 含快照中的新颜色",
      Object.values(snap.result.new_hex).every((h) => css.includes(h)));
    check("导出 CSS 含快照总 ΔL", css.includes(`ΣΔL = ${snap.result.total_delta}`));

    // ============ 场景 4：删除版本 ============
    console.log("场景 4：删除版本");
    const delBtn = document.querySelectorAll("#versions-list .icon-btn")[1]; // 0=恢复 1=删除
    delBtn.dispatchEvent(new document.defaultView.Event("click", { bubbles: true }));
    await waitFor(() =>
      document.getElementById("versions-list").querySelectorAll(".v-label").length === 0);
    const after = await (await fetch(new URL("/api/versions", baseUrl).href)).json();
    check("删除后版本列表为空", after.length === 0);

  } finally {
    proc.kill();
    // 等待进程退出，让 SQLite 关闭 WAL 后再清理临时库文件
    await new Promise((r) => setTimeout(r, 300));
    for (const f of [DB_PATH, `${DB_PATH}-wal`, `${DB_PATH}-shm`]) {
      try { fs.unlinkSync(f); } catch { /* 忽略 */ }
    }
  }

  console.log(`\n${passed} passed, ${failures.length} failed`);
  if (failures.length) {
    console.error("\n失败项：\n - " + failures.join("\n - "));
    process.exit(1);
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
