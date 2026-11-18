/**
 * The pay page, mounted. What it pins (ADR-036): the token is read from the
 * address's fragment and sent in a request body, never in an API URL; an
 * address without one is refused without asking; a dead one shows the
 * page's invalid-link sentence.
 */

import { flushPromises, mount } from '@vue/test-utils'
import MockAdapter from 'axios-mock-adapter'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'
import { createVuetify } from 'vuetify'
import { api } from '@/api/client'
import { INCOMPLETE_LINK } from '@/lib/linkToken'
import { stubResizeObserver } from '@/lib/team.fixtures'
import PayInvoicePage from './PayInvoicePage.vue'

const INVOICE = '/api/billing/pay/invoice/'
const CHECKOUT = '/api/billing/pay/checkout/'

let mock: MockAdapter

async function mountAt (path: string) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/pay', name: 'pay', component: PayInvoicePage }],
  })
  router.push(path)
  await router.isReady()
  setActivePinia(createPinia())
  const wrapper = mount(PayInvoicePage, { global: { plugins: [router, createVuetify()] } })
  await flushPromises()
  return wrapper
}

function publicInvoice () {
  return {
    organization_name: 'Sparkle Clean',
    number: 'SPK-0009',
    status: 'issued',
    bill_to_name: 'Pat',
    lines: [{ description: 'Standard clean', amount_cents: 15_000 }],
    subtotal_cents: 15_000,
    tax_rate_percent: '0',
    tax_cents: 0,
    total_cents: 15_000,
    paid_cents: 0,
    balance_cents: 15_000,
    payment_state: 'unpaid',
    due_on: '2026-12-01',
    notes: '',
    footer: '',
    payable_reason: null,
  }
}

beforeEach(() => {
  stubResizeObserver()
  mock = new MockAdapter(api)
})

afterEach(() => mock.restore())

describe('PayInvoicePage', () => {
  it('reads the token from the fragment and sends it in the body', async () => {
    mock.onPost(INVOICE).reply(200, publicInvoice())

    const wrapper = await mountAt('/pay#token=ImFiYyI:1xTixv:818Sbpq')

    expect(JSON.parse(mock.history.post[0].data)).toEqual({ token: 'ImFiYyI:1xTixv:818Sbpq' })
    expect(mock.history.get).toHaveLength(0)
    expect(mock.history.post.every(call => !call.url?.includes('ImFiYyI'))).toBe(true)
    expect(wrapper.text()).toContain('Invoice SPK-0009')
    expect(wrapper.text()).toContain('Sparkle Clean')
  })

  it('opens checkout with the token in the body too', async () => {
    mock.onPost(INVOICE).reply(200, publicInvoice())
    mock.onPost(CHECKOUT).reply(503, { detail: 'Card payments are not available right now.' })
    const wrapper = await mountAt('/pay#token=tok-1')

    await wrapper.find('button').trigger('click')
    await flushPromises()

    const call = mock.history.post.find(each => each.url === CHECKOUT)
    expect(JSON.parse(call?.data ?? '{}')).toEqual({ token: 'tok-1' })
  })

  it.each(['/pay', '/pay#token=', '/pay#tok-1'])(
    'says the link is incomplete and asks the server nothing: %s',
    async path => {
      const wrapper = await mountAt(path)

      expect(wrapper.text()).toContain(INCOMPLETE_LINK)
      expect(mock.history.post).toHaveLength(0)
    },
  )

  it('shows the invalid-link sentence for a garbled token', async () => {
    mock.onPost(INVOICE).reply(404, { detail: 'That link is not valid, or has expired.' })

    const wrapper = await mountAt('/pay#token=garbled')

    expect(wrapper.text()).toContain('That link is not valid, or has expired. Ask for a new one.')
  })
})
