/**
 * The session store's boot sequence.
 *
 * The sequence matters more than any single call: the first GET is what sets
 * the CSRF cookie, an empty object means signed out rather than broken, and
 * the organization choice has to survive a reload without ever being sent for
 * an organization the user no longer belongs to.
 */

import MockAdapter from 'axios-mock-adapter'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { api } from '@/api/client'
import { useSessionStore } from './session'

let mock: MockAdapter

function membership (organizationId: string, role = 'dispatcher') {
  return {
    id: `m-${organizationId}`,
    organization: {
      id: organizationId,
      name: `Org ${organizationId}`,
      slug: organizationId,
      timezone: 'America/Denver',
      primary_color: '',
      logo: null,
    },
    role,
    is_active: true,
  }
}

function session (overrides: Record<string, unknown> = {}) {
  return {
    id: 'user-1',
    email: 'dispatcher@example.test',
    first_name: 'Dee',
    last_name: 'Spatcher',
    full_name: 'Dee Spatcher',
    phone: '',
    is_superuser: false,
    memberships: [membership('org-a')],
    current_organization: membership('org-a').organization,
    current_role: 'dispatcher',
    ...overrides,
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
  globalThis.localStorage?.clear()
  mock = new MockAdapter(api)
})

afterEach(() => mock.restore())

describe('boot', () => {
  it('signs nobody in when the endpoint returns an empty object', async () => {
    mock.onGet('/api/users/session/').reply(200, {})
    const store = useSessionStore()

    await store.boot()

    expect(store.isAuthenticated).toBe(false)
    expect(store.user).toBeNull()
    expect(store.ready).toBe(true)
  })

  it('populates the store when a session exists', async () => {
    mock.onGet('/api/users/session/').reply(200, session())
    const store = useSessionStore()

    await store.boot()

    expect(store.isAuthenticated).toBe(true)
    expect(store.user?.email).toBe('dispatcher@example.test')
    expect(store.role).toBe('dispatcher')
    expect(store.organization?.id).toBe('org-a')
  })

  it('is ready even when the call fails', async () => {
    // A failed boot means signed out, not a broken app -- the router still has
    // to be able to send them to the login page.
    mock.onGet('/api/users/session/').networkError()
    const store = useSessionStore()

    await store.boot()

    expect(store.ready).toBe(true)
    expect(store.isAuthenticated).toBe(false)
  })

  it('adopts the only membership without making the user choose', async () => {
    mock.onGet('/api/users/session/').reply(200, session())
    const store = useSessionStore()

    await store.boot()

    expect(store.organizationId).toBe('org-a')
    expect(store.needsOrganizationChoice).toBe(false)
  })
})

describe('choosing an organization', () => {
  const multi = session({
    memberships: [membership('org-a'), membership('org-b', 'cleaner')],
    current_organization: null,
    current_role: null,
  })

  it('asks when there are several and the server resolved none', async () => {
    mock.onGet('/api/users/session/').reply(200, multi)
    const store = useSessionStore()

    await store.boot()

    expect(store.needsOrganizationChoice).toBe(true)
  })

  it('stops asking once a choice is made', async () => {
    mock.onGet('/api/users/session/').reply(200, multi)
    const store = useSessionStore()
    await store.boot()

    store.chooseOrganization('org-b')

    expect(store.needsOrganizationChoice).toBe(false)
    expect(store.organization?.id).toBe('org-b')
  })

  it('derives the role from the chosen membership', async () => {
    mock.onGet('/api/users/session/').reply(200, multi)
    const store = useSessionStore()
    await store.boot()

    store.chooseOrganization('org-b')

    expect(store.role).toBe('cleaner')
    expect(store.isCleaner).toBe(true)
    expect(store.isDispatcherOrHigher).toBe(false)
  })

  it('remembers the choice across a reload', async () => {
    mock.onGet('/api/users/session/').reply(200, multi)
    const first = useSessionStore()
    await first.boot()
    first.chooseOrganization('org-b')

    setActivePinia(createPinia())
    const second = useSessionStore()
    await second.boot()

    expect(second.organizationId).toBe('org-b')
  })

  it('drops a remembered organization the user no longer belongs to', async () => {
    // Otherwise the next request carries an X-Organization header for a
    // membership that was revoked, and every call 404s with no explanation.
    globalThis.localStorage.setItem('pink_glove.organization', 'org-gone')
    mock.onGet('/api/users/session/').reply(200, multi)
    const store = useSessionStore()

    await store.boot()

    expect(store.organizationId).toBe('org-a')
  })
})

describe('signing in', () => {
  it('reports that a code is required on a 202', async () => {
    mock.onPost('/api/auth/login/').reply(202, { two_factor_required: true })
    const store = useSessionStore()

    const outcome = await store.login('someone@example.test', 'pw')

    expect(outcome).toBe('code-required')
    expect(store.isAuthenticated).toBe(false)
  })

  it('establishes the session on a 200', async () => {
    mock.onPost('/api/auth/login/').reply(200, session())
    mock.onGet('/api/users/session/').reply(200, session())
    const store = useSessionStore()

    const outcome = await store.login('someone@example.test', 'pw')

    expect(outcome).toBe('signed-in')
    expect(store.isAuthenticated).toBe(true)
  })

  it('re-fetches the session after verifying a code', async () => {
    mock.onPost('/api/auth/verify/').reply(200, session())
    mock.onGet('/api/users/session/').reply(200, session())
    const store = useSessionStore()

    await store.verify('123456')

    expect(store.isAuthenticated).toBe(true)
  })

  it('passes remember_device through to the backend', async () => {
    mock.onPost('/api/auth/verify/').reply(200, session())
    mock.onGet('/api/users/session/').reply(200, session())
    const store = useSessionStore()

    await store.verify('123456', true)

    expect(JSON.parse(mock.history.post[0].data)).toMatchObject({
      code: '123456',
      remember_device: true,
    })
  })
})

describe('signing out', () => {
  it('clears the store', async () => {
    mock.onGet('/api/users/session/').reply(200, session())
    mock.onPost('/api/users/logout/').reply(200, {})
    const store = useSessionStore()
    await store.boot()

    await store.logout()

    expect(store.isAuthenticated).toBe(false)
    expect(store.organizationId).toBeNull()
  })

  it('clears the store even when the request fails', async () => {
    // The session is gone locally either way; leaving a stale user on screen
    // is worse than a failed round trip nobody can act on.
    mock.onGet('/api/users/session/').reply(200, session())
    mock.onPost('/api/users/logout/').reply(500, {})
    const store = useSessionStore()
    await store.boot()

    await expect(store.logout()).rejects.toThrow()

    expect(store.isAuthenticated).toBe(false)
  })

  it('forgets the remembered organization', async () => {
    mock.onGet('/api/users/session/').reply(200, session())
    mock.onPost('/api/users/logout/').reply(200, {})
    const store = useSessionStore()
    await store.boot()

    await store.logout()

    expect(globalThis.localStorage.getItem('pink_glove.organization')).toBeNull()
  })
})

describe('role helpers', () => {
  it.each([
    ['owner', { staff: true, dispatcher: true, cleaner: false, customer: false }],
    ['admin', { staff: true, dispatcher: true, cleaner: false, customer: false }],
    ['dispatcher', { staff: true, dispatcher: true, cleaner: false, customer: false }],
    ['cleaner', { staff: true, dispatcher: false, cleaner: true, customer: false }],
    ['customer', { staff: false, dispatcher: false, cleaner: false, customer: true }],
  ])('%s', async (role, expected) => {
    mock.onGet('/api/users/session/').reply(200, session({ current_role: role }))
    const store = useSessionStore()

    await store.boot()

    expect(store.isStaff).toBe(expected.staff)
    expect(store.isDispatcherOrHigher).toBe(expected.dispatcher)
    expect(store.isCleaner).toBe(expected.cleaner)
    expect(store.isCustomer).toBe(expected.customer)
  })
})
