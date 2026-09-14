<script setup lang="ts">
  /**
   * Standing appointments.
   *
   * Two things here are the point of the screen:
   *
   * 1. The RRULE is never entered blind. `plans/{id}/preview/` returns the
   *    next occurrences without persisting anything, so the form can show
   *    "so: Tue 16 Sep, 23 Sep..." while someone types.
   * 2. An edit reports `regenerated` and `kept`. Editing a plan reshuffles
   *    eight weeks of visits already on the board, keeping the ones a
   *    dispatcher moved or worked (ADR-020), and they need to see which.
   */
  import type { PlanPreview } from '@/api/endpoints'
  import type { Customer, RecurringPlan, Service, ServiceLocation } from '@/api/types'
  import { computed, ref, watch } from 'vue'
  import {
    createPlan,
    listCustomers,
    listLocations,
    listPlans,
    listServices,
    previewPlan,
    updatePlan,
  } from '@/api/endpoints'
  import { formatCents, formatDateTime, todayIn } from '@/lib/datetime'
  import { useSessionStore } from '@/stores/session'

  const session = useSessionStore()
  const timeZone = computed(() => session.organization?.timezone ?? 'UTC')

  const plans = ref<RecurringPlan[]>([])
  const customers = ref<Customer[]>([])
  const locations = ref<ServiceLocation[]>([])
  const services = ref<Service[]>([])
  const loading = ref(false)
  const error = ref('')
  const notice = ref('')

  const dialog = ref(false)
  const saving = ref(false)
  const formError = ref('')
  const editingId = ref<string | null>(null)
  const preview = ref<PlanPreview | null>(null)
  const previewing = ref(false)

  const draft = ref<Partial<RecurringPlan>>({})

  /** Common rules, so nobody has to remember RFC 5545 to book a weekly clean. */
  const RRULE_PRESETS = [
    { title: 'Every week', value: 'FREQ=WEEKLY' },
    { title: 'Every two weeks', value: 'FREQ=WEEKLY;INTERVAL=2' },
    { title: 'Every four weeks', value: 'FREQ=WEEKLY;INTERVAL=4' },
    { title: 'Every month, same date', value: 'FREQ=MONTHLY' },
    { title: 'Weekdays', value: 'FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR' },
  ]

  async function load () {
    loading.value = true
    error.value = ''

    try {
      const [planPage, customerPage, servicePage, locationPage] = await Promise.all([
        listPlans(),
        listCustomers({ limit: 200 }),
        listServices({ is_active: true }),
        listLocations({ limit: 200 }),
      ])
      plans.value = planPage.results
      customers.value = customerPage.results
      services.value = servicePage.results
      locations.value = locationPage.results
    } catch {
      error.value = 'Could not load recurring plans.'
    } finally {
      loading.value = false
    }
  }

  load()

  /** Locations belonging to the customer chosen in the form. */
  const locationsForDraft = computed(() =>
    locations.value.filter(location => location.customer === draft.value.customer),
  )

  function open (plan?: RecurringPlan) {
    editingId.value = plan?.id ?? null
    formError.value = ''
    preview.value = null
    draft.value = plan
      ? { ...plan }
      : {
        rrule: 'FREQ=WEEKLY',
        starts_on: todayIn(timeZone.value),
        preferred_start_time: '09:00:00',
        is_active: true,
      }
    dialog.value = true

    if (plan) {
      refreshPreview()
    }
  }

  /** The saved plan's next occurrences, straight from the backend. */
  async function refreshPreview () {
    if (!editingId.value) {
      return
    }
    previewing.value = true
    try {
      preview.value = await previewPlan(editingId.value, 6)
    } catch {
      preview.value = null
    } finally {
      previewing.value = false
    }
  }

  async function save () {
    saving.value = true
    formError.value = ''
    notice.value = ''

    const payload = {
      customer: draft.value.customer,
      location: draft.value.location,
      service: draft.value.service,
      rrule: draft.value.rrule,
      starts_on: draft.value.starts_on,
      ends_on: draft.value.ends_on || null,
      preferred_start_time: draft.value.preferred_start_time,
      duration_minutes: draft.value.duration_minutes,
      price_override_cents: draft.value.price_override_cents ?? null,
      is_active: draft.value.is_active,
      notes: draft.value.notes ?? '',
    }

    try {
      if (editingId.value) {
        const result = await updatePlan(editingId.value, payload)
        // What the edit did to the visits already on the board.
        notice.value = result.regenerated === undefined
          ? 'Saved.'
          : `Saved. ${result.regenerated} visit(s) rebuilt, ${result.kept} kept as they were.`
      } else {
        await createPlan(payload)
        notice.value = 'Plan created. Its visits are on the schedule.'
      }
      dialog.value = false
      await load()
    } catch (error_) {
      const data = (error_ as { response?: { data?: Record<string, unknown> } }).response?.data
      formError.value = typeof data === 'object' && data !== null
        ? Object.entries(data).map(([key, value]) => `${key}: ${String(value)}`).join(' ')
        : 'Could not save that plan.'
    } finally {
      saving.value = false
    }
  }

  // Re-preview when the rule changes on a saved plan. A new plan has nothing
  // to preview against until it exists, so the list appears after the first save.
  watch(() => [draft.value.rrule, draft.value.preferred_start_time], () => {
    if (editingId.value) {
      preview.value = null
    }
  })

  function customerName (id: string): string {
    return customers.value.find(c => c.id === id)?.display_name ?? '—'
  }

  function serviceName (id: string): string {
    return services.value.find(s => s.id === id)?.name ?? '—'
  }
</script>

<template>
  <v-container max-width="1000">
    <div class="d-flex align-center ga-2 mb-4">
      <h1 class="text-h6">Recurring plans</h1>
      <v-spacer />
      <v-btn color="primary" prepend-icon="mdi-plus" @click="open()">New</v-btn>
    </div>

    <v-alert v-if="error" class="mb-4" type="error" variant="tonal">{{ error }}</v-alert>

    <v-alert
      v-if="notice"
      class="mb-4"
      closable
      type="success"
      variant="tonal"
      @click:close="notice = ''"
    >
      {{ notice }}
    </v-alert>

    <v-progress-linear v-if="loading" class="mb-2" indeterminate />

    <v-card border flat>
      <v-list>
        <v-list-item
          v-for="plan in plans"
          :key="plan.id"
          :subtitle="`${plan.rrule} at ${plan.preferred_start_time}`"
          :title="`${customerName(plan.customer)} — ${serviceName(plan.service)}`"
        >
          <template #append>
            <v-chip
              v-if="plan.price_override_cents"
              class="mr-2"
              size="x-small"
              variant="tonal"
            >
              {{ formatCents(plan.price_override_cents) }}
            </v-chip>

            <v-chip
              v-if="!plan.is_active"
              class="mr-2"
              color="warning"
              size="x-small"
              variant="tonal"
            >
              Paused
            </v-chip>

            <v-btn
              icon="mdi-pencil-outline"
              size="x-small"
              variant="text"
              @click="open(plan)"
            />
          </template>
        </v-list-item>

        <v-list-item v-if="!loading && plans.length === 0" title="No recurring plans yet" />
      </v-list>
    </v-card>

    <v-dialog v-model="dialog" max-width="600" scrollable>
      <v-card>
        <v-card-title class="text-subtitle-1">
          {{ editingId ? 'Edit plan' : 'New recurring plan' }}
        </v-card-title>

        <v-card-text>
          <v-alert
            v-if="formError"
            class="mb-3"
            density="compact"
            type="error"
            variant="tonal"
          >
            {{ formError }}
          </v-alert>

          <v-select
            v-model="draft.customer"
            density="compact"
            item-title="display_name"
            item-value="id"
            :items="customers"
            label="Customer"
          />

          <v-select
            v-model="draft.location"
            density="compact"
            :disabled="!draft.customer"
            item-title="one_line_address"
            item-value="id"
            :items="locationsForDraft"
            label="Location"
          />

          <v-select
            v-model="draft.service"
            density="compact"
            item-title="name"
            item-value="id"
            :items="services"
            label="Service"
          />

          <v-select
            density="compact"
            :items="RRULE_PRESETS"
            label="Repeats"
            :model-value="draft.rrule"
            @update:model-value="draft.rrule = $event"
          />

          <v-text-field
            v-model="draft.rrule"
            density="compact"
            hint="RFC 5545 rule body. No DTSTART -- the date and time below supply it."
            label="Recurrence rule"
            persistent-hint
          />

          <v-row class="mt-1" dense>
            <v-col cols="12" sm="6">
              <v-text-field
                v-model="draft.starts_on"
                density="compact"
                label="Starts on"
                type="date"
              />
            </v-col>

            <v-col cols="12" sm="6">
              <v-text-field
                v-model="draft.ends_on"
                clearable
                density="compact"
                hint="Leave blank to run indefinitely."
                label="Ends on"
                type="date"
              />
            </v-col>

            <v-col cols="12" sm="6">
              <v-text-field
                v-model="draft.preferred_start_time"
                density="compact"
                hint="Local time. Stays this time across a clock change."
                label="Start time"
                persistent-hint
                type="time"
              />
            </v-col>

            <v-col cols="12" sm="6">
              <v-text-field
                v-model.number="draft.duration_minutes"
                density="compact"
                hint="Defaults from the service."
                label="Duration (minutes)"
                type="number"
              />
            </v-col>
          </v-row>

          <v-textarea v-model="draft.notes" density="compact" label="Notes" rows="2" />

          <v-switch
            v-model="draft.is_active"
            color="primary"
            density="compact"
            hide-details
            label="Active"
          />

          <v-divider class="my-3" />

          <div class="d-flex align-center ga-2 mb-2">
            <span class="text-subtitle-2">Next visits</span>

            <v-btn
              v-if="editingId"
              :loading="previewing"
              size="x-small"
              variant="text"
              @click="refreshPreview"
            >
              Preview
            </v-btn>
          </div>

          <p v-if="!editingId" class="text-caption text-medium-emphasis">
            Save the plan to see the dates it works out to.
          </p>

          <v-chip-group v-else-if="preview" column>
            <v-chip
              v-for="occurrence in preview.occurrences"
              :key="occurrence.utc"
              size="small"
              variant="tonal"
            >
              {{ formatDateTime(occurrence.utc, timeZone) }}
            </v-chip>
          </v-chip-group>

          <p v-else class="text-caption text-medium-emphasis">
            Press Preview to work out the dates.
          </p>
        </v-card-text>

        <v-card-actions>
          <v-spacer />
          <v-btn text="Cancel" @click="dialog = false" />

          <v-btn
            color="primary"
            :loading="saving"
            text="Save"
            variant="flat"
            @click="save"
          />
        </v-card-actions>
      </v-card>
    </v-dialog>
  </v-container>
</template>
