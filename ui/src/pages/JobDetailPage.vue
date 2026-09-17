<script setup lang="ts">
  /**
   * One visit, from both sides: the dispatcher who scheduled it and the
   * cleaner standing at the door.
   *
   * The status buttons are NOT driven by a copy of the state machine. The
   * server owns it: each job arrives with `next_statuses`, the moves this
   * caller may make, and a refused transition answers 409 with the list as it
   * now stands. Both are rendered as given. A client-side copy would drift the
   * first time the backend's rules changed.
   */
  import type { Job, JobNote, JobPhoto, JobStatus, TransitionConflict } from '@/api/types'
  import { computed, ref, watch } from 'vue'
  import { useRoute } from 'vue-router'
  import {
    assignJob,
    clockIn,
    clockOut,
    createJobNote,
    deleteJobNote,
    getJob,
    listAssignableStaff,
    listJobNotes,
    listJobPhotos,
    setJobStatus,
    unassignJob,
    uploadJobPhoto,
  } from '@/api/endpoints'
  import { bodyOf, errorDetail } from '@/api/errors'
  import JobStatusChip from '@/components/JobStatusChip.vue'
  import RevealCodesDialog from '@/components/RevealCodesDialog.vue'
  import { formatDateTime, formatDuration } from '@/lib/datetime'
  import { statusLabel } from '@/lib/jobStatus'
  import { formatCents } from '@/lib/money'
  import { statusOf, useSessionStore } from '@/stores/session'

  const route = useRoute()
  const session = useSessionStore()

  const jobId = computed(() => route.params.id as string)

  const job = ref<Job | null>(null)
  const notes = ref<JobNote[]>([])
  const photos = ref<JobPhoto[]>([])
  const loading = ref(true)
  const error = ref('')
  const busy = ref(false)

  /** Populated from a 409 body, so the buttons match what the server allows. */
  const allowedStatuses = ref<JobStatus[] | null>(null)
  const conflictMessage = ref('')

  const revealOpen = ref(false)
  const newNote = ref('')
  const reasonDialog = ref(false)
  const pendingStatus = ref<JobStatus | null>(null)
  const reason = ref('')

  const staff = ref<{ id: string, label: string }[]>([])
  const assigneeToAdd = ref<string | null>(null)

  /** The server's list: from the job itself, or from a 409 if that is newer. */
  const nextStatuses = computed<JobStatus[]>(
    () => allowedStatuses.value ?? job.value?.next_statuses.map(next => next.status) ?? [],
  )

  const isAssignedToMe = computed(
    () => job.value?.assignments.some(a => a.user === session.user?.id) ?? false,
  )
  const canWork = computed(() => session.isDispatcherOrHigher || isAssignedToMe.value)
  const openEntry = computed(() => job.value?.open_time_entry ?? null)

  async function load () {
    loading.value = true
    error.value = ''
    allowedStatuses.value = null

    try {
      job.value = await getJob(jobId.value)

      if (session.isStaff) {
        const [notePage, photoPage] = await Promise.all([
          listJobNotes(jobId.value).catch(() => ({ results: [] as JobNote[] })),
          listJobPhotos(jobId.value).catch(() => ({ results: [] as JobPhoto[] })),
        ])
        notes.value = notePage.results
        photos.value = photoPage.results
      }
    } catch (error_) {
      error.value = statusOf(error_) === 404
        // 404 here means "not yours", not "gone" -- the API answers 404 for a
        // cross-tenant read by design.
        ? 'That job does not exist, or it belongs to another organization.'
        : 'Could not load this job.'
    } finally {
      loading.value = false
    }
  }

  watch(jobId, load, { immediate: true })

  if (session.isDispatcherOrHigher) {
    listAssignableStaff()
      .then(people => {
        staff.value = people
      })
      .catch(() => {})
  }

  function requestStatus (status: JobStatus) {
    // Which moves need a reason is the server's rule too. After a 409 the job
    // may be stale, so an unknown move is sent bare and the 400 says so.
    const move = job.value?.next_statuses.find(next => next.status === status)
    if (move?.reason_required) {
      pendingStatus.value = status
      reason.value = ''
      reasonDialog.value = true
      return
    }
    applyStatus(status)
  }

  async function applyStatus (status: JobStatus, why = '') {
    busy.value = true
    conflictMessage.value = ''

    try {
      job.value = await setJobStatus(jobId.value, status, why)
      allowedStatuses.value = null
      reasonDialog.value = false
    } catch (error_) {
      if (statusOf(error_) === 409) {
        // The server's own list of what this job may become next.
        const body = bodyOf<TransitionConflict>(error_)
        conflictMessage.value = body?.detail ?? 'That is not possible right now.'
        allowedStatuses.value = body?.allowed ?? []
      } else if (statusOf(error_) === 400) {
        conflictMessage.value = errorDetail(error_) ?? 'That was not accepted.'
      } else {
        conflictMessage.value = 'Could not change the status.'
      }
    } finally {
      busy.value = false
    }
  }

  async function punch (which: 'in' | 'out') {
    busy.value = true
    conflictMessage.value = ''

    try {
      job.value = which === 'in' ? await clockIn(jobId.value) : await clockOut(jobId.value)
    } catch (error_) {
      // The 409 detail is the server's own explanation ("You are already
      // clocked in to this job"), which is better than anything invented here.
      conflictMessage.value = statusOf(error_) === 409
        ? (errorDetail(error_) ?? 'That is not possible right now.')
        : 'Could not update the clock.'
    } finally {
      busy.value = false
    }
  }

  async function addAssignee () {
    if (!assigneeToAdd.value) {
      return
    }
    busy.value = true
    try {
      job.value = await assignJob(jobId.value, assigneeToAdd.value)
      assigneeToAdd.value = null
    } catch {
      conflictMessage.value = 'Could not assign that person.'
    } finally {
      busy.value = false
    }
  }

  async function removeAssignee (userId: string) {
    busy.value = true
    try {
      job.value = await unassignJob(jobId.value, userId)
    } catch {
      conflictMessage.value = 'Could not remove that person.'
    } finally {
      busy.value = false
    }
  }

  async function submitNote () {
    if (!newNote.value.trim()) {
      return
    }
    busy.value = true
    try {
      const created = await createJobNote(jobId.value, newNote.value.trim())
      notes.value = [created, ...notes.value]
      newNote.value = ''
    } catch {
      conflictMessage.value = 'Could not save that note.'
    } finally {
      busy.value = false
    }
  }

  async function removeNote (id: string) {
    await deleteJobNote(id).catch(() => {})
    notes.value = notes.value.filter(note => note.id !== id)
  }

  async function onPhotoPicked (files: File[] | File | null) {
    const file = Array.isArray(files) ? files[0] : files
    if (!file) {
      return
    }
    busy.value = true
    try {
      photos.value = [await uploadJobPhoto(jobId.value, file), ...photos.value]
    } catch {
      conflictMessage.value = 'Could not upload that photo.'
    } finally {
      busy.value = false
    }
  }
</script>

<template>
  <v-container max-width="900">
    <v-progress-linear v-if="loading" indeterminate />

    <v-alert v-if="error" type="error" variant="tonal">{{ error }}</v-alert>

    <template v-if="job">
      <div class="d-flex flex-wrap align-center ga-2 mb-4">
        <h1 class="text-h6">{{ job.customer_detail.display_name }}</h1>
        <JobStatusChip :status="job.status" />
        <v-spacer />

        <span class="text-body-2 text-medium-emphasis">
          {{ formatDateTime(job.scheduled_start, session.timeZone) }}
          · {{ formatDuration(job.duration_minutes) }}
        </span>
      </div>

      <v-alert
        v-if="conflictMessage"
        class="mb-4"
        closable
        density="compact"
        type="warning"
        variant="tonal"
        @click:close="conflictMessage = ''"
      >
        {{ conflictMessage }}
      </v-alert>

      <v-row>
        <v-col cols="12" md="7">
          <v-card border class="mb-4" flat>
            <v-card-item>
              <v-card-title class="text-subtitle-1">Where</v-card-title>
            </v-card-item>

            <v-card-text>
              <p class="text-body-2">{{ job.location_detail.one_line_address }}</p>

              <p v-if="job.location_detail.access_notes" class="text-body-2 mt-2">
                <v-icon icon="mdi-door-open" size="small" />
                {{ job.location_detail.access_notes }}
              </p>

              <p v-if="job.location_detail.parking_notes" class="text-body-2 mt-2">
                <v-icon icon="mdi-car" size="small" />
                {{ job.location_detail.parking_notes }}
              </p>

              <v-alert
                v-if="job.location_detail.has_pets"
                class="mt-3"
                density="compact"
                icon="mdi-paw"
                type="info"
                variant="tonal"
              >
                {{ job.location_detail.pet_notes || 'There are pets at this address.' }}
              </v-alert>

              <v-btn
                v-if="job.location_detail.has_access_codes && canWork"
                class="mt-3"
                prepend-icon="mdi-shield-key-outline"
                variant="tonal"
                @click="revealOpen = true"
              >
                Reveal access codes
              </v-btn>
            </v-card-text>
          </v-card>

          <v-card v-if="session.isStaff" border class="mb-4" flat>
            <v-card-item>
              <v-card-title class="text-subtitle-1">Notes</v-card-title>
            </v-card-item>

            <v-card-text>
              <p v-if="job.notes" class="text-body-2 mb-3">
                <v-icon icon="mdi-clipboard-text-outline" size="small" />
                {{ job.notes }}
              </p>

              <v-textarea
                v-model="newNote"
                auto-grow
                density="compact"
                :disabled="!canWork"
                label="Add a note"
                rows="2"
              />

              <v-btn
                :disabled="!newNote.trim() || !canWork"
                :loading="busy"
                size="small"
                variant="tonal"
                @click="submitNote"
              >
                Save note
              </v-btn>

              <v-list density="compact">
                <v-list-item
                  v-for="note in notes"
                  :key="note.id"
                  :subtitle="note.user_name"
                  :title="note.body"
                >
                  <template #append>
                    <v-btn
                      icon="mdi-delete-outline"
                      size="x-small"
                      variant="text"
                      @click="removeNote(note.id)"
                    />
                  </template>
                </v-list-item>
              </v-list>
            </v-card-text>
          </v-card>

          <v-card v-if="session.isStaff" border flat>
            <v-card-item>
              <v-card-title class="text-subtitle-1">Photos</v-card-title>
            </v-card-item>

            <v-card-text>
              <v-file-input
                accept="image/*"
                density="compact"
                :disabled="!canWork"
                label="Add a photo"
                prepend-icon="mdi-camera-outline"
                @update:model-value="onPhotoPicked"
              />

              <div class="photo-grid">
                <v-img
                  v-for="photo in photos"
                  :key="photo.id"
                  aspect-ratio="1"
                  cover
                  rounded
                  :src="photo.image"
                />
              </div>
            </v-card-text>
          </v-card>
        </v-col>

        <v-col cols="12" md="5">
          <v-card v-if="canWork" border class="mb-4" flat>
            <v-card-item>
              <v-card-title class="text-subtitle-1">Status</v-card-title>
            </v-card-item>

            <v-card-text class="d-flex flex-wrap ga-2">
              <v-btn
                v-for="status in nextStatuses"
                :key="status"
                :loading="busy"
                size="small"
                variant="tonal"
                @click="requestStatus(status)"
              >
                {{ statusLabel(status) }}
              </v-btn>

              <p v-if="nextStatuses.length === 0" class="text-caption text-medium-emphasis">
                Nothing further from here.
              </p>
            </v-card-text>

            <v-divider />

            <v-card-text>
              <v-btn
                v-if="!openEntry"
                block
                color="primary"
                :loading="busy"
                prepend-icon="mdi-clock-start"
                @click="punch('in')"
              >
                Clock in
              </v-btn>

              <template v-else>
                <p class="text-caption text-medium-emphasis mb-2">
                  Clocked in at {{ formatDateTime(openEntry.clock_in, session.timeZone) }}
                </p>

                <v-btn
                  block
                  :loading="busy"
                  prepend-icon="mdi-clock-end"
                  variant="tonal"
                  @click="punch('out')"
                >
                  Clock out
                </v-btn>
              </template>
            </v-card-text>
          </v-card>

          <v-card v-if="session.isStaff" border class="mb-4" flat>
            <v-card-item>
              <v-card-title class="text-subtitle-1">Crew</v-card-title>
            </v-card-item>

            <v-card-text>
              <v-list density="compact">
                <v-list-item
                  v-for="assignment in job.assignments"
                  :key="assignment.id"
                  :subtitle="assignment.accepted_at ? 'Accepted' : 'Assigned'"
                  :title="assignment.user_name"
                >
                  <template #append>
                    <v-btn
                      v-if="session.isDispatcherOrHigher"
                      icon="mdi-close"
                      size="x-small"
                      variant="text"
                      @click="removeAssignee(assignment.user)"
                    />
                  </template>
                </v-list-item>

                <v-list-item v-if="job.assignments.length === 0" title="Nobody assigned" />
              </v-list>

              <template v-if="session.isDispatcherOrHigher">
                <v-select
                  v-model="assigneeToAdd"
                  density="compact"
                  hide-details
                  item-title="label"
                  item-value="id"
                  :items="staff"
                  label="Add crew"
                />

                <v-btn
                  block
                  class="mt-2"
                  :disabled="!assigneeToAdd"
                  :loading="busy"
                  size="small"
                  variant="tonal"
                  @click="addAssignee"
                >
                  Assign
                </v-btn>
              </template>
            </v-card-text>
          </v-card>

          <v-card border flat>
            <v-card-item>
              <v-card-title class="text-subtitle-1">Details</v-card-title>
            </v-card-item>

            <v-list density="compact">
              <v-list-item :subtitle="job.service_detail.name" title="Service" />
              <v-list-item :subtitle="formatCents(job.price_cents)" title="Price" />

              <v-list-item
                v-if="job.customer_detail.phone"
                :subtitle="job.customer_detail.phone"
                title="Phone"
              />

              <v-list-item
                v-if="job.cancellation_reason"
                :subtitle="job.cancellation_reason"
                title="Reason"
              />
            </v-list>
          </v-card>
        </v-col>
      </v-row>

      <RevealCodesDialog
        v-model="revealOpen"
        :job-id="job.id"
        :location-id="job.location"
      />

      <v-dialog v-model="reasonDialog" max-width="420">
        <v-card>
          <v-card-title class="text-subtitle-1">
            Why {{ pendingStatus ? statusLabel(pendingStatus).toLowerCase() : '' }}?
          </v-card-title>

          <v-card-text>
            <!-- Required server-side; asking here saves a round trip, but the
                 backend is what enforces it. -->
            <v-text-field v-model="reason" autofocus label="Reason" />
          </v-card-text>

          <v-card-actions>
            <v-spacer />
            <v-btn text="Cancel" @click="reasonDialog = false" />

            <v-btn
              color="primary"
              :disabled="!reason.trim()"
              :loading="busy"
              text="Confirm"
              variant="flat"
              @click="pendingStatus && applyStatus(pendingStatus, reason)"
            />
          </v-card-actions>
        </v-card>
      </v-dialog>
    </template>
  </v-container>
</template>

<style scoped>
.photo-grid {
  display: grid;
  gap: 8px;
  grid-template-columns: repeat(auto-fill, minmax(96px, 1fr));
}
</style>
