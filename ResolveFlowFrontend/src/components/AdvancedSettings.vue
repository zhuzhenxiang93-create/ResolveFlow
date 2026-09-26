<template>
  <section class="advanced-body">
    <h3>Connection & identity</h3>
    <label
      >API endpoint<input
        v-model="settings.endpoint"
        @change="saveSettings(settings)"
    /></label>
    <label v-for="role in ['user', 'reviewer', 'admin']" :key="role"
      >{{ role }} token<input
        type="password"
        v-model="settings[role + 'Token']"
        @change="saveSettings(settings)"
        autocomplete="off"
    /></label>
    <div class="actions">
      <button @click="run(() => requestMonitor(settings))">Monitor</button
      ><button
        @click="
          run(() => commerceRequest(settings, '/demo', { method: 'POST' }))
        "
      >
        Seed demo purchases
      </button>
    </div>
    <h3>Knowledge tools</h3>
    <label>Search<input v-model="query" /></label
    ><button @click="run(() => requestSearch(settings, query))">
      Search knowledge
    </button>
    <label>Document ID<input v-model="doc.id" /></label
    ><label>Title<input v-model="doc.title" /></label>
    <label
      >Domain<select v-model="doc.domain">
        <option>goods</option>
        <option>subscription</option>
        <option>general</option>
      </select></label
    >
    <label>Version<input v-model="doc.version" /></label
    ><label
      >Effective date<input type="date" v-model="doc.effective_at"
    /></label>
    <label>Content<textarea v-model="doc.content" /></label>
    <button @click="run(() => addKnowledge(settings, [doc]))">
      Save knowledge (admin)
    </button>
    <label
      >Upload knowledge<input
        type="file"
        accept=".txt,.md,.json"
        @change="upload"
    /></label>
    <p class="hint">
      Knowledge administration and monitoring require the production API and an
      admin identity.
    </p>
    <pre v-if="output">{{ output }}</pre>
  </section>
</template>
<script setup>
import { ref, reactive } from "vue";
import {
  saveSettings,
  requestMonitor,
  requestSearch,
  addKnowledge,
  uploadKnowledge,
  commerceRequest,
} from "../lib/backends";
const props = defineProps({ settings: Object });
const query = ref("退款政策"),
  output = ref("");
const doc = reactive({
  id: "",
  title: "",
  domain: "goods",
  version: "v1",
  effective_at: new Date().toISOString().slice(0, 10),
  content: "",
  source: "Demo administrator",
});
async function run(fn) {
  try {
    output.value = JSON.stringify(await fn(), null, 2);
  } catch (e) {
    output.value = e.message;
  }
}
function upload(e) {
  const file = e.target.files?.[0];
  if (file) run(() => uploadKnowledge(props.settings, file));
}
</script>
