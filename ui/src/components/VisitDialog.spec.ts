/**
 * The visit dialog, mounted.
 *
 * `lib/visitForm.spec.ts` covers what the form means. This covers the wiring
 * a spec over pure functions cannot see: that the dialog loads its options
 * when it is mounted already open (the `v-if` trap), that Save sends what the
 * form model built, and that a refusal ends up on the screen in the server's
 * words rather than swallowed.
 *
 * It is jsdom, not a browser: nothing here says how the dialog looks.
 */

import type { Job } from '@/api/types'
import { flushPromises, mount } from '@vue/test-utils'
import MockAdapter from 'axios-mock-adapter'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createVuetify } from 'vuetify'
import { api } from '@/api/client'
import { useSessionStore } from '@/stores/session'
import VisitDialog from './VisitDialog.vue'

const DENVER = 'America/Denver'

const CUSTOMER = {
  id: 'customer-1',
  display_name: 'Dana Henderson',
  locations: [
    { id: 'location-1', label: 'Home', one_line_address: '1 Oak Street', is_active: true },
    { id: 'location-old', label: 'Old flat', one_line_address: '9 Elm Row', is_active: false },
  ],
}

const SERVICE = { id: 'service-1', name: 'Standard clean', default_duration_minutes: 120 }

function job (overrides: Partial<Job> = {}): Job {
  return {
    id: 'job-1',
    customer: 'customer-1',
    customer_detail: { id: 'customer-1', display_name: 'Dana Henderson' },
    location: 'location-1',
    location_detail: { id: 'location-1', label: 'Home', one_line_address: '1 Oak Street' },
    service: 'service-1',
    service_detail: { id: 'service-1', name: 'Standard clean' },
    scheduled_start: '2027-06-10T15:00:30Z',
    scheduled_end: '2027-06-10T17:00:30Z',
    duration_minutes: 120,
    price_cents: 15_000,
    notes: 'Side gate.',
    available_actions: ['edit', 'delete'],
    ...overrides,
  } as Job
}

let mock: MockAdapter

/** What Vuetify's overlay asks of a browser and jsdom does not have. */
class ResizeObserverStub {
  observe () {}
  unobserve () {}
  disconnect () {}
}

beforeEach(() => {
  vi.stubGlobal('visualViewport', new EventTarget())
  vi.stubGlobal('ResizeObserver', ResizeObserverStub)

  setActivePinia(createPinia())
  // The store reads the zone off the chosen organization; everything the
  // dialog does with dates hangs on it.
  useSessionStore().$patch({
    user: {
      memberships: [],
      current_role: 'dispatcher',
      current_organization: { id: 'org-1', name: 'Sparkle', slug: 'sparkle', timezone: DENVER },
    },
  } as never)

  mock = new MockAdapter(api)
  mock.onGet('/api/catalog/services/').reply(200, { results: [SERVICE] })
  mock.onGet('/api/customers/customers/').reply(200, { results: [CUSTOMER] })
  mock.onGet('/api/customers/customers/customer-1/').reply(200, CUSTOMER)
})

afterEach(() => {
  vi.unstubAllGlobals()
  mock.restore()
  document.body.innerHTML = ''
})

function open (props: Record<string, unknown> = {}) {
  return mount(VisitDialog, {
    props: { modelValue: true, date: '2027-06-10', staff: [], ...props },
    global: { plugins: [createVuetify()] },
    attachTo: document.body,
  })
}

/** The dialog teleports to the body, so it is read from there, not the wrapper. */
function text (): string {
  return document.body.textContent ?? ''
}

function saveButton (): HTMLButtonElement {
  const buttons = [...document.body.querySelectorAll<HTMLButtonElement>('button')]
  return buttons.find(button => /Book visit|Save/.test(button.textContent ?? '')) as HTMLButtonElement
}

describe('VisitDialog', () => {
  it('reads the organization zone it is about to book in', () => {
    expect(useSessionStore().timeZone).toBe(DENVER)
  })

  it('loads its options when it is mounted already open', async () => {
    open()
    await flushPromises()

    expect(mock.history.get.map(request => request.url)).toEqual(
      expect.arrayContaining(['/api/catalog/services/', '/api/customers/customers/']),
    )
    expect(text()).toContain('New visit')
    expect(text()).toContain(DENVER)
  })

  it('will not send a form that is not filled in', async () => {
    open()
    await flushPromises()

    expect(saveButton().disabled).toBe(true)
  })

  it('books at the organization\'s 09:00, for the service\'s usual length, unpriced', async () => {
    const created = job({ id: 'job-new', price_cents: 15_000 })
    mock.onPost('/api/scheduling/jobs/').reply(201, created)
    const wrapper = open()
    await flushPromises()

    // Picking from Vuetify's menus is not something jsdom does faithfully, so
    // the two choices are made through the handlers the menus call.
    const dialog = wrapper.vm as unknown as {
      pickCustomer: (customer: typeof CUSTOMER) => void
      pickService: (id: string) => void
    }
    dialog.pickCustomer(CUSTOMER)
    dialog.pickService('service-1')
    await flushPromises()
    saveButton().click()
    await flushPromises()

    expect(JSON.parse(mock.history.post[0].data)).toEqual({
      customer: 'customer-1',
      // The customer's only active address, chosen for them.
      location: 'location-1',
      service: 'service-1',
      scheduled_start: '2027-06-10T15:00:00.000Z',
      scheduled_end: '2027-06-10T17:00:00.000Z',
      notes: '',
    })
    expect(wrapper.emitted('saved')?.[0]).toEqual([created])
  })

  it('shows a booking refusal under its input and keeps the form open', async () => {
    mock.onPost('/api/scheduling/jobs/').reply(400, {
      square_feet: ['This service is priced per square foot. The location has no square footage.'],
    })
    const wrapper = open()
    await flushPromises()

    const dialog = wrapper.vm as unknown as {
      pickCustomer: (customer: typeof CUSTOMER) => void
      pickService: (id: string) => void
    }
    dialog.pickCustomer(CUSTOMER)
    dialog.pickService('service-1')
    await flushPromises()
    saveButton().click()
    await flushPromises()

    expect(text()).toContain('The location has no square footage.')
    expect(wrapper.emitted('saved')).toBeUndefined()
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
  })

  it('opens an edit on the job as it stands, and offers neither customer nor crew', async () => {
    open({ job: job() })
    await flushPromises()

    expect(text()).toContain('Edit visit')
    expect(text()).not.toContain('Crew')
    expect(mock.history.get.map(request => request.url))
      .toContain('/api/customers/customers/customer-1/')
    const date = document.body.querySelector<HTMLInputElement>('input[type="date"]')
    const time = document.body.querySelector<HTMLInputElement>('input[type="time"]')
    expect(date?.value).toBe('2027-06-10')
    expect(time?.value).toBe('09:00')
  })

  it('sends only the field that changed, and hands back what the server returned', async () => {
    const updated = job({ notes: 'Front door.' })
    mock.onPatch('/api/scheduling/jobs/job-1/').reply(200, updated)
    const wrapper = open({ job: job() })
    await flushPromises()

    const notes = document.body.querySelector<HTMLTextAreaElement>('textarea:not([readonly])')
    notes!.value = 'Front door.'
    notes!.dispatchEvent(new Event('input'))
    await flushPromises()
    saveButton().click()
    await flushPromises()

    expect(JSON.parse(mock.history.patch[0].data)).toEqual({ notes: 'Front door.' })
    expect(wrapper.emitted('saved')?.[0]).toEqual([updated])
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([false])
  })

  it('makes no request when Save is pressed on an untouched edit', async () => {
    const wrapper = open({ job: job() })
    await flushPromises()

    saveButton().click()
    await flushPromises()

    expect(mock.history.patch).toHaveLength(0)
    expect(wrapper.emitted('saved')).toBeUndefined()
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([false])
  })

  it('shows a refusal under the input it is about, in the server\'s words', async () => {
    mock.onPatch('/api/scheduling/jobs/job-1/').reply(400, {
      scheduled_end: ['A job must end after it starts.'],
    })
    const wrapper = open({ job: job() })
    await flushPromises()

    const minutes = document.body.querySelector<HTMLInputElement>('input[type="number"]')
    minutes!.value = '0'
    minutes!.dispatchEvent(new Event('input'))
    await flushPromises()
    saveButton().click()
    await flushPromises()

    expect(text()).toContain('A job must end after it starts.')
    expect(wrapper.emitted('saved')).toBeUndefined()
    // Still open: the dispatcher has something to fix.
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
  })

  it('closes on a 409 and hands the server\'s message to the page', async () => {
    mock.onPatch('/api/scheduling/jobs/job-1/').reply(409, {
      detail: 'This job is complete and cannot be changed. Reopen it first.',
      status: 'complete',
    })
    const wrapper = open({ job: job() })
    await flushPromises()

    const notes = document.body.querySelector<HTMLTextAreaElement>('textarea:not([readonly])')
    notes!.value = 'Front door.'
    notes!.dispatchEvent(new Event('input'))
    await flushPromises()
    saveButton().click()
    await flushPromises()

    expect(wrapper.emitted('conflict')?.[0]).toEqual([
      'This job is complete and cannot be changed. Reopen it first.',
    ])
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([false])
  })

  it('says so when the save fails for a reason that is not about the form', async () => {
    mock.onPatch('/api/scheduling/jobs/job-1/').reply(403, {
      detail: 'You need dispatcher access to do that.',
    })
    open({ job: job() })
    await flushPromises()

    const notes = document.body.querySelector<HTMLTextAreaElement>('textarea:not([readonly])')
    notes!.value = 'Front door.'
    notes!.dispatchEvent(new Event('input'))
    await flushPromises()
    saveButton().click()
    await flushPromises()

    expect(text()).toContain('You need dispatcher access to do that.')
  })
})
