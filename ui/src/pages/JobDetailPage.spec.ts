/**
 * Edit and Delete on the job page are the server's to offer.
 *
 * The page must draw them from `available_actions` and from nothing else:
 * not the status, not the role. A dispatcher looking at a visit a plan
 * created sees Edit and no Delete, and nothing on the client knows why.
 *
 * jsdom, not a browser: this is about which controls exist and what they
 * send, not how they look.
 */

import type { Job } from '@/api/types'
import { flushPromises, mount } from '@vue/test-utils'
import MockAdapter from 'axios-mock-adapter'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'
import { createVuetify } from 'vuetify'
import { api } from '@/api/client'
import { useSessionStore } from '@/stores/session'
import JobDetailPage from './JobDetailPage.vue'

function job (overrides: Partial<Job> = {}): Job {
  return {
    id: 'job-1',
    customer: 'customer-1',
    customer_detail: { id: 'customer-1', display_name: 'Dana Henderson', phone: '' },
    location: 'location-1',
    location_detail: {
      id: 'location-1',
      label: 'Home',
      one_line_address: '1 Oak Street',
      access_notes: '',
      parking_notes: '',
      has_pets: false,
      pet_notes: '',
      has_access_codes: false,
    },
    service: 'service-1',
    service_detail: { id: 'service-1', name: 'Standard clean' },
    scheduled_start: '2027-06-10T15:00:00Z',
    scheduled_end: '2027-06-10T17:00:00Z',
    duration_minutes: 120,
    status: 'scheduled',
    is_terminal: false,
    next_statuses: [],
    available_actions: ['edit', 'delete'],
    price_cents: 15_000,
    notes: '',
    cancellation_reason: '',
    assignments: [],
    open_time_entry: null,
    ...overrides,
  } as Job
}

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
      current_organization: {
        id: 'org-1',
        name: 'Sparkle',
        slug: 'sparkle',
        timezone: 'America/Denver',
      },
    },
  } as never)

  mock = new MockAdapter(api)
  mock.onGet('/api/scheduling/job-notes/').reply(200, { results: [] })
  mock.onGet('/api/scheduling/job-photos/').reply(200, { results: [] })
  mock.onGet('/api/users/memberships/').reply(200, { results: [] })
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
      { path: '/jobs/:id', name: 'job', component: JobDetailPage },
      { path: '/schedule', name: 'schedule', component: { template: '<div>the schedule</div>' } },
    ],
  })
  await router.push('/jobs/job-1')
  await router.isReady()

  const wrapper = mount(JobDetailPage, {
    global: { plugins: [createVuetify(), router] },
    attachTo: document.body,
  })
  await flushPromises()
  return { wrapper, router }
}

function button (label: string): HTMLButtonElement | undefined {
  return [...document.body.querySelectorAll<HTMLButtonElement>('button')]
    .find(candidate => candidate.textContent?.trim() === label)
}

describe('what the job page offers', () => {
  it.each([
    [['edit', 'delete'], true, true],
    // A visit a plan created: deleting it would only bring it back.
    [['edit'], true, false],
    // A finished visit, or anyone who is not a dispatcher.
    [[], false, false],
  ])('available_actions %j -> Edit %s, Delete %s', async (actions, edit, remove) => {
    mock.onGet('/api/scheduling/jobs/job-1/').reply(200, job({ available_actions: actions }))

    await openPage()

    expect(button('Edit visit') !== undefined).toBe(edit)
    expect(button('Delete visit') !== undefined).toBe(remove)
  })

  it('does not offer them because the job is scheduled or the caller a dispatcher', async () => {
    // Everything a client-side rule would look at says yes. The server said no.
    mock.onGet('/api/scheduling/jobs/job-1/').reply(200, job({
      status: 'scheduled',
      is_terminal: false,
      available_actions: [],
    }))

    await openPage()

    expect(useSessionStore().isDispatcherOrHigher).toBe(true)
    expect(button('Edit visit')).toBeUndefined()
    expect(button('Delete visit')).toBeUndefined()
  })
})

describe('deleting a visit', () => {
  it('asks first, then deletes and goes back to the schedule', async () => {
    mock.onGet('/api/scheduling/jobs/job-1/').reply(200, job())
    mock.onDelete('/api/scheduling/jobs/job-1/').reply(204)
    const { router } = await openPage()

    button('Delete visit')!.click()
    await flushPromises()
    expect(mock.history.delete).toHaveLength(0)

    button('Delete')!.click()
    await flushPromises()

    expect(mock.history.delete).toHaveLength(1)
    // Navigation resolves a few ticks after the request does.
    await vi.waitFor(() => expect(router.currentRoute.value.name).toBe('schedule'))
  })

  it('shows the server\'s reason and re-reads the job when it is refused', async () => {
    // Somebody clocked in after the page loaded.
    mock.onGet('/api/scheduling/jobs/job-1/')
      .replyOnce(200, job())
      .onGet('/api/scheduling/jobs/job-1/')
      .reply(200, job({ status: 'in_progress', available_actions: ['edit'] }))
    mock.onDelete('/api/scheduling/jobs/job-1/').reply(409, {
      detail: 'This job has recorded time. Cancel it instead of deleting it.',
      status: 'in_progress',
    })
    const { router } = await openPage()

    button('Delete visit')!.click()
    await flushPromises()
    button('Delete')!.click()
    await flushPromises()

    expect(document.body.textContent).toContain(
      'This job has recorded time. Cancel it instead of deleting it.',
    )
    expect(router.currentRoute.value.name).toBe('job')
    // The re-read job no longer offers it.
    expect(button('Delete visit')).toBeUndefined()
    expect(button('Edit visit')).toBeDefined()
  })
})
