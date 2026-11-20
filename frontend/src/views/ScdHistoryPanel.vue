<template>
  <section class="ledger" data-testid="scd-ledger">
    <header class="ledger-head">
      <div>
        <h3>SCD 变更履历</h3>
        <p class="ledger-hint">
          履历只追加：发布、迟到补录、更正补录都会登记旧值 / 新值 / 业务日期，
          原记录保持只读，生效区间由履历按 SCD 策略重新推导。
        </p>
      </div>
      <div class="ledger-controls">
        <label class="ledger-label">
          维度
          <select v-model="dim" data-testid="ledger-dimension" @change="onDimensionChange">
            <option v-for="item in dimensions" :key="item.维度编码" :value="item.维度编码">
              {{ item.维度编码 }} · {{ item.维度名称 }}
            </option>
          </select>
        </label>
        <label class="ledger-label">
          维护人
          <select v-model="operator" data-testid="ledger-operator">
            <option v-for="role in roles" :key="role.key" :value="role.key">
              {{ role.label }}{{ role.can_correct ? '（可更正）' : '' }}
            </option>
          </select>
        </label>
        <span class="ledger-revision" data-testid="ledger-revision">版本 {{ revision }}</span>
      </div>
    </header>

    <p v-if="message" class="ledger-message" data-testid="ledger-message">{{ message }}</p>
    <p v-if="errorMessage" class="error-text" data-testid="ledger-error">{{ errorMessage }}</p>

    <div class="ledger-grid">
      <article class="ledger-card">
        <h4>生效区间</h4>
        <table class="data-table ledger-table">
          <thead>
            <tr>
              <th>字段</th><th>生效开始</th><th>生效结束</th><th>值</th>
              <th>来源履历</th><th>来源</th><th>修订来源</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="row in intervals"
              :key="row.区间ID"
              :data-testid="`interval-${row.字段}-${row.生效开始}`"
              :data-field="row.字段"
              :data-start="row.生效开始"
              :data-value="row.值"
            >
              <td>{{ row.字段 }}</td>
              <td>{{ row.生效开始 }}</td>
              <td>{{ row.开放区间 ? '至今' : row.生效结束 }}</td>
              <td>{{ row.值 }}</td>
              <td>{{ row.来源履历ID }}</td>
              <td>{{ row.来源记录来源 }}</td>
              <td>{{ row.修订来源 || '—' }}</td>
            </tr>
            <tr v-if="!intervals.length">
              <td colspan="7" class="empty-state">该维度还没有可推导的生效区间</td>
            </tr>
          </tbody>
        </table>
      </article>

      <article class="ledger-card">
        <h4>履历（只读）</h4>
        <table class="data-table ledger-table">
          <thead>
            <tr>
              <th>履历ID</th><th>字段</th><th>旧值</th><th>新值</th>
              <th>业务日期</th><th>来源</th><th>生效状态</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="row in history"
              :key="row.履历ID"
              :data-testid="`history-${row.履历ID}`"
              :data-source="row.记录来源"
              :data-business-date="row.业务日期"
              :data-readonly="String(row.是否只读)"
            >
              <td>{{ row.履历ID }}</td>
              <td>{{ row.变更字段 }}</td>
              <td>{{ row.旧值 || '—' }}</td>
              <td>{{ row.新值 }}</td>
              <td>{{ row.业务日期 }}</td>
              <td>{{ row.记录来源 }}{{ row.迟到 ? '（迟到）' : '' }}</td>
              <td>{{ row.生效状态 }}<span v-if="row.被更正为"> → {{ row.被更正为 }}</span></td>
            </tr>
            <tr v-if="!history.length">
              <td colspan="7" class="empty-state">该维度还没有履历</td>
            </tr>
          </tbody>
        </table>
      </article>
    </div>

    <article class="ledger-card">
      <h4>按日追溯</h4>
      <div class="ledger-controls">
        <label class="ledger-label">
          日期
          <input v-model="asOfDate" type="date" data-testid="ledger-asof-date" @change="loadAsOf" />
        </label>
        <button class="btn" data-testid="ledger-asof" type="button" @click="loadAsOf">追溯当日生效</button>
      </div>
      <div v-if="asOf" class="asof-wrap" data-testid="ledger-asof-result" :data-date="asOf.日期">
        <p class="asof-summary">
          {{ asOf.日期 }}：{{ asOf.字段归属.length }} 个字段生效
          <span v-if="asOf.未覆盖字段.length">，未覆盖 {{ asOf.未覆盖字段.join('、') }}</span>
          <span class="ledger-hash">版本标识 {{ asOf.版本标识 }}</span>
        </p>
        <table class="data-table ledger-table">
          <thead>
            <tr><th>字段</th><th>当日值</th><th>生效区间</th><th>来源履历</th><th>修订来源</th><th>关联更正</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in asOf.字段归属" :key="row.字段" :data-testid="`asof-${row.字段}`">
              <td>{{ row.字段 }}</td>
              <td>{{ row.值 }}</td>
              <td>{{ row.生效开始 }} ~ {{ row.开放区间 || row.生效结束 === '9999-12-31' ? '至今' : row.生效结束 }}</td>
              <td>{{ row.来源履历ID }}（{{ row.来源记录来源 }}）</td>
              <td>{{ row.修订来源 || '—' }}</td>
              <td>
                <span v-if="!row.关联更正.length">—</span>
                <span v-for="link in row.关联更正" :key="link.履历ID" class="correction-chip">
                  {{ link.履历ID }} · {{ link.记录来源 }}（{{ link.关联理由.join('、') }}）
                </span>
              </td>
            </tr>
            <tr v-if="!asOf.字段归属.length">
              <td colspan="6" class="empty-state">当日没有生效区间</td>
            </tr>
          </tbody>
        </table>
        <p v-if="asOf.字段归属.length && asOf.字段归属[0].下一变更" class="ledger-hint">
          下一变更：{{ asOf.字段归属[0].下一变更.履历ID }} 于 {{ asOf.字段归属[0].下一变更.业务日期 }} 生效
        </p>
      </div>
    </article>

    <article class="ledger-card">
      <h4>补录履历</h4>
      <form class="ledger-form" data-testid="ledger-append" @submit.prevent="submitAppend">
        <label class="ledger-label">
          字段
          <input v-model="form.field" data-testid="ledger-field" placeholder="如 product_category" required />
        </label>
        <label class="ledger-label">
          旧值
          <input v-model="form.oldValue" data-testid="ledger-old-value" placeholder="留空自动按生效顺序补齐" />
        </label>
        <label class="ledger-label">
          新值
          <input v-model="form.newValue" data-testid="ledger-new-value" required />
        </label>
        <label class="ledger-label">
          业务日期
          <input v-model="form.businessDate" type="date" data-testid="ledger-business-date" required />
        </label>
        <label class="ledger-label">
          记录来源
          <select v-model="form.recordType" data-testid="ledger-record-type">
            <option value="首发发布">首发发布</option>
            <option value="迟到补录">迟到补录</option>
            <option value="更正补录">更正补录</option>
          </select>
        </label>
        <label class="ledger-label">
          更正来源
          <input v-model="form.correctionSource" data-testid="ledger-correction-source" placeholder="被更正的履历ID，可留空" />
        </label>
        <label class="ledger-label ledger-wide">
          变更原因
          <input v-model="form.reason" data-testid="ledger-reason" placeholder="说明变更或补录缘由" />
        </label>
        <button class="btn primary" data-testid="ledger-submit" type="submit" :disabled="submitting">
          {{ submitting ? '提交中…' : '登记履历' }}
        </button>
      </form>
    </article>

    <article class="ledger-card">
      <h4>一致性校验</h4>
      <button class="btn" data-testid="ledger-integrity" type="button" @click="refreshIntegrity">
        校验生效区间与字段归属
      </button>
      <div v-if="integrity" class="integrity-wrap" data-testid="ledger-integrity-result" :data-ok="String(integrity.ok)">
        <p :class="integrity.ok ? 'ok-text' : 'error-text'">
          {{ integrity.ok ? '通过' : '存在不一致' }}：履历 {{ integrity.履历数 }} 条，
          更正 {{ integrity.更正数 }} 条，区间 {{ integrity.区间数 }} 段。
        </p>
        <ul v-if="integrity.overlaps.length" class="ledger-issues">
          <li v-for="item in integrity.overlaps" :key="item.左区间 + item.右区间">
            区间重叠：{{ item.字段 }} {{ item.重叠开始 }} ~ {{ item.重叠结束 }}
          </li>
        </ul>
        <ul v-if="integrity.drift.length" class="ledger-issues">
          <li v-for="item in integrity.drift" :key="item.区间ID">{{ item.问题 }}：{{ item.区间ID }}</li>
        </ul>
        <ul v-if="integrity.readonly_violations.length" class="ledger-issues">
          <li v-for="item in integrity.readonly_violations" :key="item.履历ID">{{ item.问题 }}：{{ item.履历ID }}</li>
        </ul>
        <ul v-if="integrity.continuity_breaks.length" class="ledger-issues">
          <li v-for="item in integrity.continuity_breaks" :key="item.履历ID">
            连续性问题：{{ item.字段 }} {{ item.业务日期 }}，{{ item.说明 }}
          </li>
        </ul>
      </div>
    </article>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  appendHistory,
  listDimensions,
  listHistory,
  listIntervals,
  listRoles,
  loadAsOf as fetchAsOf,
  loadIntegrity,
} from '@/api/scd-history'
import type {
  AsOfResult,
  DimensionSummary,
  HistoryEntry,
  HistoryRole,
  IntegrityResult,
  Interval,
} from '@/api/scd-history'

const props = defineProps<{ initialDimension?: string }>()

const dimensions = ref<DimensionSummary[]>([])
const roles = ref<HistoryRole[]>([])
const dim = ref('')
const operator = ref('data_steward')
const history = ref<HistoryEntry[]>([])
const intervals = ref<Interval[]>([])
const asOf = ref<AsOfResult | null>(null)
const asOfDate = ref(new Date().toISOString().slice(0, 10))
const integrity = ref<IntegrityResult | null>(null)
const revision = ref(0)
const message = ref('')
const errorMessage = ref('')
const submitting = ref(false)
const form = ref({
  field: '',
  oldValue: '',
  newValue: '',
  businessDate: '',
  recordType: '迟到补录',
  correctionSource: '',
  reason: '',
})

async function bootstrap() {
  try {
    const [dims, roleList] = await Promise.all([listDimensions(), listRoles()])
    dimensions.value = dims
    roles.value = roleList
    dim.value = props.initialDimension || dims[0]?.维度编码 || ''
    await refresh()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '履历数据读取失败'
  }
}

async function refresh() {
  if (!dim.value) return
  errorMessage.value = ''
  try {
    const [entries, spans] = await Promise.all([
      listHistory(dim.value),
      listIntervals(dim.value),
    ])
    history.value = entries.items
    intervals.value = spans
    revision.value = entries.revision
    if (asOfDate.value) await loadAsOf()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '履历数据读取失败'
  }
}

async function onDimensionChange() {
  asOf.value = null
  integrity.value = null
  message.value = ''
  await refresh()
}

async function loadAsOf() {
  if (!dim.value || !asOfDate.value) return
  try {
    asOf.value = await fetchAsOf(dim.value, asOfDate.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '追溯失败'
  }
}

async function submitAppend() {
  submitting.value = true
  errorMessage.value = ''
  message.value = ''
  try {
    const result = await appendHistory(dim.value, {
      ...form.value,
      operator: operator.value,
      expectedRevision: revision.value || undefined,
    })
    message.value = result.message
    form.value.newValue = ''
    form.value.oldValue = ''
    form.value.reason = ''
    form.value.correctionSource = ''
    await refresh()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '履历登记失败'
  } finally {
    submitting.value = false
  }
}

async function refreshIntegrity() {
  if (!dim.value) return
  try {
    integrity.value = await loadIntegrity(dim.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '一致性校验失败'
  }
}

onMounted(bootstrap)
</script>
