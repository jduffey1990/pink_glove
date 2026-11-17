<script setup lang="ts">
  /**
   * Book a one-off visit, or change one that exists.
   *
   * Inputs and nothing else. What the form means lives in `lib/visitForm`:
   * the date and time are the organization's wall clock, the length is a
   * duration, an edit sends only what changed. What the server will accept is
   * the server's to say, so:
   *
   * - no price is worked out here. A blank price on a new visit asks the
   *   server to quote it, and the page shows the number that comes back;
   * - a refused save is shown in the server's words, under the input it is
   *   about (ADR-023);
   * - a 409 means the visit is no longer in a state where this makes sense.
   *   The dialog closes and hands the message to the page, which re-reads the
   *   job, because a form over a stale job is the wrong thing to keep open.
   *
   * With `job` it edits; without, it books. The customer cannot be changed on
   * an existing visit, and its crew is managed by the page's own assign and
   * unassign controls, so neither is offered in edit mode.
   */
  import type { Customer, Job, Service } from '@/api/types'
  import type { IsoDate } from '@/lib/datetime'
  import type { VisitErrors, VisitField, VisitForm } from '@/lib/visitForm'
  import { computed, ref, watch } from 'vue'
  import { useDisplay } from 'vuetify'
  import {
    createJob,
    getCustomer,
    listCustomers,
    listServices,
    updateJob,
  } from '@/api/endpoints'
  import { errorDetail, statusOf } from '@/api/errors'
  import { formatDateTime, formatTime, todayIn, toIsoDate } from '@/lib/datetime'
  import {
    localErrors,
    newVisitForm,
    NO_VISIT_ERRORS,
    toJobPatch,
    toNewJobRequest,
    visitErrors,
    visitFormFromJob,
    visitSchedule,
  } from '@/lib/visitForm'
  import { useSessionStore } from '@/stores/session'

  const props = defineProps<{
    /** The visit being edited. Absent or null books a new one. */
    job?: Job | null
    /** The organization-local day a new visit starts out on. */
    date?: IsoDate
    /** Who can be put on a new visit, as `listAssignableStaff` returns them. */
    staff?: { id: string, label: string }[]
  }>()

  const emit = defineEmits<{
    saved: [job: Job]
    conflict: [message: string]
  }>()

  const model = defineModel<boolean>({ required: true })

  const session = useSessionStore()
  const { smAndDown } = useDisplay()

  const form = ref<VisitForm>(newVisitForm(todayIn(session.timeZone)))
  /** The form as it was opened, which is what an edit is measured against. */
  const before = ref<VisitForm>(form.value)
  const saving = ref(false)
  const serverErrors = ref<VisitErrors>(NO_VISIT_ERRORS)

  const services = ref<Service[]>([])
  const customer = ref<Customer | null>(null)
  const customerResults = ref<Customer[]>([])
  const customerSearch = ref('')
  const searching = ref(false)
  const optionsError = ref('')

  /** Until the dispatcher types a length, it follows the service's default. */
  const durationTouched = ref(false)

  const editing = computed(() => Boolean(props.job))

  function reset () {
    form.value = props.job
      ? visitFormFromJob(props.job, session.timeZone)
      : newVisitForm(props.date ?? todayIn(session.timeZone))
    before.value = { ...form.value, assignees: [] }
    durationTouched.value = editing.value
    serverErrors.value = NO_VISIT_ERRORS
    optionsError.value = ''
    customer.value = null
    customerSearch.value = ''
    saving.value = false

    loadOptions()
  }

  async function loadOptions () {
    try {
      const requests: Promise<unknown>[] = [
        listServices({ is_active: true }).then(page => {
          services.value = page.results
        }),
      ]

      if (props.job) {
        // The customer is fixed on an edit, but its other locations are what
        // the location picker offers.
        requests.push(getCustomer(props.job.customer).then(found => {
          customer.value = found
        }))
      } else {
        requests.push(searchCustomers(''))
      }

      await Promise.all(requests)
    } catch {
      optionsError.value = 'Could not load the customers and services to choose from.'
    }
  }

  // --- customer --------------------------------------------------------------

  let searchTicket = 0
  let searchTimer: ReturnType<typeof setTimeout> | undefined

  /** Server-side search, so the picker works past the first page of customers. */
  async function searchCustomers (text: string) {
    const ticket = ++searchTicket
    searching.value = true
    try {
      const page = await listCustomers({ search: text || undefined, limit: 25 })
      // Only the latest answer counts; a slow earlier one must not overwrite it.
      if (ticket === searchTicket) {
        customerResults.value = page.results
      }
    } finally {
      if (ticket === searchTicket) {
        searching.value = false
      }
    }
  }

  function onCustomerSearch (text: string) {
    customerSearch.value = text
    // Choosing a customer writes their name into the box. That is not a search.
    if (customer.value && text === customer.value.display_name) {
      return
    }
    clearTimeout(searchTimer)
    searchTimer = setTimeout(() => {
      searchCustomers(text).catch(() => {})
    }, 250)
  }

  /** The search results, with the chosen customer kept in so the box can show it. */
  const customerItems = computed(() => {
    const chosen = customer.value
    return chosen && !customerResults.value.some(c => c.id === chosen.id)
      ? [chosen, ...customerResults.value]
      : customerResults.value
  })

  function pickCustomer (chosen: Customer | null) {
    customer.value = chosen
    form.value.customer = chosen?.id ?? null

    // A location belongs to one customer, so the old choice cannot stand.
    // With a single address there is nothing to choose.
    const active = chosen?.locations.filter(location => location.is_active) ?? []
    form.value.location = active.length === 1 ? active[0].id : null
  }

  // --- location and service --------------------------------------------------

  const locationItems = computed(() => {
    const items = (customer.value?.locations ?? [])
      .filter(location => location.is_active || location.id === props.job?.location)
      .map(location => ({
        id: location.id,
        title: location.label
          ? `${location.label}: ${location.one_line_address}`
          : location.one_line_address,
      }))

    // The visit's own location, until the customer has loaded or if it no
    // longer appears among theirs.
    const current = props.job
    if (current && !items.some(item => item.id === current.location)) {
      items.unshift({
        id: current.location,
        title: current.location_detail.label
          ? `${current.location_detail.label}: ${current.location_detail.one_line_address}`
          : current.location_detail.one_line_address,
      })
    }
    return items
  })

  const serviceItems = computed(() => {
    const items = services.value.map(service => ({ id: service.id, title: service.name }))

    // A visit can outlive its service being retired; keep it choosable.
    const current = props.job
    if (current && !items.some(item => item.id === current.service)) {
      items.unshift({ id: current.service, title: current.service_detail.name })
    }
    return items
  })

  function pickService (id: string | null) {
    form.value.service = id
    if (durationTouched.value) {
      return
    }
    const minutes = services.value.find(service => service.id === id)?.default_duration_minutes
    form.value.durationMinutes = minutes ?? null
  }

  // --- what the form currently says ------------------------------------------

  /** "Ends 11:00", or with the day when the visit runs past midnight. */
  const endsLabel = computed(() => {
    const schedule = visitSchedule(form.value, session.timeZone)
    if (schedule === null) {
      return ''
    }
    const sameDay = toIsoDate(schedule.scheduled_end, session.timeZone) === form.value.date
    return sameDay
      ? `Ends ${formatTime(schedule.scheduled_end, session.timeZone)}`
      : `Ends ${formatDateTime(schedule.scheduled_end, session.timeZone)}`
  })

  /** The request this form would send right now, or null if it cannot be sent. */
  const request = computed(() => props.job
    ? toJobPatch(props.job, before.value, form.value, session.timeZone)
    : toNewJobRequest(form.value, session.timeZone))

  const typedErrors = computed(() => localErrors(form.value, { existing: editing.value }))

  function messages (field: VisitField): string[] {
    return [...(typedErrors.value.fields[field] ?? []), ...(serverErrors.value.fields[field] ?? [])]
  }

  // A refusal describes what was sent. Once the form changes it is stale.
  watch(form, () => {
    serverErrors.value = NO_VISIT_ERRORS
  }, { deep: true })

  async function save () {
    saving.value = true
    serverErrors.value = NO_VISIT_ERRORS

    try {
      let saved: Job
      if (props.job) {
        const patch = toJobPatch(props.job, before.value, form.value, session.timeZone)
        if (patch === null) {
          return
        }
        if (Object.keys(patch).length === 0) {
          // Nothing was changed, so there is nothing to ask the server for.
          model.value = false
          return
        }
        saved = await updateJob(props.job.id, patch)
      } else {
        const booking = toNewJobRequest(form.value, session.timeZone)
        if (booking === null) {
          return
        }
        saved = await createJob(booking)
      }

      model.value = false
      emit('saved', saved)
    } catch (error_) {
      if (statusOf(error_) === 409) {
        model.value = false
        emit('conflict', errorDetail(error_) ?? 'That is not possible right now.')
        return
      }

      const refused = statusOf(error_) === 400 ? visitErrors(error_) : NO_VISIT_ERRORS
      const said = Object.keys(refused.fields).length > 0 || refused.general.length > 0
      serverErrors.value = said
        ? refused
        : { fields: {}, general: [errorDetail(error_) ?? 'Could not save this visit.'] }
    } finally {
      saving.value = false
    }
  }

  // `immediate` matters: a caller may render this behind a `v-if` that turns
  // true in the same tick as the model, so it mounts with `model` ALREADY
  // true and a plain watcher never fires. The form would open on whatever it
  // held last, with no options loaded.
  //
  // And it is the LAST statement on purpose. An immediate watcher runs during
  // setup, so `reset` would otherwise reach the `let`s declared further down
  // before they exist; the customer search threw on exactly that, quietly,
  // inside a promise.
  watch(model, open => {
    if (open) {
      reset()
    }
  }, { immediate: true })
</script>

<template>
  <v-dialog v-model="model" :fullscreen="smAndDown" max-width="640" scrollable>
    <v-card>
      <v-card-title class="text-subtitle-1">
        {{ editing ? 'Edit visit' : 'New visit' }}
      </v-card-title>

      <v-card-text>
        <v-alert
          v-if="optionsError"
          class="mb-3"
          density="compact"
          type="error"
          variant="tonal"
        >
          {{ optionsError }}
        </v-alert>

        <v-alert
          v-for="message in serverErrors.general"
          :key="message"
          class="mb-3"
          density="compact"
          type="error"
          variant="tonal"
        >
          {{ message }}
        </v-alert>

        <v-text-field
          v-if="job"
          density="compact"
          disabled
          label="Customer"
          :model-value="job.customer_detail.display_name"
        />

        <v-autocomplete
          v-else
          autofocus
          density="compact"
          :error-messages="messages('customer')"
          item-title="display_name"
          item-value="id"
          :items="customerItems"
          label="Customer"
          :loading="searching"
          :model-value="customer"
          no-filter
          placeholder="Type a name, email or phone"
          return-object
          @update:model-value="pickCustomer"
          @update:search="onCustomerSearch"
        />

        <v-select
          v-model="form.location"
          density="compact"
          :disabled="!form.customer"
          :error-messages="messages('location')"
          item-title="title"
          item-value="id"
          :items="locationItems"
          label="Location"
          no-data-text="This customer has no active location."
        />

        <v-select
          density="compact"
          :error-messages="messages('service')"
          item-title="title"
          item-value="id"
          :items="serviceItems"
          label="Service"
          :model-value="form.service"
          @update:model-value="pickService"
        />

        <v-row density="compact">
          <v-col cols="12" sm="5">
            <v-text-field
              v-model="form.date"
              density="compact"
              :error-messages="messages('date')"
              label="Date"
              type="date"
            />
          </v-col>

          <v-col cols="6" sm="3">
            <v-text-field
              v-model="form.start"
              density="compact"
              :error-messages="messages('start')"
              label="Start"
              type="time"
            />
          </v-col>

          <v-col cols="6" sm="4">
            <v-text-field
              v-model.number="form.durationMinutes"
              density="compact"
              :error-messages="messages('duration')"
              :hint="endsLabel"
              label="Minutes"
              min="1"
              persistent-hint
              type="number"
              @update:model-value="durationTouched = true"
            />
          </v-col>
        </v-row>

        <p class="text-caption text-medium-emphasis mb-3">
          Times are {{ session.timeZone }} time.
        </p>

        <v-text-field
          v-model="form.price"
          density="compact"
          :error-messages="messages('price')"
          :hint="editing ? '' : 'Leave blank to have it priced from the service.'"
          inputmode="decimal"
          label="Price"
          persistent-hint
          prefix="$"
        />

        <v-select
          v-if="!editing"
          v-model="form.assignees"
          chips
          class="mt-3"
          closable-chips
          density="compact"
          :error-messages="messages('assignees')"
          item-title="label"
          item-value="id"
          :items="staff ?? []"
          label="Crew"
          multiple
        />

        <v-textarea
          v-model="form.notes"
          auto-grow
          class="mt-3"
          density="compact"
          :error-messages="messages('notes')"
          label="Instructions for the crew"
          rows="2"
        />
      </v-card-text>

      <v-card-actions>
        <v-spacer />
        <v-btn text="Cancel" @click="model = false" />

        <v-btn
          color="primary"
          :disabled="request === null"
          :loading="saving"
          :text="editing ? 'Save' : 'Book visit'"
          variant="flat"
          @click="save"
        />
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>
