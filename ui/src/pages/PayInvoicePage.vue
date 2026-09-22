<script setup lang="ts">
  /**
   * The customer's pay page, reached from the link in the invoice email.
   *
   * No session and no organization: the signed token in the URL is the
   * credential, and the API answers 404 for a bad or expired one. Everything
   * shown is the server's public view of the invoice, and whether the button
   * appears is the server's `payable_reason`, never worked out here.
   *
   * Back from Stripe with `?paid=1`, the webhook usually lands a moment after
   * the customer does, so the page polls briefly before saying anything.
   */
  import type { PublicInvoice } from '@/api/types'
  import { computed, ref } from 'vue'
  import { useRoute } from 'vue-router'
  import { getPublicInvoice, openCheckout } from '@/api/endpoints'
  import { errorDetail, statusOf } from '@/api/errors'
  import { formatDayLabel } from '@/lib/datetime'
  import { formatCents, formatPercent } from '@/lib/money'
  import { pollUntilPaid } from '@/lib/payPoll'

  const route = useRoute()
  const token = computed(() => String(route.params.token ?? ''))

  const invoice = ref<PublicInvoice | null>(null)
  const loading = ref(true)
  const error = ref('')
  const paying = ref(false)

  /** What the page says after a return from Stripe. */
  const outcome = ref<'checking' | 'paid' | 'processing' | 'cancelled' | null>(null)

  const paid = computed(() => invoice.value?.payment_state === 'paid')

  async function load (): Promise<void> {
    loading.value = true
    error.value = ''
    try {
      invoice.value = await getPublicInvoice(token.value)
    } catch (error_) {
      error.value = statusOf(error_) === 404
        ? 'That link is not valid, or has expired. Ask for a new one.'
        : (errorDetail(error_) ?? 'Could not load this invoice.')
    } finally {
      loading.value = false
    }
  }

  async function settleAfterReturn (): Promise<void> {
    if (route.query.cancelled) {
      outcome.value = 'cancelled'
      return
    }
    if (!route.query.paid || invoice.value === null) {
      return
    }
    if (paid.value) {
      outcome.value = 'paid'
      return
    }
    outcome.value = 'checking'
    const result = await pollUntilPaid(async () => {
      invoice.value = await getPublicInvoice(token.value)
      return invoice.value.payment_state === 'paid'
    })
    outcome.value = result === 'paid' ? 'paid' : 'processing'
  }

  load().then(settleAfterReturn)

  async function pay (): Promise<void> {
    paying.value = true
    error.value = ''
    try {
      window.location.assign(await openCheckout(token.value))
    } catch (error_) {
      error.value = errorDetail(error_) ?? 'Could not start the payment.'
      paying.value = false
      // A 409 means the invoice changed under us; show the current state.
      await load()
    }
  }
</script>

<template>
  <v-container class="fill-height" max-width="560">
    <v-card class="w-100 pa-2" flat>
      <v-card-item>
        <v-card-title class="text-h6">
          {{ invoice ? `Invoice ${invoice.number}` : 'Invoice' }}
        </v-card-title>

        <v-card-subtitle v-if="invoice">from {{ invoice.organization_name }}</v-card-subtitle>
      </v-card-item>

      <v-card-text>
        <v-progress-linear v-if="loading" class="mb-3" indeterminate />

        <v-alert v-if="error" class="mb-4" type="error" variant="tonal">{{ error }}</v-alert>

        <v-alert
          v-if="outcome === 'checking'"
          class="mb-4"
          type="info"
          variant="tonal"
        >
          <v-progress-circular class="mr-2" indeterminate size="16" width="2" />
          Confirming your payment...
        </v-alert>

        <v-alert v-else-if="outcome === 'paid'" class="mb-4" type="success" variant="tonal">
          Payment received. Thank you!
        </v-alert>

        <v-alert v-else-if="outcome === 'processing'" class="mb-4" type="info" variant="tonal">
          Your payment is processing. You will get a receipt from Stripe by email, and
          this invoice will show as paid shortly.
        </v-alert>

        <v-alert v-else-if="outcome === 'cancelled'" class="mb-4" type="info" variant="tonal">
          No payment was made.
        </v-alert>

        <template v-if="invoice">
          <div v-if="invoice.bill_to_name" class="mb-3">{{ invoice.bill_to_name }}</div>

          <v-table density="compact">
            <tbody>
              <tr v-for="(line, index) in invoice.lines" :key="index">
                <td>{{ line.description }}</td>
                <td class="text-right">{{ formatCents(line.amount_cents) }}</td>
              </tr>

              <tr>
                <td class="text-medium-emphasis">Subtotal</td>
                <td class="text-right">{{ formatCents(invoice.subtotal_cents) }}</td>
              </tr>

              <tr v-if="invoice.tax_cents > 0">
                <td class="text-medium-emphasis">
                  Tax ({{ formatPercent(invoice.tax_rate_percent) }})
                </td>

                <td class="text-right">{{ formatCents(invoice.tax_cents) }}</td>
              </tr>

              <tr class="font-weight-medium">
                <td>Total</td>
                <td class="text-right">{{ formatCents(invoice.total_cents) }}</td>
              </tr>

              <tr v-if="invoice.paid_cents > 0">
                <td class="text-medium-emphasis">Paid</td>
                <td class="text-right">{{ formatCents(invoice.paid_cents) }}</td>
              </tr>

              <tr class="font-weight-medium">
                <td>Balance due</td>
                <td class="text-right">{{ formatCents(invoice.balance_cents) }}</td>
              </tr>
            </tbody>
          </v-table>

          <p v-if="paid" class="mt-4">This invoice is paid in full. Thank you.</p>

          <p v-else-if="invoice.due_on" class="mt-4">
            Payment is due by <strong>{{ formatDayLabel(invoice.due_on) }}</strong>.
          </p>

          <p v-if="invoice.notes" class="mt-3" style="white-space: pre-line">{{ invoice.notes }}</p>

          <p v-if="invoice.footer" class="mt-3 text-medium-emphasis" style="white-space: pre-line">
            {{ invoice.footer }}
          </p>
        </template>
      </v-card-text>

      <v-card-actions v-if="invoice && !paid">
        <v-spacer />

        <v-btn
          v-if="invoice.payable_reason === null"
          color="primary"
          :loading="paying"
          prepend-icon="mdi-credit-card-outline"
          size="large"
          :text="`Pay ${formatCents(invoice.balance_cents)} by card`"
          variant="flat"
          @click="pay"
        />

        <span v-else class="text-medium-emphasis">{{ invoice.payable_reason }}</span>
      </v-card-actions>
    </v-card>
  </v-container>
</template>
