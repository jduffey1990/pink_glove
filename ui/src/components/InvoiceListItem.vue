<script setup lang="ts">
  /**
   * One invoice in a list.
   *
   * Billing and the customer page show the same row, and were showing it in
   * the same markup written twice. The number-or-"Draft", the dates, the
   * overdue chip and the total are one decision about how an invoice reads in
   * a list.
   */
  import type { Invoice } from '@/api/types'
  import InvoiceStatusChip from '@/components/InvoiceStatusChip.vue'
  import { formatDayLabel } from '@/lib/datetime'
  import { formatCents } from '@/lib/money'

  defineProps<{ invoice: Invoice, showCustomer?: boolean }>()
</script>

<template>
  <v-list-item :to="{ name: 'invoice', params: { id: invoice.id } }">
    <v-list-item-title>
      {{ invoice.number || 'Draft' }}
      <template v-if="showCustomer"> · {{ invoice.customer_detail.display_name }}</template>
    </v-list-item-title>

    <v-list-item-subtitle>
      <template v-if="invoice.issued_on">
        Issued {{ formatDayLabel(invoice.issued_on) }} ·
        due {{ formatDayLabel(invoice.due_on!) }}
      </template>

      <template v-else>Not yet issued</template>
    </v-list-item-subtitle>

    <template #append>
      <div class="d-flex align-center ga-2">
        <v-chip v-if="invoice.is_overdue" color="error" size="x-small" variant="tonal">
          Overdue
        </v-chip>

        <InvoiceStatusChip :invoice="invoice" />

        <span class="text-body-2 text-no-wrap">{{ formatCents(invoice.total_cents) }}</span>
      </div>
    </template>
  </v-list-item>
</template>
