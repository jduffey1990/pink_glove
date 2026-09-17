<script setup lang="ts">
  /**
   * The dispatcher's week.
   *
   * Monday-first, and every date is reckoned in the organization's timezone,
   * not the browser's. `date_from`/`date_to` go to the API as local dates and
   * the backend converts -- so a job at 23:30 on the 14th in Denver appears
   * under the 14th even though it is the 15th in UTC.
   */
  import type { Job, JobStatus } from '@/api/types'
  import { computed, ref, watch } from 'vue'
  import { useRouter } from 'vue-router'
  import { listAssignableStaff, listJobs } from '@/api/endpoints'
  import JobStatusChip from '@/components/JobStatusChip.vue'
  import {
    addDays,
    formatDayLabel,
    formatTime,
    todayIn,
    toIsoDate,
    weekOf,
  } from '@/lib/datetime'
  import { JOB_STATUS_OPTIONS } from '@/lib/jobStatus'
  import { formatCents } from '@/lib/money'
  import { useSessionStore } from '@/stores/session'

  const session = useSessionStore()
  const router = useRouter()

  const anchor = ref(todayIn(session.timeZone))
  const jobs = ref<Job[]>([])
  const loading = ref(false)
  const error = ref('')

  const statusFilter = ref<JobStatus[]>([])
  const assigneeFilter = ref<string | null>(null)
  const staff = ref<{ id: string, label: string }[]>([])

  const week = computed(() => weekOf(anchor.value))
  const rangeLabel = computed(
    () => `${formatDayLabel(week.value[0])} – ${formatDayLabel(week.value[6])}`,
  )

  /** Jobs bucketed by their organization-local date. */
  const byDay = computed(() => {
    const buckets = new Map<string, Job[]>(week.value.map(day => [day, []]))

    for (const job of jobs.value) {
      // Bucket on the date the API filtered by, which is the local one.
      const day = toIsoDate(job.scheduled_start, session.timeZone)
      buckets.get(day)?.push(job)
    }

    for (const list of buckets.values()) {
      list.sort((a, b) => a.scheduled_start.localeCompare(b.scheduled_start))
    }

    return buckets
  })

  async function load () {
    loading.value = true
    error.value = ''

    try {
      const page = await listJobs({
        date_from: week.value[0],
        date_to: week.value[6],
        ...(statusFilter.value.length > 0 ? { status: statusFilter.value } : {}),
        ...(assigneeFilter.value ? { assignee: assigneeFilter.value } : {}),
        limit: 500,
      })
      jobs.value = page.results
    } catch {
      error.value = 'Could not load the schedule.'
    } finally {
      loading.value = false
    }
  }

  watch(
    [anchor, statusFilter, assigneeFilter, () => session.timeZone],
    load,
    { immediate: true, deep: true },
  )

  watch(() => session.timeZone, zone => {
    anchor.value = todayIn(zone)
  })

  listAssignableStaff()
    .then(people => {
      staff.value = people
    })
    .catch(() => {})

  function shiftWeek (weeks: number) {
    anchor.value = addDays(anchor.value, weeks * 7)
  }

  function goToToday () {
    anchor.value = todayIn(session.timeZone)
  }

  const isToday = (day: string) => day === todayIn(session.timeZone)
</script>

<template>
  <v-container fluid>
    <div class="d-flex flex-wrap align-center ga-2 mb-4">
      <h1 class="text-h6 mr-2">Schedule</h1>

      <v-btn icon="mdi-chevron-left" size="small" variant="text" @click="shiftWeek(-1)" />
      <v-btn size="small" variant="text" @click="goToToday">Today</v-btn>
      <v-btn icon="mdi-chevron-right" size="small" variant="text" @click="shiftWeek(1)" />

      <span class="text-body-2 text-medium-emphasis">{{ rangeLabel }}</span>

      <v-spacer />

      <v-chip v-if="session.organization" size="small" variant="tonal">
        {{ session.timeZone }}
      </v-chip>
    </div>

    <div class="d-flex flex-wrap ga-2 mb-4">
      <v-chip-group v-model="statusFilter" column multiple>
        <v-chip
          v-for="status in JOB_STATUS_OPTIONS"
          :key="status.value"
          filter
          size="small"
          :text="status.label"
          :value="status.value"
          variant="outlined"
        />
      </v-chip-group>

      <v-select
        v-model="assigneeFilter"
        class="assignee-filter"
        clearable
        density="compact"
        hide-details
        item-title="label"
        item-value="id"
        :items="staff"
        label="Assignee"
        variant="outlined"
      />
    </div>

    <v-alert v-if="error" class="mb-4" type="error" variant="tonal">{{ error }}</v-alert>

    <v-progress-linear v-if="loading" class="mb-2" indeterminate />

    <div class="week-grid">
      <v-card
        v-for="day in week"
        :key="day"
        border
        class="pa-2"
        :color="isToday(day) ? 'primary' : undefined"
        flat
        :variant="isToday(day) ? 'tonal' : 'flat'"
      >
        <div class="text-caption font-weight-medium mb-2">
          {{ formatDayLabel(day) }}
        </div>

        <p
          v-if="byDay.get(day)?.length === 0"
          class="text-caption text-disabled"
        >
          —
        </p>

        <v-card
          v-for="job in byDay.get(day)"
          :key="job.id"
          border
          class="mb-2 pa-2"
          flat
          @click="router.push({ name: 'job', params: { id: job.id } })"
        >
          <div class="text-caption font-weight-medium">
            {{ formatTime(job.scheduled_start, session.timeZone) }}
          </div>

          <div class="text-body-2 text-truncate">
            {{ job.customer_detail.display_name }}
          </div>

          <div class="text-caption text-medium-emphasis text-truncate mb-1">
            {{ job.location_detail.label || job.location_detail.one_line_address }}
          </div>

          <JobStatusChip :status="job.status" />

          <div class="text-caption text-medium-emphasis mt-1">
            {{ formatCents(job.price_cents) }}
            <span v-if="job.assignments.length > 0">
              · {{ job.assignments.length }} crew
            </span>
          </div>
        </v-card>
      </v-card>
    </div>
  </v-container>
</template>

<style scoped>
.assignee-filter {
  max-width: 260px;
}

.week-grid {
  display: grid;
  gap: 8px;
  grid-template-columns: repeat(7, minmax(0, 1fr));
}

/* Below a tablet the seven columns stop being readable; stack them. */
@media (max-width: 960px) {
  .week-grid {
    grid-template-columns: 1fr;
  }
}
</style>
