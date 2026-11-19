<script setup lang="ts">
  /**
   * Where a customer lands (ADR-039).
   *
   * Only what the API already serves a customer: their own visits, from the
   * jobs list, which the server scopes to `customer__user` and trims to the
   * customer's shape (no crew, no office notes). Invoices are not served to
   * customers; each one reaches them by email with its own pay link, and the
   * page says so rather than linking to a list it cannot read.
   *
   * Eight weeks ahead, the horizon recurring plans are booked to, inside the
   * jobs filter's range cap. Dates are the organization's, never the browser's.
   */
  import type { Job } from '@/api/types'
  import { computed, ref, watch } from 'vue'
  import { listJobs } from '@/api/endpoints'
  import JobStatusChip from '@/components/JobStatusChip.vue'
  import { addDays, formatDayLabel, formatTime, todayIn, toIsoDate } from '@/lib/datetime'
  import { useSessionStore } from '@/stores/session'

  /** How far ahead the page looks, in days after today. */
  const HORIZON_DAYS = 55

  const session = useSessionStore()

  const visits = ref<Job[]>([])
  const loading = ref(false)
  const error = ref('')

  const greeting = computed(() => {
    const name = session.user?.first_name?.trim()
    return name ? `Hello, ${name}` : 'Hello'
  })

  async function load () {
    loading.value = true
    error.value = ''
    const today = todayIn(session.timeZone)

    try {
      const page = await listJobs({
        date_from: today,
        date_to: addDays(today, HORIZON_DAYS),
        ordering: 'scheduled_start',
        limit: 100,
      })
      visits.value = page.results
    } catch {
      error.value = 'Could not load your visits.'
    } finally {
      loading.value = false
    }
  }

  watch(() => session.timeZone, load, { immediate: true })

  function day (job: Job): string {
    return formatDayLabel(toIsoDate(job.scheduled_start, session.timeZone))
  }

  function hours (job: Job): string {
    const start = formatTime(job.scheduled_start, session.timeZone)
    const end = formatTime(job.scheduled_end, session.timeZone)
    return `${start} to ${end}`
  }
</script>

<template>
  <v-container max-width="640">
    <h1 class="text-h5 mb-1">{{ greeting }}</h1>

    <p class="text-body-2 text-medium-emphasis mb-6">
      Your visits with {{ session.organization?.name ?? 'us' }} over the next eight weeks.
    </p>

    <v-alert v-if="error" class="mb-4" type="error" variant="tonal">{{ error }}</v-alert>

    <v-progress-linear v-if="loading" class="mb-4" indeterminate />

    <v-card
      v-if="!loading && !error && visits.length === 0"
      border
      class="pa-6 mb-4 text-center text-medium-emphasis"
      flat
    >
      <v-icon class="mb-2" icon="mdi-calendar-blank-outline" size="32" />
      <div>No visits booked in the next eight weeks.</div>
    </v-card>

    <v-card
      v-for="job in visits"
      :key="job.id"
      border
      class="mb-3"
      flat
    >
      <!-- The chip shares the day's line, so the lines below get the card's
           whole width: at phone width a chip column squeezed them to wrapping. -->
      <div class="pa-4">
        <div class="d-flex align-center justify-space-between ga-3 mb-1">
          <div class="text-subtitle-1 font-weight-medium">{{ day(job) }}</div>
          <JobStatusChip class="flex-shrink-0" :status="job.status" />
        </div>

        <div class="text-body-2">
          {{ hours(job) }} · {{ job.service_detail.name }}
        </div>

        <div
          v-if="job.location_detail.one_line_address"
          class="text-body-2 text-medium-emphasis"
        >
          {{ job.location_detail.one_line_address }}
        </div>
      </div>
    </v-card>

    <v-card border class="mt-6" flat>
      <div class="d-flex align-start ga-3 pa-4">
        <v-icon class="flex-shrink-0" icon="mdi-receipt-text-outline" />

        <div>
          <div class="text-subtitle-1 font-weight-medium">Invoices</div>

          <div class="text-body-2 text-medium-emphasis">
            Each invoice comes to you by email, with a link to view and pay it.
          </div>
        </div>
      </div>
    </v-card>
  </v-container>
</template>
