<script setup lang="ts">
  /**
   * The menu. Staff read it; admins and owners change it.
   *
   * Prices are integer cents everywhere (CLAUDE.md invariant 5). The inputs
   * take dollars because nobody types cents; `lib/money` does the conversion,
   * in the one place with a spec covering the float that bit in Phase 3b.
   */
  import type { Service } from '@/api/types'
  import { ref } from 'vue'
  import { createService, listServices, updateService } from '@/api/endpoints'
  import { errorDetail } from '@/api/errors'
  import {
    centsToDollars,
    dollarsToCents,
    dollarsToRateString,
    formatCents,
  } from '@/lib/money'
  import { useSessionStore } from '@/stores/session'

  const session = useSessionStore()

  const services = ref<Service[]>([])
  const loading = ref(false)
  const error = ref('')

  const dialog = ref(false)
  const saving = ref(false)
  const formError = ref('')
  const editingId = ref<string | null>(null)

  /** The form works in dollars; the API works in cents. */
  const draft = ref({
    name: '',
    description: '',
    pricing_model: 'flat' as Service['pricing_model'],
    base_price: 0,
    hourly_rate: 0,
    per_sqft_rate: 0,
    default_duration_minutes: 120,
    is_active: true,
    is_taxable: false,
  })

  const canEdit = session.role === 'owner' || session.role === 'admin'

  const PRICING_MODELS = [
    { value: 'flat', title: 'Flat rate' },
    { value: 'hourly', title: 'Hourly' },
    { value: 'per_sqft', title: 'Per square foot' },
  ]

  async function load () {
    loading.value = true
    error.value = ''

    try {
      services.value = (await listServices()).results
    } catch {
      error.value = 'Could not load services.'
    } finally {
      loading.value = false
    }
  }

  load()

  function open (service?: Service) {
    editingId.value = service?.id ?? null
    formError.value = ''
    draft.value = service
      ? {
        name: service.name,
        description: service.description ?? '',
        pricing_model: service.pricing_model,
        base_price: centsToDollars(service.base_price_cents),
        hourly_rate: centsToDollars(Number(service.hourly_rate_cents ?? 0)),
        per_sqft_rate: centsToDollars(Number(service.per_sqft_rate_cents ?? 0)),
        default_duration_minutes: service.default_duration_minutes ?? 120,
        is_active: service.is_active ?? true,
        is_taxable: service.is_taxable ?? false,
      }
      : {
        name: '',
        description: '',
        pricing_model: 'flat',
        base_price: 0,
        hourly_rate: 0,
        per_sqft_rate: 0,
        default_duration_minutes: 120,
        is_active: true,
        is_taxable: false,
      }
    dialog.value = true
  }

  async function save () {
    saving.value = true
    formError.value = ''

    const payload = {
      name: draft.value.name,
      description: draft.value.description,
      pricing_model: draft.value.pricing_model,
      base_price_cents: dollarsToCents(draft.value.base_price),
      // A rate keeps fractions of a cent (ADR-009); an amount does not.
      hourly_rate_cents: dollarsToRateString(draft.value.hourly_rate, 2),
      per_sqft_rate_cents: dollarsToRateString(draft.value.per_sqft_rate, 3),
      default_duration_minutes: draft.value.default_duration_minutes,
      is_active: draft.value.is_active,
      is_taxable: draft.value.is_taxable,
    }

    try {
      await (editingId.value
        ? updateService(editingId.value, payload)
        : createService(payload))
      dialog.value = false
      await load()
    } catch (error_) {
      formError.value = errorDetail(error_) ?? 'Could not save that service.'
    } finally {
      saving.value = false
    }
  }

  function priceLabel (service: Service): string {
    switch (service.pricing_model) {
      case 'hourly': {
        return `${formatCents(Number(service.hourly_rate_cents))} / hour`
      }
      case 'per_sqft': {
        return `${formatCents(Number(service.per_sqft_rate_cents))} / sq ft`
      }
      default: {
        return formatCents(service.base_price_cents)
      }
    }
  }
</script>

<template>
  <v-container max-width="900">
    <div class="d-flex align-center ga-2 mb-4">
      <h1 class="text-h6">Services</h1>
      <v-spacer />
      <v-btn v-if="canEdit" color="primary" prepend-icon="mdi-plus" @click="open()">New</v-btn>
    </div>

    <v-alert v-if="error" class="mb-4" type="error" variant="tonal">{{ error }}</v-alert>
    <v-progress-linear v-if="loading" class="mb-2" indeterminate />

    <v-card border flat>
      <v-list>
        <v-list-item
          v-for="service in services"
          :key="service.id"
          :subtitle="priceLabel(service)"
          :title="service.name"
        >
          <template #append>
            <v-chip class="mr-2" size="x-small" variant="tonal">
              {{ service.default_duration_minutes }} min
            </v-chip>

            <v-chip
              v-if="service.is_taxable"
              class="mr-2"
              size="x-small"
              variant="tonal"
            >
              Taxable
            </v-chip>

            <v-chip
              v-if="!service.is_active"
              class="mr-2"
              color="warning"
              size="x-small"
              variant="tonal"
            >
              Inactive
            </v-chip>

            <v-btn
              v-if="canEdit"
              icon="mdi-pencil-outline"
              size="x-small"
              variant="text"
              @click="open(service)"
            />
          </template>
        </v-list-item>

        <v-list-item v-if="!loading && services.length === 0" title="No services yet" />
      </v-list>
    </v-card>

    <v-dialog v-model="dialog" max-width="520">
      <v-card>
        <v-card-title class="text-subtitle-1">
          {{ editingId ? 'Edit service' : 'New service' }}
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

          <v-text-field v-model="draft.name" density="compact" label="Name" />
          <v-textarea v-model="draft.description" density="compact" label="Description" rows="2" />

          <v-select
            v-model="draft.pricing_model"
            density="compact"
            :items="PRICING_MODELS"
            label="Pricing model"
          />

          <v-text-field
            v-model.number="draft.base_price"
            density="compact"
            :hint="draft.pricing_model === 'flat'
              ? 'The price.'
              : 'Minimum charge. A small job should not price below your callout cost.'"
            :label="draft.pricing_model === 'flat' ? 'Price ($)' : 'Minimum charge ($)'"
            persistent-hint
            type="number"
          />

          <v-text-field
            v-if="draft.pricing_model === 'hourly'"
            v-model.number="draft.hourly_rate"
            class="mt-3"
            density="compact"
            label="Hourly rate ($)"
            type="number"
          />

          <v-text-field
            v-if="draft.pricing_model === 'per_sqft'"
            v-model.number="draft.per_sqft_rate"
            class="mt-3"
            density="compact"
            hint="Locations without a square footage cannot be quoted for this."
            label="Rate per square foot ($)"
            persistent-hint
            step="0.001"
            type="number"
          />

          <v-text-field
            v-model.number="draft.default_duration_minutes"
            class="mt-3"
            density="compact"
            label="Default duration (minutes)"
            type="number"
          />

          <v-switch
            v-model="draft.is_taxable"
            color="primary"
            density="compact"
            hide-details
            hint="Sales tax is one rate per organization; this says where it applies."
            label="Taxable"
            persistent-hint
          />

          <v-switch
            v-model="draft.is_active"
            class="mt-3"
            color="primary"
            density="compact"
            hide-details
            label="Active"
          />
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
