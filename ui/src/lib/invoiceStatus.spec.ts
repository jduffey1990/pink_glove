import type { Invoice } from '@/api/types'
import { describe, expect, it } from 'vitest'
import { invoiceChip, offers } from './invoiceStatus'

function invoice (overrides: Partial<Invoice> = {}): Invoice {
  return {
    status: 'issued',
    payment_state: 'unpaid',
    available_actions: [],
    ...overrides,
  } as Invoice
}

describe('invoiceChip', () => {
  it('shows the payment state once the invoice is issued', () => {
    expect(invoiceChip(invoice({ payment_state: 'paid' })).label).toBe('Paid')
    expect(invoiceChip(invoice({ payment_state: 'partial' })).label).toBe('Partly paid')
    expect(invoiceChip(invoice({ payment_state: 'unpaid' })).label).toBe('Unpaid')
  })

  it('says Draft rather than Unpaid before it is issued', () => {
    // Nobody has been asked for anything yet, so "unpaid" would be misleading.
    expect(invoiceChip(invoice({ status: 'draft' })).label).toBe('Draft')
  })

  it('says Void however the ledger stood before it was voided', () => {
    expect(invoiceChip(invoice({ status: 'void', payment_state: 'paid' })).label).toBe('Void')
  })

  it('colours a paid invoice differently from an unpaid one', () => {
    expect(invoiceChip(invoice({ payment_state: 'paid' })).color)
      .not
      .toBe(invoiceChip(invoice({ payment_state: 'unpaid' })).color)
  })
})

describe('offers', () => {
  it('reads the server list rather than guessing from the status', () => {
    const issued = invoice({ available_actions: ['send', 'record_payment'] })

    expect(offers(issued, 'send')).toBe(true)
    expect(offers(issued, 'record_payment')).toBe(true)
    expect(offers(issued, 'void')).toBe(false)
  })

  it('offers nothing when the server offers nothing', () => {
    const voided = invoice({ status: 'void', available_actions: [] })

    for (const action of ['edit', 'delete', 'issue', 'void', 'send', 'record_payment'] as const) {
      expect(offers(voided, action)).toBe(false)
    }
  })

  it('does not infer an action from the status', () => {
    // An issued invoice whose payments the server has already seen offers no
    // void, and the page must not put the button back.
    const paid = invoice({ payment_state: 'paid', available_actions: ['send'] })

    expect(offers(paid, 'void')).toBe(false)
  })
})
