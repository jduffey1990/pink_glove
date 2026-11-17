/**
 * The reports page, mounted against a mocked API.
 *
 * Its arithmetic is all on the server and its date logic is in
 * `lib/reports.ts`, each with its own tests. What is left to get wrong here
 * is the wiring, and it is the kind that fails silently: asking for the
 * browser's week instead of the organization's, printing one report's error
 * over another, or downloading a different period from the one on screen.
 */

import type { Collections, Receivables, Session, Timesheet } from '@/api/types'
import { flushPromises, mount } from '@vue/test-utils'
import MockAdapter from 'axios-mock-adapter'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'
import { createVuetify } from 'vuetify'
import { api } from '@/api/client'
import { saveBlob } from '@/lib/download'
import { useSessionStore } from '@/stores/session'
import ReportsPage from './ReportsPage.vue'

vi.mock('@/lib/download', () => ({ saveBlob: vi.fn() }))

// jsdom has no ResizeObserver, and the spinner inside a loading button asks
// for one. Nothing here depends on a size, so it need only exist.
vi.stubGlobal('ResizeObserver', class {
  observe (): void {}
  unobserve (): void {}
  disconnect (): void {}
})

const TIMESHEET_URL = '/api/scheduling/reports/timesheet/'
const RECEIVABLES_URL = '/api/billing/reports/receivables/'
const COLLECTIONS_URL = '/api/billing/reports/collections/'

const TIMESHEET: Timesheet = {
  date_from: '2027-06-14',
  date_to: '2027-06-20',
  timezone: 'America/Denver',
  total_seconds: 12_600,
  entry_count: 3,
  open_entry_count: 1,
  staff: [
    {
      user: 'user-ana',
      name: 'Ana Lopez',
      total_seconds: 12_600,
      entry_count: 3,
      open_entry_count: 1,
      entries: [
        {
          id: 'entry-1',
          job: 'job-1',
          customer_name: 'Dana Hale',
          local_date: '2027-06-14',
          // 22:00 on the 14th in Denver, into the small hours of the 15th.
          clock_in: '2027-06-15T04:00:00Z',
          clock_out: '2027-06-15T07:30:00Z',
          seconds: 12_600,
          is_open: false,
        },
        {
          id: 'entry-2',
          job: 'job-2',
          customer_name: 'Oak Street Dental',
          local_date: '2027-06-16',
          clock_in: '2027-06-16T15:00:00Z',
          clock_out: null,
          seconds: null,
          is_open: true,
        },
      ],
    },
  ],
}

const RECEIVABLES: Receivables = {
  as_of: '2027-06-20',
  customers: [
    {
      customer: 'customer-1',
      customer_name: 'Dana Hale',
      invoice_count: 2,
      not_due_cents: 15_000,
      days_1_30_cents: 0,
      days_31_60_cents: 4250,
      days_61_90_cents: 0,
      days_over_90_cents: 0,
      total_cents: 19_250,
    },
  ],
  totals: {
    invoice_count: 2,
    not_due_cents: 15_000,
    days_1_30_cents: 0,
    days_31_60_cents: 4250,
    days_61_90_cents: 0,
    days_over_90_cents: 0,
    total_cents: 19_250,
  },
}

const COLLECTIONS: Collections = {
  date_from: '2027-06-14',
  date_to: '2027-06-20',
  methods: [
    {
      method: 'card',
      label: 'Card',
      payment_count: 2,
      amount_cents: 30_000,
      tip_cents: 0,
      fee_cents: 465,
      net_cents: 29_535,
      fees_pending: 1,
    },
  ],
  totals: {
    payment_count: 2,
    amount_cents: 30_000,
    tip_cents: 0,
    fee_cents: 465,
    net_cents: 29_535,
    fees_pending: 1,
  },
}

let mock: MockAdapter

/**
 * The session as the API sends it. Typed as `Session` with no cast, so a
 * field the server adds to the payload (as Phase 4c added `is_active` and
 * `billing` to the organization) stops this compiling instead of leaving the
 * page tested against a session that no longer exists.
 */
const SESSION: Session = {
  id: 'user-1',
  email: 'owner@example.test',
  first_name: 'Pat',
  last_name: 'Rivera',
  full_name: 'Pat Rivera',
  phone: '',
  is_superuser: false,
  current_role: 'owner',
  current_organization: {
    id: 'org-1',
    name: 'Sparkle Clean',
    slug: 'sparkle-clean',
    timezone: 'America/Denver',
    primary_color: '',
    logo: null,
    is_active: true,
    billing: { state: 'active', level: null, notice: null },
  },
  memberships: [],
}

/** The same owner after the subscription lapsed: the organization is read-only. */
const LAPSED_SESSION: Session = {
  ...SESSION,
  current_organization: {
    ...SESSION.current_organization!,
    is_active: false,
    billing: {
      state: 'lapsed',
      level: 'error',
      notice: 'Your subscription has lapsed. This organization is read-only.',
    },
  },
}

async function mountPage (user: Session = SESSION) {
  const pinia = createPinia()
  setActivePinia(pinia)

  const session = useSessionStore()
  session.user = user

  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/jobs/:id', name: 'job', component: { template: '<div />' } },
    ],
  })
  router.push('/')
  await router.isReady()

  const wrapper = mount(ReportsPage, {
    global: { plugins: [pinia, router, createVuetify()] },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
}

describe('ReportsPage', () => {
  beforeEach(() => {
    // 04:30 UTC on Monday 21 June 2027 is 22:30 on Sunday the 20th in
    // Denver. The organization's "this week" is 14 to 20 June; UTC's, and
    // most browsers', is 21 to 27.
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2027-06-21T04:30:00Z'))

    mock = new MockAdapter(api)
    mock.onGet(TIMESHEET_URL).reply(200, TIMESHEET)
    mock.onGet(RECEIVABLES_URL).reply(200, RECEIVABLES)
    mock.onGet(COLLECTIONS_URL).reply(200, COLLECTIONS)
  })

  afterEach(() => {
    mock.restore()
    vi.useRealTimers()
    vi.mocked(saveBlob).mockClear()
    document.body.innerHTML = ''
  })

  it('a lapsed, read-only organization still reads and downloads its reports', async () => {
    // Reports are reads, and the data is theirs (ADR-028 makes a lapsed
    // organization read-only, not locked out). The page must not gate
    // itself on `is_active`; the server is what would refuse.
    mock.onGet(`${RECEIVABLES_URL}csv/`).reply(200, new Blob(['customer\r\n']))
    const wrapper = await mountPage(LAPSED_SESSION)

    expect(useSessionStore().isActive).toBe(false)
    expect(wrapper.text()).toContain('Ana Lopez')
    expect(wrapper.text()).toContain('$192.50')
    expect(wrapper.text()).toContain('$295.35')

    const buttons = wrapper.findAll('button').filter(button => button.text() === 'CSV')
    expect(buttons.every(button => !button.attributes('disabled'))).toBe(true)
    await buttons[1].trigger('click')
    await flushPromises()

    expect(saveBlob).toHaveBeenCalledTimes(1)
  })

  it('asks for the organization\'s week, not the browser\'s', async () => {
    await mountPage()

    const asked = Object.fromEntries(mock.history.get.map(call => [call.url, call.params]))

    expect(asked[TIMESHEET_URL]).toEqual({ date_from: '2027-06-14', date_to: '2027-06-20' })
    expect(asked[COLLECTIONS_URL]).toEqual({ date_from: '2027-06-14', date_to: '2027-06-20' })
    // Receivables are what is owed now: no period, and the server's today.
    expect(asked[RECEIVABLES_URL]).toEqual({})
  })

  it('prints the server\'s figures', async () => {
    const wrapper = await mountPage()
    const text = wrapper.text()

    // Timesheet: 12,600 seconds.
    expect(text).toContain('3 h 30 m')
    expect(text).toContain('Ana Lopez')
    expect(text).toContain('1 entry is still open and is not counted.')
    // Receivables.
    expect(text).toContain('Dana Hale')
    expect(text).toContain('$42.50')
    expect(text).toContain('$192.50')
    expect(text).toContain('31-60 days')
    // Collections.
    expect(text).toContain('Card')
    expect(text).toContain('$4.65')
    expect(text).toContain('$295.35')
    expect(text).toContain('1 card payment has no fee reported yet')
  })

  it('shows an entry in the organization\'s wall-clock time, and an open one as open', async () => {
    const wrapper = await mountPage()

    await wrapper.find('.v-expansion-panel-title').trigger('click')
    await flushPromises()
    const text = wrapper.text()

    // 04:00Z to 07:30Z is 22:00 to 01:30 in Denver.
    expect(text).toContain('22:00')
    expect(text).toContain('01:30')
    expect(text).toContain('Mon 14 Jun')
    expect(text).toContain('Still clocked in')
  })

  it('one report failing leaves the others standing, and quotes the server', async () => {
    mock.onGet(TIMESHEET_URL).reply(400, {
      date_to: ['That range covers more than 62 days. Ask for a shorter window.'],
    })

    const wrapper = await mountPage()
    const text = wrapper.text()

    expect(text).toContain('That range covers more than 62 days. Ask for a shorter window.')
    expect(text).not.toContain('Ana Lopez')
    expect(text).toContain('$192.50')
    expect(text).toContain('$295.35')
  })

  it('reloads the period reports, and only those, when the period changes', async () => {
    const wrapper = await mountPage()
    mock.resetHistory()

    await wrapper.findComponent({ name: 'VSelect' }).setValue('last-month')
    await flushPromises()

    const asked = Object.fromEntries(mock.history.get.map(call => [call.url, call.params]))
    expect(asked[TIMESHEET_URL]).toEqual({ date_from: '2027-05-01', date_to: '2027-05-31' })
    expect(asked[COLLECTIONS_URL]).toEqual({ date_from: '2027-05-01', date_to: '2027-05-31' })
    expect(asked[RECEIVABLES_URL]).toBeUndefined()
  })

  it('custom starts from the dates on screen and asks nothing until both are set', async () => {
    const wrapper = await mountPage()
    await wrapper.findComponent({ name: 'VSelect' }).setValue('custom')
    await flushPromises()

    const [from, to] = wrapper.findAll('input[type="date"]')
    expect((from.element as HTMLInputElement).value).toBe('2027-06-14')
    expect((to.element as HTMLInputElement).value).toBe('2027-06-20')

    mock.resetHistory()
    await to.setValue('')
    await flushPromises()

    expect(mock.history.get).toHaveLength(0)
    expect(wrapper.text()).toContain('Choose both dates.')
    // Nothing left on screen to mistake for the answer.
    expect(wrapper.text()).not.toContain('Ana Lopez')

    await to.setValue('2027-06-16')
    await flushPromises()

    const asked = Object.fromEntries(mock.history.get.map(call => [call.url, call.params]))
    expect(asked[TIMESHEET_URL]).toEqual({ date_from: '2027-06-14', date_to: '2027-06-16' })
  })

  it('downloads the period on screen, under the name the API would give it', async () => {
    mock.onGet(`${TIMESHEET_URL}csv/`).reply(200, new Blob(['staff\r\n']))
    mock.onGet(`${RECEIVABLES_URL}csv/`).reply(200, new Blob(['customer\r\n']))
    const wrapper = await mountPage()
    mock.resetHistory()

    const buttons = wrapper.findAll('button').filter(button => button.text() === 'CSV')
    expect(buttons).toHaveLength(3)

    await buttons[0].trigger('click')
    await flushPromises()
    await buttons[1].trigger('click')
    await flushPromises()

    const [timesheet, receivables] = mock.history.get
    expect(timesheet.url).toBe(`${TIMESHEET_URL}csv/`)
    expect(timesheet.params).toEqual({ date_from: '2027-06-14', date_to: '2027-06-20' })
    expect(timesheet.responseType).toBe('blob')
    // The date the table on screen was aged from, not a second "today".
    expect(receivables.params).toEqual({ as_of: '2027-06-20' })

    expect(vi.mocked(saveBlob).mock.calls.map(call => call[1])).toEqual([
      'timesheet_2027-06-14_2027-06-20.csv',
      'receivables_2027-06-20.csv',
    ])
  })

  it('says so when a download fails', async () => {
    mock.onGet(`${COLLECTIONS_URL}csv/`).reply(403, new Blob(['{}']))
    const wrapper = await mountPage()

    const buttons = wrapper.findAll('button').filter(button => button.text() === 'CSV')
    await buttons[2].trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Could not download that report.')
    expect(saveBlob).not.toHaveBeenCalled()
  })
})
