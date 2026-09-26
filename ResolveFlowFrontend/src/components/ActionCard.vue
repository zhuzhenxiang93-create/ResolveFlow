<template>
  <article class="action-card">
    <div class="panel-heading">
      <span class="eyebrow">{{
        refund ? "Refund request" : "Business action"
      }}</span
      ><span :class="['badge', action.status]">{{
        states[action.status] || action.status
      }}</span>
    </div>
    <h3>{{ action.quote.title }}</h3>
    <p>{{ action.quote.label }}</p>
    <strong v-if="refund" class="amount"
      >{{ money(action.quote.amount_minor) }}<small> CNY</small></strong
    >
    <p class="impact">
      {{ action.reason || action.quote.reason || action.quote.effect }}
    </p>
    <p v-if="action.quote.return_required" class="hint">
      Return required · Review before processing
    </p>
    <ol class="timeline" aria-label="Action progress">
      <li v-for="step in steps" :key="step.label" :class="{ done: step.done }">
        <span>{{ step.done ? "✓" : "○" }}</span
        >{{ step.label }}
      </li>
    </ol>
    <div class="actions" v-if="!reviewer">
      <button
        v-if="action.status === 'awaiting_confirmation'"
        :disabled="busy"
        @click="$emit('decision', 'confirm')"
      >
        {{ refund ? "Confirm refund" : "Confirm action" }}
      </button>
      <button
        class="secondary"
        v-if="
          ['awaiting_confirmation', 'awaiting_approval'].includes(action.status)
        "
        :disabled="busy"
        @click="$emit('decision', 'cancel')"
      >
        Cancel
      </button>
    </div>
    <div class="actions" v-else>
      <template v-if="action.status === 'awaiting_approval'"
        ><button :disabled="busy" @click="$emit('decision', 'approve')">
          Approve</button
        ><button
          class="secondary"
          :disabled="busy"
          @click="$emit('decision', 'reject')"
        >
          Reject
        </button></template
      >
      <button
        v-if="action.status === 'awaiting_return'"
        :disabled="busy"
        @click="$emit('decision', 'receive_return')"
      >
        Simulate return received
      </button>
      <button
        v-if="action.status === 'refund_processing'"
        :disabled="busy"
        @click="$emit('decision', 'settle')"
      >
        Simulate payment receipt
      </button>
    </div>
    <p v-if="action.status === 'stale'" class="hint">
      The quote has changed or expired. Request a new quote and confirm again.
    </p>
    <small class="simulation">Simulated business · No real payment</small>
  </article>
</template>
<script setup>
import { computed } from "vue";
const props = defineProps({ action: Object, reviewer: Boolean, busy: Boolean });
defineEmits(["decision"]);
const refund = computed(() => props.action.quote.operation === "refund");
const money = (n) => `¥${(Number(n) / 100).toFixed(2)}`;
const states = {
  awaiting_confirmation: "Waiting for confirmation",
  awaiting_approval: "Waiting for independent review",
  awaiting_return: "Waiting for return",
  refund_processing: "Refund processing",
  completed: "Completed",
  rejected: "Rejected",
  cancelled: "Cancelled",
  stale: "Needs re-confirmation",
  ineligible: "Not eligible",
};
const steps = computed(() => {
  const a = props.action,
    d = a.decisions || [];
  const list = [
    { label: "Requested", done: true },
    { label: "User confirmed", done: d.includes("confirm") },
  ];
  if (a.quote.requires_review)
    list.push({
      label: "Reviewed",
      done: d.includes("approve") || d.includes("reject"),
    });
  if (a.quote.return_required)
    list.push({ label: "Return received", done: d.includes("receive_return") });
  if (refund.value)
    list.push({
      label: "Refund processing",
      done: ["refund_processing", "completed"].includes(a.status),
    });
  list.push({ label: "Completed", done: a.status === "completed" });
  return list;
});
</script>
