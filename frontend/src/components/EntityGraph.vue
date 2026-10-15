<script setup>
/**
 * 关联实体图谱（FR-26 / SPEC §8.3 EntityGraph）。
 *
 * 图谱深度固定 1 跳（中心用户 + 直接关联实体），并展示实体下的其他账号作为 2 跳；
 * 节点上限 50，由后端按 evidence_count 降序截断（R-PG-03）。
 * 采用 SVG 径向布局（SPEC §0.3：不引重型图库）；点击节点会高亮该节点及其直接关联边（R-FE2-09）。
 */
import { computed, ref } from 'vue'

const props = defineProps({
  nodes: { type: Array, default: () => [] },
  edges: { type: Array, default: () => [] },
  centerId: { type: String, default: '' },
})

const emit = defineEmits(['node-click'])

const selected = ref('')

const VB = { w: 900, h: 560, cx: 450, cy: 280, r1: 145, r2: 255 }

const TYPE_STYLE = {
  user: { fill: '#ff7875', label: '账号' },
  device: { fill: '#fa8c16', label: '设备' },
  ip: { fill: '#fa8c16', label: 'IP' },
  address: { fill: '#1677ff', label: '地址' },
  phone: { fill: '#722ed1', label: '手机号' },
}

const layout = computed(() => {
  const nodes = props.nodes || []
  const center = nodes.find((n) => n.is_center) || nodes.find((n) => n.id === props.centerId)
  if (!center) return { nodes: [], edges: [] }

  const hop1 = nodes.filter((n) => n.id !== center.id && n.type !== 'user')
  const hop2 = nodes.filter((n) => n.id !== center.id && n.type === 'user')

  // 一跳实体均匀分布在 R1 环上
  const pos = {}
  pos[center.id] = { x: VB.cx, y: VB.cy }
  const angleOf = {}
  hop1.forEach((n, i) => {
    const angle = (i / Math.max(hop1.length, 1)) * Math.PI * 2 - Math.PI / 2
    angleOf[n.id] = angle
    pos[n.id] = {
      x: VB.cx + Math.cos(angle) * VB.r1,
      y: VB.cy + Math.sin(angle) * VB.r1,
    }
  })

  // 二跳账号挂在其所属一跳实体外侧，沿同一方向略作散开
  const byParent = {}
  props.edges.forEach((e) => {
    if (hop1.some((h) => h.id === e.source) && hop2.some((h) => h.id === e.target)) {
      byParent[e.source] = byParent[e.source] || []
      if (!byParent[e.source].includes(e.target)) byParent[e.source].push(e.target)
    }
    if (hop1.some((h) => h.id === e.target) && hop2.some((h) => h.id === e.source)) {
      byParent[e.target] = byParent[e.target] || []
      if (!byParent[e.target].includes(e.source)) byParent[e.target].push(e.source)
    }
  })

  hop2.forEach((n) => {
    const parentId = Object.keys(byParent).find((p) => byParent[p].includes(n.id))
    const base = angleOf[parentId] ?? 0
    const siblings = byParent[parentId] || [n.id]
    const idx = siblings.indexOf(n.id)
    const spread = (idx - (siblings.length - 1) / 2) * 0.16
    const angle = base + spread
    pos[n.id] = {
      x: VB.cx + Math.cos(angle) * VB.r2,
      y: VB.cy + Math.sin(angle) * VB.r2,
    }
  })

  const outNodes = nodes.map((n) => {
    const p = pos[n.id] || { x: VB.cx, y: VB.cy }
    const isCenter = n.id === center.id
    return {
      ...n,
      x: p.x,
      y: p.y,
      r: isCenter ? 27 : n.type === 'user' ? 14 : 19,
      fill: isCenter ? '#ff4d4f' : TYPE_STYLE[n.type]?.fill || '#8c8c8c',
      isCenter,
    }
  })

  const index = Object.fromEntries(outNodes.map((n) => [n.id, n]))
  const outEdges = props.edges
    .filter((e) => index[e.source] && index[e.target])
    .map((e) => ({ ...e, s: index[e.source], t: index[e.target] }))

  return { nodes: outNodes, edges: outEdges }
})

function isDimmed(id) {
  if (!selected.value) return false
  if (id === selected.value) return false
  return !props.edges.some(
    (e) =>
      (e.source === selected.value && e.target === id) ||
      (e.target === selected.value && e.source === id),
  )
}

function onClick(node) {
  selected.value = selected.value === node.id ? '' : node.id
  emit('node-click', node)
}

function label(n) {
  return n.label?.length > 12 ? `${n.label.slice(0, 11)}…` : n.label
}
</script>

<template>
  <div>
    <div class="graph-box">
      <svg :viewBox="`0 0 ${VB.w} ${VB.h}`" width="100%" height="470" role="img" aria-label="关联实体图谱">
        <!-- 边 -->
        <g stroke="#d9e5f5" stroke-linejoin="round">
          <line
            v-for="(e, i) in layout.edges"
            :key="`e${i}`"
            :x1="e.s.x" :y1="e.s.y" :x2="e.t.x" :y2="e.t.y"
            :stroke-width="Math.min(1 + (e.weight || 1) * 0.35, 3.5)"
            :stroke-opacity="isDimmed(e.s.id) || isDimmed(e.t.id) ? 0.25 : 1"
          />
        </g>

        <!-- 节点 -->
        <g
          v-for="n in layout.nodes"
          :key="n.id"
          style="cursor:pointer"
          :opacity="isDimmed(n.id) ? 0.3 : 1"
          @click="onClick(n)"
        >
          <circle
            :cx="n.x" :cy="n.y" :r="n.r" :fill="n.fill"
            :stroke="selected === n.id ? '#1f1f1f' : 'none'" stroke-width="2"
          />
          <text
            v-if="n.isCenter"
            :x="n.x" :y="n.y + 4" text-anchor="middle" font-size="11" font-weight="600" fill="#fff"
          >{{ label(n) }}</text>
          <text
            v-else-if="n.type !== 'user'"
            :x="n.x" :y="n.y + 4" text-anchor="middle" font-size="9.5" fill="#fff"
          >{{ TYPE_STYLE[n.type]?.label || n.type }}</text>
          <text
            :x="n.x" :y="n.y + n.r + 13" text-anchor="middle" font-size="10"
            :fill="n.isCenter ? '#cf1322' : '#595959'"
            :font-weight="n.isCenter ? 600 : 400"
          >{{ n.isCenter ? '涉案用户' : label(n) }}</text>
        </g>
      </svg>
    </div>

    <div class="legend" style="margin-top:12px">
      <span><i style="background:#ff4d4f"></i>涉案用户（中心）</span>
      <span><i style="background:#fa8c16"></i>关联设备 / IP</span>
      <span><i style="background:#1677ff"></i>收货地址</span>
      <span><i style="background:#ff7875"></i>关联账号</span>
      <span class="muted">点击节点可高亮其直接关联边 · 节点上限 50</span>
    </div>
  </div>
</template>
