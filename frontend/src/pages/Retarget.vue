<script setup>
import { onMounted, ref } from 'vue'
import { getJson, postJson } from '../api'

const rows = ref([])
const oldKey = ref('')
const newKey = ref('')
const err = ref('')
const msg = ref('')
const busy = ref(false)

// 只读重指向证明查询（按迁移 id）
const proofId = ref('')
const proof = ref(null)
const proofErr = ref('')
const proofBusy = ref(false)

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
      `合并后剩余敞口 ${yuan(r.merged_remain_cents)} 元，重指向证明 #${r.id}`
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

async function queryProof() {
  proofBusy.value = true
  proof.value = null
  proofErr.value = ''
  try {
    proof.value = await getJson(`/api/key-retargets/${proofId.value.trim()}/repoint-proof`)
  } catch (e) {
    let reason = String(e.message || e)
    try {
      reason = JSON.parse(e.message)?.detail || reason
    } catch {
      /* 非 JSON 响应，保留原文 */
    }
    proofErr.value = reason
  } finally {
    proofBusy.value = false
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
      登记一次正向键迁移（old_key → new_key，同账户）。生效后所有仍引用旧键的<strong>未终态冲正</strong>
      同事务改指新键并同步敞口键（已隔离项释放复位），<strong>已终态冲正</strong> target 不动、留可查迁移边；
      合并超额或事务异常整笔回滚到迁移前，禁止半截 target。
    </p>
    <p class="note-strip">
      迁移关系落独立表：key_retargets（迁移登记）+ retarget_touches（逐冲正不可变迁移边）+
      retarget_repoint_proofs（重指向证明：旧键、新键、重指向数、未触及键），与流水同源，非主数据 SCD。
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
      <h2>重指向证明（只读，按迁移 id 查询）</h2>
      <div class="form-row">
        <label>
          迁移 id
          <input v-model="proofId" placeholder="如 1" />
        </label>
        <button class="btn" :disabled="proofBusy || !proofId.trim()" @click="queryProof">
          查询证明
        </button>
      </div>
      <p v-if="proofErr" class="msg">{{ proofErr }}</p>
      <table v-if="proof">
        <tbody>
          <tr><th>证明 id</th><td>{{ proof.id }}</td></tr>
          <tr><th>迁移 id / 运行</th><td>#{{ proof.retarget_id }} / 运行 #{{ proof.settle_run_id }}</td></tr>
          <tr><th>旧键 → 新键</th><td class="mono">{{ proof.old_entry_key }} → {{ proof.new_entry_key }}</td></tr>
          <tr><th>重指向数（未终态）</th><td>{{ proof.repointed_count }}</td></tr>
          <tr><th>终态留边数（已终态）</th><td>{{ proof.terminal_edge_count }}</td></tr>
          <tr>
            <th>触及冲正键</th>
            <td class="mono">{{ proof.touched_keys.join('、') || '—' }}</td>
          </tr>
          <tr>
            <th>未触及键（同账户其余冲正）</th>
            <td class="mono muted">{{ proof.untouched_keys.map(x => x.entry_key).join('、') || '—' }}</td>
          </tr>
          <tr><th>证明生成时间</th><td>{{ proof.created_at }}</td></tr>
        </tbody>
      </table>
      <table v-if="proof">
        <thead>
          <tr><th>迁移边：冲正键</th><th>处置</th><th>改前 target</th><th>改后 target</th><th>冲正状态</th></tr>
        </thead>
        <tbody>
          <tr v-for="e in proof['迁移边']" :key="e.reversal_key">
            <td class="mono">{{ e.reversal_key }}</td>
            <td><span class="tag" :class="{ on: e.action === '重指向' }">{{ e.action }}</span></td>
            <td class="mono">{{ e.from_target_key }}</td>
            <td class="mono">{{ e.to_target_key }}</td>
            <td>{{ e.reversal_status }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel">
      <h2>迁移映射（迁移表）</h2>
      <table>
        <thead>
          <tr>
            <th>#</th><th>生效运行</th><th>旧键</th><th>新键</th><th>状态</th>
            <th>重指向笔数</th><th>旧键剩余</th><th>新键剩余</th><th>合并剩余（元）</th>
            <th>当前家族敞口（元）</th><th>重指向证明</th><th>失败原因</th><th>时间</th>
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
            <td>
              <button v-if="r['重指向证明id']" class="btn btn-mini" @click="proofId = String(r.id); queryProof()">
                #{{ r['重指向证明id'] }}
              </button>
              <template v-else>—</template>
            </td>
            <td class="reason">{{ r.fail_reason || '—' }}</td>
            <td>{{ r.created_at }}</td>
          </tr>
          <tr v-if="!rows.length"><td colspan="13">尚无迁移登记</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
