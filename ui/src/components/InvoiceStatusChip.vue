<script setup lang="ts">
  /** An invoice's state, coloured consistently wherever it appears. */
  import type { Invoice } from '@/api/types'
  import { computed } from 'vue'
  import { invoiceChip } from '@/lib/invoiceStatus'

  const props = defineProps<{ invoice: Invoice, size?: string }>()

  const look = computed(() => invoiceChip(props.invoice))
</script>

<template>
  <v-chip :color="look.color" :size="size ?? 'small'" variant="tonal">
    {{ look.label }}
  </v-chip>
</template>
