<template>
  <section class="page" data-module="dc_box">
    <header class="page-head">
      <div>
        <h2>汇流箱管理管理</h2>
        <p class="page-desc">维护汇流箱，围绕汇流箱编号、所属组串、输入支路、支路电流做登记、筛选与状态流转。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记汇流箱</button>
        <button class="btn" type="button" :disabled="refreshing" @click="refreshAll">
          {{ refreshing ? '采集中…' : '重新采集' }}
        </button>
        <button class="btn" type="button" @click="exportRows">导出汇流箱管理清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <!-- 整批空态：本批次没有任何一台设备采到数据。筛选与已有记录都保留，可重试 -->
    <div v-if="batchEmpty" class="data-empty-banner" role="alert">
      <div>
        <strong>本轮采集为空或全部读取失败</strong>
        <p>
          列表仍保留上一有效批次的记录与当前筛选条件，未用零电流参与不平衡判断。
          <template v-if="batchInfo">上一有效批次：#{{ batchInfo.batch_id }}（{{ batchInfo.collected_at }}）</template>
        </p>
      </div>
      <button class="btn primary" type="button" :disabled="refreshing" @click="refreshAll">
        {{ refreshing ? '重试中…' : '重试采集' }}
      </button>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>汇流箱编号</span>
        <input v-model="filters.汇流箱编号" placeholder="按汇流箱编号检索" />
      </label>
      <label class="filter-item">
        <span>所属组串</span>
        <input v-model="filters.所属组串" placeholder="按所属组串检索" />
      </label>
      <label class="filter-item">
        <span>输入支路</span>
        <input v-model="filters.输入支路" placeholder="按输入支路检索" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :class="{ 'row-empty-data': row.采集状态 !== '正常' }">
          <td v-for="column in columns" :key="column">
            <template v-if="column === '支路电流' && row.采集状态 !== '正常'">
              <span class="data-missing">
                {{ row.支路电流 }}<template v-if="row.采集状态 === '待补资料'">（待补资料）</template>
              </span>
            </template>
            <template v-else-if="column === '通讯状态' && (row.通讯状态 === '中断' || row.通讯状态 === '未知')">
              <span class="data-missing">{{ row.通讯状态 }}<template v-if="!hasCurrent(row)">·读取失败</template></span>
            </template>
            <template v-else-if="column === '箱体状态'">
              <span :class="['status-tag', statusClass(row)]">{{ row.箱体状态 }}</span>
              <span v-if="row.采集状态 !== '正常'" class="data-state-tip" :title="String(row.采集说明 ?? '')">
                （{{ row.采集状态 }}）
              </span>
            </template>
            <template v-else>{{ row[column] ?? '—' }}</template>
          </td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button
              v-if="row.采集状态 !== '正常'"
              class="link retry-link"
              type="button"
              :disabled="retryingId === row.id"
              @click="retryRow(row)"
            >
              {{ retryingId === row.id ? '重试中…' : '重试采集' }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">
            {{ noDevice ? '暂无汇流箱管理数据，可先登记汇流箱' : '当前筛选条件下没有记录，可调整条件后重试' }}
          </td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条汇流箱管理记录<template v-if="batchInfo">；当前有效批次 #{{ batchInfo.batch_id }}（{{ batchInfo.collected_at }}）</template></span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type BatchInfo = { batch_id: number; collected_at: string; refreshed?: number; failed?: number } | null

const ENDPOINT = '/api/dc_box'
const columns = ["汇流箱编号", "所属组串", "输入支路", "支路电流", "箱体温度", "通讯状态", "上次检修日", "箱体状态"]
const actions = ["排查支路", "复位通讯", "安排检修"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const refreshing = ref(false)
const retryingId = ref<number | null>(null)
// 最近一次成功采集的批次；整批失败时后端不换批次，这里也保持旧值
const batchInfo = ref<BatchInfo>(null)
// 最近一次操作是“整批采集且全部失败”：展示整批空态，但不清空 rows/筛选
const batchEmpty = ref(false)
// 空数据期间不拿旧记录冲抵：标记本次是否为真正有设备的列表
const filters = reactive<Record<string, string>>({ 汇流箱编号: '', 所属组串: '', 输入支路: '' })

const stats = computed(() => [
  { label: '正常汇流箱', value: rows.value.filter((row) => row.箱体状态 === '正常运行').length },
  { label: '异常汇流箱', value: rows.value.filter((row) => row.箱体状态 === '支路异常').length },
  { label: '通讯/采集异常', value: rows.value.filter((row) => row.采集状态 !== '正常').length },
  { label: '检修中汇流箱', value: rows.value.filter((row) => row.箱体状态 === '检修中').length },
])

const noDevice = computed(() => total.value === 0 && !Object.values(filters).some(Boolean))

function hasCurrent(row: Row): boolean {
  const current = String(row.支路电流 ?? '')
  return current !== '' && current !== '读取失败' && current !== '待补资料'
}

function statusClass(row: Row): string {
  if (row.箱体状态 === '支路异常') return 'status-abnormal'
  if (row.箱体状态 === '通讯中断') return 'status-offline'
  if (row.箱体状态 === '检修中') return 'status-repair'
  return 'status-normal'
}

function resetFilters() {
  Object.keys(filters).forEach((key) => { filters[key] = '' })
  void reload()
}

function exportRows() {
  const query = new URLSearchParams(
    Object.fromEntries(Object.entries(filters).filter(([, value]) => value)),
  ).toString()
  window.open(`${ENDPOINT}/export${query ? `?${query}` : ''}`, '_blank')
}

function openCreate() {
  errorMessage.value = '汇流箱登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('汇流箱管理动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '汇流箱管理操作失败'
  }
}

function buildQuery(): string {
  return new URLSearchParams(
    Object.fromEntries(Object.entries(filters).filter(([, value]) => value)),
  ).toString()
}

async function reload() {
  errorMessage.value = ''
  // 读取列表失败时保留已有 rows 与筛选，只提示，不把旧数据清成空表
  try {
    const response = await request(`${ENDPOINT}?${buildQuery()}`)
    if (!response.ok) {
      throw new Error('汇流箱列表读取失败，当前显示为上一有效数据')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '汇流箱管理列表读取失败'
  }
  try {
    const batchResponse = await request(`${ENDPOINT}/batch`)
    if (batchResponse.ok) {
      const payload = await batchResponse.json()
      batchInfo.value = payload.batch ?? null
    }
  } catch {
    // 批次信息读不到不影响列表展示
  }
}

async function refreshAll() {
  errorMessage.value = ''
  refreshing.value = true
  try {
    const response = await request(`${ENDPOINT}/refresh`, { method: 'POST' })
    if (!response.ok) {
      throw new Error('采集请求未送达，仍保留当前批次记录')
    }
    const result = await response.json()
    // advanced=false 表示整批空数据：批次不前进，旧记录与筛选原样保留
    batchEmpty.value = result.advanced === false
    if (result.advanced) {
      batchInfo.value = result.batch
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集失败，当前显示为上一有效数据'
  } finally {
    refreshing.value = false
  }
}

async function retryRow(row: Row) {
  errorMessage.value = ''
  retryingId.value = Number(row.id)
  try {
    const response = await request(`${ENDPOINT}/${row.id}/retry`, { method: 'POST' })
    const payload = await response.json().catch(() => null)
    if (!response.ok || payload?.ok === false) {
      // 单台仍失败：保留当前筛选与该设备的待补资料/读取失败空态
      errorMessage.value = payload?.message ?? '重试失败，采集尚未恢复'
      return
    }
    batchEmpty.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重试请求未送达，采集未恢复'
  } finally {
    retryingId.value = null
  }
}

onMounted(reload)
</script>
