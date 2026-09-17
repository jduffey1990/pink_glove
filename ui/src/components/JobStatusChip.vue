<script setup lang="ts">
  /** A job's status, coloured consistently wherever it appears. */
  import type { JobStatus } from '@/api/types'
  import { computed } from 'vue'
  import { JOB_STATUS_LOOK } from '@/lib/jobStatus'

  const props = defineProps<{ status: JobStatus, size?: string }>()

  const look = computed(() => JOB_STATUS_LOOK[props.status])
</script>

<template>
  <v-chip
    :color="look.color"
    :prepend-icon="look.icon"
    :size="size ?? 'small'"
    variant="tonal"
  >
    {{ look.label }}
  </v-chip>
</template>
