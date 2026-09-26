<template>
  <div class="app-shell">
    <header class="topbar">
      <div class="brand">
        <span class="brand-mark">R<span>↗</span></span>
        <div>
          <h1>ResolveFlow</h1>
          <p>AI Customer Service with Controlled Business Actions</p>
        </div>
      </div>
      <div class="header-actions">
        <span class="badge">{{
          demoMode ? "Demo environment" : "Connected workspace"
        }}</span
        ><span :class="['connection', connected ? 'online' : '']"
          >● {{ connected ? "API connected" : "API unavailable" }}</span
        ><button
          v-if="demoMode"
          class="secondary"
          :disabled="busy"
          @click="reset"
        >
          Reset Demo
        </button>
      </div>
    </header>
    <main>
      <section class="hero">
        <div>
          <span class="eyebrow">From conversation to resolution</span>
          <h2>Helpful answers.<br />Actions you can verify.</h2>
          <p>
            Understand requests, find the right purchase, and complete
            after-sales workflows with explicit confirmation.
          </p>
        </div>
        <div class="value-list">
          <p><b>01</b> Answer <span>Grounded in policy sources</span></p>
          <p><b>02</b> Query <span>Connected to business records</span></p>
          <p><b>03</b> Action <span>Confirmed, reviewed, verified</span></p>
        </div>
      </section>
      <div class="demo-note" v-if="demoMode">
        {{
          mode === "live-model"
            ? "Live model connected"
            : "Offline rules demo · Technical Agent requires a model connection"
        }}<span>All payments and fulfilment are simulated.</span>
      </div>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <section class="scenario-strip" aria-label="Quick start scenarios">
        <div>
          <span class="eyebrow">Try a scenario</span>
          <p>A complete story in a few clicks</p>
        </div>
        <button
          v-for="(s, i) in scenarios"
          :key="s.title"
          class="scenario"
          :disabled="busy || !connected"
          @click="ask(s.prompt)"
        >
          <small>0{{ i + 1 }} / {{ s.type }}</small
          ><strong>{{ s.title }} ↗</strong>
        </button>
      </section>
      <div class="view-tabs">
        <button :class="{ selected: !reviewer }" @click="reviewer = false">
          User view</button
        ><button :class="{ selected: reviewer }" @click="reviewer = true">
          Reviewer view</button
        ><span>Separate identities · Shared, verifiable case state</span>
      </div>
      <div class="workspace">
        <section class="chat-panel">
          <div class="panel-heading chat-heading">
            <div>
              <span class="eyebrow">Conversation</span>
              <h2>Your resolution starts here</h2>
            </div>
            <button
              class="text-button"
              :disabled="busy"
              @click="newConversation"
            >
              New chat
            </button>
          </div>
          <div class="messages" ref="messageList" aria-live="polite">
            <div v-if="!messages.length" class="empty-state">
              <span class="welcome-symbol">↗</span>
              <h3>How can I help?</h3>
              <p>
                Ask about a policy, check a purchase,<br />or start a request.
                You stay in control.
              </p>
              <span class="badge">Answer · Query · Action</span>
            </div>
            <article
              v-for="m in messages"
              :key="m.id"
              :class="['message', m.role]"
            >
              <div class="message-meta">
                {{ m.role === "user" ? "You" : "ResolveFlow"
                }}<small>{{ m.meta }}</small>
              </div>
              <p>{{ m.content }}</p>
              <div v-if="m.sources?.length" class="sources">
                <details v-for="s in m.sources" :key="s.document_id">
                  <summary>↗ {{ s.title }} · {{ s.version }}</summary>
                  <p>{{ s.source }}</p>
                  <small>{{ s.document_id }}</small>
                </details>
              </div>
            </article>
            <div v-if="busy" class="typing" role="status">
              ● ● ● &nbsp; Checking your request and business state…
            </div>
          </div>
          <form class="composer" @submit.prevent="send">
            <label class="sr-only" for="message">Message</label
            ><textarea
              id="message"
              v-model="draft"
              rows="2"
              maxlength="4000"
              placeholder="Ask a question or tell us what you need…"
              @keydown.enter.exact.prevent="send"
            /><button :disabled="busy || !draft.trim() || !connected">
              Send ↗
            </button>
          </form>
          <p class="composer-note">
            Business changes always require your confirmation.
          </p>
        </section>
        <CommercePanel
          :settings="settings"
          :latest="latest"
          :candidates="candidates"
          :reviewer="reviewer"
          @ask="ask"
          @select="select"
          @updated="updated"
        />
      </div>
      <details class="developer">
        <summary>Developer details</summary>
        <p>Conversation · {{ settings.conversationId || "Not started" }}</p>
        <pre>{{ trace }}</pre>
      </details>
      <details class="developer" @toggle="advanced = $event.target.open">
        <summary>Advanced settings</summary>
        <AdvancedSettings v-if="advanced" :settings="settings" />
      </details>
      <footer>
        ResolveFlow / Portfolio demo
        <span>Business authority stays in CommerceStore.</span>
      </footer>
    </main>
  </div>
</template>
<script setup>
import { reactive, ref, onMounted, watch, nextTick } from "vue";
import CommercePanel from "./components/CommercePanel.vue";
import AdvancedSettings from "./components/AdvancedSettings.vue";
import {
  createInitialSettings,
  saveSettings,
  requestChat,
  requestHealth,
  demoMode,
  startDemo,
} from "./lib/backends";
const settings = reactive(createInitialSettings()),
  messages = ref([]),
  draft = ref(""),
  busy = ref(false),
  connected = ref(false),
  reviewer = ref(false),
  latest = ref(null),
  candidates = ref([]),
  error = ref(""),
  trace = ref({}),
  advanced = ref(false),
  mode = ref(""),
  messageList = ref(null);
const scenarios = [
  { type: "Answer", title: "Refund policy", prompt: "商品退款政策是什么？" },
  { type: "Action", title: "Choose a purchase", prompt: "我要退款。" },
  {
    type: "Action",
    title: "Refund headphones",
    prompt: "帮我把无线耳机退掉。",
  },
  {
    type: "Action",
    title: "Cancel renewal",
    prompt: "Basic 会员下个月别续了。",
  },
  {
    type: "Mixed",
    title: "Billing + technical",
    prompt: "把 Pro 会员重复扣的钱退掉，而且登录一直报 401。",
  },
];
function append(role, content, extra = {}) {
  messages.value.push({ id: crypto.randomUUID(), role, content, ...extra });
}
function newConversation() {
  messages.value = [];
  settings.conversationId = "";
  candidates.value = [];
  trace.value = {};
  saveSettings(settings);
}
async function reset() {
  if (busy.value) return;
  busy.value = true;
  try {
    await startDemo(settings);
    newConversation();
    reviewer.value = false;
    latest.value = { reset: Date.now() };
    error.value = "";
    connected.value = true;
    mode.value = settings.mode;
  } catch (e) {
    error.value = e.message;
  } finally {
    busy.value = false;
  }
}
async function ask(text) {
  if (busy.value) return;
  reviewer.value = false;
  draft.value = text;
  await send();
}
async function select(id) {
  if (busy.value) return;
  draft.value = id;
  await send(id);
}
async function send(selection) {
  if (busy.value || !draft.value.trim()) return;
  const content = draft.value.trim();
  draft.value = "";
  append("user", content);
  busy.value = true;
  const identity = settings.userToken;
  try {
    const r = await requestChat(settings, content, {
      orderId: typeof selection === "string" ? selection : undefined,
    });
    if (identity !== settings.userToken) return;
    settings.conversationId = r.conversationId;
    saveSettings(settings);
    latest.value = r.raw.commerce_case || { at: Date.now() };
    candidates.value = r.raw.candidates || [];
    trace.value = r.raw;
    append("assistant", readable(r.response), {
      sources: r.sources,
      meta: r.knowledgeUsed
        ? "Knowledge sources"
        : r.agentType || "Business support",
    });
  } catch (e) {
    if (identity === settings.userToken)
      append("assistant", e.message, { meta: "Unable to complete request" });
  } finally {
    busy.value = false;
    await nextTick();
    messageList.value?.scrollTo({
      top: messageList.value.scrollHeight,
      behavior: "smooth",
    });
  }
}
function readable(text) {
  return text
    .split("\n")
    .map((line) => {
      const i = line.indexOf("：");
      if (i >= 0) {
        try {
          const data = JSON.parse(line.slice(i + 1));
          if (data && typeof data === "object")
            return (
              line.slice(0, i) +
              "：业务记录已更新，请展开右侧 View details 查看物流、发票或退款明细。"
            );
        } catch {}
      }
      return line;
    })
    .join("\n");
}
function updated(result) {
  latest.value = result;
  append("assistant", result.response, { meta: "Business state verified" });
}
watch(
  () => settings.userToken,
  () => {
    messages.value = [];
    candidates.value = [];
    latest.value = null;
    trace.value = {};
    settings.conversationId = "";
    saveSettings(settings);
  },
);
onMounted(async () => {
  try {
    const h = await requestHealth(settings);
    connected.value = h.status === "ok";
    mode.value = h.mode || "";
    if (demoMode && !settings.userToken) await reset();
  } catch (e) {
    error.value =
      "Could not connect to the API. Check the server in Advanced settings.";
  }
});
</script>
