<script setup>
/**
 * 页面六 /strategy/watchlist —— 名单库管理（v3.0）
 *
 * 五维 user / phone / ip / device / address × 三类型 black / white / gray（FR-36/37）。
 * API-3.10 ~ 3.14：blacklistApi.list / create / remove / importItems / stats。
 * 过期记录保留可查但不触发拦截（expire_time / expire_at 为空表示永久）；
 * 手机号等敏感值由后端统一脱敏（R-FE2-03）。
 */
import { computed, onMounted, reactive, ref } from 'vue'

import { blacklistApi } from '@/api'
import ConfirmDialog from '@/components/ConfirmDialog.vue'
import RiskTag from '@/components/RiskTag.vue'
import StateBlock from '@/components/StateBlock.vue'
import { useUserStore } from '@/stores/user'

const store = useUserStore()
/**
 * 可写判定 —— **必须与后端角色依赖逐字对齐**（SPEC R-FE2-20 / PRD §2.1.1）。
 *
 * 后端名单写操作是 `require_role("strategist")`：**admin 对名单只读**。
 * v3.0 这里写成 ['strategist','admin']，管理员会看到可点的「新增 / 删除 / 批量导入」，
 * 点击后只能拿到后端 403。
 */
const canEdit = computed(() => store.role === 'strategist')

const TABS = [
  { key: 'black', label: '黑名单' },
  { key: 'white', label: '白名单' },
  { key: 'gray', label: '灰名单' },
]
const DIMS = [
  { value: 'user', label: 'user 用户' },
  { value: 'phone', label: 'phone 手机号' },
  { value: 'ip', label: 'ip IP' },
  { value: 'device', label: 'device 设备' },
  { value: 'address', label: 'address 地址' },
]
const DIM_LABEL = { user: '用户', phone: '手机号', ip: 'IP', device: '设备', address: '地址' }
const EXPIRE_OPTS = [
  { value: 0, label: '永久' },
  { value: 7, label: '7 天' },
  { value: 30, label: '30 天' },
  { value: 90, label: '90 天' },
]

const tab = ref('black')
const loading = ref(true)
const error = ref('')
const toast = ref('')
const rows = ref([])
const counts = ref({ black: 0, white: 0, gray: 0 })
const total = ref(0)

const filters = reactive({ entity_type: '', keyword: '', status: '', page: 1, page_size: 20 })

/* ---------------------------------------------------------------- 过期判定 */
const expireOf = (w) => w.expire_time ?? w.expire_at ?? null
const isExpired = (w) => {
  const e = expireOf(w)
  return !!e && new Date(e).getTime() < Date.now()
}
const expireLabel = (w) => {
  const e = expireOf(w)
  if (!e) return '永久'
  if (isExpired(w)) return '已过期'
  return String(e).slice(0, 10)
}

const filtered = computed(() =>
  rows.value.filter((w) => {
    if (filters.status === 'active') return !isExpired(w)
    if (filters.status === 'expired') return isExpired(w)
    return true
  }),
)

/* ---------------------------------------------------------------- 数据加载 */
async function load() {
  loading.value = true
  try {
    const params = { list_type: tab.value, page: filters.page, page_size: filters.page_size }
    if (filters.entity_type) params.entity_type = filters.entity_type
    if (filters.keyword) params.keyword = filters.keyword
    const data = await blacklistApi.list(params)
    rows.value = data?.items || []
    counts.value = data?.counts || counts.value
    total.value = data?.total || 0
    error.value = ''
  } catch (err) {
    error.value = err.message
  } finally {
    loading.value = false
  }
}

function switchTab(key) {
  tab.value = key
  filters.page = 1
  load()
}

/* ---------------------------------------------------------------- 新增 */
const addVisible = ref(false)
const saving = ref(false)
const addError = ref('')
const form = reactive({ list_type: 'black', entity_type: 'device', entity_value: '', reason: '', expire_days: 0 })

function openAdd() {
  Object.assign(form, { list_type: tab.value, entity_type: 'device', entity_value: '', reason: '', expire_days: 0 })
  addError.value = ''
  addVisible.value = true
}

async function saveAdd() {
  if (!form.entity_value.trim()) { addError.value = '请填写名单值'; return }
  saving.value = true
  addError.value = ''
  try {
    const expireTime = form.expire_days > 0
      ? new Date(Date.now() + form.expire_days * 86400000).toISOString()
      : null
    await blacklistApi.create({
      list_type: form.list_type,
      entity_type: form.entity_type,
      entity_value: form.entity_value.trim(),
      reason: form.reason,
      expire_time: expireTime,
    })
    addVisible.value = false
    toast.value = '名单项已新增'
    await load()
  } catch (err) {
    addError.value = err.message
  } finally {
    saving.value = false
  }
}

/* ---------------------------------------------------------------- 批量导入 */
const importVisible = ref(false)
const importText = ref('')
const importDim = ref('phone')
const importListType = ref('black')
const importing = ref(false)
const importResult = ref(null)

function openImport() {
  importText.value = ''
  importResult.value = null
  importDim.value = filters.entity_type || 'phone'
  importListType.value = tab.value
  importVisible.value = true
}

/** 文本域每行 `值,原因`（FR-38 逐行预校验由后端返回失败行号与原因） */
async function doImport() {
  const lines = importText.value.split('\n').map((l) => l.trim()).filter(Boolean)
  if (!lines.length) { importResult.value = { success: 0, failed: 0, total: 0, errors: [] }; return }
  importing.value = true
  try {
    importResult.value = await blacklistApi.importItems({
      list_type: importListType.value,
      entity_type: importDim.value,
      items: lines.map((l) => {
        const [value, reason] = l.split(',').map((s) => (s || '').trim())
        return { entity_value: value, reason: reason || '批量导入' }
      }),
    })
    await load()
  } catch (err) {
    importResult.value = { success: 0, failed: lines.length, total: lines.length, errors: [{ row: '-', value: '-', reason: err.message }] }
  } finally {
    importing.value = false
  }
}

/* ---------------------------------------------------------------- 导出（UTF-8 BOM CSV） */
function exportCsv() {
  const header = ['list_type', 'entity_type', 'entity_value', 'reason', 'operator', 'expire_time', 'hit_count']
  const lines = [header.join(',')]
  for (const w of filtered.value) {
    lines.push([
      w.list_type, w.entity_type, w.entity_value, w.reason || '', w.operator || '', expireOf(w) || '', w.hit_count ?? 0,
    ].map((v) => `"${String(v).replace(/"/g, '""')}"`).join(','))
  }
  const blob = new Blob(['﻿' + lines.join('\n')], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `watchlist-${tab.value}-${new Date().toISOString().slice(0, 10)}.csv`
  a.click()
  URL.revokeObjectURL(url)
  toast.value = `已导出 ${filtered.value.length} 条记录（UTF-8 BOM）`
}

/* ---------------------------------------------------------------- 移除 */
const removeTarget = ref(null)
const removing = ref(false)

async function confirmRemove() {
  removing.value = true
  try {
    const id = removeTarget.value.list_id
    await blacklistApi.remove(id)
    toast.value = '名单项已移除'
    removeTarget.value = null
    await load()
  } catch (err) {
    toast.value = `移除失败：${err.message}`
  } finally {
    removing.value = false
  }
}

onMounted(load)
</script>

<template>
  <div>
    <div v-if="toast" class="alert alert-info mb-16"><div>{{ toast }}</div></div>

    <div class="card">
      <!-- 名单类型标签页 · FR-37 -->
      <div class="wl-tabs">
        <span v-for="t in TABS" :key="t.key" :class="{ on: tab === t.key }" @click="switchTab(t.key)">
          {{ t.label }}<span class="cnt">{{ counts[t.key] ?? 0 }}</span>
        </span>
      </div>

      <!-- 工具栏 · FR-36 / FR-38 -->
      <div class="card-bd tight" style="border-bottom:1px solid var(--border2);padding-top:14px;padding-bottom:14px">
        <div class="row-gap-12">
          <div class="field" style="min-width:150px">
            <label>维度筛选</label>
            <select v-model="filters.entity_type" @change="load">
              <option value="">全部维度</option>
              <option v-for="d in DIMS" :key="d.value" :value="d.value">{{ d.label }}</option>
            </select>
          </div>
          <div class="field" style="min-width:140px">
            <label>状态</label>
            <select v-model="filters.status">
              <option value="">全部</option>
              <option value="active">生效中</option>
              <option value="expired">已过期</option>
            </select>
          </div>
          <div class="field" style="min-width:200px">
            <label>关键字</label>
            <input v-model.trim="filters.keyword" type="text" placeholder="输入名单值或加入原因" @keyup.enter="load">
          </div>
          <div style="flex:1" />
          <button v-if="canEdit" class="btn btn-primary" @click="openAdd">+ 新增</button>
          <button v-if="canEdit" class="btn" @click="openImport">批量导入</button>
          <button class="btn" @click="exportCsv">导出</button>
        </div>
      </div>

      <StateBlock :loading="loading" :error="error" :empty="filtered.length === 0" empty-text="该名单下暂无记录">
        <table class="tb">
          <thead>
            <tr>
              <th style="width:76px">类型</th>
              <th style="width:70px">维度</th>
              <th style="width:168px">名单值</th>
              <th>加入原因</th>
              <th style="width:92px">操作人</th>
              <th style="width:100px">有效期</th>
              <th class="num" style="width:92px">命中次数</th>
              <th v-if="canEdit" style="width:88px">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="w in filtered" :key="w.list_id" :style="isExpired(w) ? { opacity: .62, background: '#fcfcfc' } : {}">
              <td><RiskTag kind="listType" :value="w.list_type" /></td>
              <td><span class="tag tag-gray">{{ DIM_LABEL[w.entity_type] || w.entity_type }}</span></td>
              <td class="mono" style="font-size:12.5px">{{ w.entity_value }}</td>
              <td style="color:var(--text2);font-size:12.5px">{{ w.reason || '-' }}</td>
              <td>{{ w.operator || '-' }}</td>
              <td>
                <span v-if="isExpired(w)" class="tag tag-gray">已过期</span>
                <span v-else-if="!expireOf(w)" class="tag tag-gray">永久</span>
                <span v-else class="tag tag-blue">{{ expireLabel(w) }}</span>
              </td>
              <td class="num mono">{{ w.hit_count }}</td>
              <td v-if="canEdit">
                <span class="link" style="color:var(--danger-text)" @click="removeTarget = w">移除</span>
              </td>
            </tr>
          </tbody>
        </table>

        <div class="pagination">
          <span class="total">共 {{ total }} 条 · 每页 {{ filters.page_size }} 条</span>
        </div>
      </StateBlock>
    </div>

    <!-- 新增弹窗 -->
    <div v-if="addVisible" class="modal-mask" @click.self="addVisible = false">
      <div class="modal-box" style="width:460px">
        <div class="modal-hd">
          <span>新增名单项</span>
          <span class="muted" style="font-size:16px;cursor:pointer" @click="addVisible = false">×</span>
        </div>
        <div class="modal-bd" style="display:flex;flex-direction:column;gap:14px">
          <div class="field">
            <label>名单类型<span class="req">*</span></label>
            <select v-model="form.list_type">
              <option value="black">black 黑名单</option>
              <option value="white">white 白名单</option>
              <option value="gray">gray 灰名单</option>
            </select>
          </div>
          <div class="field">
            <label>维度<span class="req">*</span></label>
            <select v-model="form.entity_type">
              <option v-for="d in DIMS" :key="d.value" :value="d.value">{{ d.label }}</option>
            </select>
          </div>
          <div class="field">
            <label>名单值<span class="req">*</span></label>
            <input v-model.trim="form.entity_value" type="text" class="mono" placeholder="如 13800001234 / DEV-A9F2">
          </div>
          <div class="field">
            <label>加入原因</label>
            <input v-model.trim="form.reason" type="text" placeholder="如 恶意退款索赔">
          </div>
          <div class="field">
            <label>有效期</label>
            <select v-model.number="form.expire_days">
              <option v-for="o in EXPIRE_OPTS" :key="o.value" :value="o.value">{{ o.label }}</option>
            </select>
          </div>
          <div class="muted" style="font-size:11.5px;line-height:1.7">
            到期后记录保留但<b>不再触发拦截</b>，可在列表中按「已过期」状态查看与续期。
          </div>
          <div v-if="addError" class="alert alert-danger"><div>{{ addError }}</div></div>
        </div>
        <div class="modal-ft">
          <button class="btn" @click="addVisible = false">取消</button>
          <button class="btn btn-primary" :disabled="saving" @click="saveAdd">{{ saving ? '保存中…' : '保存' }}</button>
        </div>
      </div>
    </div>

    <!-- 批量导入弹窗 · FR-38 -->
    <div v-if="importVisible" class="modal-mask" @click.self="importVisible = false">
      <div class="modal-box" style="width:560px">
        <div class="modal-hd">
          <span>批量导入</span>
          <span class="muted" style="font-size:16px;cursor:pointer" @click="importVisible = false">×</span>
        </div>
        <div class="modal-bd" style="display:flex;flex-direction:column;gap:14px">
          <div class="row-gap-12" style="gap:12px">
            <div class="field" style="flex:1">
              <label>名单类型<span class="req">*</span></label>
              <select v-model="importListType">
                <option value="black">black 黑名单</option>
                <option value="white">white 白名单</option>
                <option value="gray">gray 灰名单</option>
              </select>
            </div>
            <div class="field" style="flex:1">
              <label>导入维度<span class="req">*</span></label>
              <select v-model="importDim">
                <option v-for="d in DIMS" :key="d.value" :value="d.value">{{ d.label }}</option>
              </select>
            </div>
          </div>
          <div class="field">
            <label>粘贴数据（每行一条，格式：<span class="mono">名单值,加入原因</span>）</label>
            <textarea v-model="importText" rows="7" class="mono"
                      placeholder="13800001234,恶意退款&#10;13900005678,批量刷券&#10;13700001111" />
            <div class="muted" style="font-size:11.5px">
              手机号维度会逐行校验格式（须 11 位数字），失败行会返回行号与原因，不影响其余行入库。
            </div>
          </div>

          <div v-if="importResult" class="alert" :class="importResult.failed ? 'alert-warn' : 'alert-info'">
            <div>
              待导入 <b>{{ importResult.total }}</b> 行 · 成功 <b>{{ importResult.success }}</b> ·
              失败 <b>{{ importResult.failed }}</b>
            </div>
          </div>

          <table v-if="importResult?.errors?.length" class="tb">
            <thead><tr><th style="width:70px">行号</th><th>失败原因</th></tr></thead>
            <tbody>
              <tr v-for="(e, i) in importResult.errors.slice(0, 10)" :key="i">
                <td class="mono">{{ e.row }}</td>
                <td style="color:var(--danger-text);font-size:12.5px">{{ e.reason }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="modal-ft">
          <button class="btn" @click="importVisible = false">关闭</button>
          <button class="btn btn-primary" :disabled="importing" @click="doImport">
            {{ importing ? '导入中…' : '执行导入' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 移除二次确认 -->
    <ConfirmDialog
      :visible="!!removeTarget"
      title="移除名单项"
      :actions="removeTarget ? [
        `移除 ${DIM_LABEL[removeTarget.entity_type] || removeTarget.entity_type} ${removeTarget.entity_value}`,
        '移除后该主体将不再被名单快筛拦截',
        '操作将写入审计日志',
      ] : []"
      confirm-text="确认移除"
      :loading="removing"
      @cancel="removeTarget = null"
      @confirm="confirmRemove"
    />
  </div>
</template>

<style scoped>
.wl-tabs { display: flex; border-bottom: 1px solid var(--border); padding: 0 18px; }
.wl-tabs span {
  padding: 13px 4px; margin-right: 26px; font-size: 13.5px; color: var(--text2);
  cursor: pointer; border-bottom: 2px solid transparent; margin-bottom: -1px;
}
.wl-tabs span.on { color: var(--primary); font-weight: 600; border-bottom-color: var(--primary); }
.wl-tabs .cnt {
  display: inline-block; min-width: 18px; padding: 0 5px; margin-left: 6px;
  background: #f0f2f5; color: var(--text2); border-radius: 9px; font-size: 11px; text-align: center;
}
.wl-tabs span.on .cnt { background: var(--primary-bg); color: var(--primary); }
</style>
