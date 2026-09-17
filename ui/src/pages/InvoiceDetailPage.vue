<script setup lang="ts">
  /**
   * One invoice: its lines, its totals, its ledger, and whatever the server
   * currently lets you do to it.
   *
   * Every button is drawn from `available_actions` rather than from the
   * status (ADR-023). That is not a nicety: whether an issued invoice may be
   * voided depends on whether money points at it, which is a question about
   * the ledger that only the server can answer.
   */
  import type { Invoice, InvoiceLine, PaymentMethod } from '@/api/types'
  import { computed, ref } from 'vue'
  import { useRoute, useRouter } from 'vue-router'
  import {
    addInvoiceLine,
    deleteInvoice,
    deleteInvoiceLine,
    getInvoice,
    issueInvoice,
    recordPayment,
    repriceInvoiceLine,
    sendInvoice,
    updateInvoice,
    voidInvoice,
    voidPayment,
  } from '@/api/endpoints'
  import { errorDetail } from '@/api/errors'
  import InvoiceStatusChip from '@/components/InvoiceStatusChip.vue'
  import { todayIn } from '@/lib/datetime'
  import { offers, PAYMENT_METHOD_LABEL, PAYMENT_METHOD_OPTIONS } from '@/lib/invoiceStatus'
  import { centsToDollars, dollarsToCents, formatCents, formatPercent } from '@/lib/money'
  import { useSessionStore } from '@/stores/session'

  const session = useSessionStore()
  const route = useRoute()
  const router = useRouter()

  const timeZone = computed(() => session.organization?.timezone ?? 'UTC')

  const invoice = ref<Invoice | null>(null)
  const loading = ref(false)
  const error = ref('')
  const busy = ref('')

  const notes = ref('')

  // --- dialogs -------------------------------------------------------------

  const paymentDialog = ref(false)
  const paymentError = ref('')
  const payment = ref({
    method: 'check' as PaymentMethod,
    amount: 0,
    tip: 0,
    received_on: '',
    reference: '',
  })

  const adjustmentDialog = ref(false)
  const adjustmentError = ref('')
  const adjustment = ref({ description: '', amount: 0, is_taxable: false })

  const reasonDialog = ref<'invoice' | 'payment' | null>(null)
  const reasonError = ref('')
  const reason = ref('')
  const voidingPaymentId = ref<string | null>(null)

  // --- loading -------------------------------------------------------------

  async function load (): Promise<void> {
    loading.value = true
    error.value = ''

    try {
      invoice.value = await getInvoice(route.params.id as string)
      notes.value = invoice.value.notes ?? ''
    } catch (error_) {
      error.value = errorDetail(error_) ?? 'Could not load that invoice.'
    } finally {
      loading.value = false
    }
  }

  load()

  const livePayments = computed(() => invoice.value?.payments ?? [])
  const canEdit = computed(() => invoice.value !== null && offers(invoice.value, 'edit'))

  function may (action: Parameters<typeof offers>[1]): boolean {
    return invoice.value !== null && offers(invoice.value, action)
  }

  /**
   * Run a server action and take its answer as the new truth.
   *
   * Every one of these returns the whole invoice, including a fresh
   * `available_actions`, so the page never has to reason about what the action
   * did to what else is allowed.
   */
  async function run (name: string, action: () => Promise<Invoice>): Promise<boolean> {
    busy.value = name
    error.value = ''

    try {
      invoice.value = await action()
      return true
    } catch (error_) {
      error.value = errorDetail(error_) ?? 'That did not work.'
      // A 409 means this page is out of date about the record, so re-read it
      // rather than leaving stale buttons on screen.
      await load()
      return false
    } finally {
      busy.value = ''
    }
  }

  // --- actions -------------------------------------------------------------

  async function saveNotes (): Promise<void> {
    if (invoice.value === null) {
      return
    }
    const id = invoice.value.id
    await run('notes', () => updateInvoice(id, { notes: notes.value }))
  }

  async function issue (): Promise<void> {
    const id = invoice.value?.id
    if (id) {
      await run('issue', () => issueInvoice(id))
    }
  }

  async function send (): Promise<void> {
    const id = invoice.value?.id
    if (id) {
      await run('send', () => sendInvoice(id))
    }
  }

  async function discard (): Promise<void> {
    const id = invoice.value?.id
    if (!id) {
      return
    }

    busy.value = 'delete'
    try {
      await deleteInvoice(id)
      router.push({ name: 'billing' })
    } catch (error_) {
      error.value = errorDetail(error_) ?? 'Could not delete that draft.'
    } finally {
      busy.value = ''
    }
  }

  function openReason (which: 'invoice' | 'payment', paymentId?: string): void {
    reason.value = ''
    reasonError.value = ''
    voidingPaymentId.value = paymentId ?? null
    reasonDialog.value = which
  }

  async function confirmVoid (): Promise<void> {
    if (reason.value.trim() === '') {
      reasonError.value = 'Say why.'
      return
    }

    const id = invoice.value?.id
    if (!id) {
      return
    }

    if (reasonDialog.value === 'payment' && voidingPaymentId.value) {
      const paymentId = voidingPaymentId.value
      busy.value = 'void-payment'
      try {
        await voidPayment(paymentId, reason.value)
        reasonDialog.value = null
        await load()
      } catch (error_) {
        reasonError.value = errorDetail(error_) ?? 'Could not void that payment.'
      } finally {
        busy.value = ''
      }
      return
    }

    const ok = await run('void', () => voidInvoice(id, reason.value))
    reasonDialog.value = ok ? null : null
  }

  function openPayment (): void {
    paymentError.value = ''
    payment.value = {
      method: 'check',
      // Pre-filled with the balance, which is what is being paid nine times
      // out of ten. Anything larger is refused by the server with the balance
      // named, so the field is not the guard.
      amount: centsToDollars(invoice.value?.balance_cents ?? 0),
      tip: 0,
      received_on: todayIn(timeZone.value),
      reference: '',
    }
    paymentDialog.value = true
  }

  async function savePayment (): Promise<void> {
    const id = invoice.value?.id
    if (!id) {
      return
    }

    const amount = dollarsToCents(payment.value.amount)
    const tip = dollarsToCents(payment.value.tip)

    if (Number.isNaN(amount) || amount <= 0) {
      paymentError.value = 'How much was paid?'
      return
    }
    if (Number.isNaN(tip) || tip < 0) {
      paymentError.value = 'That tip is not a number.'
      return
    }

    busy.value = 'payment'
    paymentError.value = ''

    try {
      await recordPayment({
        invoice: id,
        method: payment.value.method,
        amount_cents: amount,
        tip_cents: tip,
        received_on: payment.value.received_on,
        reference: payment.value.reference,
      })
      paymentDialog.value = false
      await load()
    } catch (error_) {
      paymentError.value = errorDetail(error_) ?? 'Could not record that payment.'
    } finally {
      busy.value = ''
    }
  }

  function openAdjustment (): void {
    adjustmentError.value = ''
    adjustment.value = { description: '', amount: 0, is_taxable: false }
    adjustmentDialog.value = true
  }

  async function saveAdjustment (): Promise<void> {
    const id = invoice.value?.id
    if (!id) {
      return
    }

    const amount = dollarsToCents(adjustment.value.amount)
    if (Number.isNaN(amount) || amount === 0) {
      adjustmentError.value = 'How much, and which way?'
      return
    }
    if (adjustment.value.description.trim() === '') {
      adjustmentError.value = 'Say what this is for -- the customer reads it.'
      return
    }

    busy.value = 'adjustment'
    adjustmentError.value = ''

    try {
      await addInvoiceLine({
        invoice: id,
        kind: 'adjustment',
        description: adjustment.value.description,
        amount_cents: amount,
        is_taxable: adjustment.value.is_taxable,
      })
      adjustmentDialog.value = false
      await load()
    } catch (error_) {
      adjustmentError.value = errorDetail(error_) ?? 'Could not add that line.'
    } finally {
      busy.value = ''
    }
  }

  async function removeLine (line: InvoiceLine): Promise<void> {
    busy.value = line.id
    error.value = ''

    try {
      await deleteInvoiceLine(line.id)
      await load()
    } catch (error_) {
      error.value = errorDetail(error_) ?? 'Could not remove that line.'
      await load()
    } finally {
      busy.value = ''
    }
  }

  async function reprice (line: InvoiceLine): Promise<void> {
    busy.value = line.id
    error.value = ''

    try {
      await repriceInvoiceLine(line.id)
      await load()
    } catch (error_) {
      // Usually a 409: the service is not hourly, or nobody recorded any time.
      // The server's own sentence says which, so it is shown as it stands.
      error.value = errorDetail(error_) ?? 'Could not re-price that line.'
    } finally {
      busy.value = ''
    }
  }
</script>

<template>
  <v-container max-width="900">
    <v-btn
      class="mb-3"
      prepend-icon="mdi-arrow-left"
      size="small"
      :to="{ name: 'billing' }"
      variant="text"
    >
      Billing
    </v-btn>

    <v-alert v-if="error" class="mb-4" type="error" variant="tonal">{{ error }}</v-alert>
    <v-progress-linear v-if="loading" class="mb-2" indeterminate />

    <template v-if="invoice">
      <div class="d-flex align-center flex-wrap ga-2 mb-4">
        <h1 class="text-h6">{{ invoice.number || 'Draft invoice' }}</h1>
        <InvoiceStatusChip :invoice="invoice" />

        <v-chip v-if="invoice.is_overdue" color="error" size="small" variant="tonal">
          Overdue
        </v-chip>

        <v-spacer />

        <v-btn
          v-if="may('issue')"
          color="primary"
          :loading="busy === 'issue'"
          size="small"
          text="Issue"
          variant="flat"
          @click="issue"
        />

        <v-btn
          v-if="may('record_payment')"
          color="primary"
          size="small"
          text="Record payment"
          variant="flat"
          @click="openPayment"
        />

        <v-btn
          v-if="may('send')"
          :loading="busy === 'send'"
          prepend-icon="mdi-email-outline"
          size="small"
          text="Email"
          variant="tonal"
          @click="send"
        />

        <v-btn
          v-if="may('void')"
          color="error"
          size="small"
          text="Void"
          variant="text"
          @click="openReason('invoice')"
        />

        <v-btn
          v-if="may('delete')"
          color="error"
          :loading="busy === 'delete'"
          size="small"
          text="Delete draft"
          variant="text"
          @click="discard"
        />
      </div>

      <v-alert
        v-if="invoice.status === 'void'"
        class="mb-4"
        type="warning"
        variant="tonal"
      >
        Voided. {{ invoice.void_reason }}
      </v-alert>

      <v-row>
        <v-col cols="12" md="8">
          <!-- Lines -->
          <v-card border class="mb-4" flat>
            <v-card-title class="d-flex align-center text-subtitle-2">
              Lines
              <v-spacer />

              <v-btn
                v-if="canEdit"
                prepend-icon="mdi-plus"
                size="x-small"
                variant="text"
                @click="openAdjustment"
              >
                Adjustment
              </v-btn>
            </v-card-title>

            <v-list density="compact">
              <v-list-item v-for="line in invoice.lines" :key="line.id">
                <v-list-item-title>{{ line.description }}</v-list-item-title>
                <v-list-item-subtitle v-if="line.is_taxable">Taxable</v-list-item-subtitle>

                <template #append>
                  <div class="d-flex align-center ga-1">
                    <span class="text-body-2 text-no-wrap">
                      {{ formatCents(line.amount_cents) }}
                    </span>

                    <template v-if="canEdit && line.kind === 'visit'">
                      <v-btn
                        icon="mdi-timer-outline"
                        :loading="busy === line.id"
                        size="x-small"
                        title="Re-price from the hours worked"
                        variant="text"
                        @click="reprice(line)"
                      />
                    </template>

                    <v-btn
                      v-if="canEdit"
                      icon="mdi-close"
                      :loading="busy === line.id"
                      size="x-small"
                      variant="text"
                      @click="removeLine(line)"
                    />
                  </div>
                </template>
              </v-list-item>

              <v-list-item v-if="invoice.lines.length === 0" title="No lines yet" />
            </v-list>

            <v-divider />

            <v-card-text>
              <div class="d-flex justify-space-between">
                <span>Subtotal</span>
                <span>{{ formatCents(invoice.subtotal_cents) }}</span>
              </div>

              <div v-if="invoice.tax_cents > 0" class="d-flex justify-space-between mt-1">
                <span>Tax ({{ formatPercent(invoice.tax_rate_percent) }})</span>
                <span>{{ formatCents(invoice.tax_cents) }}</span>
              </div>

              <v-divider class="my-2" />

              <div class="d-flex justify-space-between font-weight-medium">
                <span>Total</span>
                <span>{{ formatCents(invoice.total_cents) }}</span>
              </div>

              <div
                v-if="invoice.status === 'issued'"
                class="d-flex justify-space-between mt-1 text-medium-emphasis"
              >
                <span>Balance</span>
                <span>{{ formatCents(invoice.balance_cents) }}</span>
              </div>
            </v-card-text>
          </v-card>

          <!-- Payments -->
          <v-card border class="mb-4" flat>
            <v-card-title class="text-subtitle-2">Payments</v-card-title>

            <v-list density="compact">
              <v-list-item v-for="entry in livePayments" :key="entry.id">
                <v-list-item-title :class="entry.is_void ? 'text-decoration-line-through' : ''">
                  {{ PAYMENT_METHOD_LABEL[entry.method] }}
                  {{ formatCents(entry.amount_cents) }}
                  <span v-if="(entry.tip_cents ?? 0) > 0" class="text-medium-emphasis">
                    (+ {{ formatCents(entry.tip_cents) }} tip)
                  </span>
                </v-list-item-title>

                <v-list-item-subtitle>
                  {{ entry.received_on }}
                  <template v-if="entry.reference"> · {{ entry.reference }}</template>
                  <template v-if="entry.recorded_by_name"> · {{ entry.recorded_by_name }}</template>
                  <template v-if="entry.is_void"> · voided: {{ entry.void_reason }}</template>
                </v-list-item-subtitle>

                <template #append>
                  <v-btn
                    v-if="!entry.is_void"
                    color="error"
                    size="x-small"
                    text="Void"
                    variant="text"
                    @click="openReason('payment', entry.id)"
                  />
                </template>
              </v-list-item>

              <v-list-item v-if="livePayments.length === 0" title="Nothing received yet" />
            </v-list>
          </v-card>
        </v-col>

        <v-col cols="12" md="4">
          <v-card border class="mb-4" flat>
            <v-card-title class="text-subtitle-2">Bill to</v-card-title>

            <v-card-text>
              <div>{{ invoice.bill_to_name || invoice.customer_detail.display_name }}</div>
              <div class="text-medium-emphasis">{{ invoice.bill_to_email }}</div>

              <div class="text-medium-emphasis" style="white-space: pre-line">
                {{ invoice.bill_to_address }}
              </div>

              <v-divider class="my-3" />

              <div v-if="invoice.issued_on" class="text-body-2">
                Issued {{ invoice.issued_on }}<br>
                Due {{ invoice.due_on }}
              </div>

              <div v-else class="text-body-2 text-medium-emphasis">
                Not yet issued. Nothing here is fixed until it is.
              </div>

              <div v-if="invoice.sent_at" class="text-body-2 text-medium-emphasis mt-2">
                Emailed.
              </div>
            </v-card-text>
          </v-card>

          <v-card border flat>
            <v-card-title class="text-subtitle-2">Notes to the customer</v-card-title>

            <v-card-text>
              <v-textarea
                v-model="notes"
                density="compact"
                :readonly="!canEdit"
                rows="3"
                variant="outlined"
              />

              <v-btn
                v-if="canEdit"
                block
                :loading="busy === 'notes'"
                size="small"
                text="Save notes"
                variant="tonal"
                @click="saveNotes"
              />
            </v-card-text>
          </v-card>
        </v-col>
      </v-row>
    </template>

    <!-- Record payment -->
    <v-dialog v-model="paymentDialog" max-width="440">
      <v-card>
        <v-card-title class="text-subtitle-1">Record a payment</v-card-title>

        <v-card-text>
          <v-alert
            v-if="paymentError"
            class="mb-3"
            density="compact"
            type="error"
            variant="tonal"
          >
            {{ paymentError }}
          </v-alert>

          <v-select
            v-model="payment.method"
            density="compact"
            :items="PAYMENT_METHOD_OPTIONS"
            label="Method"
          />

          <v-text-field
            v-model.number="payment.amount"
            density="compact"
            label="Amount ($)"
            step="0.01"
            type="number"
          />

          <v-text-field
            v-model.number="payment.tip"
            density="compact"
            hint="Kept apart from the bill: a tip is not revenue and settles nothing."
            label="Tip ($)"
            persistent-hint
            step="0.01"
            type="number"
          />

          <v-text-field
            v-model="payment.received_on"
            class="mt-3"
            density="compact"
            label="Received on"
            type="date"
          />

          <v-text-field
            v-model="payment.reference"
            density="compact"
            label="Reference"
            placeholder="Check number, Zelle confirmation"
          />
        </v-card-text>

        <v-card-actions>
          <v-spacer />
          <v-btn text="Cancel" @click="paymentDialog = false" />

          <v-btn
            color="primary"
            :loading="busy === 'payment'"
            text="Record"
            variant="flat"
            @click="savePayment"
          />
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- Adjustment -->
    <v-dialog v-model="adjustmentDialog" max-width="440">
      <v-card>
        <v-card-title class="text-subtitle-1">Add an adjustment</v-card-title>

        <v-card-text>
          <v-alert
            v-if="adjustmentError"
            class="mb-3"
            density="compact"
            type="error"
            variant="tonal"
          >
            {{ adjustmentError }}
          </v-alert>

          <v-text-field
            v-model="adjustment.description"
            density="compact"
            hint="The customer reads this."
            label="Description"
            persistent-hint
          />

          <v-text-field
            v-model.number="adjustment.amount"
            class="mt-3"
            density="compact"
            hint="Negative for a discount."
            label="Amount ($)"
            persistent-hint
            step="0.01"
            type="number"
          />

          <v-switch
            v-model="adjustment.is_taxable"
            color="primary"
            density="compact"
            hide-details
            label="Taxable"
          />
        </v-card-text>

        <v-card-actions>
          <v-spacer />
          <v-btn text="Cancel" @click="adjustmentDialog = false" />

          <v-btn
            color="primary"
            :loading="busy === 'adjustment'"
            text="Add"
            variant="flat"
            @click="saveAdjustment"
          />
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- Void, with its reason -->
    <v-dialog max-width="440" :model-value="reasonDialog !== null" @update:model-value="reasonDialog = null">
      <v-card>
        <v-card-title class="text-subtitle-1">
          {{ reasonDialog === 'payment' ? 'Void this payment' : 'Void this invoice' }}
        </v-card-title>

        <v-card-text>
          <v-alert
            v-if="reasonError"
            class="mb-3"
            density="compact"
            type="error"
            variant="tonal"
          >
            {{ reasonError }}
          </v-alert>

          <p class="text-body-2 text-medium-emphasis mb-3">
            The record stays and keeps its number. The reason is part of it.
          </p>

          <v-text-field v-model="reason" density="compact" label="Reason" />
        </v-card-text>

        <v-card-actions>
          <v-spacer />
          <v-btn text="Cancel" @click="reasonDialog = null" />

          <v-btn
            color="error"
            :loading="busy === 'void' || busy === 'void-payment'"
            text="Void"
            variant="flat"
            @click="confirmVoid"
          />
        </v-card-actions>
      </v-card>
    </v-dialog>
  </v-container>
</template>
