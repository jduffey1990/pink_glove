/**
 * Booking from the week view.
 *
 * The form is `VisitDialog` and has its own spec. What belongs to this page
 * is which day the form opens on, and what happens once the server has
 * booked the visit: the board shows the week it landed in, and the notice
 * quotes the price the server returned rather than one worked out here.
 *
 * jsdom, not a browser: nothing here says how the page looks.
 */

import type { Job } from '@/api/types'
import { flushPromises, mount } from '@vue/test-utils'
import MockAdapter from 'axios-mock-adapter'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'
import { createVuetify } from 'vuetify'
import { api } from '@/api/client'
import VisitDialog from '@/components/VisitDialog.vue'
import { todayIn, weekOf } from '@/lib/datetime'
import { useSessionStore } from '@/stores/session'
import SchedulePage from './SchedulePage.vue'

const DENVER = 'America/Denver'

/** A visit far outside whatever week the test happens to run in. */
const BOOKED = {
  id: 'job-new',
  customer_detail: { id: 'customer-1', display_name: 'Dana Henderson' },
  scheduled_start: '2031-06-10T15:00:00Z',
  price_cents: 18_250,
} as Job

class ResizeObserverStub {
  observe () {}
  unobserve () {}
  disconnect () {}
}

let mock: MockAdapter

beforeEach(() => {
  vi.stubGlobal('visualViewport', new EventTarget())
  vi.stubGlobal('ResizeObserver', ResizeObserverStub)

  setActivePinia(createPinia())
  useSessionStore().$patch({
    user: {
      id: 'user-1',
      memberships: [],
      current_role: 'dispatcher',
      current_organization: { id: 'org-1', name: 'Sparkle', slug: 'sparkle', timezone: DENVER },
    },
  } as never)

  mock = new MockAdapter(api)
  mock.onGet('/api/scheduling/jobs/').reply(200, { results: [] })
  mock.onGet('/api/users/memberships/').reply(200, { results: [] })
  mock.onGet('/api/catalog/services/').reply(200, { results: [] })
  mock.onGet('/api/customers/customers/').reply(200, { results: [] })
})

afterEach(() => {
  vi.unstubAllGlobals()
  mock.restore()
  document.body.innerHTML = ''
})

async function openPage () {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/schedule', name: 'schedule', component: SchedulePage },
      { path: '/jobs/:id', name: 'job', component: { template: '<div />' } },
    ],
  })
  await router.push('/schedule')
  await router.isReady()

  const wrapper = mount(SchedulePage, {
    global: { plugins: [createVuetify(), router] },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
}

function button (label: string): HTMLButtonElement | undefined {
  return [...document.body.querySelectorAll<HTMLButtonElement>('button')]
    .find(candidate => candidate.textContent?.trim() === label)
}

function weeksAskedFor (): string[] {
  return mock.history.get
    .filter(request => request.url === '/api/scheduling/jobs/')
    .map(request => request.params.date_from)
}

describe('booking from the schedule', () => {
  it('opens the form on today, in the organization zone', async () => {
    const wrapper = await openPage()

    button('New visit')!.click()
    await flushPromises()

    const dialog = wrapper.findComponent(VisitDialog)
    expect(dialog.props('modelValue')).toBe(true)
    expect(dialog.props('date')).toBe(todayIn(DENVER))
  })

  it('opens the form on the day whose plus was pressed', async () => {
    const wrapper = await openPage()
    const week = weekOf(todayIn(DENVER))

    const plus = [...document.body.querySelectorAll<HTMLButtonElement>('button[aria-label]')]
      .filter(candidate => candidate.getAttribute('aria-label')?.startsWith('New visit on'))
    expect(plus).toHaveLength(7)
    plus[6].click()
    await flushPromises()

    expect(wrapper.findComponent(VisitDialog).props('date')).toBe(week[6])
  })

  it('opens on the first day shown when today is not on screen', async () => {
    const wrapper = await openPage()
    const thisMonday = weekOf(todayIn(DENVER))[0]

    // One week forward: today is no longer among the seven days.
    document.body.querySelector<HTMLButtonElement>('.mdi-chevron-right')!.closest('button')!.click()
    await flushPromises()
    button('New visit')!.click()
    await flushPromises()

    const opened = wrapper.findComponent(VisitDialog).props('date') as string
    expect(opened).not.toBe(todayIn(DENVER))
    expect(weekOf(opened)[0]).toBe(opened)
    expect(opened > thisMonday).toBe(true)
  })

  it('shows what the server booked, at the server\'s price, and goes to its week', async () => {
    const wrapper = await openPage()
    const before = weeksAskedFor().length

    wrapper.findComponent(VisitDialog).vm.$emit('saved', BOOKED)
    await flushPromises()

    const notice = document.body.textContent ?? ''
    expect(notice).toContain('Booked Dana Henderson for Tue 10 Jun, 09:00')
    expect(notice).toContain('$182.50')
    // 10 June 2031 is a Tuesday; its week starts on Monday the 9th.
    expect(weeksAskedFor().slice(before)).toEqual(['2031-06-09'])
  })

  it('reloads the board when the visit landed in the week already on screen', async () => {
    const wrapper = await openPage()
    const before = weeksAskedFor().length
    const thisWeek = weekOf(todayIn(DENVER))[0]

    wrapper.findComponent(VisitDialog).vm.$emit('saved', {
      ...BOOKED,
      scheduled_start: new Date().toISOString(),
    })
    await flushPromises()

    expect(weeksAskedFor().slice(before)).toEqual([thisWeek])
  })
})
