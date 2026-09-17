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
  import { computed, ref } from 'vue'
  import { useRouter } from 'vue-router'
  import { draftInvoice, listBillableJobs, listInvoices } from '@/api/endpoints'
  import { errorDetail } from '@/api/errors'
  import InvoiceStatusChip from '@/components/InvoiceStatusChip.vue'
  import { formatDateTime } from '@/lib/datetime'
  import { formatCents } from '@/lib/money'
  import { useSessionStore } from '@/stores/session'

  const session = useSessionStore()
  const router = useRouter()

  const timeZone = computed(() => session.organization?.timezone ?? 'UTC')

  const ready = ref<BillableJob[]>([])
  const invoices = ref<Invoice[]>([])
  const loading = ref(false)
  const error = ref('')

  /** Which visits are ticked, by id. Reset whenever the list is reloaded. */
  const selected = ref<Set<string>>(new Set())
  const creating = ref<string | null>(null)

  const statusFilter = ref<InvoiceStatus | null>(null)
  const paymentFilter = ref<PaymentState | null>(null)

  const STATUS_OPTIONS: { value: InvoiceStatus, title: string }[] = [
    { value: 'draft', title: 'Draft' },
    { value: 'issued', title: 'Issued' },
    { value: 'void', title: 'Void' },
  ]

  const PAYMENT_OPTIONS: { value: PaymentState, title: string }[] = [
    { value: 'unpaid', title: 'Unpaid' },
    { value: 'partial', title: 'Partly paid' },
    { value: 'paid', title: 'Paid' },
  ]

  /** The ready-to-invoice list, grouped the way an invoice is actually made. */
  const byCustomer = computed(() => {
    const groups = new Map<string, { name: string, jobs: BillableJob[] }>()

    for (const job of ready.value) {
      const group = groups.get(job.customer) ?? { name: job.customer_name, jobs: [] }
      group.jobs.push(job)
      groups.set(job.customer, group)
    }

    return [...groups.entries()].map(([id, group]) => ({ id, ...group }))
  })

  function selectedIn (customerId: string): string[] {
    const group = byCustomer.value.find(entry => entry.id === customerId)
    return (group?.jobs ?? []).filter(job => selected.value.has(job.id)).map(job => job.id)
  }

  function selectedTotal (customerId: string): number {
    const group = byCustomer.value.find(entry => entry.id === customerId)
    return (group?.jobs ?? [])
      .filter(job => selected.value.has(job.id))
      .reduce((sum, job) => sum + job.amount_cents, 0)
  }

  function toggle (jobId: string): void {
    // A new Set so the computed properties above see the change.
    const next = new Set(selected.value)
    if (next.has(jobId)) {
      next.delete(jobId)
    } else {
      next.add(jobId)
    }
    selected.value = next
  }

  function toggleAll (customerId: string): void {
    const group = byCustomer.value.find(entry => entry.id === customerId)
    const ids = (group?.jobs ?? []).map(job => job.id)
    const next = new Set(selected.value)
    const allOn = ids.every(id => next.has(id))

    for (const id of ids) {
      if (allOn) {
        next.delete(id)
      } else {
        next.add(id)
      }
    }
    selected.value = next
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

  async function create (customerId: string): Promise<void> {
    const jobs = selectedIn(customerId)
    if (jobs.length === 0) {
      return
    }

    creating.value = customerId
    error.value = ''

    try {
      const invoice = await draftInvoice(customerId, jobs)
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

        <v-btn size="x-small" variant="text" @click="toggleAll(group.id)">
          Select all
        </v-btn>
      </v-card-title>

      <v-list density="compact">
        <v-list-item
          v-for="job in group.jobs"
          :key="job.id"
          @click="toggle(job.id)"
        >
          <template #prepend>
            <v-checkbox-btn
              density="compact"
              :model-value="selected.has(job.id)"
              @click.stop="toggle(job.id)"
            />
          </template>

          <v-list-item-title>{{ job.description }}</v-list-item-title>

          <v-list-item-subtitle>
            {{ formatDateTime(job.scheduled_start, timeZone) }}
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
          {{ selectedIn(group.id).length }} selected · {{ formatCents(selectedTotal(group.id)) }}
        </span>

        <v-spacer />

        <v-btn
          color="primary"
          :disabled="selectedIn(group.id).length === 0"
          :loading="creating === group.id"
          text="Create invoice"
          variant="flat"
          @click="create(group.id)"
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
        :items="STATUS_OPTIONS"
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
        :items="PAYMENT_OPTIONS"
        label="Payment"
        max-width="160"
        variant="outlined"
        @update:model-value="load"
      />
    </div>

    <v-card border flat>
      <v-list>
        <v-list-item
          v-for="invoice in invoices"
          :key="invoice.id"
          :to="{ name: 'invoice', params: { id: invoice.id } }"
        >
          <v-list-item-title>
            {{ invoice.number || 'Draft' }} · {{ invoice.customer_detail.display_name }}
          </v-list-item-title>

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

        <v-list-item v-if="!loading && invoices.length === 0" title="No invoices yet" />
      </v-list>
    </v-card>
  </v-container>
</template>
