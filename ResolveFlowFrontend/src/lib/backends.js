const BACKEND = {
  label: 'Python',
  baseUrl: import.meta.env.VITE_PYTHON_API_URL || '/api/python'
}

export function createInitialSettings() {
  const saved = readSettings()
  return {
    userId: saved.userId || 'u1001',
    conversationId: saved.conversationId || '',
    userToken: saved.userToken || '',
    reviewerToken: saved.reviewerToken || '',
    adminToken: saved.adminToken || '',
    endpoint: saved.endpoint || saved.endpoints?.python || BACKEND.baseUrl
  }
}

export function saveSettings(settings) {
  localStorage.setItem('resolveflow.frontend.settings', JSON.stringify(settings))
}

export function backendMeta(settings) {
  return { ...BACKEND, baseUrl: normalizeBaseUrl(settings.endpoint || BACKEND.baseUrl) }
}

export async function requestHealth(settings) {
  return requestJson(backendMeta(settings).baseUrl, '/health')
}

// /monitor exposes internal Agent/tool operational stats — requires the admin role
// (read endpoints like /search only need any authenticated role, but this one is
// treated as ops-only, same tier as knowledge-base writes and /skills/reload).
export async function requestMonitor(settings) {
  return requestJson(backendMeta(settings).baseUrl, '/monitor', { headers: authHeaders(settings, 'admin') })
}

export async function requestKnowledgeStats(settings) {
  return requestJson(backendMeta(settings).baseUrl, '/knowledge/stats', { headers: authHeaders(settings, 'user') })
}

export async function requestSearch(settings, query, topK = 5) {
  const params = new URLSearchParams({ query, top_k: String(topK) })
  return requestJson(backendMeta(settings).baseUrl, `/search?${params}`, {
    method: 'POST',
    headers: authHeaders(settings, 'user')
  })
}

export async function requestChat(settings, message, { taskId, orderId } = {}) {
  const meta = backendMeta(settings)
  const payload = buildChatPayload(settings, message, { taskId, orderId })
  const raw = await requestJson(meta.baseUrl, '/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders(settings, 'user') },
    body: JSON.stringify(payload)
  })
  return normalizeChatResponse(raw)
}

export async function addKnowledge(settings, documents) {
  return requestJson(backendMeta(settings).baseUrl, '/knowledge/add', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders(settings, 'admin') },
    body: JSON.stringify({ documents })
  })
}

export async function uploadKnowledge(settings, file) {
  const form = new FormData()
  form.append('file', file)
  return requestJson(backendMeta(settings).baseUrl, '/knowledge/upload', {
    method: 'POST',
    headers: authHeaders(settings, 'admin'),
    body: form
  })
}

// ── Action layer: tasks, confirmation, review ─────────────────────────────────

export async function seedOrder(settings) {
  return requestJson(backendMeta(settings).baseUrl, '/agent/demo/orders', {
    method: 'POST',
    headers: authHeaders(settings, 'user')
  })
}

export async function confirmTask(settings, taskId, confirmationId, accepted) {
  return requestJson(backendMeta(settings).baseUrl, `/agent/tasks/${encodeURIComponent(taskId)}/confirmation`, {
    method: 'POST',
    headers: { ...authHeaders(settings, 'user'), 'Content-Type': 'application/json' },
    body: JSON.stringify({ confirmation_id: confirmationId, accepted })
  })
}

export async function cancelTask(settings, taskId) {
  return requestJson(backendMeta(settings).baseUrl, `/agent/tasks/${encodeURIComponent(taskId)}/cancel`, {
    method: 'POST',
    headers: authHeaders(settings, 'user')
  })
}

export async function reviseTask(settings, taskId, message) {
  return requestJson(backendMeta(settings).baseUrl, `/agent/tasks/${encodeURIComponent(taskId)}/revise`, {
    method: 'POST',
    headers: { ...authHeaders(settings, 'user'), 'Content-Type': 'application/json' },
    body: JSON.stringify({ message })
  })
}

// Reviewer-role actions use reviewerToken, never userToken — the server rejects a
// reviewer whose subject matches the task owner, and this UI should not blur that
// boundary by reusing one token for both roles.
export async function approveTask(settings, taskId, approvalId, approved) {
  return requestJson(backendMeta(settings).baseUrl, `/agent/tasks/${encodeURIComponent(taskId)}/approval`, {
    method: 'POST',
    headers: { ...authHeaders(settings, 'reviewer'), 'Content-Type': 'application/json' },
    body: JSON.stringify({ approval_id: approvalId, approved })
  })
}

export async function releaseTask(settings, taskId) {
  return requestJson(backendMeta(settings).baseUrl, `/agent/tasks/${encodeURIComponent(taskId)}/release`, {
    method: 'POST',
    headers: authHeaders(settings, 'reviewer')
  })
}

const TOKEN_LABELS = { reviewer: '审核员 Token', admin: '管理员 Token', user: '用户 Token' }

// A raw fetch() throws an opaque "non ISO-8859-1 code point" TypeError if a
// header value contains anything outside Latin-1 — which is exactly what
// happens when a pasted JWT picks up a stray character (curly quotes, a
// zero-width space, a stray Chinese character from copying alongside
// instructional text). Validating against the real JWT shape up front turns
// that into a message the user can actually act on.
const JWT_PATTERN = /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/

function authHeaders(settings, role) {
  const label = TOKEN_LABELS[role] || TOKEN_LABELS.user
  const raw = role === 'reviewer' ? settings.reviewerToken : role === 'admin' ? settings.adminToken : settings.userToken
  const token = String(raw || '').trim()
  if (!token) return {}
  if (!JWT_PATTERN.test(token)) {
    throw new Error(`${label}格式不对（不是合法的 JWT）。粘贴时可能带上了多余字符（比如中文引号、空格或其他不可见字符）——清空输入框，只粘贴 token 本身，不要带前后的说明文字，重新试一次。`)
  }
  return { Authorization: `Bearer ${token}` }
}

function buildChatPayload(settings, message, { taskId, orderId } = {}) {
  const payload = {
    message,
    user_id: settings.userId || 'anonymous',
    conv_id: settings.conversationId || undefined
  }
  if (taskId) payload.task_id = taskId
  if (orderId) payload.order_id = orderId
  return payload
}

function normalizeChatResponse(raw) {
  return {
    conversationId: raw.conversation_id || raw.conversationId || raw.conv_id || '',
    response: raw.response || '',
    intent: raw.intent || 'other',
    agentType: raw.agent_type || raw.agentType || '',
    escalated: Boolean(raw.escalated),
    latencyMs: Number(raw.latency_ms ?? raw.latencyMs ?? 0),
    knowledgeUsed: Boolean(raw.knowledge_used ?? raw.knowledgeUsed),
    verified: raw.verified,
    grounded: raw.grounded,
    actionTask: raw.action_task || null,
    sources: raw.sources || [],
    raw
  }
}

async function requestJson(baseUrl, path, options = {}) {
  const url = `${normalizeBaseUrl(baseUrl)}${path}`
  const response = await fetch(url, options)
  const text = await response.text()
  let data = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = text
  }
  if (!response.ok) {
    const detail = typeof data === 'string' ? data : JSON.stringify(data)
    throw new Error(`${response.status} ${response.statusText}: ${detail}`)
  }
  return data
}

function normalizeBaseUrl(value) {
  return String(value || '').replace(/\/+$/, '')
}

function readSettings() {
  try {
    return JSON.parse(localStorage.getItem('resolveflow.frontend.settings') || '{}')
  } catch {
    return {}
  }
}

export async function commerceRequest(settings, path, { method = 'GET', body, reviewer = false } = {}) {
  return requestJson(backendMeta(settings).baseUrl, `/commerce${path}`, {
    method,
    headers: { ...authHeaders(settings, reviewer ? 'reviewer' : 'user'), 'Content-Type': 'application/json' },
    ...(body ? { body: JSON.stringify(body) } : {})
  })
}
