<template>
  <section class="commerce-panel">
    <div class="panel-heading">
      <div>
        <span class="eyebrow">Business context</span>
        <h2>
          {{ reviewer ? "Independent review" : "Purchases & after-sales" }}
        </h2>
      </div>
      <button class="text-button" :disabled="busy" @click="refresh">
        Refresh
      </button>
    </div>
    <p v-if="reviewer" class="hint">
      Separate reviewer identity · User confirmation must happen first.
    </p>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <section v-if="candidates.length && !reviewer" class="selection-box">
      <h3>Which purchase would you like to refund?</h3>
      <p>You choose the object. No action has been authorized.</p>
      <button
        class="secondary"
        v-for="c in candidates"
        :key="c.id"
        :disabled="busy"
        @click="$emit('select', c.id)"
      >
        {{ c.title }} <span>→</span>
      </button>
    </section>
    <section v-if="!reviewer && settings.userToken" class="memory-card" aria-labelledby="memory-title">
      <div class="panel-heading">
        <span class="eyebrow">Memory</span
        ><span class="badge">Never used as consent</span>
      </div>
      <h3 id="memory-title">What ResolveFlow remembers</h3>
      <dl>
        <dt>Reply style</dt>
        <dd>
          {{ styleLabel }}
          <small v-if="!profile.response_style">· say “以后回复简短一点” or pick below</small>
        </dd>
        <dt>“Last time” can mean</dt>
        <dd v-if="recent.length">
          <span v-for="r in recent.slice(0, 3)" :key="r.id" class="memory-chip"
            >{{ r.title }} · {{ r.operation }} · {{ caseLabel(r.status) }}</span
          >
        </dd>
        <dd v-else>Nothing yet — queries and requests you make show up here.</dd>
      </dl>
      <div class="actions">
        <button
          :class="['secondary', { selected: profile.response_style === 'concise' }]"
          :disabled="busy"
          @click="setStyle('concise')"
        >
          Concise</button
        ><button
          :class="['secondary', { selected: profile.response_style === 'detailed' }]"
          :disabled="busy"
          @click="setStyle('detailed')"
        >
          Detailed</button
        ><button class="text-button" :disabled="busy" @click="forget">
          Forget conversation memory
        </button>
      </div>
      <p class="hint">
        Memory helps with “上次那笔” and reply length. It never changes
        eligibility or authorization; a remembered request still needs your
        confirmation on its card. One-time codes and card numbers are not
        stored.
      </p>
    </section>
    <div class="case-list">
      <template v-for="c in visibleCases" :key="c.id"
        ><ActionCard
          v-for="a in c.operations"
          :key="a.id"
          :action="a"
          :reviewer="reviewer"
          :busy="busy || externalBusy"
          @decision="act(c, a, $event)"
      /></template>
    </div>
    <p class="empty-review" v-if="reviewer && !visibleCases.length">
      No requests waiting for review.
    </p>
    <template v-if="!reviewer">
      <div class="purchase-grid">
        <article v-for="o in objects" :key="o.id" class="purchase-card">
          <div class="panel-heading">
            <span class="object-icon">{{
              o.domain === "goods" ? "▣" : "◇"
            }}</span
            ><span class="badge">{{ label(o.status) }}</span>
          </div>
          <h3>{{ o.title }}</h3>
          <p class="paid">
            Paid
            <strong>{{
              money(o.payments.reduce((n, p) => n + p.amount_minor, 0))
            }}</strong>
          </p>
          <p v-if="o.domain === 'subscription'">
            Auto-renew <strong>{{ o.auto_renew ? "ON" : "OFF" }}</strong
            ><br />Benefits · {{ o.entitlement }}
          </p>
          <details>
            <summary>View details</summary>
            <dl>
              <dt>Order</dt>
              <dd>{{ o.id }}</dd>
              <dt>Invoice</dt>
              <dd>{{ label(o.invoice?.status || "not_requested") }}</dd>
            </dl>
            <p v-for="p in o.payments" :key="p.id">
              {{ label(p.kind) }} · {{ money(p.amount_minor) }}
            </p>
            <p v-if="o.shipment">
              {{ o.shipment.carrier }} · {{ o.shipment.tracking }}
            </p>
            <p v-for="(e, i) in o.shipment?.events || []" :key="i">
              {{ label(e.status) }} ·
              {{ new Date(e.at * 1000).toLocaleDateString() }}
            </p>
            <p v-for="r in o.refunds" :key="r.id">
              Refund · {{ money(r.amount_minor) }} · {{ label(r.status) }}
            </p>
            <p v-if="!o.refunds.length">No refunds recorded</p>
          </details>
          <div class="actions">
            <button
              class="secondary"
              :disabled="busy"
              @click="$emit('ask', `查询 ${o.id}`)"
            >
              View status</button
            ><button
              class="secondary"
              :disabled="busy"
              @click="
                $emit(
                  'ask',
                  o.status === 'unpaid'
                    ? `请取消订单 ${o.id}`
                    : `请帮我申请退款 ${o.id}`,
                )
              "
            >
              {{
                o.status === "unpaid" ? "Cancel order" : "Request refund"
              }}</button
            ><button
              class="text-button"
              v-if="o.domain === 'subscription' && o.auto_renew"
              :disabled="busy"
              @click="$emit('ask', `请关闭自动续费 ${o.id}`)"
            >
              Cancel renewal
            </button>
          </div>
        </article>
      </div>
    </template>
  </section>
</template>
<script setup>
import { ref, computed, watch, onMounted } from "vue";
import { commerceRequest } from "../lib/backends";
import ActionCard from "./ActionCard.vue";
const props = defineProps({
  settings: Object,
  latest: Object,
  reviewer: Boolean,
  externalBusy: Boolean,
  candidates: { type: Array, default: () => [] },
});
const emit = defineEmits(["ask", "select", "updated", "busy"]);
const objects = ref([]),
  cases = ref([]),
  profile = ref({}),
  recent = ref([]),
  error = ref(""),
  busy = ref(false);
const visibleCases = computed(() => cases.value);
const money = (n) => `¥${(Number(n) / 100).toFixed(2)}`;
const labels = {
  delivered: "Delivered",
  paid: "Paid",
  unpaid: "Unpaid",
  shipped: "Shipped",
  active: "Active",
  cancelled: "Cancelled",
  terminated: "Terminated",
  issued: "Issued",
  not_requested: "Not requested",
  adjustment_required: "Adjustment required",
  initial: "Initial payment",
  duplicate: "Duplicate charge",
  renewal: "Renewal",
  succeeded_simulated: "Completed · simulated",
  refund_processing: "Refund processing",
  awaiting_return: "Waiting for return",
};
const label = (s) => labels[s] || s;
const caseStates = {
  awaiting_confirmation: "awaiting your confirmation",
  awaiting_approval: "awaiting review",
  awaiting_return: "waiting for return",
  refund_processing: "refund processing",
  completed: "completed",
  cancelled: "cancelled",
  rejected: "rejected",
  stale: "expired",
  ineligible: "not eligible",
};
const caseLabel = (s) => caseStates[s] || s;
const styleLabel = computed(() =>
  profile.value.response_style === "concise"
    ? "Concise"
    : profile.value.response_style === "detailed"
      ? "Detailed"
      : "Default (detailed)",
);
let generation = 0;
async function refresh() {
  const g = ++generation;
  if (!props.settings.userToken) {
    objects.value = [];
    cases.value = [];
    return;
  }
  try {
    const [o, c, m] = await Promise.all([
      commerceRequest(props.settings, "/objects"),
      commerceRequest(props.settings, props.reviewer ? "/review" : "/cases", {
        reviewer: props.reviewer,
      }),
      commerceRequest(props.settings, "/memory"),
    ]);
    if (g !== generation) return;
    objects.value = o.objects;
    cases.value = c.cases;
    profile.value = m.profile;
    recent.value = m.recent_objects || [];
    error.value = "";
  } catch (e) {
    if (g === generation) error.value = e.message;
  }
}
async function act(c, a, decision) {
  if (busy.value || props.externalBusy) return;
  busy.value = true;
  emit('busy', true);
  const identity = props.settings.userToken;
  try {
    const r = await commerceRequest(
      props.settings,
      props.reviewer ? `/review/${c.id}` : `/cases/${c.id}/decision`,
      {
        method: "POST",
        body: { action_id: a.id, decision },
        reviewer: props.reviewer,
      },
    );
    if (identity !== props.settings.userToken) return;
    emit("updated", r);
    await refresh();
  } catch (e) {
    if (identity === props.settings.userToken) error.value = e.message;
  } finally {
    busy.value = false;
    emit('busy', false);
  }
}
async function setStyle(response_style) {
  try {
    await commerceRequest(props.settings, "/memory", {
      method: "PUT",
      body: { response_style },
    });
    await refresh();
  } catch (e) {
    error.value = e.message;
  }
}
async function forget() {
  try {
    await commerceRequest(props.settings, "/memory", { method: "DELETE" });
    await refresh();
  } catch (e) {
    error.value = e.message;
  }
}
watch(
  () => [props.settings.userToken, props.reviewer],
  () => {
    objects.value = [];
    cases.value = [];
    profile.value = {};
    recent.value = [];
    error.value = '';
    refresh();
  },
);
watch(() => props.latest, refresh);
onMounted(refresh);
</script>
