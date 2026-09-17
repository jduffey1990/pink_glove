<script setup lang="ts">
  /**
   * Billing: what is ready to invoice, and what has been invoiced.
   *
   * The ready-to-invoice list is grouped by customer because that is how an
   * invoice is made -- one customer, a batch of their visits. The amount
   * beside each visit comes from the server (`billable-jobs`), so what is
   * shown before pressing the button is what lands on the line.
   */
  import type { BillableJob, Invoice, InvoiceStatus, PaymentState } from '@/api/types'
  import type { BillableGroup } from '@/lib/billable'
  import { computed, ref } from 'vue'
  import { useRouter } from 'vue-router'
  import { draftInvoice, listBillableJobs, listInvoices } from '@/api/endpoints'
  import { errorDetail } from '@/api/errors'
  import InvoiceListItem from '@/components/InvoiceListItem.vue'
  import {
    groupByCustomer,
    selectedIn,
    selectedTotal,
    toggle,
    toggleAll,
  } from '@/lib/billable'
  import { formatDateTime } from '@/lib/datetime'
  import { INVOICE_STATUS_OPTIONS, PAYMENT_STATE_OPTIONS } from '@/lib/invoiceStatus'
  import { formatCents } from '@/lib/money'
  import { useSessionStore } from '@/stores/session'

  const session = useSessionStore()
  const router = useRouter()

  const ready = ref<BillableJob[]>([])
  const invoices = ref<Invoice[]>([])
  const loading = ref(false)
  const error = ref('')

  /** Which visits are ticked, by id. Reset whenever the list is reloaded. */
  const selected = ref<Set<string>>(new Set())
  const creating = ref<string | null>(null)

  const statusFilter = ref<InvoiceStatus | null>(null)
  const paymentFilter = ref<PaymentState | null>(null)

  /** The ready-to-invoice list, grouped the way an invoice is actually made. */
  const byCustomer = computed(() => groupByCustomer(ready.value))

  function selectedFor (group: BillableGroup): string[] {
    return selectedIn(group, selected.value)
  }

  function totalFor (group: BillableGroup): number {
    return selectedTotal(group, selected.value)
  }

  function tick (jobId: string): void {
    selected.value = toggle(selected.value, jobId)
  }

  function tickAll (group: BillableGroup): void {
    selected.value = toggleAll(selected.value, group)
  }

  async function load (): Promise<void> {
    loading.value = true
    error.value = ''

    try {
      const [billable, page] = await Promise.all([
        listBillableJobs(),
        listInvoices({
          ...(statusFilter.value ? { status: [statusFilter.value] } : {}),
          ...(paymentFilter.value ? { payment_state: paymentFilter.value } : {}),
          limit: 100,
        }),
      ])
      ready.value = billable
      invoices.value = page.results
      selected.value = new Set()
    } catch (error_) {
      error.value = errorDetail(error_) ?? 'Could not load billing.'
    } finally {
      loading.value = false
    }
  }

  load()

  async function create (group: BillableGroup): Promise<void> {
    const jobs = selectedFor(group)
    if (jobs.length === 0) {
      return
    }

    creating.value = group.id
    error.value = ''

    try {
      const invoice = await draftInvoice(group.id, jobs)
      router.push({ name: 'invoice', params: { id: invoice.id } })
    } catch (error_) {
      error.value = errorDetail(error_) ?? 'Could not open that invoice.'
      await load()
    } finally {
      creating.value = null
    }
  }
</script>

<template>
  <v-container max-width="1100">
    <div class="d-flex align-center ga-2 mb-4">
      <h1 class="text-h6">Billing</h1>
      <v-spacer />

      <v-btn
        prepend-icon="mdi-cog-outline"
        size="small"
        :to="{ name: 'billing-settings' }"
        variant="text"
      >
        Settings
      </v-btn>
    </div>

    <v-alert v-if="error" class="mb-4" type="error" variant="tonal">{{ error }}</v-alert>
    <v-progress-linear v-if="loading" class="mb-2" indeterminate />

    <!-- Ready to invoice -->
    <h2 class="text-subtitle-1 font-weight-medium mb-2">Ready to invoice</h2>

    <v-card v-if="byCustomer.length === 0" border class="mb-6" flat>
      <v-card-text class="text-medium-emphasis">
        Nothing finished is waiting to be billed.
      </v-card-text>
    </v-card>

    <v-card
      v-for="group in byCustomer"
      :key="group.id"
      border
      class="mb-3"
      flat
    >
      <v-card-title class="d-flex align-center text-subtitle-2 ga-2">
        <span>{{ group.name }}</span>
        <v-chip size="x-small" variant="tonal">{{ group.jobs.length }}</v-chip>
        <v-spacer />

        <v-btn size="x-small" variant="text" @click="tickAll(group)">
          Select all
        </v-btn>
      </v-card-title>

      <v-list density="compact">
        <v-list-item
          v-for="job in group.jobs"
          :key="job.id"
          @click="tick(job.id)"
        >
          <template #prepend>
            <v-checkbox-btn
              density="compact"
              :model-value="selected.has(job.id)"
              @click.stop="tick(job.id)"
            />
          </template>

          <v-list-item-title>{{ job.description }}</v-list-item-title>

          <v-list-item-subtitle>
            {{ formatDateTime(job.scheduled_start, session.timeZone) }}
            <v-chip
              v-if="job.status === 'no_access'"
              class="ml-2"
              color="warning"
              size="x-small"
              variant="tonal"
            >
              No access
            </v-chip>
          </v-list-item-subtitle>

          <template #append>
            <span class="text-body-2">{{ formatCents(job.amount_cents) }}</span>
          </template>
        </v-list-item>
      </v-list>

      <v-card-actions>
        <span class="text-body-2 text-medium-emphasis ml-2">
          {{ selectedFor(group).length }} selected · {{ formatCents(totalFor(group)) }}
        </span>

        <v-spacer />

        <v-btn
          color="primary"
          :disabled="selectedFor(group).length === 0"
          :loading="creating === group.id"
          text="Create invoice"
          variant="flat"
          @click="create(group)"
        />
      </v-card-actions>
    </v-card>

    <!-- Invoices -->
    <div class="d-flex align-center ga-2 mb-2 mt-8">
      <h2 class="text-subtitle-1 font-weight-medium">Invoices</h2>
      <v-spacer />

      <v-select
        v-model="statusFilter"
        clearable
        density="compact"
        hide-details
        :items="INVOICE_STATUS_OPTIONS"
        label="Status"
        max-width="160"
        variant="outlined"
        @update:model-value="load"
      />

      <v-select
        v-model="paymentFilter"
        clearable
        density="compact"
        hide-details
        :items="PAYMENT_STATE_OPTIONS"
        label="Payment"
        max-width="160"
        variant="outlined"
        @update:model-value="load"
      />
    </div>

    <v-card border flat>
      <v-list>
        <InvoiceListItem
          v-for="invoice in invoices"
          :key="invoice.id"
          :invoice="invoice"
          show-customer
        />

        <v-list-item v-if="!loading && invoices.length === 0" title="No invoices yet" />
      </v-list>
    </v-card>
  </v-container>
</template>
