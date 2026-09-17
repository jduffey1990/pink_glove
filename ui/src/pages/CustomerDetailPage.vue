<script setup lang="ts">
  /**
   * One customer and the addresses they have cleaned.
   *
   * Access codes are write-only on this form: they can be set and changed
   * here, but reading one back needs the audited reveal (ADR-016). The
   * "codes on file" indicator comes from `has_access_codes`, which is exactly
   * what that field exists for.
   */
  import type { Customer, Invoice, ServiceLocation, ServiceLocationRequest } from '@/api/types'
  import { computed, ref, watch } from 'vue'
  import { useRoute } from 'vue-router'
  import {
    createLocation,
    getCustomer,
    listInvoices,
    listLocations,
    updateCustomer,
    updateLocation,
  } from '@/api/endpoints'
  import InvoiceStatusChip from '@/components/InvoiceStatusChip.vue'
  import RevealCodesDialog from '@/components/RevealCodesDialog.vue'
  import { formatCents } from '@/lib/money'
  import { statusOf, useSessionStore } from '@/stores/session'

  const route = useRoute()
  const session = useSessionStore()
  const customerId = computed(() => route.params.id as string)

  const customer = ref<Customer | null>(null)
  const locations = ref<ServiceLocation[]>([])
  const loading = ref(true)
  const error = ref('')
  const saving = ref(false)
  const notice = ref('')

  const locationDialog = ref(false)
  // The REQUEST type, not the response one: access codes are write-only and
  // exist only on the way in (ADR-016).
  const locationDraft = ref<ServiceLocationRequest>({})
  const editingLocationId = ref<string | null>(null)
  const formError = ref('')

  const revealOpen = ref(false)
  const revealLocationId = ref<string | null>(null)

  const invoices = ref<Invoice[]>([])

  async function load () {
    loading.value = true
    error.value = ''

    try {
      customer.value = await getCustomer(customerId.value)
      locations.value = (await listLocations({ customer: customerId.value })).results

      // Dispatchers and above only. A cleaner can reach a customer page but
      // gets a 403 from billing, and an error card there would be noise about
      // a rule working correctly.
      invoices.value = session.isDispatcherOrHigher
        ? (await listInvoices({ customer: customerId.value, limit: 20 })).results
        : []
    } catch (error_) {
      error.value = statusOf(error_) === 404
        ? 'That customer does not exist, or belongs to another organization.'
        : 'Could not load this customer.'
    } finally {
      loading.value = false
    }
  }

  watch(customerId, load, { immediate: true })

  async function saveCustomer () {
    if (!customer.value) {
      return
    }
    saving.value = true
    notice.value = ''

    try {
      customer.value = await updateCustomer(customerId.value, {
        first_name: customer.value.first_name,
        last_name: customer.value.last_name,
        company_name: customer.value.company_name,
        email: customer.value.email,
        phone: customer.value.phone,
        notes: customer.value.notes,
      })
      notice.value = 'Saved.'
    } catch {
      error.value = 'Could not save.'
    } finally {
      saving.value = false
    }
  }

  function openLocation (location?: ServiceLocation) {
    editingLocationId.value = location?.id ?? null
    formError.value = ''
    locationDraft.value = location
      ? { ...location }
      : { customer: customerId.value, country: 'US', is_active: true }
    locationDialog.value = true
  }

  async function saveLocation () {
    saving.value = true
    formError.value = ''

    // Only send codes that were actually typed: the fields render empty
    // (they are write-only), and sending "" would wipe a code on file.
    const payload = { ...locationDraft.value }
    for (const field of ['gate_code', 'alarm_code', 'key_location'] as const) {
      if (!payload[field]) {
        delete payload[field]
      }
    }

    try {
      await (editingLocationId.value ? updateLocation(editingLocationId.value, payload) : createLocation({ ...payload, customer: customerId.value }))
      locationDialog.value = false
      await load()
    } catch (error_) {
      const data = (error_ as { response?: { data?: Record<string, unknown> } }).response?.data
      formError.value = typeof data === 'object' && data !== null
        ? Object.entries(data).map(([key, value]) => `${key}: ${String(value)}`).join(' ')
        : 'Could not save that location.'
    } finally {
      saving.value = false
    }
  }

  function reveal (locationId: string) {
    revealLocationId.value = locationId
    revealOpen.value = true
  }
</script>

<template>
  <v-container max-width="900">
    <v-progress-linear v-if="loading" indeterminate />
    <v-alert v-if="error" class="mb-4" type="error" variant="tonal">{{ error }}</v-alert>

    <template v-if="customer">
      <h1 class="text-h6 mb-4">{{ customer.display_name }}</h1>

      <v-alert
        v-if="notice"
        class="mb-4"
        density="compact"
        type="success"
        variant="tonal"
      >
        {{ notice }}
      </v-alert>

      <v-card border class="mb-4" flat>
        <v-card-item>
          <v-card-title class="text-subtitle-1">Details</v-card-title>
        </v-card-item>

        <v-card-text>
          <v-row dense>
            <v-col cols="12" sm="6">
              <v-text-field v-model="customer.first_name" density="compact" label="First name" />
            </v-col>

            <v-col cols="12" sm="6">
              <v-text-field v-model="customer.last_name" density="compact" label="Last name" />
            </v-col>

            <v-col cols="12" sm="6">
              <v-text-field v-model="customer.company_name" density="compact" label="Company" />
            </v-col>

            <v-col cols="12" sm="6">
              <v-text-field v-model="customer.email" density="compact" label="Email" />
            </v-col>

            <v-col cols="12" sm="6">
              <v-text-field v-model="customer.phone" density="compact" label="Phone" />
            </v-col>

            <v-col cols="12">
              <v-textarea v-model="customer.notes" density="compact" label="Notes" rows="2" />
            </v-col>
          </v-row>

          <v-btn color="primary" :loading="saving" variant="tonal" @click="saveCustomer">
            Save
          </v-btn>
        </v-card-text>
      </v-card>

      <v-card border flat>
        <v-card-item>
          <v-card-title class="text-subtitle-1">Locations</v-card-title>

          <template #append>
            <v-btn size="small" variant="tonal" @click="openLocation()">Add</v-btn>
          </template>
        </v-card-item>

        <v-list>
          <v-list-item
            v-for="location in locations"
            :key="location.id"
            :subtitle="location.one_line_address"
            :title="location.label || location.line1"
          >
            <template #append>
              <v-chip
                v-if="location.has_access_codes"
                class="mr-2"
                prepend-icon="mdi-shield-key-outline"
                size="x-small"
                variant="tonal"
              >
                Codes on file
              </v-chip>

              <v-btn
                v-if="location.has_access_codes"
                size="x-small"
                variant="text"
                @click="reveal(location.id)"
              >
                Reveal
              </v-btn>

              <v-btn
                icon="mdi-pencil-outline"
                size="x-small"
                variant="text"
                @click="openLocation(location)"
              />
            </template>
          </v-list-item>

          <v-list-item v-if="locations.length === 0" title="No locations yet" />
        </v-list>
      </v-card>

      <v-card v-if="session.isDispatcherOrHigher" border class="mt-4" flat>
        <v-card-item>
          <v-card-title class="text-subtitle-1">Invoices</v-card-title>

          <template #append>
            <v-btn size="small" :to="{ name: 'billing' }" variant="text">Billing</v-btn>
          </template>
        </v-card-item>

        <v-list>
          <v-list-item
            v-for="invoice in invoices"
            :key="invoice.id"
            :to="{ name: 'invoice', params: { id: invoice.id } }"
          >
            <v-list-item-title>{{ invoice.number || 'Draft' }}</v-list-item-title>

            <v-list-item-subtitle>
              <template v-if="invoice.issued_on">
                Issued {{ invoice.issued_on }} · due {{ invoice.due_on }}
              </template>

              <template v-else>Not yet issued</template>
            </v-list-item-subtitle>

            <template #append>
              <div class="d-flex align-center ga-2">
                <v-chip
                  v-if="invoice.is_overdue"
                  color="error"
                  size="x-small"
                  variant="tonal"
                >
                  Overdue
                </v-chip>

                <InvoiceStatusChip :invoice="invoice" />

                <span class="text-body-2 text-no-wrap">
                  {{ formatCents(invoice.total_cents) }}
                </span>
              </div>
            </template>
          </v-list-item>

          <v-list-item v-if="invoices.length === 0" title="Nothing billed yet" />
        </v-list>
      </v-card>

      <v-dialog v-model="locationDialog" max-width="560">
        <v-card>
          <v-card-title class="text-subtitle-1">
            {{ editingLocationId ? 'Edit location' : 'New location' }}
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

            <v-text-field v-model="locationDraft.label" density="compact" label="Label" />
            <v-text-field v-model="locationDraft.line1" density="compact" label="Address" />

            <v-row dense>
              <v-col cols="6">
                <v-text-field v-model="locationDraft.city" density="compact" label="City" />
              </v-col>

              <v-col cols="3">
                <v-text-field v-model="locationDraft.state" density="compact" label="State" />
              </v-col>

              <v-col cols="3">
                <v-text-field
                  v-model="locationDraft.postal_code"
                  density="compact"
                  label="Postcode"
                />
              </v-col>
            </v-row>

            <v-text-field
              v-model.number="locationDraft.square_feet"
              density="compact"
              hint="Required for per-square-foot services."
              label="Square feet"
              persistent-hint
              type="number"
            />

            <v-textarea
              v-model="locationDraft.access_notes"
              density="compact"
              label="Access notes (not sensitive)"
              rows="2"
            />

            <v-divider class="my-3" />

            <p class="text-caption text-medium-emphasis mb-2">
              Access codes are encrypted and write-only. Leave blank to keep
              what is on file; reading one back is a logged reveal.
            </p>

            <v-text-field
              v-model="locationDraft.gate_code"
              density="compact"
              label="Gate code"
              type="password"
            />

            <v-text-field
              v-model="locationDraft.alarm_code"
              density="compact"
              label="Alarm code"
              type="password"
            />
          </v-card-text>

          <v-card-actions>
            <v-spacer />
            <v-btn text="Cancel" @click="locationDialog = false" />

            <v-btn
              color="primary"
              :loading="saving"
              text="Save"
              variant="flat"
              @click="saveLocation"
            />
          </v-card-actions>
        </v-card>
      </v-dialog>

      <RevealCodesDialog
        v-if="revealLocationId"
        v-model="revealOpen"
        :location-id="revealLocationId"
      />
    </template>
  </v-container>
</template>
