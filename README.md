# 多约束无障碍调色台（Accessible Palette Workbench）

录入带角色的语义色板与前景—背景 WCAG 对比度约束，系统**只调整可调色的 HSL 明度**（色相、饱和度保持不变），在满足全部关系的候选中选出**总明度改变量最小**的方案；平局时按角色名稳定打破；无解时列出极小冲突约束集。页面提供样张预览、前后对比度对比、版本保存与 CSS 变量导出。

- 后端：Python 3.12 · Flask · SQLite（标准库 `sqlite3`）
- 前端：原生 HTML / CSS / JavaScript，无构建步骤
- 求解器：自研 CSP —— AC-3 弧相容 + 前向检查 + MRV + 分支限界（101 档明度，整数位集域）

## 一条命令启动

```bash
docker compose up --build
```

打开 <http://localhost:8000>。数据持久化在 Docker 卷 `palette-data` 中。

停止：

```bash
docker compose down        # 保留数据
docker compose down -v     # 同时删除数据卷
```

## 本地开发（不用 Docker）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
DATABASE_PATH=./data/palette.db python wsgi_local.py
# 或：DATABASE_PATH=./data/palette.db flask --app wsgi run --port 8000
```

`wsgi_local.py` 会初始化 SQLite 后启动 Flask 开发服务器。

## 使用说明

1. **语义角色**：填写角色名（如 `text`、`primary`、`background`）、十六进制颜色；勾选「锁定」后该角色明度在求解中固定。
2. **前景—背景关系**：选择前景角色、背景角色与最低对比度（AA 正文 4.5:1，AA 大字号 3:1，AAA 正文 7:1）。
3. 点击 **求解调色**：
   - 成功：展示每个角色的旧/新 HEX、明度变化 `ΔL`、每条关系的 `旧对比度 → 新对比度`，以及实际前景/背景配色的样张。
   - 无解：列出极小冲突集（可能有多个相互独立的冲突簇，例如两个锁定色自身对比不足），并给出放宽建议。
   - 求解成功后只要再改动**角色颜色 / 名称 / 锁定状态、增删角色，或调整前景—背景关系与门槛**，旧结果会立即清除，「存为版本」与 CSS 导出同步禁用，必须针对当前配置重新求解后才能再次使用，避免结果与配置错配。
4. **保存配置**：把当前角色与关系写入 SQLite，刷新后自动恢复。
5. **存为版本**：保存一次求解结果快照（配置与结果成对绑定），可一键恢复或删除。服务端不信任客户端提交的结果——保存时会对同一份 payload 重新求解并做绑定校验，拒绝不一致、无解或未完成的结果，落库快照始终来自服务端权威重算。
6. **导出 CSS 变量**：把方案下载为 `variables.css`，含明度变化注释与对比度校验注释。导出同样基于与 `存为版本` 相同的 payload/result 绑定校验（也兼容只提交 result 的旧用法），保证快照、预览与 CSS 来自同一配置。

## 求解模型

对每个角色，H、S 固定，明度 L ∈ {0,…,100} 是唯一变量。角色 i 在明度 v 下的相对亮度（WCAG 相对亮度公式）预计算为 `Y_i(v)`。关系 `(fg, bg, c*)` 要求：

```
(max(Y_fg, Y_bg) + 0.05) / (min(Y_fg, Y_bg) + 0.05) ≥ c*
```

- 用 101 位整数位集表示每个变量的取值域；
- **AC-3** 先做弧相容；DFS 按 MRV 选变量（域最小→角色名靠前），按 `|v−L₀|` 升序取值，前向检查传播；
- 以「各变量域内距初始明度最近值之和」为可采纳下界做**分支限界**；
- 目标 `min Σ|L−L₀|`；总代价相同时取角色名顺序下明度元组字典序最小，结果完全确定；
- 无解时用删除法（greedy reduction）求极小不可满足子集，并对「锁定—锁定」死结做多种子枚举，给出多个独立冲突簇。

角色上限 14 个，搜索节点预算 50 万（冲突诊断子问题各 6 万），gunicorn 请求超时 120 秒。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/config` | 读取当前配置 |
| `POST` | `/api/config` | 保存配置 `{roles, relations}` |
| `POST` | `/api/solve` | 求解，返回 `optimal` / `infeasible` / `exhausted` |
| `GET` | `/api/versions` | 版本列表 |
| `POST` | `/api/versions` | 保存版本 `{label, payload, result}`：服务端对 payload 重算并绑定校验 result，通过后落库权威结果（400 拒绝不一致/无解/未完成） |
| `GET` | `/api/versions/<id>` | 读取版本（含求解结果） |
| `DELETE` | `/api/versions/<id>` | 删除版本 |
| `POST` | `/api/export` | 生成 `text/css` 变量文件。请求体 `{payload, result}`（推荐，与版本同一份 payload）；仅提交 `{result}` 的旧用法也会先从结果重建配置再重算校验 |

快速验证：

```bash
curl -s http://localhost:8000/api/config
curl -s -X POST http://localhost:8000/api/solve \
  -H 'Content-Type: application/json' \
  -d '{"roles":[{"name":"bg","hex":"#ffffff","locked":true},{"name":"text","hex":"#777777","locked":false}],"relations":[{"fg":"text","bg":"bg","min_contrast":4.5}]}'
```

## 项目结构

```
app/
  color.py          # HEX/HSL/相对亮度/WCAG 对比度（与前端 JS 等价实现）
  solver.py         # CSP 建模、AC-3、分支限界搜索、冲突集诊断
  storage.py        # SQLite：config / versions
  server.py         # Flask 路由、入参校验、CSS 导出名生成
  static/
    index.html
    styles.css
    app.js          # 编辑器、结果/样张/冲突渲染、版本与导出
wsgi.py             # gunicorn 入口（初始化 DB）
wsgi_local.py       # 本地开发入口
Dockerfile
docker-compose.yml
requirements.txt
tests/
  test_server.py    # 后端回归：绑定校验、版本成对落库/恢复、导出一致性
  e2e/run.js        # 端到端回归：连续编辑→清结果/禁用、保存→恢复→导出（jsdom + 真实服务）
```

## 测试

后端（标准库 `unittest`，无需额外依赖）：

```bash
python -m unittest discover -s tests -v
```

端到端（Node 18+；自动拉起一个临时 Flask 服务，使用临时 SQLite）：

```bash
cd tests/e2e
npm install        # 仅需 jsdom
npm test
```

覆盖场景：求解成功后连续编辑（颜色 / 名称 / 锁定 / 增删角色 / 关系与门槛）立即清除旧结果并禁用「存为版本」与 CSS 导出；重新求解后恢复；版本保存 / 恢复 / 删除；服务端拒绝过期、篡改、无解或未完成的 result；恢复错配历史快照时只恢复配置；导出 CSS 与 SQLite 快照来自同一份配置。
