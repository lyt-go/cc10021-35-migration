<template>
  <section class="page">
    <header class="page-head">
      <div>
        <h2>质量报告跨版本核对</h2>
        <p class="page-desc">
          v1 汇总日报与 v2 报告快照共存：按原报告编号回查，缺项标记“存量兼容缺项”，
          事件证据固定为快照时点，不与当前状态拼接。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="migrate" type="button" :disabled="busy" @click="migrateAll">
          迁移存量 v1 日报
        </button>
      </div>
    </header>

    <form class="filter-bar" @submit.prevent="loadReport">
      <label class="filter-item">
        <span>报告编号</span>
        <input v-model="reportNo" data-testid="report-no" placeholder="例如 DQ_R-0001" />
      </label>
      <button class="btn" data-testid="query-report" type="submit" :disabled="busy">按原编号回查</button>
      <button class="btn ghost" type="button" :disabled="busy" @click="loadDaily">按日报日期看事件归属</button>
      <label class="filter-item">
        <span>日报日期</span>
        <input v-model="day" data-testid="report-day" placeholder="YYYY-MM-DD" />
      </label>
    </form>

    <p v-if="message" class="page-foot" data-testid="message">{{ message }}</p>
    <p v-if="errorMessage" class="error-text" data-testid="error">{{ errorMessage }}</p>

    <template v-if="versions">
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">原编号核对</span>
          <strong class="stat-value" data-testid="number-match">{{ versions.原编号核对 ? '一致' : '不一致' }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">可回查版本数</span>
          <strong class="stat-value">{{ versions.版本数 }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">实际取得结构</span>
          <strong class="stat-value" data-testid="effective-structure">{{ versions.最新有效.来源结构 }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">补齐率</span>
          <strong class="stat-value">{{ Math.round(versions.最新有效.补齐率 * 100) }}%</strong>
        </article>
      </div>

      <h3>版本历史</h3>
      <table class="data-table" data-testid="version-table">
        <thead>
          <tr>
            <th>结构版本</th><th>类型</th><th>快照编号</th><th>修订号</th>
            <th>日报日期</th><th>签收状态</th><th>补齐率</th><th>缺项</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in versions.历史" :key="`${item.结构版本}-${item.快照编号 ?? 'v1'}`" :data-structure="item.结构版本">
            <td>{{ item.结构版本 }}</td>
            <td>{{ item.类型 }}</td>
            <td>{{ item.快照编号 ?? '—' }}</td>
            <td>{{ item.修订号 ?? '—' }}</td>
            <td>{{ item.日报日期 ?? '—' }}</td>
            <td>{{ item.签收状态 ?? '—' }}</td>
            <td>{{ item.补齐率 === undefined ? '—' : `${Math.round(item.补齐率 * 100)}%` }}</td>
            <td>{{ item.缺项?.length ? item.缺项.join('、') : '无' }}</td>
          </tr>
        </tbody>
      </table>

      <h3>实际取得的报告</h3>
      <table class="data-table" data-testid="effective-table">
        <thead>
          <tr><th>指标</th><th>取值</th><th>字段来源</th><th>说明</th></tr>
        </thead>
        <tbody>
          <tr v-for="(value, name) in versions.最新有效.报告" :key="name" :data-metric="name">
            <td>{{ name }}</td>
            <td>{{ value ?? '—' }}</td>
            <td>{{ versions.最新有效.字段来源?.[name]?.结构版本 ?? versions.最新有效.来源结构 }}</td>
            <td>
              <span v-if="versions.最新有效.字段来源?.[name]?.回退" data-testid="fallback">
                存量兼容缺项，按原编号回退 v1
              </span>
              <span v-else>{{ versions.最新有效.字段来源?.[name]?.重做状态 ?? '可直接取得' }}</span>
            </td>
          </tr>
        </tbody>
      </table>

      <h3>事件回查证据（快照时点）</h3>
      <table v-if="versions.最新有效.事件回查.length" class="data-table" data-testid="evidence-table">
        <thead>
          <tr><th>事件编号</th><th>触发时间</th><th>当时状态</th><th>处置归属</th><th>来源时点</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in versions.最新有效.事件回查" :key="`${item.事件编号}-${item.来源时点}`">
            <td>{{ item.事件编号 }}</td>
            <td>{{ item.触发时间 }}</td>
            <td>{{ evidenceStatus(item) }}</td>
            <td>{{ item.处置归属 }}</td>
            <td>{{ item.来源时点 }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="page-foot">该快照没有当天事件证据，旧日报可能当时缺少来源记录。</p>

      <h3>签收与重做</h3>
      <div class="filter-bar">
        <label class="filter-item">
          <span>复核人</span>
          <input v-model="reviewer" data-testid="reviewer" placeholder="复核人" />
        </label>
        <button
          v-for="snapshot in snapshots"
          :key="snapshot.id"
          class="btn"
          type="button"
          :data-testid="`snapshot-${snapshot.快照编号}`"
          :disabled="busy || !canEditSnapshot(snapshot)"
          @click="sign(snapshot, '通过')"
        >
          {{ snapshot.快照编号 }} 签收
        </button>
      </div>

      <table v-if="snapshots.length" class="data-table" data-testid="snapshot-table">
        <thead>
          <tr>
            <th>快照编号</th><th>修订号</th><th>结构版本</th><th>数据时点</th>
            <th>签收状态</th><th>指标</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="snapshot in snapshots" :key="snapshot.id">
            <td>{{ snapshot.快照编号 }}</td>
            <td>{{ snapshot.修订号 }}</td>
            <td>{{ snapshot.结构版本 }}</td>
            <td>{{ snapshot.数据时点 }}</td>
            <td>{{ snapshot.签收状态 }}</td>
            <td>{{ snapshotMetrics(snapshot) }}</td>
            <td class="row-actions">
              <button
                class="link"
                type="button"
                :disabled="busy"
                :data-testid="`recompute-${snapshot.快照编号}`"
                @click="recompute(snapshot)"
              >
                逐项重做
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </template>

    <template v-if="daily">
      <h3>按天回查：{{ daily.日期 }}</h3>
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">v2 快照</span>
          <strong class="stat-value">{{ daily.v2快照.length }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">v1 旧日报</span>
          <strong class="stat-value">{{ daily.v1旧日报.length }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">缺来源旧日报</span>
          <strong class="stat-value" data-testid="missing-legacy">{{ daily.缺少来源记录的旧日报.length }}</strong>
        </article>
      </div>
      <table class="data-table" data-testid="attribution-table">
        <thead>
          <tr><th>处置归属</th><th>事件数</th><th>当时状态</th><th>快照</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in daily.事件处置归属" :key="item.处置归属">
            <td>{{ item.处置归属 }}</td>
            <td>{{ item.事件数 }}</td>
            <td>{{ Object.entries(item.当时状态).map(([k, v]) => `${k}:${v}`).join('，') }}</td>
            <td>{{ item.快照.join('、') }}</td>
          </tr>
          <tr v-if="!daily.事件处置归属.length">
            <td colspan="4" class="empty-state">当天没有可从快照定位的事件证据</td>
          </tr>
        </tbody>
      </table>
    </template>
  </section>
</template>

<script setup lang="ts">
import { ref } from 'vue'

import {
  effectiveReport,
  listSnapshots,
  migrateLegacyReports,
  recomputeSnapshot,
  reportVersions,
  signSnapshot,
} from '@/api/report-compat'
import { request } from '@/api/client'
import { canEditSnapshot, evidenceStatus } from '@/domain/dq_report'
import type { ReportSnapshot, ReportVersions } from '@/domain/dq_report'

const reportNo = ref('DQ_R-0001')
const day = ref('')
const reviewer = ref('数据工程组')
const busy = ref(false)
const message = ref('')
const errorMessage = ref('')
const versions = ref<ReportVersions | null>(null)
const snapshots = ref<ReportSnapshot[]>([])
const daily = ref<{
  日期: string
  v2快照: ReportSnapshot[]
  v1旧日报: Record<string, unknown>[]
  事件处置归属: { 处置归属: string; 事件数: number; 当时状态: Record<string, number>; 快照: string[] }[]
  缺少来源记录的旧日报: { 报告编号: string; 原因: string }[]
} | null>(null)

function snapshotMetrics(snapshot: ReportSnapshot): string {
  return snapshot.指标
    .map((metric) => `${metric.名称}=${metric.值}${metric.是否缺项 ? '(缺项)' : ''}`)
    .join('，')
}

async function loadReport() {
  if (!reportNo.value.trim()) {
    errorMessage.value = '请先填写报告编号'
    return
  }
  busy.value = true
  errorMessage.value = ''
  message.value = ''
  try {
    versions.value = await reportVersions(reportNo.value.trim())
    snapshots.value = (await listSnapshots(reportNo.value.trim())).items
    // 用实际取得接口再核对一次，避免版本列表与适配器结论不一致
    const effective = await effectiveReport(reportNo.value.trim())
    versions.value = { ...versions.value, 最新有效: effective }
  } catch (error) {
    versions.value = null
    snapshots.value = []
    errorMessage.value = error instanceof Error ? error.message : '报告回查失败'
  } finally {
    busy.value = false
  }
}

async function loadDaily() {
  if (!day.value.trim()) {
    errorMessage.value = '请先填写日报日期'
    return
  }
  busy.value = true
  errorMessage.value = ''
  try {
    const response = await request(`/api/report/daily/${encodeURIComponent(day.value.trim())}`)
    if (!response.ok) throw new Error('按天回查失败')
    daily.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '按天回查失败'
  } finally {
    busy.value = false
  }
}

async function migrateAll() {
  busy.value = true
  errorMessage.value = ''
  try {
    const result = await migrateLegacyReports()
    message.value = `迁移状态：${String(result.状态)}，已提交 ${String(result.已提交快照数)} 份快照`
    if (versions.value) await loadReport()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '迁移失败'
  } finally {
    busy.value = false
  }
}

async function sign(snapshot: ReportSnapshot, result: '通过' | '驳回') {
  busy.value = true
  errorMessage.value = ''
  try {
    const response = await signSnapshot(snapshot.id, result, reviewer.value, '跨版本核对')
    message.value = response.message
    await loadReport()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '签收失败'
  } finally {
    busy.value = false
  }
}

async function recompute(snapshot: ReportSnapshot) {
  busy.value = true
  errorMessage.value = ''
  try {
    const response = await recomputeSnapshot(
      snapshot.id,
      snapshot.指标.map((metric) => metric.名称),
      reviewer.value,
    )
    message.value = `${response.message}，新快照 ${response.snapshot.快照编号}`
    await loadReport()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重做失败'
  } finally {
    busy.value = false
  }
}
</script>
