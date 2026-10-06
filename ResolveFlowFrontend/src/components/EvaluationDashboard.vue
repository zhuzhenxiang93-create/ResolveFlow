<template>
  <section class="eval" aria-labelledby="eval-title">
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <div v-else-if="!data" class="eval-loading" role="status">Loading the latest evaluation report…</div>
    <template v-else>
      <header class="eval-head">
        <div>
          <span class="eyebrow">Product evaluation</span>
          <h2 id="eval-title">Does the agent finish the job, safely?</h2>
          <p>
            {{ cur.dataset.case_count }} scripted conversations are replayed through the real
            <code>/chat → ConversationService → CommerceConversation → CommerceStore</code> chain.
            Success is judged on the persisted SQLite business state and audit trail, not on what the assistant says.
          </p>
        </div>
        <div class="eval-meta">
          <span class="badge">{{ modeLabel(cur.mode) }}</span>
          <span class="badge">SQLite · BM25 lexical retrieval</span>
          <span class="badge">Simulated payments &amp; orders</span>
          <small>Run {{ cur.run_id }} · commit {{ cur.git_commit || "n/a" }}{{ cur.git_dirty ? " (uncommitted changes)" : "" }}</small>
        </div>
      </header>

      <div class="run-switch" role="tablist" aria-label="Report run">
        <button v-for="r in runOptions" :key="r.key" role="tab" :aria-selected="run === r.key"
          :class="['secondary', { selected: run === r.key }]" @click="run = r.key">
          {{ r.label }}
        </button>
        <span>{{ runNote }}</span>
      </div>

      <div class="tiles primary-tiles">
        <article v-for="t in primaryTiles" :key="t.key" class="tile">
          <span class="tile-label">{{ t.label }}</span>
          <strong :class="t.tone">{{ t.value }}</strong>
          <small>{{ t.detail }}</small>
          <small v-if="t.delta" class="delta">{{ t.delta }}</small>
        </article>
      </div>
      <div class="tiles secondary-tiles">
        <article v-for="t in secondaryTiles" :key="t.key" class="tile small">
          <span class="tile-label">{{ t.label }}</span>
          <strong>{{ t.value }}</strong>
          <small>{{ t.detail }}</small>
          <small v-if="t.delta" class="delta">{{ t.delta }}</small>
        </article>
      </div>

      <div class="eval-grid">
        <section class="eval-card" v-if="comparison">
          <h3>Baseline → after fixes</h3>
          <p class="hint">Same 80-case dataset (sha256 {{ comparison.same_dataset ? "matches" : "differs" }}). The fixes were derived from these failures, so the re-run overstates generalisation; see the holdout.</p>
          <div class="legend" aria-hidden="true"><span class="key base"></span>Baseline <span class="key now"></span>After fixes</div>
          <div v-for="row in comparisonRows" :key="row.key" class="compare-row" :title="`${row.label}: baseline ${row.base}, after fixes ${row.now}`">
            <span>{{ row.label }}</span>
            <div class="pair">
              <i class="bar base" :style="{ width: row.baseW }"></i>
              <i class="bar now" :style="{ width: row.nowW }"></i>
            </div>
            <b>{{ row.base }} → {{ row.now }}</b>
          </div>
          <p class="hint">Bad cases {{ comparison.bad_cases_baseline }} → {{ comparison.bad_cases_current }} · regressions: {{ comparison.regressed_cases.length || "none" }}</p>
        </section>

        <section class="eval-card" v-if="holdout.current">
          <h3>Holdout check</h3>
          <p class="hint">{{ holdout.current.dataset.case_count }} paraphrased cases written before any fix and never used to tune the rules.</p>
          <div class="holdout-figures">
            <div><span class="tile-label">Before fixes</span><strong>{{ pct(holdout.baseline?.metrics.task_success_rate) }}</strong><small>{{ frac(holdout.baseline?.metrics.task_success_rate) }}</small></div>
            <span class="arrow">→</span>
            <div><span class="tile-label">After fixes</span><strong>{{ pct(holdout.current.metrics.task_success_rate) }}</strong><small>{{ frac(holdout.current.metrics.task_success_rate) }}</small></div>
          </div>
          <p class="hint">Task success on the holdout. Remaining failures stay visible under Bad cases → Holdout.</p>
        </section>

        <section class="eval-card" v-if="memory.current">
          <h3>Memory &amp; personalisation</h3>
          <p class="hint">{{ memory.current.dataset.case_count }} cases on cross-session recall, preferences, forgetting, redaction and “memory is never consent”.</p>
          <div class="holdout-figures">
            <div><span class="tile-label">Before fixes</span><strong>{{ pct(memory.baseline?.metrics.task_success_rate) }}</strong><small>{{ frac(memory.baseline?.metrics.task_success_rate) }}</small></div>
            <span class="arrow">→</span>
            <div><span class="tile-label">After fixes</span><strong>{{ pct(memory.current.metrics.task_success_rate) }}</strong><small>{{ frac(memory.current.metrics.task_success_rate) }}</small></div>
          </div>
          <ul class="check-list">
            <li v-for="c in memory.checks" :key="c.id" :class="c.passed ? 'ok' : 'bad'" :title="c.id">
              <span>{{ c.passed ? "✓" : "✕" }}</span>{{ c.title }}
            </li>
          </ul>
        </section>

        <section class="eval-card">
          <h3>Pass rate by scenario</h3>
          <div v-for="c in categories" :key="c.key" class="cat-row" :title="`${c.label}: ${c.passed} of ${c.total} passed`">
            <span>{{ c.label }}</span>
            <div class="track"><i class="bar now" :style="{ width: c.w }"></i></div>
            <b>{{ c.passed }}/{{ c.total }}</b>
          </div>
        </section>
      </div>

      <section class="eval-card bad-cases">
        <div class="bad-head">
          <div>
            <h3>Bad case breakdown</h3>
            <p class="hint">Failure types are assigned by deterministic rules over the recorded evidence. Root causes are only stated when provable; otherwise NEEDS_REVIEW.</p>
          </div>
          <div class="run-switch compact" role="tablist" aria-label="Bad case run">
            <button v-for="r in badRunOptions" :key="r.key" role="tab" :aria-selected="badRun === r.key"
              :class="['secondary', { selected: badRun === r.key }]" @click="selectBadRun(r.key)">{{ r.label }}</button>
          </div>
        </div>
        <p v-if="!badSummary.total" class="hint">No failing cases in this run.</p>
        <div class="breakdown" v-else>
          <button v-for="b in breakdown" :key="b.key" :class="['type-row', { active: filterType === b.key }]"
            :disabled="!b.count" @click="filterType = filterType === b.key ? '' : b.key"
            :title="`${b.label}: ${b.count} cases, ${b.share} of failures`">
            <span>{{ b.label }}</span>
            <div class="track"><i class="bar warn" :style="{ width: b.w }"></i></div>
            <b>{{ b.count }} · {{ b.share }}</b>
          </button>
        </div>
        <div class="case-browser" v-if="badSummary.total">
          <ul class="case-list" aria-label="Failing cases">
            <li v-for="c in caseRows" :key="c.id">
              <button :class="['case-item', { active: detail?.case.id === c.id }]" @click="openCase(c.id)">
                <span class="case-id">{{ c.id }}</span>
                <span class="case-msg">“{{ c.first_message || c.title }}”</span>
                <small>{{ label(c.failure_type) }}</small>
              </button>
            </li>
          </ul>
          <article class="case-detail" v-if="detail" aria-live="polite">
            <header>
              <div><span class="eyebrow">{{ detail.case.category }}</span><h4>{{ detail.case.id }} · {{ detail.case.title }}</h4></div>
              <span :class="['result', detail.case.passed ? 'ok' : 'bad']">{{ detail.case.passed ? "✓ PASSED" : "✕ FAILED" }}</span>
            </header>
            <dl>
              <dt>User</dt>
              <dd><span v-for="(u, i) in userTurns" :key="i" class="turn">{{ u }}</span></dd>
              <dt>Expected</dt><dd>{{ detail.case.expected_behaviour }}</dd>
              <dt>Actual</dt><dd>{{ actualText }}</dd>
              <template v-if="detail.bad_case">
                <dt>Failure type</dt><dd><b>{{ detail.bad_case.failure_type }}</b><span v-if="detail.bad_case.secondary_failure_types.length" class="hint"> · also {{ detail.bad_case.secondary_failure_types.join(", ") }}</span></dd>
                <dt>Root cause</dt><dd>{{ detail.bad_case.root_cause }}</dd>
                <dt>Failed metrics</dt><dd>{{ detail.bad_case.failed_metrics.join(", ") }}</dd>
                <dt>Evidence</dt><dd><ul class="evidence"><li v-for="(e, i) in detail.bad_case.evidence" :key="i">{{ e }}</li></ul></dd>
                <dt>Final state</dt><dd><code class="state">{{ JSON.stringify(detail.bad_case.final_business_state) }}</code></dd>
              </template>
            </dl>
            <details class="trace">
              <summary>Replay trace ({{ detail.case.trace.length }} steps)</summary>
              <ol>
                <li v-for="t in detail.case.trace" :key="t.step">
                  <b>{{ t.kind }}</b> {{ stepText(t) }}
                  <div v-if="t.response" class="trace-response">{{ t.response }}</div>
                </li>
              </ol>
            </details>
          </article>
          <p v-else class="hint case-detail empty">Select a case to see what the user said, what was expected, and what the system actually persisted.</p>
        </div>
      </section>

      <section class="eval-card" v-if="fixes.length">
        <h3>What was fixed after the baseline analysis</h3>
        <ul class="fix-list">
          <li v-for="(f, i) in fixes" :key="i"><code>{{ f.root_causes.join(" · ") }}</code><p>{{ f.fix }}</p></li>
        </ul>
      </section>

      <section class="eval-card component" v-if="data.component">
        <h3>Component evaluation <small>(separate from product metrics)</small></h3>
        <p class="hint">{{ data.component.scope_note }} Generated {{ data.component.generated_at?.slice(0, 10) }}.</p>
        <div class="tiles secondary-tiles">
          <article v-for="t in componentTiles" :key="t.label" class="tile small">
            <span class="tile-label">{{ t.label }}</span><strong>{{ t.value }}</strong><small>{{ t.detail }}</small>
          </article>
        </div>
      </section>

      <p class="eval-foot">
        <b>Scope.</b> Offline-rules mode is deterministic and needs no API key; live-model runs are reproducible with
        <code>run_product_eval.py --live</code> but were not part of this report unless the mode badge says so.
        No real payments, orders or production traffic. Full-backend Redis / ChromaDB / hybrid RAG is not exercised here.
      </p>
    </template>
  </section>
</template>

<script setup>
import { computed, onMounted, ref, watch } from "vue";
import { requestEval } from "../lib/backends";

const props = defineProps({ settings: { type: Object, required: true } });
const data = ref(null), error = ref(""), run = ref("current"), badRun = ref("baseline");
const caseRows = ref([]), detail = ref(null), filterType = ref("");

const LABELS = {
  AUTHORIZATION: "Authorization", CONFIRMATION_VIOLATION: "Confirmation violation", MISSING_CLARIFICATION: "Missing clarification",
  UNNECESSARY_CLARIFICATION: "Unnecessary clarification", OBJECT_RESOLUTION: "Object resolution", TASK_PLANNING: "Task planning",
  WRONG_ACTION: "Wrong action", STATE_VERIFICATION: "State verification", MEMORY: "Memory", RESPONSE_QUALITY: "Response quality", HARNESS_ERROR: "Harness error",
};
const CATEGORY = {
  single_task: "Single task", disambiguation: "Object disambiguation", multi_intent: "Multi-intent", multi_turn: "Multi-turn context",
  business_rule: "Business rules", confirmation_approval: "Confirmation & approval", idempotency_state: "Idempotency & state",
  authorization: "Authorization & safety", policy_technical: "Policy & technical tools", memory: "Memory",
};
const label = (k) => LABELS[k] || k || "—";
const pct = (m) => (m && m.value != null ? `${(m.value * 100).toFixed(1)}%` : "n/a");
const frac = (m) => (m && m.denominator != null ? `${m.numerator}/${m.denominator}` : "");
const modeLabel = (m) => (m === "live-model" ? "Live model" : "Offline rules (deterministic)");

const cur = computed(() => (run.value === "baseline" && data.value.baseline ? data.value.baseline : data.value.current));
const holdout = computed(() => data.value?.holdout || {});
const memory = computed(() => data.value?.memory || {});
const comparison = computed(() => data.value?.current.baseline_comparison);
const runOptions = computed(() => [
  { key: "current", label: data.value?.baseline ? "After fixes" : "Latest" },
  ...(data.value?.baseline ? [{ key: "baseline", label: "Baseline (first run)" }] : []),
]);
const runNote = computed(() => run.value === "baseline"
  ? "The first evaluation, before any fix."
  : comparison.value ? "Re-run on the same dataset after the targeted fixes." : "");
const badRunOptions = computed(() => [
  ...(data.value?.baseline ? [{ key: "baseline", label: `Baseline · ${data.value.baseline.bad_case_summary.total}` }] : []),
  { key: "current", label: `Current · ${data.value?.current.bad_case_summary.total}` },
  ...(holdout.value.current ? [{ key: "holdout", label: `Holdout · ${holdout.value.current.bad_case_summary.total}` }] : []),
  ...(holdout.value.baseline ? [{ key: "holdout_baseline", label: `Holdout baseline · ${holdout.value.baseline.bad_case_summary.total}` }] : []),
  ...(memory.value.baseline ? [{ key: "memory_baseline", label: `Memory baseline · ${memory.value.baseline.bad_case_summary.total}` }] : []),
]);
const badSummary = computed(() => {
  const d = data.value;
  const map = { current: d.current, baseline: d.baseline, holdout: holdout.value.current, holdout_baseline: holdout.value.baseline,
    memory: memory.value.current, memory_baseline: memory.value.baseline };
  return (map[badRun.value] || d.current).bad_case_summary;
});

function delta(key, isCount) {
  const c = comparison.value?.metrics?.[key];
  if (run.value !== "current" || !c || c.baseline == null || c.current == null) return "";
  if (isCount) return `Baseline ${c.baseline}`;
  return `Baseline ${(c.baseline * 100).toFixed(1)}% · ${c.delta >= 0 ? "+" : ""}${(c.delta * 100).toFixed(1)} pp`;
}
const primaryTiles = computed(() => {
  const m = cur.value.metrics, u = m.unauthorized_operations;
  return [
    { key: "t", label: "Task success rate", value: pct(m.task_success_rate), detail: `${frac(m.task_success_rate)} cases reached the expected persisted state`, delta: delta("task_success_rate") },
    { key: "o", label: "Object resolution accuracy", value: pct(m.object_resolution_accuracy), detail: `${frac(m.object_resolution_accuracy)} · correct object or a correct clarification`, delta: delta("object_resolution_accuracy") },
    { key: "c", label: "Confirmation compliance", value: pct(m.confirmation_compliance), detail: `${frac(m.confirmation_compliance)} high-risk writes preceded by user consent in the audit log`, delta: delta("confirmation_compliance") },
    { key: "u", label: "Unauthorized operations", value: String(u.value), tone: u.value ? "bad" : "ok", detail: `${u.attacks_blocked}/${u.attack_attempts} attack attempts blocked · ${u.unconfirmed_writes} unconfirmed writes`, delta: delta("unauthorized_operations", true) },
  ];
});
const secondaryTiles = computed(() => {
  const m = cur.value.metrics, t = m.avg_turns_to_resolution, cl = m.clarification_accuracy;
  return [
    { key: "m", label: "Multi-intent completion", value: pct(m.multi_intent_completion_rate), detail: frac(m.multi_intent_completion_rate), delta: delta("multi_intent_completion_rate") },
    { key: "cl", label: "Clarification accuracy", value: pct(cl), detail: `${frac(cl)} · ${cl.unnecessary_clarifications} unnecessary · ${cl.missed_clarifications} missed`, delta: delta("clarification_accuracy") },
    { key: "a", label: "Action / tool success", value: pct(m.action_tool_success_rate), detail: frac(m.action_tool_success_rate), delta: delta("action_tool_success_rate") },
    { key: "turns", label: "Avg turns to resolution", value: t.value ?? "n/a", detail: `P50 ${t.p50} · P90 ${t.p90} · ${t.n} successful cases`, delta: delta("avg_turns_to_resolution", true) },
  ];
});
const comparisonRows = computed(() => {
  const keys = [["task_success_rate", "Task success"], ["object_resolution_accuracy", "Object resolution"], ["multi_intent_completion_rate", "Multi-intent"],
    ["clarification_accuracy", "Clarification"], ["action_tool_success_rate", "Action / tool"], ["confirmation_compliance", "Confirmation"]];
  return keys.map(([key, l]) => {
    const c = comparison.value.metrics[key];
    return { key, label: l, base: pct({ value: c.baseline }), now: pct({ value: c.current }),
      baseW: `${(c.baseline || 0) * 100}%`, nowW: `${(c.current || 0) * 100}%` };
  });
});
const categories = computed(() => Object.entries(cur.value.category_breakdown).map(([k, c]) => ({
  key: k, label: CATEGORY[k] || k, passed: c.passed, total: c.total, w: `${c.rate * 100}%` })));
const breakdown = computed(() => Object.entries(badSummary.value.by_failure_type)
  .map(([k, v]) => ({ key: k, label: v.label, count: v.count, share: `${(v.share_of_bad_cases * 100).toFixed(0)}%`,
    w: `${badSummary.value.total ? (v.count / badSummary.value.total) * 100 : 0}%` }))
  .sort((a, b) => b.count - a.count));
const fixes = computed(() => data.value?.fix_log?.fixes || []);
const componentTiles = computed(() => {
  const m = data.value.component.metrics, p = (v) => (v == null ? "n/a" : `${(v * 100).toFixed(1)}%`);
  return [
    { label: "Intent accuracy", value: p(m.intent_accuracy), detail: `${m.intent_case_count} golden samples` },
    { label: "Routing accuracy", value: p(m.routing_accuracy), detail: `${m.routing_case_count} holdout · ${m.routing_eval_mode}` },
    { label: "RAG top-3 hit", value: p(m.rag_top_3_hit_rate), detail: `${m.rag_query_count} queries · ${m.rag_retriever_mode}` },
    { label: "Response quality (judge pass)", value: p(m.eval_pass_rate), detail: `${m.eval_result_count} dialogs` },
  ];
});
const userTurns = computed(() => (detail.value?.case.trace || []).filter((t) => ["user", "select", "confirm", "cancel"].includes(t.kind))
  .map((t) => (t.kind === "user" ? `“${t.text}”` : t.kind === "select" ? `clicks ${t.text || t.skipped}` : `${t.kind} ${t.action || t.skipped || ""}`)));
const actualText = computed(() => {
  const b = detail.value?.bad_case;
  if (!b) return "All checks passed against the persisted state.";
  const acts = b.actual.actions.map((a) => `${a.object}/${a.operation} = ${a.status}${a.amount_minor ? ` (¥${a.amount_minor / 100})` : ""}`);
  return `${acts.length ? acts.join(", ") : "No business action persisted"}. Routes: ${b.actual.routes.join(" → ") || "—"}. Last reply: ${b.actual.last_response || "—"}`;
});
function stepText(t) {
  if (t.kind === "user") return `“${t.text}” → ${t.status}${t.candidates?.length ? ` · candidates ${t.candidates.join(", ")}` : ""}${t.operations?.length ? ` · ${t.operations.map((o) => `${o.object}/${o.operation}=${o.status}`).join(", ")}` : ""}`;
  if (t.skipped) return `skipped: ${t.skipped}`;
  if (t.attack) return `${t.attack.attack} · HTTP ${JSON.stringify(t.attack.http)} · ${t.attack.succeeded ? "SUCCEEDED" : "blocked"}`;
  if (t.action) return `${t.decision || ""} ${t.action} · HTTP ${JSON.stringify(t.http)}`;
  if (t.system) return t.system;
  return t.text ? `${t.text} → ${t.status}` : "";
}

async function loadCases() {
  detail.value = null;
  const q = new URLSearchParams({ run: badRun.value, status: "failed" });
  if (filterType.value) q.set("failure_type", filterType.value);
  try {
    caseRows.value = (await requestEval(props.settings, `/eval/product/cases?${q}`)).cases;
  } catch (e) {
    caseRows.value = [];
  }
}
async function openCase(id) {
  detail.value = await requestEval(props.settings, `/eval/product/cases/${encodeURIComponent(id)}?run=${badRun.value}`);
}
function selectBadRun(key) {
  badRun.value = key;
  filterType.value = "";
}
watch([badRun, filterType], loadCases);
onMounted(async () => {
  try {
    data.value = await requestEval(props.settings, "/eval/product/latest");
    if (!data.value.baseline) badRun.value = "current";
    await loadCases();
  } catch (e) {
    error.value = `Evaluation report unavailable (${e.message}). Generate it with: cd ResolveFlow && PYTHONPATH=. python scripts/run_product_eval.py`;
  }
});
</script>
