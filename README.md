# 电商风险控制系统（ERCS）v3.0

> 面向电商业务的**实时风险控制系统**：六类事件接入 → **33 个特征**计算 → 名单快筛 → **30 条规则**匹配 → 评分与**一票否决** → **五档决策** → 案件复核 → 处置留痕；
> 内置 **LangGraph AI 智能助手**（8 个工具 + 多轮会话）。
>
> **技术栈**：Python 3.10+ / FastAPI / SQLAlchemy / **MySQL 8** + Vue 3 / Vite / ECharts 5。

---

## 一、目录与文档

```
bussiness_risk_control/
├── PRD-电商风险控制系统.md      需求规格说明书（68 条 FR / 33 特征 / 30 规则 / 五档 / 五角色 / 13 页面）
├── SPEC-电商风险控制系统.md     技术规格说明书（13 个模块 / DDL / 接口契约 / 算法 / 任务拆分 / 测试规格）
├── 原型图/                      13 个页面的高保真原型（纯静态 HTML，双击 index.html）
├── demo/                        企业级参考文档集（Doc-01~07，本次合并的上游输入）
│
├── backend/                     后端
│   ├── app/
│   │   ├── main.py              应用入口、路由注册、统一异常处理
│   │   ├── config.py            Pydantic Settings（.env + 环境变量）
│   │   ├── database.py          引擎、连接池、审计触发器
│   │   ├── models.py            17 张业务表 + 16 张风控表
│   │   ├── schemas.py           Pydantic 模型
│   │   ├── deps.py              统一响应、JWT 解析、五角色校验
│   │   ├── engine/              特征工程 / 规则引擎 / 决策引擎
│   │   ├── services/            认证 / 事件管道 / 案件 / 画像图谱 / 审计 / 挑战
│   │   ├── agent/               8 个 LangChain 工具 + LangGraph ReAct
│   │   ├── metrics.py           接口调用计数器（供 /system/health 真实统计）
│   │   └── routers/             10 个路由模块
│   ├── scripts/
│   │   ├── init_db.py           建库建表 + 灌 30 规则 / 7 场景 / 5 账号 + 导出 DDL
│   │   ├── gen_business_data.py 生成 17 张业务表模拟数据
│   │   ├── gen_mock_events.py   生成模拟事件流（1200 条主样本）
│   │   ├── replay_events.py     按事件时间回放
│   │   ├── selfcheck.py         端到端验收自检（86 项）
│   │   └── smoke_frontend.py    前端冒烟自检（Edge + CDP，30 个页面实例）
│   ├── tests/                   pytest 测试套件（**206 条**）
│   ├── sql/                     自动导出的 DDL（由 ORM 元数据生成）
│   ├── .env.example             环境变量模板
│   └── run.py                   后端启动脚本（带预检）
│
├── frontend/                    Vue 3 + Vite
│   └── src/{api,stores,router,layouts,components,views,styles}/
├── .gitignore                   生成物忽略清单（data/、dist/、.env …）
└── start.py                     一键启动
```

**流程链**：`PRD → 原型图 → SPEC → 代码`（尚硅谷 Spec Coding 方法论）。

---

## 二、快速开始

### 1. 前置

- **MySQL ≥ 8.0** 已启动（`JSON_TABLE` 函数要求）
- Python ≥ 3.10、Node.js ≥ 18

### 2. 首次准备

```bash
# 后端依赖
cd backend
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
# .venv/bin/python -m pip install -r requirements.txt         # macOS / Linux

# 前端依赖
cd ../frontend && npm install
```

> 网络受限时可用镜像：
> pip `-i https://mirrors.aliyun.com/pypi/simple/` · npm `--registry=https://registry.npmmirror.com`

### 3. 配置 `.env`（**必做**）

```bash
cd backend
cp .env.example .env
```

编辑 `backend/.env`，至少填 **一项**：

| 变量 | 必填 | 说明 |
|------|:----:|------|
| `DB_PASSWORD` | ✅ | 你本机 MySQL 的连接密码 |
| `LLM_API_KEY` | ⭕ | 阿里云百炼（DashScope）API Key，格式 `sk-xxxxxxxx`。**留空时 AI 助手走降级路径，不影响其余功能** |

> ⚠ 常见错误：把 **Base URL** 填进了 `LLM_API_KEY`。
> `LLM_API_KEY` 只放 `sk-` 开头的密钥；接口地址放 `LLM_BASE_URL`。
> 若使用私有 MaaS 端点，把该 URL 填到 `LLM_BASE_URL`。

### 4. 一键启动（推荐）

```bash
python start.py             # 自动建库 → 建表 → 灌配置 → 生成数据 → 回放 → 起前后端
python start.py --backend   # 只起后端（前端你自己起）
python start.py --skip-seed # 跳过数据生成与回放（数据已就绪时更快）
python start.py --reset     # **删表重建**并重新灌入全部数据（改了 models.py 后用这个）
```

按 `Ctrl+C` 一次停掉全部服务。打开 <http://localhost:5173> → 自动跳转登录页。

> ⚠ `--reset` 走的是 `init_db.py --recreate`（**删表重建**），不只是清数据行 ——
> 因为 `create_all()` 不会给已存在的表加字段 / 索引 / 外键，只清行等于没修 schema。
> 重建前请先停掉后端（Windows 下服务占着库会让删表失败）。

### 5. 手动启动（等价于 SPEC §10.5）

```bash
cd backend
python scripts/init_db.py             # 建库 + 33 张表 + 30 规则 + 5 账号 + 触发器
python scripts/gen_business_data.py   # 17 张业务表模拟数据
python scripts/gen_mock_events.py     # 生成事件流
python scripts/replay_events.py       # 回放，让大盘有数据
python run.py                         # → http://127.0.0.1:8000  /docs

cd ../frontend && npm run dev         # → http://localhost:5173
```

### 6. 演示账号（5 个角色）

| 用户名 | 密码 | 角色 | 默认落地页 |
|---|---|---|---|
| `reviewer` | `reviewer@123` | 风控审核员 | 审核工作台 |
| `strategist` | `strategist@123` | 风控策略师 | 策略规则配置 |
| `analyst` | `analyst@123` | 数据分析师 | AI 智能助手 |
| `admin` | `admin@123` | 系统管理员 | 态势大盘 |
| `ops` | `ops@123` | 系统运维 | 态势大盘 |

---

## 三、验证（三层，缺一层都算没验完）

| 层 | 命令 | 覆盖范围 | 最近结果 |
|---|---|---|---|
| ① 单元 + 集成 | `pytest tests/ -q` | 引擎算法、特征口径、接口、权限矩阵、案件状态机、Agent 工具 | **206 通过** |
| ② 端到端自检 | `scripts/selfcheck.py` | PRD §9 全部 AC + v3.1 落地修正项（**需后端已启动**） | **86/86 通过** |
| ③ 前端冒烟 | `scripts/smoke_frontend.py` | 4 个角色的 30 个页面实例真的在浏览器里渲染、无 JS 报错（**需前后端都启动**） | **7/7 通过** |

```bash
# ① 单元 + 集成（自动建毁测试库 ecs_test，不污染演示库 ecs）
cd backend
.venv/Scripts/python.exe -m pytest tests/ -q
.venv/Scripts/python.exe -m pytest tests/ --cov=app --cov-report=term-missing   # 覆盖率

# ② 端到端自检（先 python run.py）
.venv/Scripts/python.exe scripts/selfcheck.py --url http://127.0.0.1:8000

# ③ 前端冒烟（先 python run.py + npm run dev）
.venv/Scripts/python.exe scripts/smoke_frontend.py --frontend http://localhost:5173
```

**为什么需要第 ③ 层**：pytest 覆盖后端、selfcheck 覆盖接口契约，但**没有任何东西验证
「页面在浏览器里真的渲染出来」**。Vue 的失败模式很安静 —— 引用不存在的变量、`v-if` 判错、
路由守卫把页面弹回登录页 —— 构建照样通过、接口也正常，只有人肉点开才能发现。
`smoke_frontend.py` 用 Edge + CDP 真跑一遍，并收集 `console.error` / `pageerror`。

> `selfcheck.py` 会**刻意**触发一次登录锁定来验证 FR-70，结束时自动复位演示账号；
> 若中途失败把账号锁住了，重跑一次即可（脚本开头会先复位全部 5 个账号）。
>
> 若运行环境无法访问 LLM 域名（受限网络 / 代理），对话相关检查项会降级为
> `LLM_UNREACHABLE` —— 属环境依赖，非代码缺陷（降级路径本身已验证，见 R-AG-01）。

---

## 四、核心设计要点

### 4.1 五档决策（PRD §2.4）

| 档位 | 分值区间（默认） | 决策 | 系统动作 |
|---|---|---|---|
| 1 | 0 ~ 29 | **Pass** 放行 | 同步返回，异步落库 |
| 2 | 30 ~ 59 | **Mark** 标记观察 | 记录并打标，**放行** |
| 3 | 60 ~ 79（命中规则**全部可挑战**） | **Challenge** 二次验证 | 返回验证指令，**不建案**；通过→Pass，失败/超时→Review |
| 4 | 60 ~ 79（命中含**不可挑战**规则） | **Review** 人审 | 自动建案，推送工作台 |
| 5 | 80 ~ 100 | **Reject** 拦截 | 拦截交易 + 自动建案 |

### 4.2 评分与一票否决

```text
final = min( max(命中分值) + BONUS × (命中数 − 1), 100 )     # BONUS 默认 3
若命中任一「极高」等级规则 → final = max(final, 90)          # 一票否决
```

**为什么用「最高分 + 额外加分」而不是简单累加**：以最高分为主可避免多条低分规则
叠加造成的误判；额外加分体现「数量佐证」而非「数量决定」。

### 4.3 33 个特征（4 维度）

| 维度 | 数量 | 数据源 |
|---|---:|---|
| 用户 | 14 | `order_info` / `order_detail` / `postsale` / `receive_info` / `logistics_complaints_record` |
| 订单 | 8 | `order_detail` / `order_info` / `sku_info` |
| 地址 | 3 | `receive_info` / `order_info` |
| 设备与团伙 | 8 | 进程内滑动窗口（识别同设备/同 IP/同地址的账号矩阵） |

> 「设备与团伙」维度是**团伙化作弊识别**的关键：单个账号看正常，
> 但同一设备下聚集了多个账号 → 命中「同设备多账号」（极高，不可挑战）→ 直接拦截。

### 4.4 关键约束（易错点）

- **窗口基准是事件时间戳**，不是服务器当前时间 —— 保证回放/仿真结果可复现（R-FE-01）
- **先写窗口再算特征** —— 目标事件计入自身特征（R-FE-02）
- **`order_pay_interval_sec` 未支付时必须是 -1 哨兵值** —— 若默认成 0 会误命中「支付间隔过短」
- **规则缓存必须存不可变快照，不能存 ORM 实例** —— 否则跨请求访问会抛 `DetachedInstanceError`
- **审计表由数据库触发器禁止 UPDATE/DELETE**，应用层亦不提供改删路由

---

## 五、主要接口

统一前缀 `/api/v1`，响应 `{ "code": 0, "message": "success", "data": ... }`。

| 模块 | 方法与路径 | 说明 |
|---|---|---|
| 认证 | `POST /auth/login`、`GET /auth/me`、`POST /auth/logout`、`POST /auth/change-password` | JWT（8 小时） |
| 事件网关 | `POST /risk/check` ⭐ | **免 JWT**，走 `X-Service-Id` 服务标识 |
| | `GET /risk/event/{id}`、`GET /events` | 事件详情 / 列表 |
| 规则 | `GET/POST /rules`、`PUT/DELETE /rules/{id}`、`PATCH\|PUT /rules/{id}/toggle` | 读放开 4 角色，**写仅 strategist** |
| 场景阈值 | `GET /rules/scenes`、`PUT /rules/scenes/{code}` | **改：仅 admin** |
| 特征 | `GET /features/dict` | 33 条，可按维度过滤 |
| 名单 | `GET/POST/DELETE /blacklist`、`POST /blacklist/import`、`GET /blacklist/stats` | 五维 × 黑白灰 |
| 案件 | `GET /cases`、`GET /cases/statistics`、`GET /cases/{id}` | 列表 / 统计 / 详情（读操作联动状态）；`case_status`/`risk_level` 筛选**中英文枚举均可**（`pending`≡`待审核`、`critical`≡`极高`…） |
| | `POST /cases/{id}/dispose\|hold\|close` | **仅 reviewer** |
| 画像 | `GET /profile/{uid}`、`GET /graph/{uid}` | 画像（含降级）/ 关联图谱 |
| 大盘 | `GET /dashboard/overview\|trend\|risk_dist\|top_rules\|realtime_events\|loss_prevented\|rule_effectiveness` | 态势指标；`trend` 支持 `?days=&event_type=`，点位含四档等级当日计数；`rule_effectiveness` 支持 `?days=&event_type=`（缺省全量） |
| 系统 | `GET /system/health`、`GET /system/stats`、`GET /system/config` | admin / ops |
| 审计 | `GET /audit/logs` | admin 全量 / 其余仅本人；**无改删接口** |
| 仿真 | `POST /simulate/run`、`GET /simulate/templates`、`POST /simulate/run_template/{code}` | 不落库 |
| **AI Agent** | `POST /agent/chat`、`POST /agent/clear`、`GET /agent/sessions`、`GET /agent/sessions/{id}/messages`、`DELETE /agent/sessions/{id}`、`GET /agent/tools`、`GET /agent/status` | strategist / analyst / admin（会话按归属隔离） |
| 挑战 | `POST /challenge/{event_id}/resolve`、`GET /challenge/{event_id}` | 二次验证回调与查询；回调会**顺带触发超时兜底扫描** |
| 运维 | `GET /admin/health`（免鉴权）、`POST /admin/reset`、`POST /admin/seed-config` | — |

`/docs` 有完整 OpenAPI 文档。

**前端未调用的 11 个接口（有意保留，属 API-only 能力）**

前端 45 个调用点与后端契约**逐条核对通过**（无「调不存在的接口」）。
以下接口前端不调用，各有明确用途：

| 接口 | 为什么没有页面调用 |
|---|---|
| `POST /risk/check` | 面向**外部业务系统**的入口；页面侧用 `/simulate/run`（dry_run）演示同一链路 |
| `GET /profile/{uid}`、`GET /graph/{uid}` | 画像与图谱已**内嵌在案件详情**的聚合响应里；独立端点供 AI 工具与外部集成使用 |
| `GET /risk/event/{id}`、`GET /events` | 单事件下落的排查能力，暂不做页面 |
| `POST/GET /challenge/**` | 真实链路里 Challenge 回调是**外部系统 / 短信通道**的职责，不是后台按钮；由 pytest 与 selfcheck 直接驱动验证 |
| `GET /system/config` | 运维面只读配置（登录策略、LLM 模型名、图谱上限） |
| `GET /admin/health`、`POST /admin/reset`、`POST /admin/seed-config` | 存活探针与数据维护，由脚本 / 容器探针调用 |

---

## 六、AI 智能助手

基于 **LangGraph ReAct** 循环，温度 0.1（风控场景需确定性输出）。

**8 个内置工具**

| 分组 | 工具 | 功能 |
|---|---|---|
| 风控决策 | `risk_check` | 实时风险检查 |
| | `query_cases` | 案件查询 |
| | `query_user_profile` | 用户画像 |
| | `manage_blacklist` | 名单管理（add/remove/check/list） |
| 数据分析 | `query_dashboard_stats` | 仪表盘统计 |
| | `analyze_risk_trend` | 趋势分析 |
| | `analyze_rule_effectiveness` | 规则效果分析 |
| | `query_business_data` | 业务数据查询（6 种类型） |

**降级设计**：未配置 `LLM_API_KEY` 时返回明确的降级提示（`degraded=true`）并禁用输入框，
**不影响风控主链路与其余 12 个页面**。每次工具调用写审计（`biz_type = agent_call`）。

**试试这样问**：
- 「查看今天的风控统计数据」
- 「帮我检查一下用户 U-1401 的风险」
- 「哪些规则效果不好？」
- 「分析最近 30 天的风控趋势」

---

## 七、演示建议路径

1. **大盘**（admin）→ 5 指标卡、风险趋势、四档分布、规则排行、实时滚屏
2. **审核工作台**（reviewer）→ 筛选「极高 + 待审」→ 打开案件详情
3. **案件详情** → 决策摘要（含**一票否决标记**）→ 33 特征分维度对比 → 关联图谱
   → 选「违规拦截并拉黑」→ 二次确认 → 提交（订单状态联动变更）
4. **审计日志**（admin）→ 展开 diff 看变更前后；确认无删除/编辑入口
5. **事件仿真**（strategist）→ 依次回放 4 个模板，观察判定链路四步与 Challenge 分支
6. **策略规则配置** → 内联改分值、切换启停；打开规则看 JSON 条件树编辑器
7. **特征与阈值**（admin 改阈值 / strategist 置灰）→ 改 `mark_threshold` 后回仿真页观察结论变化
8. **AI 智能助手**（analyst）→ 自然语言问上述问题，观察工具调用过程
9. **权限验证** → 用 reviewer 登录后地址栏直敲 `/strategy/rules` → 渲染 403 页

---

## 八、已知事项

### 8.1 `LLM_API_KEY` 的常见误填（已解决，保留排查指引）

**v3.0 期的问题**：`.env` 的 `LLM_API_KEY` 里放的是**接口地址**（`https://…`）而不是密钥，
DashScope 返回 `401 Incorrect API key`，AI 助手一直走降级路径。

**v3.1 状态**：已配置正确（密钥 `sk-` 开头、地址走
`LLM_BASE_URL=https://maas.qianwenaiapi.com/compatible-mode/v1`），
`run.py` 启动预检会打印 `✓ LLM 连通性：可达`。

若再次遇到「AI 助手一直说不可用」，按这个顺序排查：

```ini
# backend/.env
# 密钥：只放 sk- 开头的那一串
LLM_API_KEY=sk-xxxxxxxxxxxxxxxx
# 地址：接口端点（注意结尾的 /v1）
LLM_BASE_URL=https://maas.qianwenaiapi.com/compatible-mode/v1
LLM_MODEL_NAME=qwen3.8-flash
```

> **为什么能给出精确提示**：`app/config.py` 会粗判密钥形状
> （`ok` / `missing` / `looks_like_url` / `unexpected_prefix`），
> 填成地址时**直接判为不可用**并给出可操作指引，而不是发一个注定 401 的请求。
> `GET /api/v1/agent/status` 返回 `key_shape` 与 `config_hint` 供前端展示；
> `run.py` 的启动预检还会真实探测一次连通性 —— 出网被拦这类问题在启动阶段就暴露，
> 不用等用户开口对话才发现。

### 8.2 实现过程中发现并修复的问题

| # | 问题 | 修复 |
|---|---|---|
| 1 | `order_pay_interval_sec` 默认 0，误命中「支付间隔过短」194 次 | 改为 **-1** 哨兵值（符合 SPEC §2.3.2） |
| 2 | 规则缓存存 ORM 实例 → 跨请求 `DetachedInstanceError` | 改为缓存 **`CachedRule` 不可变快照** |
| 3 | 业务数据 SKU 价格均匀分布 39~8999，导致 86% 用户被「平均订单金额异常」误命中 | 改为**双峰分布**（大众 ¥39~900 / 高价值 ¥3000~9000），误命中降至 8.9% |
| 4 | 边界样本每人 11 单，误触发「设备高频下单」冲到 Reject | 降到 4 单，落在 63 分验证 Challenge 档 |
| 5 | 仿真模板 TPL-02 请求漏 `receive_id` → 地址团伙规则静默不命中 | 补上该字段 |
| 6 | `_build_response` 漏 `event_type` → 仿真页 500 | 补齐字段 |
| 7 | `STRATEGY_ROLES` 误含 admin → admin 能改规则 | 收紧为**仅 strategist**（对齐 SPEC R-AU-11） |
| 8 | `init_db --ddl-only` 用 `__tablename__` 访问 `Table` 对象 | 改为 `.name` |

### 8.3 文档口径说明

- **风控表 16 张** = 配置 4（规则 / 场景阈值 / 名单 / 用户）+ 事件与决策 5（事件 / 特征快照 /
  评估 / 二次验证 / 画像）+ 案件与处置 2 + 画像图谱 2 + 审计与 Agent 3。
  v3.0 文档曾写「8 张」（指核心表），已勘误（PRD §8.2）
- **业务表 17 张** 完全对齐 `demo/` Doc-02 §3.2
- **原型 13 页**（v2.0 的 11 页 + AI 智能助手 + 趋势分析）+ 403 页
- **71 条 FR**（P0 55 / P1 14 / P2 2），v3.0 文档曾写「68 条」，已勘误（PRD §4.9）

### 8.4 AI 助手会话功能修复（2026-10-14）

上线后发现的会话类缺陷，均已修复并补测试（`tests/test_api.py` 新增 8 例）：

| # | 现象 | 根因 | 修复 |
|---|---|---|---|
| 1 | 切换会话后「之前的对话消失」 | 前端清空 `messages` 后**没有历史消息接口可回填** | 新增 `GET /agent/sessions/{id}/messages`，`switchSession` 拉取回填 |
| 2 | 对话「删不掉」 | 只有清空消息，**无删除会话接口**；且 `clear` 刷新 `update_time` 反把会话置顶 | 新增 `DELETE /agent/sessions/{id}`；`clear` 改为不刷新 `update_time` |
| 3 | 可读写他人会话 | `chat` / `clear` **未校验会话归属** | 新增 `assert_owner()`，非 admin 访问他人会话返回 403 |
| 4 | 模型每轮多收到一条重复提问 | `chat()` 先落库再 `_load_history`，读回本轮提问后又 `append` | 改为**先读历史再落库**；移除无界 `_history_cache` |
| 5 | 多轮工具调用时前端先超时 | 前端 `timeout=20s` < 后端 `LLM_TIMEOUT_SECONDS=30s` | 前端超时提到 **120s**（v3.1 再放宽，见 `api/index.js` 注释） |

### 8.5 趋势与规则效果分析页修复（2026-10-15）

页面上的三个筛选/图表控件此前**对着后端不存在的参数**，属于「控件在、功能不在」：

| # | 现象 | 根因 | 修复 |
|---|---|---|---|
| 1 | 「事件类型」下拉选了没反应 | `load()` 从不读 `eventType`；后端 `/dashboard/trend` 也**没有该参数** | 后端加入 `event_type`（join `risk_event`）；前端下拉 `@change="load"` 并透传 |
| 2 | 「风险等级分布趋势」是**四条水平直线** | 趋势点位不含等级字段 → 前端只能拿全量分布常数兜底 | 趋势点位补 `low_cnt/medium_cnt/high_cnt/critical_cnt`（四档之和恒等于 `assess_cnt`）；前端改绘真实日趋势，并剔除无评估量的空日避免假断点 |
| 3 | 规则效果表「近 N 天」是**假标签** | `dashboard_rule_effectiveness` 恒按全量统计，`days` 参数被忽略 | 加入 `days` 与 `event_type`（缺省全量，向后兼容）；命中率分子分母同窗口 |
| 4 | 「决策分布」未标注窗口，切范围时不变 | 硬编码 `slice(-7)` | 窗口改为 `min(所选范围, 7)` 并在卡片标题标注「近 N 天」 |

> 说明：`_hit_counts_in_memory()` 降级路径同步支持 `start` / `event_type`，与 JSON_TABLE 主路径口径一致。
> 新增 3 条回归用例（`tests/test_api.py`），断言「四档之和 == 评估量」与「窗口/类型过滤生效」。

### 8.6 v3.1 全量核对与修复（2026-10-15）

本轮先做了一次「文档 → 实现」的逐条核对（结果见 PRD 附录 D.3 差异台账），
再动手修。**下面每一条都是实测确认过的**，不是推测。

#### 8.6.1 文档与实现不一致（改文档 or 改代码，二选一）

| # | 条目 | v3.0 声明 | 实现现状 | 处理 |
|---|---|---|---|---|
| 1 | PRD §4.9 需求条目数 | 「合计 68」 | 分段相加为 71 | 勘误为 71 并补分段明细 |
| 2 | PRD §8.2 风控库表数 | 「8 张」 | 实际 16 张 | 改 16 张，补齐 3 处缺失实体 |
| 3 | FR-19 / FR-29 / FR-40a | 有需求 | ER 图里没有对应实体 | 补 `risk_challenge_record` / `risk_disposal_record` / `risk_rule_scene` |
| 4 | FR-10 特征字典 | 「可配置查看」+ 原型「新增特征」按钮 | 只有只读接口；前端按钮**没有 click 处理器** | **收窄文档**为只读，删控件（PRD §1.4 范围外新增一行） |
| 5 | §7.7 阈值页 | 画了「不可挑战规则直接人审」开关 | 后端 `SceneThresholdUpdate` 从无该字段，保存被静默丢弃 | **删控件**：该语义由规则 `challengeable` 逐条决定，非全局参数 |
| 6 | API-6.8 SSE | 规划了 `GET /dashboard/stream` + 任务 T-27 | 后端无实现，前端用 5 s 轮询 | 删接口与任务（PRD 只要求「刷新周期 ≤ 5 s」，轮询即满足） |
| 7 | SPEC §3.8 API-3.3 | 请求体含 `rule_id` | `RuleCreate` 无该字段，服务端生成编码 | 改文档：编码由服务端生成 |

#### 8.6.2 已确认的真缺陷（**都会在页面上看到，或让整块功能静默失效**）

| # | 现象 | 根因 | 修复 |
|---|---|---|---|
| 1 | `/risk/check` 返回的 `assessment_id` **在库里查不到** | `_build_response()` 在非 dry_run 分支又调了一次 `new_assessment_id()`，与落库主键不是同一个值 | 由调用方传入 `assessment_row.assessment_id` |
| 2 | 误输 5 次密码后，该演示账号**整场演示都废了** | `status='locked'` 是永久状态，`LOGIN_LOCK_SECONDS` 从未被读取（提示却写着「请 10 分钟后重试」） | 新增 `risk_user.locked_at`；校验顺序改为「先判过期、再判锁定」；过期自动解锁 |
| 3 | 服务跑一会儿后，仿真模板回放**结果漂移**（TPL-01 由 Mark 变 Pass） | 模板的种子事件时间在**模块导入时**就算成常量，运行越久种子越旧、逐渐滑出 1h 窗口 | 模板只存「分钟偏移」，回放当刻现算（实测 95 分钟后仍为 Mark） |
| 4 | 案件**永远停在「已处置」** | 状态机声明了 `已处置 → 已归档`，但**没有任何代码触发它**；`BackgroundTasks` 参数一直没被使用 | 处置接口用后台任务执行归档，可重试、失败不阻断已返回结果 |
| 5 | **「挽回资损」永远显示 ¥0** | `RiskCheckRequest` 里**没有 `amount` 字段**，而代码用 `getattr(request,"amount",0)` 取金额 → 每条事件金额恒为 0 | 补 `amount` 字段；缺省时从订单明细反查。实测：非零金额 0 → 680 条，资损 ¥283,807 |
| 6 | 案件详情「当前订单」为空、**「拦截订单」永远返回「订单不存在」** | 事件数据集用**编造的** `source_id`（`O-N100424`），反查 `order_info` 必然落空 → 8 个订单维度特征与金额全为 0 | 生成器先读业务库真实单据，事件**轮询绑定**真实订单号与金额；并给事件用到的账号补齐业务记录 |
| 7 | 大盘「接口健康度」**从不变化** | 返回的是写死的示例常量（`calls: 8240, errors: 12`） | 新增 `app/metrics.py` + 中间件按「方法 + 路由模板」真实累加 |
| 8 | 同一张指标卡上的数字**互相之间没有关系** | `assess_total` 是今日值，`high_risk_count`/`pass_rate`/`loss_prevented` 是全量值；`pass_rate_delta` 硬编码 `0.0`、`loss_delta` 写成 `loss × 0.14` 的估算常量 | 统一口径并下发 `scope` 标注（今日 / 累计 / 实时），delta 全部真实计算 |
| 9 | 用户既不点验证也不回调时，事件**既不放行也不人审** | `is_expired()` 只在回调到达时求值 → 挑战永久停在 `pending` | 决策入口加**超时兜底扫描**（走 `idx_ch_pending`，正常命中 0 条） |
| 10 | Agent 的「移除黑名单」可能**把白名单删了** | 只按 `(entity_type, entity_value)` 查，而唯一约束是三元组 | 补 `list_type='black'` 过滤 |
| 11 | 「拦截订单」把**已发货**订单回改成「已取消」 | 无条件 `SET order_status='已取消'` | 只对「待付款 / 已支付」生效，其余标注 `skipped` 并说明原因 |
| 12 | 前端**admin 能看到可点的**「新建规则 / 编辑 / 启停 / 内联改分」 | `canEdit = ['strategist','admin']`，而后端写操作只放 strategist → 一点就吃 403（§8.2 第 7 条修了后端没修前端） | 改为 `role === 'strategist'`；名单页同理 |
| 13 | 规则 / 名单 / 特征三页**对 reviewer 与 analyst 进不去** | 前端路由只放 strategist / admin，与 PRD §2.1.1「只读可见」不符（后端早已放开读） | 路由与菜单按 PRD 放开，只读角色按钮置灰 |
| 14 | 生成数据脚本**每跑一次数据就涨**（投诉 80→240，订单金额上浮） | `logistics_complaints_record` 自增主键无业务唯一键、`order_detail` 行数由随机数决定 → 无法幂等 upsert | 默认 `--clean`（先清空再生成），实测连跑两次结果完全一致 |
| 15 | 业务数据里**每个用户都被补了一单**（600→831） | `autoflush=False` 时 `db.query()` 看不到本次会话新增的订单 → 全部被判为「没有订单」 | 查 `covered` 之前先 `db.flush()` |
| 16 | `python run.py` / `python scripts/*.py` 在 GBK 控制台**直接崩** | 打印 `✓` / `✗` / `⚠` 时 `UnicodeEncodeError`，而预检的 print 没有兜底（看起来像数据库挂了） | 统一把 stdout/stderr `reconfigure` 为 UTF-8 |
| 17 | 阈值保存产生 **7 条重复审计 + 7 倍写放大** | 前端遍历 7 个场景码逐个 `PUT`，而后端本就是全局同步 | 前端只提交一次（`scenes/all`），响应回传 `synced_scenes` |
| 18 | 规则效果页/案件列表页存在**全表读进内存**的聚合 | `db.query(...).all()` 后 Python 遍历，大盘刷新一次要跑 4 遍 | 全部改 SQL `GROUP BY` |
| 19 | 每次风控检查都**全表扫特征快照** | `baseline_features()` 按 `compute_time` 过滤，但该列无索引 | 加 `idx_feat_baseline (compute_time, feature_name)` |
| 20 | 案件详情页图谱最多发 **~150 条 SQL** | 1~2 跳展开写成逐节点查询 | 改 3 次批量查询（`IN (...)`） |

#### 8.6.2b 修「超时兜底」时自己踩的坑（记录在案）

修 8.6.2 第 9 条（Challenge 悬空）时，第一版把兜底扫描挂在了**决策返回之前**。
跑起来才发现：**任意一个事件**（哪怕只是一次登录）都会顺手改写历史上所有已超窗口的挑战 ——
111 条评估被静默改成 `Review`，大盘「决策分布」里 Challenge 一次请求就从 111 变 0，
而且审计里查不到是谁改的。**副作用不该属于发起它的那个请求。**

改为 `main.py` 里的后台周期任务（启动即扫一次 + 每 30 s 一次）后行为正确了。
顺带把两个口径显式化（SPEC R-RE-19）：

- `risk_assessment.decision` 是**当前状态**，会被二次验证结果改写（R-RE-14 的要求）；
- 所以要回答「五档各命中多少」，必须看**决策当刻**的统计。

`replay_events.py` 现在会：① 按固定比例模拟二次验证回调（≈35% 通过 / 45% 失败），
让演示数据覆盖 R-RE-14 的**两条分支**；② 把「决策当刻分布」与「最终状态分布」
**并排打印**，并解释为什么历史数据上 Challenge 趋近于 0（已到期升级，不是没触发）。

> 实测：决策当刻 `Challenge 111`；二次验证 `passed 46 / failed 45 / timeout 20`；
> 最终 `Pass 897 / Mark 134 / Review 98 / Reject 171`。

| 21 | 回放时刷屏「售后单 O-000xxx 未关联到订单，补全失败」 | 售后事件的 `source_id` 被绑定成了**订单号**（该用户没有售后单时的退化绑定），而补全逻辑只按 `postsale_id` 查 → 查不到就放弃 | 补全逻辑加**按订单号再查一次**的容错分支，info 日志说明原因；生成器显式上报这类条数。70/70 条恢复订单上下文，金额覆盖 680 → 744 条 |

#### 8.6.3 冗余代码清理（共删除约 1500 行）

| 类别 | 内容 |
|---|---|
| 死模块（6 个 / ~1400 行） | `routers/event.py`、`routers/challenge.py`、`services/step1_event_ingest.py`、`services/step2_feature_engine.py`、`services/step3_rule_engine.py`、`services/step4_case_flow.py` —— 是一套**与主线并行的重复实现**，从 `app.main` 做可达性分析确认无人引用 |
| 死函数（8 个） | `utils.py` 的 `today_str` / `bump_rule_seq` / `_next_seq` 与日期计数器 / `mask_value` / `json_text` / `to_int` / `to_json`；`step3_rule.py` 的 `SUPPORTED_OPERATORS` |
| 死响应模型 | `schemas.py` 的 `RiskCheckResponse` / `RuleHitInfo` / `Role` —— 从未作为 `response_model` 使用，字段与实际响应也对不上（真契约在 SPEC §1.4 与 `_build_response()`） |
| 死前端包装 | `api/index.js` 的 `riskApi` / `profileApi` / `challengeApi` —— 全项目零引用 |
| 死控件 | 特征字典「+ 新增特征」按钮（无 click 处理）、阈值页「不可挑战直接人审」开关（后端不认） |
| 死兼容路由 | `/api/v1/watchlist`、`/api/v1/watchlist/import` —— v2 遗留，零引用 |
| 未使用导入 | 14 个模块共 40 余处 |
| 仓库杂物 | `_kill3.txt`、`_pytest.txt`、`.tmp_edge2/`（Edge 用户目录）、`backend/_*_probe.py`、`backend/_*_out.txt`、`backend/.pytest_cache/`、`frontend/dist/` |

#### 8.6.4 数据库优化

| 变更 | 内容 | 解决的问题 |
|---|---|---|
| 外键 4 条 | `risk_feature.event_id` → `risk_event`；`risk_challenge_record.event_id` → `risk_event`；`risk_disposal_record.case_id` → `risk_case`；`risk_agent_message.session_id` → `risk_agent_session` | SPEC 声明了但 ORM 里一条都没有（外键只写在文档里） |
| 索引 8 个 | `idx_feat_baseline`、`idx_ch_pending`、`idx_case_ast`、`idx_case_event` + 补齐既有 | 见 8.6.2 第 19 条 |
| 新字段 | `risk_user.locked_at` | 见 8.6.2 第 2 条 |
| 删除顺序 | `POST /admin/reset` 改为**先子后父** | 加外键后顺序写反会直接抛约束错误，重置功能整体失效 |
| DDL 导出 | `sql/*.sql` 现在包含**外键**与**审计触发器**，可独立执行 | 之前照 `sql/` 手工建库跑出来的库**没有触发器** → 「审计不可篡改」在手工建库路径下形同虚设 |
| 重建入口 | 新增 `init_db.py --recreate` | `create_all()` 不会 ALTER 已存在的表；改模型后只跑 `--reset` 会得到「模型有字段、库里没有」的状态，报错点还会漂到别处 |

#### 8.6.5 回归测试（新增 22 条，`tests/test_v31_regression.py`）

新增 **23** 条，每条都写明「它守的是哪个坑」—— 回归测试最有价值的部分是那段「为什么」。
覆盖：响应标识一致性、锁定限时与自动解锁、仿真模板长期稳定、Challenge 超时兜底、
处置边界与幂等、异步归档、大盘口径、接口真实计数、阈值单次同步、权限矩阵、加外键后的数据重置、
审计触发器在数据库层拦改删。

---

## 九、范围外（对齐 PRD §10.4）

不含：真实高并发分布式部署与多机房容灾、机器学习模型训练与在线推理
（LLM 仅用于 Agent 对话与工具编排，**不参与风险评分**）、真实设备指纹 SDK 采集、
对接真实支付/物流/售后生产系统、完整 RBAC 权限模型、SSO 单点登录、
容器化部署与 CI/CD、移动端适配。
