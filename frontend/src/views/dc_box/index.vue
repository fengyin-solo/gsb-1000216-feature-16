<template>
  <section class="page" data-module="dc_box">
    <header class="page-head">
      <div>
        <h2>汇流箱管理</h2>
        <p class="page-desc">围绕支路电流做不平衡监测；支路电流或通讯状态缺失时进入独立空态，不按零电流计算，恢复采集后按新批次重新判断。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记汇流箱</button>
        <button class="btn" type="button" @click="exportRows">导出汇流箱清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <!-- 读取失败：保留当前筛选条件与已有记录，只提示重试，不清空表格 -->
    <div v-if="loadError" class="retry-banner" role="alert">
      <span>{{ loadError }}（当前筛选与已有记录已保留，未当作新批次）</span>
      <button class="btn" type="button" :disabled="loading" @click="reload">重试加载</button>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>汇流箱编号</span>
        <input v-model="filters.keyword" placeholder="按汇流箱编号检索" />
      </label>
      <label class="filter-item">
        <span>箱体状态</span>
        <select v-model="filters.status">
          <option value="">全部状态</option>
          <option v-for="state in statuses" :key="state" :value="state">{{ state }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>数据批次</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td>{{ row['汇流箱编号'] ?? '—' }}</td>
          <td>{{ row['所属组串'] ?? '—' }}</td>
          <td>{{ row['输入支路'] ?? '—' }}</td>
          <!-- 支路电流：每条支路独立空态，缺测不显示为 0 -->
          <td>
            <ul v-if="row['支路明细']?.length" class="branch-list">
              <li v-for="branch in row['支路明细']" :key="branch['支路']">
                <span class="branch-name">{{ branch['支路'] }}</span>
                <span v-if="branch['电流'] === null" class="branch-empty">无数据</span>
                <span v-else>{{ branch['电流'] }} A</span>
              </li>
            </ul>
            <span v-else class="cell-empty">待补资料：未配置输入支路</span>
          </td>
          <!-- 不平衡判断：空数据期间不给结论，不用零电流凑均值 -->
          <td>
            <template v-if="row['采集状态'] === '正常'">
              <span :class="['judge', row['不平衡判断']['结果'] === '不平衡' ? 'bad' : 'ok']">
                {{ row['不平衡判断']['结果'] }}
              </span>
              <span v-if="row['不平衡判断']['偏差'] !== null" class="judge-meta">
                最大偏差 {{ formatDeviation(row['不平衡判断']['偏差']) }}
              </span>
              <span v-else class="cell-empty">{{ row['不平衡判断']['说明'] || '有效电流不足，无法判断' }}</span>
            </template>
            <span v-else class="cell-empty">
              {{ row['采集状态'] === '待补资料' ? '待补资料' : '采集中断' }}，未参与不平衡判断
            </span>
          </td>
          <td>
            <span v-if="row['箱体温度'] === null" class="cell-empty">无数据</span>
            <span v-else>{{ row['箱体温度'] }} ℃</span>
          </td>
          <td>
            <span :class="['collect-badge', collectClass(row['采集状态'])]">{{ row['采集状态'] }}</span>
            <div class="collect-raw">原始：{{ row['通讯状态'] || '空（读取失败）' }}</div>
          </td>
          <td>{{ row['上次检修日'] ?? '—' }}</td>
          <td>
            <span v-if="row['批次号']">{{ row['批次号'] }}</span>
            <span v-else class="cell-empty">尚无批次</span>
            <span v-if="row.stale" class="stale-tag" title="采集中断，当前展示的是上一批次保留数据，恢复后才会重新计算">旧批次</span>
          </td>
          <td class="row-actions">
            <button
              v-if="row['采集状态'] === '采集中断' || row['采集状态'] === '待补资料'"
              class="link"
              type="button"
              :disabled="retryingId === row.id"
              @click="retryRow(row)"
            >
              {{ retryingId === row.id ? '采集中…' : '重试采集' }}
            </button>
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!loading && !rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无符合条件的汇流箱数据，可先登记汇流箱</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条汇流箱记录</span>
      <span v-if="actionMessage" :class="actionOk ? 'ok-text' : 'error-text'">{{ actionMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

interface Branch {
  支路: string
  电流: number | null
  状态: '正常' | '无数据'
}

interface Imbalance {
  结果: '平衡' | '不平衡' | '无法判断'
  偏差: number | null
  有效支路数: number
  缺失支路数: number
  说明: string
}

type Row = {
  id: number
  status: string
  stale: boolean
  汇流箱编号: string
  所属组串: string
  输入支路: number | null
  支路明细: Branch[]
  支路数: number
  箱体温度: number | null
  通讯状态: string
  上次检修日: string | null
  采集状态: '正常' | '采集中断' | '待补资料'
  不平衡判断: Imbalance
  批次号: string
}

const ENDPOINT = '/api/dc_box'
const columns = ['汇流箱编号', '所属组串', '输入支路', '支路电流', '不平衡判断', '箱体温度', '通讯状态', '上次检修日']
const actions = ['排查支路', '复位通讯', '安排检修']
const statuses = ['正常运行', '支路异常', '通讯中断', '检修中']

const rows = ref<Row[]>([])
const total = ref(0)
const loading = ref(false)
const loadError = ref('')
const retryingId = ref<number | null>(null)
const actionMessage = ref('')
const actionOk = ref(true)
// 空数据/读取失败期间筛选条件始终保留在这个对象里，不会被重置
const filters = ref<{ keyword: string; status: string }>({ keyword: '', status: '' })

const stats = computed(() => {
  const waiting = rows.value.filter((row) => row['采集状态'] !== '正常').length
  return [
    { label: '正常汇流箱', value: rows.value.filter((row) => row.status === '正常运行').length },
    { label: '支路异常', value: rows.value.filter((row) => row.status === '支路异常').length },
    { label: '采集中断/待补资料', value: waiting },
    { label: '检修中汇流箱', value: rows.value.filter((row) => row.status === '检修中').length },
  ]
})

function formatDeviation(value: number): string {
  return `${(value * 100).toFixed(1)}%`
}

function collectClass(state: Row['采集状态']): string {
  if (state === '正常') return 'collect-ok'
  if (state === '待补资料') return 'collect-pending'
  return 'collect-off'
}

function resetFilters() {
  filters.value = { keyword: '', status: '' }
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  actionOk.value = false
  actionMessage.value = '汇流箱登记入口尚未接入审批流'
}

async function reload() {
  loading.value = true
  const params = new URLSearchParams()
  if (filters.value.keyword) params.set('keyword', filters.value.keyword)
  if (filters.value.status) params.set('status', filters.value.status)
  try {
    const response = await request(`${ENDPOINT}?${params.toString()}`)
    if (!response.ok) {
      throw new Error(`汇流箱列表读取失败（HTTP ${response.status}）`)
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    loadError.value = ''
  } catch (error) {
    // 关键：失败时不清空 rows / total / filters，旧记录保留在界面上，等待重试
    loadError.value = error instanceof Error ? error.message : '汇流箱列表读取失败'
  } finally {
    loading.value = false
  }
}

async function retryRow(row: Row) {
  actionMessage.value = ''
  retryingId.value = row.id
  try {
    const response = await request(`${ENDPOINT}/${row.id}/retry`, { method: 'POST' })
    const payload = await response.json().catch(() => null)
    if (!response.ok || !payload?.ok) {
      throw new Error(payload?.message ?? '重试采集未生效，请稍后再试')
    }
    actionOk.value = true
    actionMessage.value = payload.message
    // 设备可能因恢复采集而改变状态，按当前筛选重新拉取；筛选条件不变
    await reload()
  } catch (error) {
    // 重试失败同样保留旧记录，不把旧批次数据冒充为新批次
    actionOk.value = false
    actionMessage.value = error instanceof Error ? error.message : '重试采集失败'
  } finally {
    retryingId.value = null
  }
}

async function runAction(action: string, row: Row) {
  actionMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const payload = await response.json().catch(() => null)
    if (!response.ok || !payload?.ok) {
      throw new Error(payload?.message ?? '汇流箱管理动作未生效，请稍后重试')
    }
    // 动作（如复位通讯）可能带来新批次，按当前筛选重新拉取，筛选条件不变
    await reload()
    actionOk.value = true
    actionMessage.value = payload.message
  } catch (error) {
    actionOk.value = false
    actionMessage.value = error instanceof Error ? error.message : '汇流箱管理操作失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.branch-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
.branch-name { color: var(--muted); margin-right: 4px; }
.branch-empty { color: #b54708; background: #fffaeb; border: 1px solid #fedf89; border-radius: 4px; padding: 0 6px; font-size: 12px; }
.cell-empty { color: var(--muted); font-style: italic; }
.judge { font-weight: 600; }
.judge.ok { color: #067647; }
.judge.bad { color: #b42318; }
.judge-meta { display: block; color: var(--muted); font-size: 12px; margin-top: 2px; }
.collect-badge { border-radius: 10px; padding: 1px 8px; font-size: 12px; }
.collect-ok { color: #067647; background: #ecfdf3; border: 1px solid #abefc6; }
.collect-off { color: #b42318; background: #fef3f2; border: 1px solid #fecdca; }
.collect-pending { color: #b54708; background: #fffaeb; border: 1px solid #fedf89; }
.collect-raw { color: var(--muted); font-size: 11px; margin-top: 2px; }
.stale-tag { margin-left: 6px; color: #b54708; background: #fffaeb; border: 1px solid #fedf89; border-radius: 4px; padding: 0 6px; font-size: 11px; }
.retry-banner { display: flex; justify-content: space-between; align-items: center; gap: 12px; background: #fef3f2; border: 1px solid #fecdca; color: #b42318; border-radius: 6px; padding: 8px 12px; margin-bottom: 12px; font-size: 13px; }
.ok-text { color: #067647; }
</style>
