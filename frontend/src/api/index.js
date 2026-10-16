/**
 * 统一请求封装（SPEC §0.4.3 src/api/index.js、R-FE2-10）。
 *
 * 职责：
 *  1. 请求拦截器统一注入 Authorization: Bearer <token>
 *  2. 响应拦截器拆掉 {code, message, data} 外层，业务层直接用 data
 *  3. code 4001（未认证/过期）→ 清本地 token + 清 Pinia 状态 + 跳 /login
 *  4. 其他非 0 code → 抛出带 code 的 Error，由页面提示（如 4003 权限不足）
 *
 * 接口分组对应 SPEC 模块：1 事件网关 / 3 规则名单 / 4 案件 / 5 画像图谱 /
 * 6 监控审计 / 7 仿真 / 9 AI Agent / 11 认证。
 */
import axios from 'axios'

const http = axios.create({
  baseURL: '/api/v1',
  // 必须 ≥ 后端 `LLM_TIMEOUT_SECONDS`（当前 30s，见 backend/.env）。
  // AI 助手走 LangGraph ReAct，一轮可能连续调用多个工具，最坏情况耗时
  // ≈ 工具数 × 单次 LLM 超时；前端若先超时，会出现「后端仍在执行、
  // 用户已看到失败」的假故障。留足余量 → 120s。
  // ⚠ 改后端 LLM_TIMEOUT_SECONDS 时请同步核对这里。
  timeout: 120000,
  headers: { 'Content-Type': 'application/json' },
})

/** 供 App.vue 注入的跳转回调，避免 api 层直接依赖 router（循环引用） */
let onUnauthorized = null
export function setUnauthorizedHandler(fn) {
  onUnauthorized = fn
}

/**
 * 认证失效统一处理（R-FE2-10 / R-AU-07）：
 * 清 sessionStorage → 清 Pinia 内存态（防止守卫把 /login 又弹回落地页）→ 通知 App 跳转。
 */
async function handleAuthExpired() {
  sessionStorage.removeItem('ercs_token')
  sessionStorage.removeItem('ercs_user')
  try {
    // 动态导入，避免 api ↔ store 循环引用在模块初始化期报错
    const { useUserStore } = await import('@/stores/user')
    useUserStore().clear()
  } catch {
    // pinia 尚未就绪（如应用启动早期）时忽略
  }
  if (onUnauthorized) onUnauthorized()
}

http.interceptors.request.use((config) => {
  const token = sessionStorage.getItem('ercs_token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

http.interceptors.response.use(
  (response) => {
    const body = response.data
    // 非标准包装（如 /docs）直接返回
    if (!body || typeof body !== 'object' || !('code' in body)) return body

    const code = String(body.code)
    if (code === '0') return body.data ?? null
    if (code === '4001') {
      handleAuthExpired()
    }
    return Promise.reject(Object.assign(new Error(body.message || '请求失败'), { code }))
  },
  (error) => {
    const resp = error.response
    const body = resp?.data || {}
    const code = String(body.code ?? resp?.status ?? '')
    const message = body.message || error.message || '网络异常'

    // R-FE2-10：4001 / HTTP 401 → 清 token 跳登录页
    if (code === '4001' || resp?.status === 401) {
      handleAuthExpired()
    }

    return Promise.reject(Object.assign(new Error(message), {
      code,
      status: resp?.status,
    }))
  },
)

/* ---------------------------------------------------------- 模块 11 认证 */
export const authApi = {
  login: (payload) => http.post('/auth/login', payload),
  me: () => http.get('/auth/me'),
  logout: () => http.post('/auth/logout'),
  changePassword: (payload) => http.post('/auth/change-password', payload),
  users: () => http.get('/auth/users'),
  /** 管理员专属：账号锁定状态 / 手动解锁 */
  lockState: (userId) => http.get(`/auth/users/${userId}/lock-state`),
  unlock: (userId) => http.post(`/auth/users/${userId}/unlock`),
}

/* ---------------------------------------- 模块 6 分析报告导出（分析师专属） */
/**
 * 拉取后端生成的 CSV 报告（带 UTF-8 BOM，Excel 打开不乱码）。
 *
 * ⚠ 必须走 blob：走默认 JSON 拦截器会把二进制当 JSON 解析而失败。
 *   这里直接用 axios 实例、不经响应拦截器的 `code` 拆包逻辑。
 */
export function fetchAnalysisCsv(days = 30, eventType = '') {
  return http.get('/analysis/export', {
    params: { days, ...(eventType ? { event_type: eventType } : {}) },
    responseType: 'blob',
  })
}

/* ------------------------------------- 模块 2/3 规则、场景阈值、特征字典 */
export const ruleApi = {
  list: (params) => http.get('/rules', { params }),
  get: (ruleId) => http.get(`/rules/${ruleId}`),
  create: (payload) => http.post('/rules', payload),
  update: (ruleId, payload) => http.put(`/rules/${ruleId}`, payload),
  toggle: (ruleId) => http.put(`/rules/${ruleId}/toggle`),
  remove: (ruleId) => http.delete(`/rules/${ruleId}`),
  scenes: () => http.get('/rules/scenes'),
  updateScene: (sceneCode, payload) => http.put(`/rules/scenes/${sceneCode}`, payload),
  featureDict: (params) => http.get('/features/dict', { params }),
  /** 策略师专属：规则仿真对比（含该规则 vs 排除该规则） */
  compare: (ruleId, payload) => http.post(`/rules/${ruleId}/compare`, payload),
}

/* ---------------------------------------------- 模块 3 名单库（/blacklist） */
export const blacklistApi = {
  list: (params) => http.get('/blacklist', { params }),
  create: (payload) => http.post('/blacklist', payload),
  remove: (id) => http.delete(`/blacklist/${id}`),
  importItems: (payload) => http.post('/blacklist/import', payload),
  stats: (params) => http.get('/blacklist/stats', { params }),
}

/* ------------------------------------------------------- 模块 4 案件流转 */
export const caseApi = {
  list: (params) => http.get('/cases', { params }),
  statistics: (params) => http.get('/cases/statistics', { params }),
  detail: (caseId) => http.get(`/cases/${caseId}`),
  dispose: (caseId, payload) => http.post(`/cases/${caseId}/dispose`, payload),
  hold: (caseId, payload) => http.post(`/cases/${caseId}/hold`, payload),
  close: (caseId, payload) => http.post(`/cases/${caseId}/close`, payload),
  /** 审核员专属：批量处置（逐件返回结果，单件失败不影响其他件） */
  batchDispose: (payload) => http.post('/cases/batch-dispose', payload),
}

/* ------------------------------------------------- 模块 1 事件详情（/risk） */
/**
 * 单条事件的完整快照：事件字段 + `event_data` + 33 个特征 + 命中规则 + 评估结果。
 *
 * ⚠ 该接口后端早就实现（SPEC API-1.2），但前端一直**没有封装、也没有任何页面调用** ——
 *   属于「后端有、前端没人用」的死接口，事件报文里的 `event_data` 因此永远没机会露面。
 *   大盘滚屏与案件详情现在都通过它来展示事件快照。
 *   （列表接口 `GET /risk/events` 暂无页面需要，仍不封装。）
 */
export const eventApi = {
  detail: (eventId) => http.get(`/risk/event/${eventId}`),
}

/* -------------------------------------------- 模块 6 大盘监控（含健康度） */
export const dashboardApi = {
  overview: () => http.get('/dashboard/overview'),
  trend: (days = 7, eventType = '') => http.get('/dashboard/trend', {
    params: { days, ...(eventType ? { event_type: eventType } : {}) },
  }),
  riskDist: () => http.get('/dashboard/risk_dist'),
  topRules: (top = 5) => http.get('/dashboard/top_rules', { params: { top } }),
  realtimeEvents: (limit = 20) => http.get('/dashboard/realtime_events', { params: { limit } }),
  lossPrevented: () => http.get('/dashboard/loss_prevented'),
  ruleEffectiveness: (params) => http.get('/dashboard/rule_effectiveness', { params }),
}

/* -------------------------------------------------------- 模块 6 审计 */
export const auditApi = {
  logs: (params) => http.get('/audit/logs', { params }),
}

/* ------------------------------------------ 模块 6 系统健康度与数据统计 */
export const systemApi = {
  health: () => http.get('/system/health'),
  stats: () => http.get('/system/stats'),
  /** 运维专属：最近的运行日志（内存环形缓冲，最多 500 条） */
  logs: (params) => http.get('/system/logs', { params }),
}

/* ---------------------------------------------------------- 模块 7 仿真 */
export const simulateApi = {
  run: (payload) => http.post('/simulate/run', payload),
  templates: () => http.get('/simulate/templates'),
  runTemplate: (templateCode) => http.post(`/simulate/run_template/${templateCode}`),
}

/* ------------------------------------------------------- 模块 9 AI Agent */
export const agentApi = {
  chat: (payload) => http.post('/agent/chat', payload),
  clear: (sessionId) => http.post('/agent/clear', { session_id: sessionId }),
  sessions: () => http.get('/agent/sessions'),
  /** 读取指定会话的历史消息（切换会话时回填气泡与工具卡片） */
  sessionMessages: (sessionId) => http.get(`/agent/sessions/${sessionId}/messages`),
  /** 彻底删除会话（会话条目 + 全部消息），区别于 clear 的「仅清空消息」 */
  deleteSession: (sessionId) => http.delete(`/agent/sessions/${sessionId}`),
  tools: () => http.get('/agent/tools'),
  status: () => http.get('/agent/status'),
}

/*
 * ------------------------------------------------- 模块 3 二次验证（无前端封装）
 *
 * `POST /challenge/{event_id}/resolve`（SPEC API-3.15）与
 * `GET /challenge/{event_id}`（API-3.16）**刻意不提供前端封装**：
 * 仿真执行恒为 dry_run，不落库 → 不产生 `risk_challenge_record`，
 * 页面里没有真实 `event_id` 可回调；而真实事件的 Challenge 回调是
 * **外部系统 / 短信通道**的职责，不是后台页面的按钮。
 * 该能力由 pytest（`test_api.py`）与 `scripts/selfcheck.py` 直接驱动接口验证。
 */
export default http
