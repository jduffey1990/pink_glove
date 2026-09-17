<script setup lang="ts">
  /**
   * The settings every invoice is built from: the tax rate, the no-access
   * fee, the numbering and the terms.
   *
   * Admin and owner only, which the API enforces independently -- hiding the
   * Save button is a courtesy, not a permission. The warning about issued
   * invoices is not decoration either: these values are frozen onto an invoice
   * when it is issued (ADR-026), so editing them changes what the *next* one
   * says and nothing that has already gone out.
   */
  import type { Organization } from '@/api/types'
  import { computed, ref } from 'vue'
  import { getCurrentOrganization, updateOrganization } from '@/api/endpoints'
  import { errorDetail } from '@/api/errors'
  import { noAccessFeeToApi, noAccessFeeToForm } from '@/lib/money'
  import { useSessionStore } from '@/stores/session'

  const session = useSessionStore()

  const organization = ref<Organization | null>(null)
  const loading = ref(false)
  const saving = ref(false)
  const error = ref('')
  const saved = ref(false)

  // The API enforces this independently; hiding the button is a courtesy.
  const canEdit = computed(() => session.isAdminOrHigher)

  const FEE_TYPES = [
    { value: 'none', title: 'No charge' },
    { value: 'flat', title: 'Flat amount' },
    { value: 'percent', title: 'Percentage of the visit' },
  ]

  /** Dollars in the form, cents and decimal strings on the wire. */
  const form = ref({
    tax_rate_percent: '0.000',
    no_access_fee_type: 'none' as Organization['no_access_fee_type'],
    /** Dollars when the fee is flat, a plain percentage when it is not. */
    no_access_fee_value: 0,
    invoice_prefix: 'INV',
    invoice_terms_days: 14,
    invoice_footer: '',
  })

  function fill (current: Organization): void {
    form.value = {
      tax_rate_percent: String(current.tax_rate_percent ?? '0.000'),
      no_access_fee_type: current.no_access_fee_type ?? 'none',
      no_access_fee_value: noAccessFeeToForm(
        current.no_access_fee_type ?? 'none',
        current.no_access_fee_value,
      ),
      invoice_prefix: current.invoice_prefix ?? 'INV',
      invoice_terms_days: current.invoice_terms_days ?? 14,
      invoice_footer: current.invoice_footer ?? '',
    }
  }

  async function load (): Promise<void> {
    loading.value = true
    error.value = ''

    try {
      organization.value = await getCurrentOrganization()
      fill(organization.value)
    } catch (error_) {
      error.value = errorDetail(error_) ?? 'Could not load these settings.'
    } finally {
      loading.value = false
    }
  }

  load()

  async function save (): Promise<void> {
    saving.value = true
    error.value = ''
    saved.value = false

    // Cents for a flat fee and a percentage otherwise -- see `lib/money`,
    // which has the spec for the two directions.
    const value = noAccessFeeToApi(
      form.value.no_access_fee_type ?? 'none',
      form.value.no_access_fee_value,
    )

    if (value === 'NaN') {
      error.value = 'That fee is not a number.'
      saving.value = false
      return
    }

    try {
      organization.value = await updateOrganization({
        tax_rate_percent: String(form.value.tax_rate_percent),
        no_access_fee_type: form.value.no_access_fee_type,
        no_access_fee_value: value,
        invoice_prefix: form.value.invoice_prefix,
        invoice_terms_days: form.value.invoice_terms_days,
        invoice_footer: form.value.invoice_footer,
      })
      fill(organization.value)
      saved.value = true
    } catch (error_) {
      error.value = errorDetail(error_) ?? 'Could not save these settings.'
    } finally {
      saving.value = false
    }
  }
</script>

<template>
  <v-container max-width="700">
    <v-btn
      class="mb-3"
      prepend-icon="mdi-arrow-left"
      size="small"
      :to="{ name: 'billing' }"
      variant="text"
    >
      Billing
    </v-btn>

    <h1 class="text-h6 mb-4">Billing settings</h1>

    <v-alert v-if="error" class="mb-4" type="error" variant="tonal">{{ error }}</v-alert>
    <v-alert v-if="saved" class="mb-4" type="success" variant="tonal">Saved.</v-alert>
    <v-progress-linear v-if="loading" class="mb-2" indeterminate />

    <v-alert class="mb-4" density="compact" type="info" variant="tonal">
      These are copied onto each invoice when it is issued. Changing one here
      affects the next invoice, never one that has already gone out.
    </v-alert>

    <v-card border flat>
      <v-card-text>
        <v-text-field
          v-model="form.tax_rate_percent"
          density="compact"
          hint="One rate for the whole organization. Each service says whether it applies."
          label="Sales tax rate (%)"
          persistent-hint
          :readonly="!canEdit"
          step="0.001"
          type="number"
        />

        <v-select
          v-model="form.no_access_fee_type"
          class="mt-4"
          density="compact"
          hint="What to charge when the crew turned up and could not get in."
          :items="FEE_TYPES"
          label="No-access fee"
          persistent-hint
          :readonly="!canEdit"
        />

        <v-text-field
          v-if="form.no_access_fee_type !== 'none'"
          v-model.number="form.no_access_fee_value"
          class="mt-4"
          density="compact"
          :label="form.no_access_fee_type === 'flat'
            ? 'Fee ($)'
            : 'Fee (% of the visit price)'"
          :readonly="!canEdit"
          step="0.01"
          type="number"
        />

        <v-divider class="my-5" />

        <v-text-field
          v-model="form.invoice_prefix"
          density="compact"
          hint="Numbers read like INV-0001."
          label="Invoice prefix"
          persistent-hint
          :readonly="!canEdit"
        />

        <v-text-field
          v-model.number="form.invoice_terms_days"
          class="mt-4"
          density="compact"
          hint="Days from issue to the due date."
          label="Payment terms (days)"
          persistent-hint
          :readonly="!canEdit"
          type="number"
        />

        <v-textarea
          v-model="form.invoice_footer"
          class="mt-4"
          density="compact"
          hint="Printed at the foot of every invoice."
          label="Invoice footer"
          persistent-hint
          :readonly="!canEdit"
          rows="2"
        />
      </v-card-text>

      <v-card-actions v-if="canEdit">
        <v-spacer />

        <v-btn
          color="primary"
          :loading="saving"
          text="Save"
          variant="flat"
          @click="save"
        />
      </v-card-actions>
    </v-card>
  </v-container>
</template>
