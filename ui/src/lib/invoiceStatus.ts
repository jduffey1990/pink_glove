/**
 * How an invoice's state is worded and coloured, in one place.
 *
 * Presentation only, as with `jobStatus.ts`. What an invoice will *accept*
 * comes from the server on `available_actions`, and whether it is paid comes
 * from `payment_state` -- both derived from the ledger there and never
 * recomputed here (ADR-023, ADR-025).
 */

import type { Invoice, InvoiceAction, InvoiceStatus, PaymentMethod, PaymentState } from '@/api/types'

export const INVOICE_STATUS_LOOK: Record<InvoiceStatus, { label: string, color: string }> = {
  draft: { label: 'Draft', color: 'grey' },
  issued: { label: 'Issued', color: 'info' },
  void: { label: 'Void', color: 'error' },
}

export const PAYMENT_STATE_LOOK: Record<PaymentState, { label: string, color: string }> = {
  unpaid: { label: 'Unpaid', color: 'warning' },
  partial: { label: 'Partly paid', color: 'info' },
  paid: { label: 'Paid', color: 'success' },
}

export const PAYMENT_METHOD_LABEL: Record<PaymentMethod, string> = {
  cash: 'Cash',
  check: 'Check',
  money_order: 'Money order',
  zelle: 'Zelle',
  card: 'Card',
  other: 'Other',
}

export const PAYMENT_METHOD_OPTIONS = (
  Object.keys(PAYMENT_METHOD_LABEL) as PaymentMethod[]
).map(value => ({ value, title: PAYMENT_METHOD_LABEL[value] }))

/**
 * The same labels as the chips use, shaped for a select.
 *
 * Derived rather than re-typed: a filter dropdown that says "Partly paid"
 * while the chip beside it says something else is the kind of drift this file
 * exists to prevent.
 */
export const INVOICE_STATUS_OPTIONS = (
  Object.keys(INVOICE_STATUS_LOOK) as InvoiceStatus[]
).map(value => ({ value, title: INVOICE_STATUS_LOOK[value].label }))

export const PAYMENT_STATE_OPTIONS = (
  Object.keys(PAYMENT_STATE_LOOK) as PaymentState[]
).map(value => ({ value, title: PAYMENT_STATE_LOOK[value].label }))

/**
 * What an invoice is showing right now: its payment state once issued, its
 * own status while it is not.
 *
 * A draft is not "unpaid" in any useful sense -- nobody has been asked for
 * anything yet -- so the chip says Draft, and Void says Void however the
 * ledger stood before it was voided.
 */
export function invoiceChip (invoice: Invoice): { label: string, color: string } {
  if (invoice.status !== 'issued') {
    return INVOICE_STATUS_LOOK[invoice.status]
  }
  return PAYMENT_STATE_LOOK[invoice.payment_state]
}

/**
 * Whether the server is currently offering this action.
 *
 * The page asks this rather than working it out from the status, so that a
 * rule change on the server reaches the buttons without a matching edit here.
 */
export function offers (invoice: Invoice, action: InvoiceAction): boolean {
  return invoice.available_actions.includes(action)
}
