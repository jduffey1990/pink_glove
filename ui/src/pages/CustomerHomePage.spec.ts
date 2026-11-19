/**
 * The customer's home (ADR-039): what it asks the API for and what it shows.
 *
 * Only the jobs list is read, over organization-local dates; nothing that
 * a customer is refused (invoices, customers, the organization's settings)
 * is asked for, so a customer never meets a 403 here.
 *
 * jsdom, not a browser: nothing here says how the page looks.
 */

import type { Job } from '@/api/types'
import { flushPromises, mount } from '@vue/test-utils'
import MockAdapter from 'axios-mock-adapter'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'
import { createVuetify } from 'vuetify'
import { api } from '@/api/client'
import { addDays, todayIn } from '@/lib/datetime'
import { ORGANIZATION, stubResizeObserver } from '@/lib/team.fixtures'
import { useSessionStore } from '@/stores/session'
import CustomerHomePage from './CustomerHomePage.vue'

const JOBS = '/api/scheduling/jobs/'

let mock: MockAdapter

function visit (overrides: Record<string, unknown> = {}): Job {
  return {
    id: 'job-1',
    // 16:00Z is 10:00 in Denver in June.
    scheduled_start: '2031-06-10T16:00:00Z',
    scheduled_end: '2031-06-10T18:00:00Z',
    status: 'scheduled',
    service_detail: { id: 'svc-1', name: 'Standard clean', default_duration_minutes: 120 },
    location_detail: { id: 'loc-1', label: 'Home', one_line_address: '12 Oak Street, Denver' },
    ...overrides,
  } as Job
}

async function openPage (firstName = 'Dana') {
  const pinia = createPinia()
  setActivePinia(pinia)
  useSessionStore().$patch({
    user: {
      id: 'user-1',
      first_name: firstName,
      memberships: [],
      current_role: 'customer',
      current_organization: ORGANIZATION,
    },
  } as never)

  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/my-visits', name: 'customer-home', component: CustomerHomePage }],
  })
  await router.push('/my-visits')
  await router.isReady()

  const wrapper = mount(CustomerHomePage, {
    global: { plugins: [pinia, router, createVuetify()] },
  })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  stubResizeObserver()
  mock = new MockAdapter(api)
})

afterEach(() => mock.restore())

describe('CustomerHomePage', () => {
  it('greets the customer and lists their visits in the organization\'s time', async () => {
    mock.onGet(JOBS).reply(200, { results: [visit()] })

    const wrapper = await openPage()

    expect(wrapper.text()).toContain('Hello, Dana')
    expect(wrapper.text()).toContain('Sparkle Clean')
    expect(wrapper.text()).toContain('Tue 10 Jun')
    expect(wrapper.text()).toContain('10:00 to 12:00 · Standard clean')
    expect(wrapper.text()).toContain('12 Oak Street, Denver')
    expect(wrapper.text()).toContain('Scheduled')
  })

  it('asks for eight weeks from today in the organization\'s zone, and nothing else', async () => {
    mock.onGet(JOBS).reply(200, { results: [] })

    await openPage()

    const today = todayIn(ORGANIZATION.timezone)
    expect(mock.history.get).toHaveLength(1)
    expect(mock.history.get[0].params).toMatchObject({
      date_from: today,
      date_to: addDays(today, 55),
      ordering: 'scheduled_start',
    })
    // Not served to customers, so never asked for.
    expect(mock.history.get.some(call => call.url?.includes('invoices'))).toBe(false)
  })

  it('says so when nothing is booked, and how invoices reach them', async () => {
    mock.onGet(JOBS).reply(200, { results: [] })

    const wrapper = await openPage('')

    expect(wrapper.text()).toContain('Hello')
    expect(wrapper.text()).not.toContain('Hello,')
    expect(wrapper.text()).toContain('No visits booked in the next eight weeks.')
    expect(wrapper.text()).toContain('Each invoice comes to you by email, with a link to view and pay it.')
  })

  it('says it could not load, rather than that nothing is booked', async () => {
    mock.onGet(JOBS).reply(500)

    const wrapper = await openPage()

    expect(wrapper.text()).toContain('Could not load your visits.')
    expect(wrapper.text()).not.toContain('No visits booked')
  })
})
