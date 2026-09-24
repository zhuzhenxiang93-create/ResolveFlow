<template>
  <main class="app-shell">
    <aside class="sidebar">
      <section class="brand">
        <div class="brand-mark">EM</div>
        <div>
          <h1>ResolveFlow Console</h1>
          <p>调试 ResolveFlow 后端</p>
        </div>
      </section>

      <a class="profile-card" href="https://xhslink.com/m/558VOQs4Otc" target="_blank" rel="noreferrer">
        <span>我的主页</span>
        <strong>小红书 69.6K 次赞与收藏</strong>
        <em>来看看我的主页 &gt;&gt;</em>
      </a>

      <section class="panel">
        <div class="panel-heading">
          <h2>后端</h2>
          <span class="pill">{{ currentBackend.label }}</span>
        </div>

        <label>
          <span>API 地址</span>
          <input v-model="settings.endpoint" @change="persist" placeholder="/api/python" />
        </label>
        <label>
          <span>用户 ID</span>
          <input v-model="settings.userId" @change="persist" placeholder="u1001" />
        </label>
        <label>
          <span>会话 ID</span>
          <input v-model="settings.conversationId" @change="persist" placeholder="自动生成" />
        </label>

        <div class="actions">
          <button @click="checkHealth">健康检查</button>
          <button @click="loadStats">刷新状态</button>
        </div>
      </section>

      <section class="panel">
        <div class="panel-heading">
          <h2>身份令牌</h2>
          <span class="pill soft">全站鉴权</span>
        </div>
        <p class="hint">
          Python 后端现在全站都要本地签发的 JWT——`/chat` 及全部接口都不再信任匿名 user_id。
          三个角色权限不同：user 聊天/办理业务，reviewer 审批（必须与用户是不同身份，服务端拒绝自批），
          admin 管运维/知识库写入。用 <code>make mint-token SUBJECT=alice ROLE=user</code> 之类的命令签发。
        </p>
        <label>
          <span>用户 Token</span>
          <input v-model="settings.userToken" @change="persist" placeholder="role=user 的 JWT" />
        </label>
        <label>
          <span>审核员 Token</span>
          <input v-model="settings.reviewerToken" @change="persist" placeholder="role=reviewer 的 JWT（不同身份）" />
        </label>
        <label>
          <span>管理员 Token</span>
          <input v-model="settings.adminToken" @change="persist" placeholder="role=admin 的 JWT（监控/知识库写入）" />
        </label>
      </section>

      <section class="panel status-panel">
        <div class="panel-heading">
          <h2>状态</h2>
          <span :class="['status-dot', healthOk ? 'online' : 'offline']"></span>
        </div>
        <dl>
          <div>
            <dt>当前后端</dt>
            <dd>{{ currentBackend.label }}</dd>
          </div>
          <div>
            <dt>健康状态</dt>
            <dd :class="healthOk ? 'ok' : 'muted'">{{ healthLabel }}</dd>
          </div>
          <div>
            <dt>知识片段</dt>
            <dd>{{ knowledgeCount }}</dd>
          </div>
        </dl>
        <pre v-if="statusText">{{ statusText }}</pre>
      </section>
    </aside>

    <section class="workspace">
      <header class="workspace-header">
        <div>
          <span class="eyebrow">ResolveFlow Workspace</span>
          <h2>对话调试</h2>
          <p>{{ currentBackend.baseUrl }}</p>
        </div>
        <div class="header-actions">
          <a class="profile-link" href="https://xhslink.com/m/558VOQs4Otc" target="_blank" rel="noreferrer">小红书主页</a>
          <a :href="docsUrl" target="_blank" rel="noreferrer">API 文档</a>
        </div>
      </header>

      <section class="chat-panel">
        <div class="messages" ref="messageList">
          <article v-for="item in messages" :key="item.id" :class="['message', item.role]">
            <div class="message-meta">
              <span>{{ item.role === 'user' ? '用户' : currentBackend.label }}</span>
              <small v-if="item.meta">{{ item.meta }}</small>
            </div>
            <p>{{ item.content }}</p>
            <ul v-if="item.sources?.length"><li v-for="source in item.sources" :key="source.document_id">依据：{{ source.title }} · {{ source.version }} · {{ source.source }}</li></ul>
          </article>
          <div v-if="messages.length === 0" class="empty-state">
            <h3>开始一次客服对话</h3>
            <p>输入问题即可开始，右侧会显示任务状态与确认/审批操作。</p>
          </div>
        </div>

        <form class="composer" @submit.prevent="sendMessage">
          <textarea v-model="draft" rows="3" placeholder="输入问题，例如：我想申请退款，订单号是 #12345"></textarea>
          <button :disabled="busy || !draft.trim()">{{ busy ? '发送中' : '发送' }}</button>
        </form>
      </section>

      <CommercePanel :settings="settings" :latest="latestCommerce" :candidates="commerceCandidates" @ask="askCommerce" @select="selectCommerce" @updated="commerceUpdated" />



      <section class="tools-grid">
        <article class="tool-panel">
          <div class="panel-heading">
            <h2>知识库检索</h2>
            <span class="pill soft">RAG</span>
          </div>
          <div class="inline-form">
            <input v-model="searchQuery" placeholder="退款多久能到账" />
            <button @click="searchKnowledge" :disabled="busy || !searchQuery.trim()">检索</button>
          </div>
          <div class="result-list">
            <article v-for="item in searchResults" :key="item.id || item.title" class="result-item">
              <strong>{{ item.title || '未命名结果' }}</strong>
              <span>score {{ item.score ?? '-' }}</span>
              <p>{{ item.content }}</p>
            <ul v-if="item.sources?.length"><li v-for="source in item.sources" :key="source.document_id">依据：{{ source.title }} · {{ source.version }} · {{ source.source }}</li></ul>
            </article>
          </div>
        </article>

        <article class="tool-panel">
          <div class="panel-heading">
            <h2>导入知识</h2>
            <span class="pill soft">Docs</span>
          </div>
          <label><span>文档 ID（更新时保持一致）</span><input v-model="docId" placeholder="goods-new-policy" /></label>
          <label><span>业务域</span><select v-model="docDomain"><option value="goods">商品</option><option value="subscription">订阅</option><option value="general">通用</option></select></label>
          <label><span>文档版本</span><input v-model="docVersion" /></label>
          <label><span>生效日期</span><input v-model="docEffective" type="date" /></label>
          <label>
            <span>标题</span>
            <input v-model="docTitle" placeholder="退款补充政策" />
          </label>
          <label>
            <span>内容</span>
            <textarea v-model="docContent" rows="5" placeholder="输入知识库内容"></textarea>
          </label>
          <div class="actions">
            <button @click="submitKnowledge" :disabled="busy || !docTitle.trim() || !docContent.trim()">添加文档</button>
            <label class="file-button">
              上传文件
              <input type="file" accept=".txt,.md,.json" @change="handleUpload" />
            </label>
          </div>
        </article>
      </section>
    </section>
  </main>
</template>

<script setup>
import CommercePanel from './components/CommercePanel.vue'
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import {
  addKnowledge,
  approveTask,
  backendMeta,
  cancelTask,
  confirmTask,
  createInitialSettings,
  releaseTask,
  requestChat,
  requestHealth,
  requestKnowledgeStats,
  requestMonitor,
  requestSearch,
  reviseTask,
  saveSettings,
  seedOrder,
  uploadKnowledge
} from './lib/backends'

const latestCommerce = ref(null)
const commerceCandidates = ref([])
const selectedObject = ref('')
const settings = reactive(createInitialSettings())
const messages = ref([])
const draft = ref('')
const busy = ref(false)
const healthOk = ref(false)
const healthLabel = ref('未检查')
const statusText = ref('')
const knowledgeCount = ref('-')
const searchQuery = ref('退款多久能到账')
const searchResults = ref([])
const docId=ref('')
const docDomain=ref('goods')
const docVersion=ref('v1')
const docEffective=ref(new Date().toISOString().slice(0,10))
const docTitle = ref('商品说明')
const docContent = ref('')
const messageList = ref(null)
const seededOrderId = ref('')
const currentTask = ref(null)
const showReviseForm = ref(false)
const reviseDraft = ref('')

const STATUS_LABELS = {
  awaiting_clarification: '待补充信息',
  running: '执行中',
  awaiting_confirmation: '待用户确认',
  awaiting_approval: '待人工审批',
  needs_human: '需人工处理',
  completed: '已完成',
  cancelled: '已取消',
  rejected: '已拒绝',
  superseded: '已被替代'
}

const currentBackend = computed(() => backendMeta(settings))
const docsUrl = computed(() => `${currentBackend.value.baseUrl}/docs`)
// Independent goals can each have their own pending confirmation/approval at
// once now (the backend keeps them as `confirmations`/`approvals` dicts
// keyed by tool, not a single `confirmation`/`approval` field), so surface
// every one that's still pending rather than assuming there is only one.
const pendingConfirmations = computed(() =>
  Object.values(currentTask.value?.confirmations || {}).filter((c) => c.status === 'pending')
)
const pendingApprovals = computed(() =>
  Object.values(currentTask.value?.approvals || {}).filter((a) => a.status === 'pending')
)

watch(
  () => settings.conversationId,
  () => persist()
)

onMounted(() => {
  checkHealth()
  loadStats()
})

function statusLabel(status) {
  return STATUS_LABELS[status] || status
}

function statusClass(status) {
  if (['completed'].includes(status)) return 'status-good'
  if (['awaiting_approval', 'awaiting_confirmation', 'ready', 'pending'].includes(status)) return 'status-pending'
  if (['needs_human', 'rejected', 'blocked'].includes(status)) return 'status-alert'
  return 'status-neutral'
}

function persist() {
  saveSettings(settings)
}

function applyTask(task, label) {
  if (!task) return
  currentTask.value = task
  showReviseForm.value = false
  messages.value.push({ id: crypto.randomUUID(), role: 'assistant', content: task.response || '', meta: label })
}

async function sendMessage() {
  const content = draft.value.trim()
  if (!content) return
  messages.value.push({ id: crypto.randomUUID(), role: 'user', content })
  draft.value = ''
  busy.value = true
  try {
    const response = await requestChat(settings, content, { orderId: selectedObject.value || undefined })
    selectedObject.value = ''
    latestCommerce.value = response.raw.commerce_case || { refreshed: Date.now() }
    commerceCandidates.value = response.raw.candidates || []
    if (response.conversationId && !settings.conversationId) {
      settings.conversationId = response.conversationId
      persist()
    }
    if (response.actionTask) {
      currentTask.value = response.actionTask
    }
    const meta = [
      response.intent,
      response.agentType,
      response.knowledgeUsed ? 'RAG' : '',
      response.actionTask ? statusLabel(response.actionTask.status) : '',
      response.escalated ? '转人工' : ''
    ].filter(Boolean).join(' · ')
    messages.value.push({
      id: crypto.randomUUID(),
      role: 'assistant',
      content: response.response,
      sources: response.sources,
      meta
    })
  } catch (error) {
    messages.value.push({
      id: crypto.randomUUID(),
      role: 'assistant',
      content: error.message,
      meta: '请求失败'
    })
  } finally {
    busy.value = false
    await nextTick()
    messageList.value?.scrollTo({ top: messageList.value.scrollHeight, behavior: 'smooth' })
  }
}

watch(() => settings.userToken, () => {
  messages.value=[];currentTask.value=null;latestCommerce.value=null;commerceCandidates.value=[]
  settings.conversationId='';seededOrderId.value='';persist()
})
watch(() => settings.conversationId, (value, old) => {
  if(old && value !== old){messages.value=[];currentTask.value=null;latestCommerce.value=null;commerceCandidates.value=[]}
})
function askCommerce(message){draft.value=message;sendMessage()}
function selectCommerce(id){selectedObject.value=id;draft.value=id;sendMessage()}
function commerceUpdated(result){latestCommerce.value=result;messages.value.push({id:crypto.randomUUID(),role:'assistant',content:result.response,meta:'业务状态已核验'})}

async function createSeedOrder() {
  busy.value = true
  try {
    const order = await seedOrder(settings)
    seededOrderId.value = order.id
  } catch (error) {
    statusText.value = error.message
  } finally {
    busy.value = false
  }
}

function insertOrderId() {
  draft.value = draft.value ? `${draft.value} ${seededOrderId.value}` : seededOrderId.value
}

async function respondConfirmation(confirmationId, accepted) {
  if (!currentTask.value || !confirmationId) return
  busy.value = true
  try {
    const task = await confirmTask(settings, currentTask.value.id, confirmationId, accepted)
    applyTask(task, accepted ? '已确认' : '已拒绝确认')
  } catch (error) {
    statusText.value = error.message
  } finally {
    busy.value = false
  }
}

async function respondApproval(approvalId, approved) {
  if (!currentTask.value || !approvalId) return
  busy.value = true
  try {
    const task = await approveTask(settings, currentTask.value.id, approvalId, approved)
    applyTask(task, approved ? '审核员已批准' : '审核员已拒绝')
  } catch (error) {
    statusText.value = error.message
  } finally {
    busy.value = false
  }
}

async function releaseCurrentTask() {
  if (!currentTask.value) return
  busy.value = true
  try {
    const task = await releaseTask(settings, currentTask.value.id)
    applyTask(task, '审核员已释放')
  } catch (error) {
    statusText.value = error.message
  } finally {
    busy.value = false
  }
}

async function cancelCurrentTask() {
  if (!currentTask.value) return
  busy.value = true
  try {
    const task = await cancelTask(settings, currentTask.value.id)
    applyTask(task, '已取消')
  } catch (error) {
    statusText.value = error.message
  } finally {
    busy.value = false
  }
}

async function reviseCurrentTask() {
  if (!currentTask.value || !reviseDraft.value.trim()) return
  busy.value = true
  try {
    const task = await reviseTask(settings, currentTask.value.id, reviseDraft.value.trim())
    reviseDraft.value = ''
    applyTask(task, '已提交修订')
  } catch (error) {
    statusText.value = error.message
  } finally {
    busy.value = false
  }
}

async function checkHealth() {
  try {
    const data = await requestHealth(settings)
    healthOk.value = data.status === 'ok'
    healthLabel.value = data.status || 'ok'
    statusText.value = JSON.stringify(data, null, 2)
  } catch (error) {
    healthOk.value = false
    healthLabel.value = '不可用'
    statusText.value = error.message
  }
}

async function loadStats() {
  try {
    const [stats, monitor] = await Promise.allSettled([
      requestKnowledgeStats(settings),
      requestMonitor(settings)
    ])
    if (stats.status === 'fulfilled') {
      knowledgeCount.value = stats.value.total_chunks ?? stats.value.totalChunks ?? '-'
    }
    if (monitor.status === 'fulfilled') {
      statusText.value = JSON.stringify(monitor.value, null, 2)
    }
  } catch (error) {
    statusText.value = error.message
  }
}

async function searchKnowledge() {
  busy.value = true
  try {
    const data = await requestSearch(settings, searchQuery.value, 5)
    searchResults.value = data.results || []
  } catch (error) {
    statusText.value = error.message
  } finally {
    busy.value = false
  }
}

async function submitKnowledge() {
  busy.value = true
  try {
    const data = await addKnowledge(settings, [
      { id:docId.value.trim() || undefined, domain:docDomain.value, version:docVersion.value, effective_at:docEffective.value, source:"管理员上传的演示资料", title: docTitle.value.trim(), content: docContent.value.trim() }
    ])
    statusText.value = JSON.stringify(data, null, 2)
    await loadStats()
  } catch (error) {
    statusText.value = error.message
  } finally {
    busy.value = false
  }
}

async function handleUpload(event) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file) return
  busy.value = true
  try {
    const data = await uploadKnowledge(settings, file)
    statusText.value = JSON.stringify(data, null, 2)
    await loadStats()
  } catch (error) {
    statusText.value = error.message
  } finally {
    busy.value = false
  }
}
</script>
