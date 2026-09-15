<script setup lang="ts">
  /** A job's status, coloured consistently wherever it appears. */
  import type { JobStatus } from '@/api/types'
  import { computed } from 'vue'

  const props = defineProps<{ status: JobStatus, size?: string }>()

  const LOOK: Record<JobStatus, { label: string, color: string, icon: string }> = {
    scheduled: { label: 'Scheduled', color: 'grey', icon: 'mdi-calendar-blank-outline' },
    en_route: { label: 'En route', color: 'info', icon: 'mdi-car' },
    in_progress: { label: 'In progress', color: 'primary', icon: 'mdi-progress-wrench' },
    complete: { label: 'Complete', color: 'success', icon: 'mdi-check-circle-outline' },
    cancelled: { label: 'Cancelled', color: 'error', icon: 'mdi-close-circle-outline' },
    // Not a cancellation and not a completion: the crew turned up and could
    // not get in. It bills differently from both.
    no_access: { label: 'No access', color: 'warning', icon: 'mdi-door-closed-lock' },
  }

  const look = computed(() => LOOK[props.status])
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
