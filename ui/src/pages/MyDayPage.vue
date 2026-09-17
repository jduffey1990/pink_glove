<script setup lang="ts">
  /**
   * The cleaner's screen. Designed at 400px first; the desktop view is the
   * same list with more room.
   *
   * Everything a person standing in a driveway needs is on this one screen:
   * where to go, whether they are clocked in, and the three outcomes a visit
   * can have. Anything that needs a second tap is a tap taken in the rain.
   */
  import type { Job } from '@/api/types'
  import { computed, ref, watch } from 'vue'
  import { useRouter } from 'vue-router'
  import { clockIn, clockOut, listJobs, setJobStatus } from '@/api/endpoints'
  import { errorDetail } from '@/api/errors'
  import JobStatusChip from '@/components/JobStatusChip.vue'
  import RevealCodesDialog from '@/components/RevealCodesDialog.vue'
  import { addDays, formatDayLabel, formatTime, todayIn } from '@/lib/datetime'
  import { canMoveTo } from '@/lib/jobStatus'
  import { statusOf, useSessionStore } from '@/stores/session'

  const session = useSessionStore()
  const router = useRouter()

  const day = ref(todayIn(session.timeZone))
  const jobs = ref<Job[]>([])
  const loading = ref(false)
  const error = ref('')
  const busyId = ref<string | null>(null)
  const message = ref('')

  const revealOpen = ref(false)
  const revealJob = ref<Job | null>(null)

  const reasonDialog = ref(false)
  const reasonFor = ref<Job | null>(null)
  const reason = ref('')

  async function load () {
    loading.value = true
    error.value = ''

    try {
      const page = await listJobs({
        mine: true,
        date_from: day.value,
        date_to: day.value,
        ordering: 'scheduled_start',
        limit: 100,
      })
      jobs.value = page.results
    } catch {
      error.value = 'Could not load your day.'
    } finally {
      loading.value = false
    }
  }

  watch([day, () => session.timeZone], load, { immediate: true })

  watch(() => session.timeZone, zone => {
    day.value = todayIn(zone)
  })

  async function act (job: Job, action: () => Promise<Job>) {
    busyId.value = job.id
    message.value = ''

    try {
      const updated = await action()
      jobs.value = jobs.value.map(existing => (existing.id === updated.id ? updated : existing))
    } catch (error_) {
      message.value = statusOf(error_) === 409
        ? (errorDetail(error_) ?? 'That is not possible right now.')
        : 'Something went wrong. Try again.'
    } finally {
      busyId.value = null
    }
  }

  function openReveal (job: Job) {
    revealJob.value = job
    revealOpen.value = true
  }

  function askReason (job: Job) {
    reasonFor.value = job
    reason.value = ''
    reasonDialog.value = true
  }

  async function confirmNoAccess () {
    const job = reasonFor.value
    if (!job) {
      return
    }
    reasonDialog.value = false
    await act(job, () => setJobStatus(job.id, 'no_access', reason.value.trim()))
  }

  const isToday = computed(() => day.value === todayIn(session.timeZone))
</script>

<template>
  <v-container max-width="640">
    <div class="d-flex align-center ga-1 mb-3">
      <v-btn icon="mdi-chevron-left" size="small" variant="text" @click="day = addDays(day, -1)" />

      <div class="flex-grow-1 text-center">
        <div class="text-subtitle-1 font-weight-medium">
          {{ isToday ? 'Today' : formatDayLabel(day) }}
        </div>

        <div class="text-caption text-medium-emphasis">
          {{ formatDayLabel(day) }}
        </div>
      </div>

      <v-btn icon="mdi-chevron-right" size="small" variant="text" @click="day = addDays(day, 1)" />
    </div>

    <v-alert v-if="error" class="mb-3" type="error" variant="tonal">{{ error }}</v-alert>

    <v-alert
      v-if="message"
      class="mb-3"
      closable
      density="compact"
      type="warning"
      variant="tonal"
      @click:close="message = ''"
    >
      {{ message }}
    </v-alert>

    <v-progress-linear v-if="loading" class="mb-2" indeterminate />

    <v-card
      v-if="!loading && jobs.length === 0"
      border
      class="pa-6 text-center text-medium-emphasis"
      flat
    >
      <v-icon class="mb-2" icon="mdi-coffee-outline" size="32" />
      <div>Nothing scheduled.</div>
    </v-card>

    <v-card
      v-for="job in jobs"
      :key="job.id"
      border
      class="mb-3"
      flat
    >
      <v-card-item>
        <template #prepend>
          <div class="text-h6 font-weight-medium">
            {{ formatTime(job.scheduled_start, session.timeZone) }}
          </div>
        </template>

        <v-card-title class="text-body-1">
          {{ job.customer_detail.display_name }}
        </v-card-title>

        <v-card-subtitle class="text-wrap">
          {{ job.location_detail.one_line_address }}
        </v-card-subtitle>
      </v-card-item>

      <v-card-text class="pt-0">
        <JobStatusChip :status="job.status" />

        <v-chip
          v-if="job.location_detail.has_pets"
          class="ml-1"
          prepend-icon="mdi-paw"
          size="small"
          variant="tonal"
        >
          Pets
        </v-chip>

        <p v-if="job.location_detail.access_notes" class="text-body-2 mt-2">
          {{ job.location_detail.access_notes }}
        </p>

        <p v-if="job.notes" class="text-body-2 text-medium-emphasis mt-1">
          {{ job.notes }}
        </p>
      </v-card-text>

      <v-card-actions class="flex-wrap ga-1 px-4 pb-4">
        <!-- Full-width primary action: this is a thumb on a phone. -->
        <v-btn
          v-if="!job.open_time_entry"
          block
          color="primary"
          :disabled="job.is_terminal"
          :loading="busyId === job.id"
          prepend-icon="mdi-clock-start"
          variant="flat"
          @click="act(job, () => clockIn(job.id))"
        >
          Clock in
        </v-btn>

        <v-btn
          v-else
          block
          :loading="busyId === job.id"
          prepend-icon="mdi-clock-end"
          variant="tonal"
          @click="act(job, () => clockOut(job.id))"
        >
          Clock out
        </v-btn>

        <div class="d-flex ga-1 w-100 mt-1">
          <v-btn
            v-if="canMoveTo(job, 'en_route')"
            class="flex-grow-1"
            :loading="busyId === job.id"
            size="small"
            variant="outlined"
            @click="act(job, () => setJobStatus(job.id, 'en_route'))"
          >
            En route
          </v-btn>

          <v-btn
            v-if="canMoveTo(job, 'complete')"
            class="flex-grow-1"
            color="success"
            :loading="busyId === job.id"
            size="small"
            variant="outlined"
            @click="act(job, () => setJobStatus(job.id, 'complete'))"
          >
            Complete
          </v-btn>

          <v-btn
            class="flex-grow-1"
            color="warning"
            :disabled="!canMoveTo(job, 'no_access')"
            size="small"
            variant="outlined"
            @click="askReason(job)"
          >
            No access
          </v-btn>
        </div>

        <div class="d-flex ga-1 w-100 mt-1">
          <v-btn
            v-if="job.location_detail.has_access_codes"
            class="flex-grow-1"
            prepend-icon="mdi-shield-key-outline"
            size="small"
            variant="text"
            @click="openReveal(job)"
          >
            Codes
          </v-btn>

          <v-btn
            class="flex-grow-1"
            size="small"
            variant="text"
            @click="router.push({ name: 'job', params: { id: job.id } })"
          >
            Details
          </v-btn>
        </div>
      </v-card-actions>
    </v-card>

    <RevealCodesDialog
      v-if="revealJob"
      v-model="revealOpen"
      :job-id="revealJob.id"
      :location-id="revealJob.location"
    />

    <v-dialog v-model="reasonDialog" max-width="400">
      <v-card>
        <v-card-title class="text-subtitle-1">Why no access?</v-card-title>

        <v-card-text>
          <!-- "We could not get in" is a distinct outcome from a cancellation
               and bills differently, so the detail is required. -->
          <v-text-field
            v-model="reason"
            autofocus
            label="What happened"
            placeholder="Gate code refused"
          />
        </v-card-text>

        <v-card-actions>
          <v-spacer />
          <v-btn text="Cancel" @click="reasonDialog = false" />

          <v-btn
            color="warning"
            :disabled="!reason.trim()"
            text="Confirm"
            variant="flat"
            @click="confirmNoAccess"
          />
        </v-card-actions>
      </v-card>
    </v-dialog>
  </v-container>
</template>
