<template>
  <section class="commerce-panel tool-panel">
    <div class="panel-heading"><h2>我的购买与售后</h2><span class="pill soft">模拟业务 · 金额 CNY</span></div>
    <div class="actions">
      <button @click="seed" :disabled="busy">初始化演示购买记录</button>
      <button @click="refresh" :disabled="busy">刷新购买和售后</button>
    </div>
    <p class="hint">确认只针对卡片上的具体操作。退款还需独立审核；模拟处理完成不代表真实到账。</p>
    <p role="status" v-if="error">{{ error }}</p>
    <div class="purchase-grid">
      <article v-for="o in objects" :key="o.id" class="purchase-card">
        <strong>{{ o.title }}</strong><span class="pill soft">{{ o.domain === 'goods' ? '商品' : '订阅' }}</span>
        <p>{{ o.id }} · {{ o.status }}</p>
        <p v-if="o.domain === 'subscription'">续费：{{ o.auto_renew ? '开启' : '关闭' }} · 权益：{{ o.entitlement }}</p>
        <p v-for="p in o.payments" :key="p.id">账单 {{ p.id }} · {{ money(p.amount_minor) }} · {{ p.kind }}</p>
        <details><summary>商品、物流、发票和退款明细</summary><pre>{{ JSON.stringify({ items:o.items, shipment:o.shipment, invoice:o.invoice, refunds:o.refunds }, null, 2) }}</pre></details>
        <div class="actions">
          <button @click="$emit('ask', `查询 ${o.id}`)">查询</button>
          <button v-if="o.status !== 'unpaid'" @click="$emit('ask', `请帮我申请退款 ${o.id}`)">申请退款</button>
          <button v-if="o.status === 'unpaid'" @click="$emit('ask', `请取消订单 ${o.id}`)">取消订单</button>
          <button v-if="o.domain === 'subscription'" @click="$emit('ask', `请关闭自动续费 ${o.id}`)">关闭续费</button>
        </div>
      </article>
    </div>
    <div v-if="candidates.length" class="selection-box"><h3>请选择本次业务对象</h3>
      <button v-for="c in candidates" :key="c.id" @click="$emit('select', c.id)">{{ c.title }} · {{ c.id }}</button>
    </div>
    <article v-for="c in cases" :key="c.id" class="case-card">
      <h3>售后任务 · {{ c.id }}</h3>
      <div v-for="a in c.operations" :key="a.id" class="operation-card">
        <strong>{{ a.quote.title }} · {{ a.quote.label }}</strong>
        <p>{{ a.quote.object_id }} <span v-if="a.quote.item_id">/ {{ a.quote.item_id }}</span></p>
        <p v-if="a.quote.amount_minor">退款金额 {{ money(a.quote.amount_minor) }} · 账单 {{ a.quote.payment_id }}</p>
        <p>{{ a.quote.effect }}</p><p>{{ states[a.status] || a.status }} {{ a.reason || a.quote.reason || '' }}</p>
        <div class="actions">
          <button v-if="a.status === 'awaiting_confirmation'" @click="act(c,a,'confirm')" :disabled="busy">确认此操作</button>
          <button v-if="['awaiting_confirmation','awaiting_approval'].includes(a.status)" @click="act(c,a,'cancel')" :disabled="busy">取消此操作</button>
          <template v-if="settings.reviewerToken">
            <button v-if="a.status === 'awaiting_approval'" @click="act(c,a,'approve',true)" :disabled="busy">审核员批准</button>
            <button v-if="a.status === 'awaiting_approval'" @click="act(c,a,'reject',true)" :disabled="busy">审核员拒绝</button>
            <button v-if="a.status === 'awaiting_return'" @click="act(c,a,'receive_return',true)" :disabled="busy">模拟退货验收</button>
            <button v-if="a.status === 'refund_processing'" @click="act(c,a,'settle',true)" :disabled="busy">模拟支付成功回执</button>
          </template>
        </div>
      </div>
    </article>
    <div class="memory-box"><h3>我的记忆偏好</h3>
      <p class="hint">用于回答风格与上下文；不影响退款资格。删除记忆会清除历史摘要，业务记录和审计保留。</p>
      <pre>{{ profile }}</pre>
      <p class="hint" v-if="memoryStatus === 'pending'">偏好正在更新，可稍后刷新查看。</p>
      <p class="hint" v-if="memoryStatus === 'failed'">偏好更新暂时失败，业务结果不受影响；可直接选择回答风格。</p>
      <div class="actions"><button @click="setStyle('concise')">以后简洁回答</button><button @click="setStyle('detailed')">以后详细回答</button><button @click="forget">删除我的记忆</button></div>
    </div>
  </section>
</template>
<script setup>
import { ref, watch, onMounted } from 'vue'
import { commerceRequest } from '../lib/backends'
const props = defineProps({ settings:Object, latest:Object, candidates:{ type:Array, default:()=>[] } })
const emit = defineEmits(['ask','select','updated'])
const objects=ref([]), cases=ref([]), profile=ref({}), error=ref(''), busy=ref(false)
const memoryStatus=ref('idle')
const states={ awaiting_confirmation:'待用户确认', awaiting_approval:'待独立审核', awaiting_return:'待退货验收', refund_processing:'模拟退款处理中', completed:'已完成（模拟）', rejected:'已拒绝', cancelled:'已取消', stale:'数据已变更，请重新申请', ineligible:'不符合演示规则' }
const money=n=>`CNY ${(n/100).toFixed(2)}`
let generation=0
async function refresh(){
 const current=++generation
 if(!props.settings.userToken){objects.value=[];cases.value=[];profile.value={};return}
 try{
  const [o,c,m]=await Promise.all([commerceRequest(props.settings,'/objects'),commerceRequest(props.settings,`/cases${props.settings.conversationId?'?conversation='+encodeURIComponent(props.settings.conversationId):''}`),commerceRequest(props.settings,'/memory')])
  if(current!==generation)return
  objects.value=o.objects;cases.value=c.cases;profile.value=m.profile;memoryStatus.value=m.update_status?.state || 'idle';error.value=''
 }catch(e){if(current===generation)error.value=e.message}
}
async function seed(){busy.value=true;try{await commerceRequest(props.settings,'/demo',{method:'POST'});await refresh()}catch(e){error.value=e.message}finally{busy.value=false}}
async function act(c,a,decision,reviewer=false){busy.value=true;try{const result=await commerceRequest(props.settings,reviewer?`/review/${c.id}`:`/cases/${c.id}/decision`,{method:'POST',body:{action_id:a.id,decision},reviewer});emit('updated',result);await refresh()}catch(e){error.value=e.message}finally{busy.value=false}}
async function setStyle(response_style){try{await commerceRequest(props.settings,'/memory',{method:'PUT',body:{response_style}});await refresh()}catch(e){error.value=e.message}}
async function forget(){try{await commerceRequest(props.settings,'/memory',{method:'DELETE'});await refresh()}catch(e){error.value=e.message}}
watch(()=>[props.settings.userToken,props.settings.conversationId],()=>{objects.value=[];cases.value=[];profile.value={};refresh()})
watch(()=>props.latest,refresh)
onMounted(refresh)
</script>
<style scoped>
.commerce-panel{margin:18px 0}.purchase-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(255px,1fr));gap:12px}.purchase-card,.case-card,.memory-box{border:1px solid #d7dfeb;border-radius:12px;padding:14px;margin-top:12px;min-width:0}.purchase-card p{font-size:12px;overflow-wrap:anywhere}.operation-card{padding:12px 0;border-top:1px solid #dde3ec}.operation-card p{overflow-wrap:anywhere}.selection-box button{margin:5px}pre{max-height:240px;overflow:auto;white-space:pre-wrap}summary{cursor:pointer}
</style>
