<template>
  <div>
    <h1>{{ props.layer }} 层 · 预演不动余量</h1>
    <span v-for="x in rows" :key="x.id" class="lot">{{ x.name }} ×{{ x.qty_remain }} · {{ x.expiry }}</span>
  </div>
</template>
<script setup>
import { ref, watch, onMounted, onUnmounted } from 'vue'
import { api } from '../api'
import { bus } from '../bus'
const props = defineProps({ layer: String })
const rows = ref([])
async function load() { rows.value = await api('/fridge?layer=' + props.layer) }
watch(() => props.layer, load)
onMounted(load)
// Preview never moves this page; a landed confirm or sweep refreshes it.
const off = bus.on((e) => { if (e.type === 'shelf-changed') load() })
onUnmounted(off)
</script>
