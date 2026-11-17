<script setup>
import { onMounted, ref } from 'vue'
import { getJson, postJson } from '../api'

const rows = ref([])
const oldKey = ref('')
const newKey = ref('')
const err = ref('')
const msg = ref('')
const busy = ref(false)

async function load() {
  rows.value = await getJson('/api/key-retargets')
}

onMounted(async () => {
  try {
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

async function submit() {
  busy.value = true
  err.value = ''
  msg.value = ''
  try {
    const r = await postJson('/api/key-retargets', {
      old_key: oldKey.value.trim(),
      new_key: newKey.value.trim(),
    })
    msg.value =
      `迁移生效（运行 #${r.settle_run_id}）：重指向冲正 ${r.retargeted_reversal_count} 笔，` +
      `合并后剩余敞口 ${yuan(r.merged_remain_cents)} 元`
    oldKey.value = ''
    newKey.value = ''
    await load()
  } catch (e) {
    // 闭失败：响应体 {"detail": 失败登记}（postJson 抛出的 message 即原文）
    let reason = String(e.message || e)
    try {
      reason = JSON.parse(e.message)?.detail?.fail_reason || reason
    } catch {
      /* 非 JSON 响应，保留原文 */
    }
    err.value = reason
    await load()
  } finally {
    busy.value = false
  }
}

function yuan(cents) {
  return (Number(cents || 0) / 100).toFixed(2)
}
</script>

<template>
  <div>
    <h1>键迁移台</h1>
    <p class="lead">
      登记一次正向键迁移（old_key → new_key，同账户）。生效后所有仍引用旧键的冲正自动改指新键，
      两键剩余敞口按家族合并；超额时登记失败记录，冲正指向以落库为准。
    </p>
    <p class="note-strip">
      管道内键重映射（装载重放换键），非主数据 SCD 历史版价。迁移边不可变，落 key_retargets 表。
    </p>

    <div class="panel">
      <h2>登记迁移</h2>
      <div class="form-row">
        <label>
          旧键 old_key
          <input v-model="oldKey" placeholder="如 FWD-20261116-002" />
        </label>
        <span class="arrow">→</span>
        <label>
          新键 new_key
          <input v-model="newKey" placeholder="如 NEW-20261117-002" />
        </label>
        <button class="btn" :disabled="busy || !oldKey.trim() || !newKey.trim()" @click="submit">
          登记并原子切换
        </button>
      </div>
      <p v-if="msg" class="ok">{{ msg }}</p>
      <p v-if="err" class="msg">迁移闭失败：{{ err }}</p>
    </div>

    <div class="panel">
      <h2>迁移映射（迁移表）</h2>
      <table>
        <thead>
          <tr>
            <th>#</th><th>生效运行</th><th>旧键</th><th>新键</th><th>状态</th>
            <th>重指向笔数</th><th>旧键剩余</th><th>新键剩余</th><th>合并剩余（元）</th>
            <th>当前家族敞口（元）</th><th>失败原因</th><th>时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in rows" :key="r.id">
            <td>{{ r.id }}</td>
            <td>{{ r.settle_run_id || '—' }}</td>
            <td class="mono">{{ r.old_entry_key }}</td>
            <td class="mono">{{ r.new_entry_key }}</td>
            <td>
              <span class="tag" :class="r.status === '生效' ? 'on' : 'bad'">{{ r.status }}</span>
            </td>
            <td>{{ r.retargeted_reversal_count }}</td>
            <td>{{ yuan(r.old_remain_cents) }}</td>
            <td>{{ yuan(r.new_remain_cents) }}</td>
            <td>{{ yuan(r.merged_remain_cents) }}</td>
            <td>{{ r['当前家族敞口分'] == null ? '—' : yuan(r['当前家族敞口分']) }}</td>
            <td class="reason">{{ r.fail_reason || '—' }}</td>
            <td>{{ r.created_at }}</td>
          </tr>
          <tr v-if="!rows.length"><td colspan="12">尚无迁移登记</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
