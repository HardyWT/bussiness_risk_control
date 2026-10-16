# 电商风险控制系统（ERCS）v3.1

> 面向电商业务的**实时风险控制系统**：六类事件接入 → **33 个特征**计算 → 名单快筛 →
> **31 条规则**匹配 → 评分与**一票否决** → **五档决策** → 案件复核 → 处置留痕；
> 内置 **LangGraph AI 智能助手**（8 个工具 + 多轮会话）。
>
> **技术栈**：Python 3.10+ / FastAPI / SQLAlchemy / **MySQL 8** + Vue 3 / Vite / ECharts 5。

---

## 一、系统概览

| 维度 | 内容 |
|---|---|
| **事件接入** | 六类业务事件：`login` / `coupon_receive` / `order_create` / `order_pay` / `after_sale_apply` / `logistics_complaint` |
| **特征体系** | 33 个特征，四个维度：用户 14 · 订单 8 · 地址 3 · 设备与团伙 8 |
| **规则引擎** | 31 条规则，六类风险场景：订单欺诈 / 支付风险 / 账户风险 / 售后滥用 / 地址风险 / 物流风险 |
| **决策输出** | 五档：Pass 放行 · Mark 标记观察 · Challenge 二次验证 · Review 人审 · Reject 拦截 |
| **案件流转** | 待审核 → 审核中 → 已处置 → 已归档（另有「已关闭」终态），处置动作全量留痕 |
| **角色体系** | 五角色：风控审核员 · 风控策略师 · 数据分析师 · 系统管理员 · 系统运维，各有专属能力 |
| **前端页面** | 13 个业务页面 + 403 页 |
| **数据存储** | 17 张业务表 + 16 张风控表（含审计表，由数据库触发器保证不可篡改） |

**核心处理链路**

```text
事件接入 → 名单快筛 → 特征计算(33) → 规则匹配(31) → 评分 + 一票否决 → 五档仲裁
                                                                        ↓
                            处置留痕 ← 案件复核 ← 自动建案（Review / Reject）
```

---

## 二、目录结构

```
bussiness_risk_control/
├── 原型图/                      13 个页面的高保真原型（纯静态 HTML，双击 index.html）
│
├── backend/                     后端服务
│   ├── app/
│   │   ├── main.py              应用入口、路由注册、后台周期任务、统一异常处理
│   │   ├── config.py            Pydantic Settings（.env + 环境变量）
│   │   ├── database.py          引擎、连接池、审计触发器
│   │   ├── models.py            17 张业务表 + 16 张风控表
│   │   ├── schemas.py           Pydantic 模型
│   │   ├── deps.py              统一响应、JWT 解析、五角色校验
│   │   ├── engine/              特征工程 / 规则引擎 / 决策引擎
│   │   ├── services/            认证 / 事件管道 / 案件 / 画像图谱 / 审计 / 二次验证
│   │   ├── agent/               8 个 LangChain 工具 + LangGraph ReAct
│   │   ├── metrics.py           接口调用计数器（供 /system/health 真实统计）
│   │   └── routers/             11 个路由模块
│   ├── scripts/
│   │   ├── init_db.py           建库建表 + 灌 31 规则 / 7 场景 / 5 账号 + 导出 DDL
│   │   ├── gen_business_data.py 生成 17 张业务表模拟数据
│   │   ├── gen_mock_events.py   生成模拟事件流（1622 条主样本 + 240 条边界上下文）
│   │   ├── replay_events.py     按事件时间回放
│   │   ├── selfcheck.py         端到端验收自检（98 项）
│   │   ├── smoke_frontend.py    前端冒烟自检（Edge + CDP，30 个页面实例）
│   │   └── data_health.py       演示数据集体检
│   ├── tests/                   pytest 测试套件（**216 条**）
│   ├── sql/                     自动导出的 DDL（由 ORM 元数据生成）
│   ├── .env.example             环境变量模板
│   └── run.py                   后端启动脚本（带预检）
│
├── frontend/                    Vue 3 + Vite
│   └── src/{api,stores,router,layouts,components,views,styles}/
│
├── .gitignore                   生成物忽略清单（data/、dist/、.env …）
└── start.py                     一键启动
```

---

## 三、快速开始

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

编辑 `backend/.env`：

| 变量 | 必填 | 说明 |
|------|:----:|------|
| `DB_PASSWORD` | ✅ | 本机 MySQL 的连接密码 |
| `LLM_API_KEY` | ⭕ | 阿里云百炼（DashScope）API Key，格式 `sk-xxxxxxxx`。**留空时 AI 助手走降级路径，不影响其余功能** |
| `LLM_BASE_URL` | ⭕ | 模型接口端点（注意保留结尾的 `/v1`） |
| `LLM_MODEL_NAME` | ⭕ | 默认 `qwen3.8-flash` |

> **两个字段别填反**：`LLM_API_KEY` 只放 `sk-` 开头的密钥，
> 接口地址放 `LLM_BASE_URL`。使用私有 MaaS 端点时把该 URL 填到 `LLM_BASE_URL` 即可。

### 4. 一键启动（推荐）

```bash
python start.py             # 自动建库 → 建表 → 灌配置 → 生成数据 → 回放 → 起前后端
python start.py --backend   # 只起后端（前端自行启动）
python start.py --skip-seed # 跳过数据生成与回放（数据已就绪时更快）
python start.py --reset     # 删表重建并重新灌入全部数据（调整过数据模型后用这个）
```

按 `Ctrl+C` 一次停掉全部服务。打开 <http://localhost:5173> → 自动跳转登录页。

> `--reset` 走的是 `init_db.py --recreate`（**删表重建**），不只是清数据行：
> `create_all()` 不会给已存在的表补字段 / 索引 / 外键，只清行不会更新表结构。
> 重建前请先停掉后端 —— Windows 下服务占用数据库时无法完成删表。

### 5. 手动启动

```bash
cd backend
python scripts/init_db.py             # 建库 + 33 张表 + 31 规则 + 5 账号 + 触发器
python scripts/gen_business_data.py   # 17 张业务表模拟数据
python scripts/gen_mock_events.py     # 生成事件流
python scripts/replay_events.py       # 回放，让大盘有数据
python run.py                         # → http://127.0.0.1:8000  /docs

cd ../frontend && npm run dev         # → http://localhost:5173
```

`run.py` 启动时依次打印：数据库连接 → 数据就绪度 → LLM 配置 → **LLM 连通性真实探测**。
看到 `✓ LLM 连通性：可达` 说明该进程可以调用 LLM。

### 6. 演示账号（5 个角色）

| 用户名 | 密码 | 角色 | 默认落地页 |
|---|---|---|---|
| `reviewer` | `reviewer@123` | 风控审核员 | 审核工作台 |
| `strategist` | `strategist@123` | 风控策略师 | 策略规则配置 |
| `analyst` | `analyst@123` | 数据分析师 | AI 智能助手 |
| `admin` | `admin@123` | 系统管理员 | 态势大盘 |
| `ops` | `ops@123` | 系统运维 | 态势大盘 |

---

## 四、验证（四层）

四层分别覆盖「算法与契约」「端到端接口」「浏览器真实渲染」「演示数据形态」，互为补充。

| 层 | 命令 | 覆盖范围 | 最近结果 |
|---|---|---|---|
| ① 单元 + 集成 | `pytest tests/ -q` | 引擎算法、特征口径、接口、权限矩阵、案件状态机、Agent 工具 | **216 通过** |
| ② 端到端自检 | `scripts/selfcheck.py` | 全部验收项 + 五角色专属能力（**需后端已启动**） | **98/98 通过** |
| ③ 前端冒烟 | `scripts/smoke_frontend.py` | 4 个角色的 30 个页面实例在浏览器中真实渲染、无 JS 报错、**渲染出各页预期内容**（**需前后端都启动**） | **7/7 通过** |
| ④ 数据体检 | `scripts/data_health.py` | 演示数据集的分类覆盖、时间分布、角色痕迹、五档分布、事件信息完整度 | **①~⑤ 全部通过** |

```bash
# ① 单元 + 集成（自动建毁测试库 ecs_test，不污染演示库 ecs）
cd backend
.venv/Scripts/python.exe -m pytest tests/ -q
.venv/Scripts/python.exe -m pytest tests/ --cov=app --cov-report=term-missing   # 覆盖率

# ② 端到端自检（先 python run.py）
.venv/Scripts/python.exe scripts/selfcheck.py --url http://127.0.0.1:8000

# ③ 前端冒烟（先 python run.py + npm run dev）
.venv/Scripts/python.exe scripts/smoke_frontend.py --frontend http://localhost:5173

# ④ 数据体检（改动数据生成器后必跑）
.venv/Scripts/python.exe scripts/data_health.py
```

**第 ③ 层为什么必要**：前三层都无法回答「页面在浏览器里真的渲染出来了吗」。
构建通过、接口正常返回，并不等于页面渲染正确 —— 模板取值、条件分支、路由守卫
都只在实际运行时才生效。`smoke_frontend.py` 用 Edge + CDP 真实访问每个页面，
逐页断言渲染出了预期内容，并收集 `console.error` / `pageerror`。

> `selfcheck.py` 会验证「连续失败锁定 → 自动解锁」链路，结束时自动复位演示账号；
> 若自检被中断导致账号仍处锁定状态，重跑一次即可（脚本开头会先复位全部 5 个账号）。
>
> 运行环境无法访问 LLM 域名时（受限网络 / 代理），AI 相关检查项会返回
> `LLM_UNREACHABLE` —— 属环境依赖，其余功能不受影响。

---

## 五、核心设计要点

### 5.1 五档决策

| 档位 | 分值区间（默认） | 决策 | 系统动作 |
|---|---|---|---|
| 1 | 0 ~ 29 | **Pass** 放行 | 同步返回，异步落库 |
| 2 | 30 ~ 59 | **Mark** 标记观察 | 记录并打标，**放行** |
| 3 | 60 ~ 79（命中规则**全部可挑战**） | **Challenge** 二次验证 | 返回验证指令，**不建案**；通过→Pass，失败/超时→Review |
| 4 | 60 ~ 79（命中含**不可挑战**规则） | **Review** 人审 | 自动建案，推送工作台 |
| 5 | 80 ~ 100 | **Reject** 拦截 | 拦截交易 + 自动建案 |

Challenge 是**瞬态**档位：二次验证通过即升级为 Pass（不建案），失败或超时升级为 Review（建案）。
因此大盘上「决策当刻」与「当前状态」两个口径会不同，页面均标注了统计口径。

### 5.2 评分与一票否决

```text
final = min( max(命中分值) + BONUS × (命中数 − 1), 100 )     # BONUS 默认 3
若命中任一「极高」等级规则 → final = max(final, 90)          # 一票否决
```

以最高分为主、额外加分体现「数量佐证」，避免多条低分规则叠加造成误判。
规则可标注 `challengeable`：**不可挑战**的规则命中后不再走二次验证，直接进入人审。

### 5.3 33 个特征（4 维度）

| 维度 | 数量 | 数据源 |
|---|---:|---|
| 用户 | 14 | `order_info` / `order_detail` / `postsale` / `receive_info` / `logistics_complaints_record` |
| 订单 | 8 | `order_detail` / `order_info` / `sku_info` |
| 地址 | 3 | `receive_info` / `order_info` |
| 设备与团伙 | 8 | 进程内滑动窗口（识别同设备 / 同 IP / 同地址的账号矩阵） |

> 「设备与团伙」维度是**团伙化作弊识别**的关键：单个账号看正常，
> 但同一设备下聚集了多个账号 → 命中「同设备多账号」（极高，不可挑战）→ 直接拦截。

### 5.4 实现约定

- **窗口基准是事件时间戳**，不是服务器当前时间 —— 保证回放与仿真结果可复现
- **先写窗口再算特征** —— 目标事件计入自身特征
- **`order_pay_interval_sec` 未支付时取 -1 哨兵值** —— 与「支付间隔为 0 秒」区分开
- **规则缓存保存不可变快照** —— 避免跨请求访问 ORM 实例
- **审计表由数据库触发器禁止 UPDATE / DELETE**，应用层亦不提供改删路由

---

## 六、演示数据集

`python start.py` 会自动生成一整套可复现的演示数据（固定随机种子）。
数据形态由 `scripts/data_health.py` 持续校验。

### 6.1 业务数据：17 张表

用户与收货信息、商品与 SKU、订单与明细、物流与投诉、售后与退款等，
覆盖全部业务链路，供特征计算与案件详情反查使用。

### 6.2 事件数据：1862 条，跨 90 天

| 类别 | 条数 | 特征 |
|------|-----:|------|
| 正常用户事件 | 900 | 各特征正常，按真实作息分布 |
| 风险画像样本 | 50 | 10 类画像各 5 人（高频下单 / 高频售后 / 频繁取消 / 高额退款 / 多地址跨省 / 批发扫货 / 大额折扣 / 秒付 / 夜间大额 / 零订单新号） |
| 羊毛党领券 | 186 | 同设备多账号 + 高频领券 |
| 恶意退款 | 108 | 同地址聚集 + 同手机号高退款率 |
| 黄牛囤货 | 216 | 同设备高频大额下单 |
| 物流投诉异常 | 42 | 5 个惯投诉账号 |
| 边界样本 | 360 | 分值落在阈值边界，用于验证 Challenge 分支 |

**时间分布**：天粒度均匀铺开（≈10/天），天内按真实电商作息加权 ——
晚间 18~23 点为峰、凌晨 0~5 点为谷，实测峰谷比约 **1.8×**。
团伙作案采用「**90 天内多次作案窗口**」形态：每次持续 1~3 小时、窗口内密集操作，
符合滑窗特征的识别要求，也避免时间轴出现人为尖峰。

**单据绑定**：事件通过 `source_id` 指向真实业务单据（订单号 / 售后单号 / 投诉记录号），
金额与订单维度特征由反查得到 —— 因此「挽回资损」「当前订单」「拦截订单」等展示均取自真实数据。

**事件快照 `event_data`**：每条事件携带该类型专有的业务上下文，
例如 `order_create` 带渠道 / 支付方式 / 活动号，`login` 带登录类型 / 场景 / 结果。

### 6.3 复现与体检

```bash
python start.py --reset                  # 完整重跑：建表 → 业务数据 → 事件 → 回放
.venv/Scripts/python.exe scripts/data_health.py    # 体检数据集形态
```

---

## 七、主要接口

统一前缀 `/api/v1`，响应 `{ "code": 0, "message": "success", "data": ... }`。

| 模块 | 方法与路径 | 说明 |
|---|---|---|
| 认证 | `POST /auth/login`、`GET /auth/me`、`POST /auth/logout`、`POST /auth/change-password` | JWT（8 小时） |
| 事件网关 | `POST /risk/check` ⭐ | **免 JWT**，走 `X-Service-Id` 服务标识，供外部业务系统接入 |
| | `GET /risk/event/{id}`、`GET /events` | 事件详情 / 列表 |
| 规则 | `GET/POST /rules`、`PUT/DELETE /rules/{id}`、`PATCH\|PUT /rules/{id}/toggle` | 读放开 4 角色，**写仅 strategist** |
| 场景阈值 | `GET /rules/scenes`、`PUT /rules/scenes/{code}` | **改：仅 admin** |
| 特征 | `GET /features/dict` | 33 条，可按维度过滤 |
| 名单 | `GET/POST/DELETE /blacklist`、`POST /blacklist/import`、`GET /blacklist/stats` | 五维 × 黑白灰 |
| 案件 | `GET /cases`、`GET /cases/statistics`、`GET /cases/{id}` | 列表 / 统计 / 详情；`case_status`、`risk_level` 筛选中英文枚举均可 |
| | `POST /cases/{id}/dispose\|hold\|close`、`POST /cases/batch-dispose` | 处置 / 认领 / 关闭；批量处置**仅 reviewer** |
| 画像 | `GET /profile/{uid}`、`GET /graph/{uid}` | 用户画像（含降级）/ 关联图谱 |
| 大盘 | `GET /dashboard/overview\|trend\|risk_dist\|top_rules\|realtime_events\|loss_prevented\|rule_effectiveness` | 态势指标；`trend` 与 `rule_effectiveness` 支持 `?days=&event_type=` |
| 系统 | `GET /system/health`、`GET /system/stats`、`GET /system/logs`、`GET /system/config` | admin / ops |
| 审计 | `GET /audit/logs` | admin 全量 / 其余仅本人；**无改删接口** |
| 仿真 | `POST /simulate/run`、`GET /simulate/templates`、`POST /simulate/run_template/{code}` | 不落库 |
| AI Agent | `POST /agent/chat`、`POST /agent/clear`、`GET /agent/sessions`、`GET /agent/sessions/{id}/messages`、`DELETE /agent/sessions/{id}`、`GET /agent/tools`、`GET /agent/status` | strategist / analyst / admin（会话按归属隔离） |
| 二次验证 | `POST /challenge/{event_id}/resolve`、`GET /challenge/{event_id}` | 验证回调与查询 |
| 分析 | `GET /analysis/export` | 分析报告导出（CSV + UTF-8 BOM），**analyst / strategist / admin** |
| 运维 | `GET /admin/health`（免鉴权）、`POST /admin/reset`、`POST /admin/seed-config` | 存活探针与数据维护 |

`/docs` 提供完整的 OpenAPI 交互式文档。

**服务端专用接口**（无对应页面，供外部集成与自动化调用）

| 接口 | 用途 |
|---|---|
| `POST /risk/check` | 外部业务系统的事件接入入口；页面侧用 `/simulate/run` 演示同一决策链路 |
| `GET /profile/{uid}`、`GET /graph/{uid}` | 画像与图谱同时内嵌在案件详情响应中，独立端点供 Agent 工具与外部集成使用 |
| `POST` / `GET /challenge/**` | 真实链路中由外部系统或短信通道回调，页面不承载该动作 |
| `GET /system/config` | 运维只读配置（登录策略、模型名、图谱上限） |
| `GET /admin/health`、`POST /admin/reset`、`POST /admin/seed-config` | 存活探针与数据维护，由脚本或容器探针调用 |

---

## 八、AI 智能助手

基于 **LangGraph ReAct** 循环，温度 0.1（风控场景需要确定性输出）。

**8 个内置工具**

| 分组 | 工具 | 功能 |
|---|---|---|
| 风控决策 | `risk_check` | 实时风险检查 |
| | `query_cases` | 案件查询 |
| | `query_user_profile` | 用户画像 |
| | `manage_blacklist` | 名单管理（add / remove / check / list） |
| 数据分析 | `query_dashboard_stats` | 仪表盘统计 |
| | `analyze_risk_trend` | 趋势分析 |
| | `analyze_rule_effectiveness` | 规则效果分析 |
| | `query_business_data` | 业务数据查询（6 种类型） |

**降级设计**：未配置 `LLM_API_KEY` 时返回明确的降级提示（`degraded=true`）并禁用输入框，
**不影响风控主链路与其余 12 个页面**。每次工具调用写入审计（`biz_type = agent_call`）。

**试试这样问**

- 「查看今天的风控统计数据」
- 「帮我检查一下用户 U-1401 的风险」
- 「哪些规则效果不好？」
- 「分析最近 30 天的风控趋势」

---

## 九、演示建议路径

1. **态势大盘**（admin）→ 5 个指标卡、风险趋势、四档分布、规则排行、实时事件滚屏
   （**点击任意一行可展开该条事件的完整报文与快照**）
2. **审核工作台**（reviewer）→ 筛选「极高 + 待审」→ 打开案件详情
3. **案件详情** → 决策摘要（含**一票否决标记**）→ 33 个特征分维度对比 → 关联图谱
   → 「查看事件快照」→ 选择处置结论 → 二次确认 → 提交（订单状态联动变更）
4. **审计日志**（admin）→ 展开查看变更前后 diff；确认无删除 / 编辑入口
5. **事件仿真**（strategist）→ 依次回放 4 个模板，观察判定链路四步与 Challenge 分支
6. **策略规则配置** → 内联改分值、切换启停；查看规则条件树编辑器
7. **特征与阈值**（admin 改阈值 / strategist 只读）→ 调整 `mark_threshold` 后回仿真页观察结论变化
8. **AI 智能助手**（analyst）→ 用自然语言提问，观察工具调用过程
9. **权限验证** → 用 reviewer 登录后地址栏直敲 `/strategy/rules` → 渲染 403 页

---

## 十、范围外

不含：真实高并发分布式部署与多机房容灾、机器学习模型训练与在线推理
（LLM 仅用于 Agent 对话与工具编排，**不参与风险评分**）、真实设备指纹 SDK 采集、
对接真实支付 / 物流 / 售后生产系统、完整 RBAC 权限模型、SSO 单点登录、
容器化部署与 CI/CD、移动端适配。
