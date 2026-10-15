<script setup>
/**
 * 页面九 /chat —— AI 智能助手（SPEC 模块 9 / FR-56~65 / R-FE2-18）。
 *
 * · 会话列表 + 对话气泡 + 工具调用过程可见 + 输入区
 * · LLM 未配置时顶部展示**降级横幅**，并禁用输入（R-AG-01）
 * · 每次工具调用在后端写审计（biz_type = agent_call，FR-65）
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'

import { agentApi } from '@/api'
import ConfirmDialog from '@/components/ConfirmDialog.vue'
import StateBlock from '@/components/StateBlock.vue'
import ToolCallCard from '@/components/ToolCallCard.vue'
import { useUserStore } from '@/stores/user'

const store = useUserStore()

const loading = ref(true)
const error = ref('')
const sending = ref(false)
const toast = ref('')

const sessions = ref([])
const activeSession = ref(null)
const messages = ref([])          // [{ role:'me'|'ai', content, toolCalls: [] }]
const input = ref('')
const inputEl = ref(null)
const bodyEl = ref(null)

const status = ref({ llm_configured: true, model: '', tool_count: 8 })
const tools = ref([])

const pendingDelete = ref(null)   // 待删除的会话对象（非空即弹出二次确认）
const deleting = ref(false)

const degraded = computed(() => status.value.llm_configured === false)
const canSend = computed(() => !sending.value && !degraded.value && input.value.trim().length > 0)

/** 按工具分组展示（风控决策 / 数据分析） */
const toolGroups = computed(() => {
  const groups = {}
  tools.value.forEach((t) => {
    groups[t.group] = groups[t.group] || []
    groups[t.group].push(t)
  })
  return Object.entries(groups).map(([name, items]) => ({ name, items }))
})

/** 工具名 → 示例问题：点击工具标签即填入输入框，降低试用门槛 */
const TOOL_SAMPLES = {
  risk_check: '帮我检查一下用户 U-1401 的实时风险',
  query_cases: '列出当前待审核的风险案件',
  query_user_profile: '查看用户 U-1401 的风险画像',
  manage_blacklist: '查一下黑名单列表目前的情况',
  query_dashboard_stats: '查看今天的风控统计数据',
  analyze_risk_trend: '帮我分析最近 7 天的风控趋势',
  analyze_rule_effectiveness: '哪些规则效果不好？',
  query_business_data: '查一下最近的高价值订单',
}

/** 点击工具标签：把对应示例问题填入输入框并聚焦（不自动发送，留给用户确认后再发）。 */
function useTool(t) {
  if (degraded.value) {
    toast.value = 'LLM 未配置，AI 助手暂不可用'
    return
  }
  input.value = TOOL_SAMPLES[t.name] || `请用 ${t.name} 工具帮我查一下`
  toast.value = `已填入示例问题，按 Enter 或点「发送」即可`
  nextTick(() => inputEl.value?.focus())
}

function scrollToBottom() {
  nextTick(() => {
    if (bodyEl.value) bodyEl.value.scrollTop = bodyEl.value.scrollHeight
  })
}

async function loadStatus() {
  try {
    status.value = await agentApi.status()
    tools.value = await agentApi.tools()
  } catch {
    status.value = { llm_configured: false, model: '未知', tool_count: 8 }
  }
}

async function loadSessions() {
  try {
    sessions.value = await agentApi.sessions()
  } catch (err) {
    error.value = err.message
  }
}

async function send() {
  const text = input.value.trim()
  if (!text || sending.value || degraded.value) return

  messages.value.push({ role: 'me', content: text, toolCalls: [] })
  input.value = ''
  sending.value = true
  scrollToBottom()

  try {
    const data = await agentApi.chat({
      message: text,
      session_id: activeSession.value || null,
    })
    activeSession.value = data.session_id
    messages.value.push({
      role: 'ai',
      content: data.reply,
      toolCalls: data.tool_calls || [],
    })
    if (data.degraded) {
      status.value = { ...status.value, llm_configured: false }
      toast.value = `AI 助手降级：${data.degrade_reason || 'LLM 不可用'}`
    } else {
      await loadSessions()
    }
  } catch (err) {
    messages.value.push({
      role: 'ai',
      content: `请求失败：${err.message}`,
      toolCalls: [],
    })
  } finally {
    sending.value = false
    scrollToBottom()
  }
}

/** 从后端拉取指定会话的历史消息并回填（修复「切换会话后历史消失」）。 */
async function loadMessages(sid) {
  messages.value = []
  try {
    const rows = await agentApi.sessionMessages(sid)
    messages.value = (rows || []).map((r) => ({
      role: r.role === 'me' ? 'me' : 'ai',
      content: r.content,
      toolCalls: r.tool_calls || [],
    }))
    scrollToBottom()
  } catch (err) {
    toast.value = `加载历史消息失败：${err.message}`
  }
}

async function clearSession() {
  if (!activeSession.value) return
  try {
    await agentApi.clear(activeSession.value)
    messages.value = []
    toast.value = '会话消息已清空（会话条目保留，可在左侧列表中删除）'
    await loadSessions()
  } catch (err) {
    toast.value = `清除失败：${err.message}`
  }
}

function newSession() {
  activeSession.value = null
  messages.value = []
  toast.value = ''
}

async function switchSession(sid) {
  if (sid === activeSession.value || sending.value) return
  activeSession.value = sid
  await loadMessages(sid)
}

/** 打开删除确认弹窗（列出具体动作，符合 SPEC §7.3 交互约定） */
function askDelete(session) {
  pendingDelete.value = session
}

/** 彻底删除会话（会话条目 + 全部消息）。 */
async function doDelete() {
  const target = pendingDelete.value
  if (!target) return
  deleting.value = true
  try {
    await agentApi.deleteSession(target.session_id)
    if (target.session_id === activeSession.value) {
      activeSession.value = null
      messages.value = []
    }
    pendingDelete.value = null
    await loadSessions()
    toast.value = '会话已删除'
  } catch (err) {
    toast.value = `删除失败：${err.message}`
  } finally {
    deleting.value = false
  }
}

function onKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    send()
  }
}

onMounted(async () => {
  await Promise.all([loadStatus(), loadSessions()])
  loading.value = false
})

watch(messages, scrollToBottom, { deep: true })
</script>

<template>
  <StateBlock :loading="loading" :error="error" loading-text="加载 AI 助手…">

    <!-- 降级横幅（R-AG-01）：按后端返回的 config_hint 展示**精确**修复指引 -->
    <div v-if="degraded" class="alert alert-warn mb-16">
      <svg width="15" height="15" viewBox="0 0 16 16" fill="none" style="flex:0 0 15px;margin-top:2px">
        <path d="M8 1.8l6.2 11H1.8L8 1.8z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>
        <path d="M8 6.4v3.2M8 11.6v.6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
      </svg>
      <div>
        <b>AI 助手当前不可用</b>：{{ status.config_hint || 'LLM 配置有误' }}<br>

        <!-- 最常见的误填：把接口地址填进了密钥字段 -->
        <div v-if="status.key_shape === 'looks_like_url'" style="margin-top:8px">
          参考写法（<span class="mono">backend/.env</span>）：
          <div class="code" style="margin-top:6px">LLM_API_KEY=sk-xxxxxxxxxxxxxxxx
LLM_BASE_URL={{ status.base_url }}</div>
        </div>

        ⚠ 配置问题<b>不影响</b>风控主链路与其余 12 个页面 ——
        可改用「态势大盘」「趋势分析」「审核工作台」完成分析。下方工具清单仍可查看。
      </div>
    </div>

    <div v-if="toast" class="alert alert-info mb-16">
      <div>{{ toast }}</div>
    </div>

    <div class="chat-wrap">

      <!-- ============ 左：会话列表 ============ -->
      <div class="card" style="margin-bottom:0">
        <div class="card-hd" style="padding:12px 16px">
          <span>会话</span>
          <span class="link" style="font-size:12.5px" @click="newSession">+ 新建</span>
        </div>
        <div class="card-bd tight" style="padding:10px">
          <div v-if="!sessions.length" class="muted" style="padding:12px 4px;font-size:12.5px">
            暂无历史会话
          </div>
          <div
            v-for="s in sessions" :key="s.session_id"
            class="sess-item" :class="{ on: s.session_id === activeSession }"
            @click="switchSession(s.session_id)"
          >
            <div class="sess-hd">
              <div class="t">{{ s.title || '未命名会话' }}</div>
              <span class="del" title="删除该会话" @click.stop="askDelete(s)">×</span>
            </div>
            <div class="m">
              {{ (s.update_time || s.create_time || '').slice(11, 16) }} · {{ s.message_count }} 条消息
            </div>
          </div>
        </div>
      </div>

      <!-- ============ 右：对话区 ============ -->
      <div class="card chat-panel" style="margin-bottom:0">

        <div class="card-hd">
          <span>对话</span>
          <span class="row-gap-8">
            <span class="tag tag-purple">{{ status.model || 'qwen-plus' }}</span>
            <span v-if="activeSession" class="muted mono" style="font-size:11.5px">
              {{ activeSession.slice(0, 20) }}…
            </span>
            <span v-else class="muted" style="font-size:11.5px">新会话</span>
          </span>
        </div>

        <div ref="bodyEl" class="chat-body">
          <div v-if="!messages.length" class="empty" style="padding:64px 20px">
            <span class="ic">⚡</span>
            <div v-if="degraded">AI 助手不可用，请先配置 LLM_API_KEY</div>
            <div v-else>
              试试问：<br>
              「查看今天的风控统计数据」<br>
              「帮我检查一下用户 U-1401 的风险」<br>
              「哪些规则效果不好？」
            </div>
          </div>

          <template v-for="(m, i) in messages" :key="i">
            <!-- 消息气泡 -->
            <div class="msg" :class="m.role === 'me' ? 'me' : 'ai'">
              <div class="av">{{ m.role === 'me' ? (store.displayName.slice(0, 1) || 'U') : 'AI' }}</div>
              <div class="bubble" style="white-space:pre-wrap">{{ m.content }}</div>
            </div>

            <!-- 工具调用过程（R-FE2-18） -->
            <div v-if="m.toolCalls?.length" class="msg ai" style="margin-top:-8px;margin-bottom:18px">
              <div class="av" style="visibility:hidden">AI</div>
              <div style="flex:1;min-width:0">
                <ToolCallCard
                  v-for="(tc, j) in m.toolCalls" :key="j"
                  :tool-name="tc.tool"
                  :args="tc.args"
                  :result-summary="tc.result_summary"
                  :cost-ms="tc.cost_ms"
                  :status="tc.status"
                />
              </div>
            </div>
          </template>

          <div v-if="sending" class="msg ai">
            <div class="av">AI</div>
            <div class="bubble muted" style="font-style:italic">
              <span class="spinner" style="width:12px;height:12px;border-width:2px;display:inline-block;vertical-align:-2px;margin:0 6px 0 0" />
              正在思考…
            </div>
          </div>
        </div>

        <!-- 输入区 -->
        <div class="chat-input">
          <textarea
            ref="inputEl"
            v-model="input" rows="2"
            :disabled="degraded || sending"
            :placeholder="degraded ? 'LLM 未配置，暂不可用' : '输入问题，Enter 发送 · Shift+Enter 换行'"
            @keydown="onKeydown"
          />
          <div class="bar">
            <span class="muted" style="font-size:11.5px">
              工具调用会写入审计日志（<span class="mono">biz_type = agent_call</span>）
            </span>
            <div class="row-gap-8">
              <button class="btn btn-sm" :disabled="!activeSession" @click="clearSession">清除会话</button>
              <button class="btn btn-sm btn-primary" :disabled="!canSend" @click="send">
                {{ sending ? '发送中…' : '发送' }}
              </button>
            </div>
          </div>
        </div>

      </div>
    </div>

    <!-- ============ 可用工具清单 ============ -->
    <div class="card">
      <div class="card-hd">
        <span>可用工具（{{ tools.length || status.tool_count }} 个）</span>
        <span class="muted">点击任一工具可填入示例问题 · Agent 按问题自主选择并组合调用</span>
      </div>
      <div class="card-bd">
        <div v-for="(g, gi) in toolGroups" :key="g.name">
          <div class="sec-title" :style="gi === 0 ? { marginTop: 0 } : {}">
            <span>{{ g.name }}</span><span class="muted">{{ g.items.length }} 个</span>
          </div>
          <div class="row-gap-12" style="margin-bottom:6px">
            <button
              v-for="t in g.items" :key="t.name"
              type="button" class="tool-chip"
              :title="`点击填入示例问题：${TOOL_SAMPLES[t.name] || t.name}`"
              @click="useTool(t)"
            >
              <span class="dot dot-purple" />{{ t.name }}
              <span class="muted">{{ t.description }}</span>
            </button>
          </div>
        </div>

        <div class="muted" style="font-size:12px;margin-top:14px;line-height:1.9">
          <span class="mono">query_business_data</span> 支持 6 种查询类型：
          <span class="mono">user_orders</span> 用户订单 ·
          <span class="mono">user_postsales</span> 用户售后 ·
          <span class="mono">user_addresses</span> 用户地址 ·
          <span class="mono">order_detail</span> 订单明细 ·
          <span class="mono">recent_orders</span> 最近订单 ·
          <span class="mono">high_value_orders</span> 高价值订单
        </div>
      </div>
    </div>

    <div class="alert alert-info">
      <div><b>评审要点</b>：本页对应 PRD §7.9 与 SPEC 模块 9。
        Agent 基于 <b>LangGraph ReAct</b> 循环，温度 0.1 保证风控场景输出稳定；
        本页仅 <b>策略师 / 数据分析师 / 管理员</b> 可访问。
      </div>
    </div>

    <!-- 删除会话的二次确认（必须列出具体动作，SPEC §7.3 交互约定） -->
    <ConfirmDialog
      :visible="!!pendingDelete"
      title="删除会话"
      :actions="[
        `删除会话「${pendingDelete?.title || '未命名会话'}」（${pendingDelete?.message_count ?? 0} 条消息）`,
        '该会话的全部对话与工具调用记录将被永久删除',
      ]"
      warning="删除后不可恢复。若只想清空内容、保留会话，请改用「清除会话」。"
      confirm-text="确认删除"
      :loading="deleting"
      @confirm="doDelete"
      @cancel="pendingDelete = null"
    />

  </StateBlock>
</template>

<style scoped>
.chat-wrap { display: grid; grid-template-columns: 250px 1fr; gap: 16px; align-items: start; }

.sess-item { display: block; padding: 9px 11px; border-radius: 6px; cursor: pointer;
             border: 1px solid transparent; margin-bottom: 4px; }
.sess-item:hover { background: #f5f7fa; }
.sess-item.on { background: var(--primary-bg); border-color: var(--primary-border); }
.sess-hd { display: flex; align-items: center; gap: 6px; }
.sess-item .t { flex: 1; min-width: 0; font-size: 13px;
                white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.sess-item .m { font-size: 11.5px; color: var(--text3); margin-top: 2px; }
.sess-item .del { flex: 0 0 auto; font-size: 14px; line-height: 1; color: var(--text3);
                  padding: 1px 4px; border-radius: 3px; opacity: 0; transition: opacity .15s; }
.sess-item:hover .del, .sess-item.on .del { opacity: 1; }
.sess-item .del:hover { color: var(--danger-text); background: #fff1f0; }

.chat-panel { display: flex; flex-direction: column; height: calc(100vh - 200px); min-height: 520px; }
.chat-body { flex: 1; overflow-y: auto; padding: 18px; background: #fbfcfe; }
.msg { display: flex; gap: 10px; margin-bottom: 18px; }
.msg.me { flex-direction: row-reverse; }
.msg .av { width: 32px; height: 32px; border-radius: 50%; flex: 0 0 32px;
           display: flex; align-items: center; justify-content: center;
           font-size: 12.5px; font-weight: 600; }
.msg.me .av { background: var(--primary); color: #fff; }
.msg.ai .av { background: var(--purple-bg); color: var(--purple); border: 1px solid #d3adf7; }
.msg .bubble { max-width: 76%; padding: 10px 14px; border-radius: 10px;
               font-size: 13.5px; line-height: 1.75; word-break: break-word; }
.msg.me .bubble { background: var(--primary); color: #fff; }
.msg.ai .bubble { background: #fff; border: 1px solid var(--border); }

.chat-input { border-top: 1px solid var(--border); background: #fff; padding: 14px 18px; }
.chat-input textarea { width: 100%; resize: none; font-size: 13.5px; font-family: inherit; }
.chat-input .bar { display: flex; align-items: center; justify-content: space-between; margin-top: 10px; }

.tool-chip { display: inline-flex; align-items: center; gap: 5px; padding: 3px 9px;
             border-radius: 4px; font-size: 11.5px; background: #fafbfc;
             border: 1px solid var(--border); color: var(--text2);
             font-family: var(--mono); margin: 0 6px 6px 0; line-height: 1.6;
             cursor: pointer; transition: background .15s, border-color .15s, color .15s; }
.tool-chip:hover { background: var(--primary-bg); border-color: var(--primary-border);
                   color: var(--primary); }
.tool-chip:active { transform: translateY(1px); }
.tool-chip:focus-visible { outline: 2px solid var(--primary-border); outline-offset: 1px; }
</style>
