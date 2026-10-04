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
4. **保存配置**：把当前角色与关系写入 SQLite，刷新后自动恢复。
5. **存为版本**：保存一次求解结果快照，可一键恢复或删除。
6. **导出 CSS 变量**：把方案下载为 `variables.css`，含明度变化注释与对比度校验注释。

> 求解成功后，只要继续修改角色颜色、名称、锁定状态、增删角色，或调整前景—背景关系与门槛，
> 界面会**立即清除旧结果**并禁用「存为版本」「导出 CSS」，直到重新求解——
> 防止旧结果与当前配置错配。恢复版本时，配置与结果成对生效。
>
> 服务端不信任客户端带来的结果：保存版本与导出 CSS 时都会按所提交的配置**重新求解并做绑定校验**，
> 结果不一致（编辑后未重解）、无解或未完成（预算耗尽）一律拒绝（`409`）；
> SQLite 快照、样张预览与导出的 CSS 始终来自同一份配置。

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
| `POST` | `/api/versions` | 保存版本 `{label, payload, result}`；服务端按 `payload` 重算并与 `result` 绑定校验，不一致/无解/未完成返回 `409`，入库的是规范化 payload 与权威 result |
| `GET` | `/api/versions/<id>` | 读取版本（含求解结果，配置与结果成对返回） |
| `DELETE` | `/api/versions/<id>` | 删除版本 |
| `POST` | `/api/export` | 由 `{payload, result}` 生成 `text/css` 变量文件；与存版本走同一道绑定校验，过期结果返回 `409` |

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
  server.py         # Flask 路由、入参校验、结果绑定校验、CSS 导出名生成
  static/
    index.html
    styles.css
    app.js          # 编辑器（编辑即作废旧结果）、结果/样张/冲突渲染、版本与导出
tests/
  test_palette_server.py   # 后端回归：连续编辑/保存/恢复/导出的绑定校验（unittest）
  js/frontend.test.js      # 前端回归：jsdom 加载真实页面 + 真实 Flask 后端
wsgi.py             # gunicorn 入口（初始化 DB）
wsgi_local.py       # 本地开发入口
Dockerfile
docker-compose.yml
requirements.txt
```

## 测试

后端回归（连续编辑后旧结果拒绝、版本保存/恢复成对、SQLite 快照与 CSS 同源、篡改/无解/未完成拒绝）：

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

前端回归（真实 `index.html` + `app.js` 跑在 jsdom 中，后端是临时 SQLite 的真实 Flask；
会自动选择空闲端口，并通过 `PYTHON_USER_SITE` 指定 Python 依赖目录，可选）：

```bash
npm --prefix tests/js install
# 依赖装在系统 Python 时可直接运行；Flask 在用户目录时：
PYTHON_USER_SITE=$(python3 -c "import site; print(site.getusersitepackages())") \
  node tests/js/frontend.test.js
```
