/**
 * Where each role lands on `/`, through the real route table and the real
 * guard (ADR-039).
 *
 * Before ADR-039 everyone who was not a cleaner was sent to `schedule`, so a
 * customer, refused it, was sent back to `/`, round and round: a built
 * bundle froze the tab. The loop runs on promises alone, so it starves the
 * test's own timeout too: the guard reaching for the session store is
 * counted, and a navigation that does so hundreds of times is failed here
 * instead of hanging the run.
 */

import type { Role } from '@/api/types'
import MockAdapter from 'axios-mock-adapter'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { ORGANIZATION } from '@/lib/team.fixtures'
import { useSessionStore } from '@/stores/session'
import router, { admits, landingFor, routes } from './index'

const lookups = vi.hoisted(() => ({ count: 0 }))

vi.mock('@/stores/session', async importOriginal => {
  const actual = await importOriginal<Record<string, unknown> & {
    useSessionStore: typeof useSessionStore
  }>()
  return {
    ...actual,
    useSessionStore: () => {
      lookups.count += 1
      if (lookups.count > 200) {
        throw new Error('Redirect loop: the guard has run over 200 times for one page load')
      }
      return actual.useSessionStore()
    },
  }
})

let mock: MockAdapter

function signedInAs (role: Role | null, overrides: Record<string, unknown> = {}) {
  const memberships = role === null
    ? []
    : [{ id: 'm-1', organization: ORGANIZATION, role, is_active: true }]
  mock.onGet('/api/users/session/').reply(200, {
    id: 'user-1',
    email: 'someone@example.test',
    first_name: 'Sam',
    last_name: 'Body',
    full_name: 'Sam Body',
    phone: '',
    is_superuser: false,
    memberships,
    current_organization: role === null ? null : ORGANIZATION,
    current_role: role,
    ...overrides,
  })
}

/** A fresh page load at `path`: the session is fetched again first. */
async function land (path: string) {
  setActivePinia(createPinia())
  lookups.count = 0
  await router.push(path)
  return router.currentRoute.value
}

beforeEach(() => {
  mock = new MockAdapter(api)
})

afterEach(async () => {
  mock.restore()
  // Leave the shared router somewhere every next test navigates away from.
  setActivePinia(createPinia())
  lookups.count = 0
  mock = new MockAdapter(api)
  mock.onGet('/api/users/session/').reply(200, {})
  await router.push('/login')
  mock.restore()
})

describe('/ for each role', () => {
  it.each([
    ['owner', 'schedule'],
    ['admin', 'schedule'],
    ['dispatcher', 'schedule'],
    ['cleaner', 'my-day'],
    ['customer', 'customer-home'],
  ] as [Role, string][])('a signed-in %s lands on %s', async (role, expected) => {
    signedInAs(role)

    const route = await land('/')

    expect(route.name).toBe(expected)
  })

  it('a superuser with no membership lands on the page that says so, not in a loop', async () => {
    signedInAs(null, { is_superuser: true })

    const route = await land('/')

    expect(route.name).toBe('no-access')
  })

  it('someone signed out is sent to sign in', async () => {
    mock.onGet('/api/users/session/').reply(200, {})

    const route = await land('/')

    expect(route.name).toBe('login')
  })
})

describe('a page a role is refused', () => {
  it.each([
    ['/schedule'],
    ['/billing'],
    ['/my-day'],
    ['/team'],
  ])('sends a customer from %s to their home', async path => {
    signedInAs('customer')

    const route = await land(path)

    expect(route.name).toBe('customer-home')
  })

  it.each([
    ['owner', 'schedule'],
    ['dispatcher', 'schedule'],
    ['cleaner', 'my-day'],
  ] as [Role, string][])('never shows a %s the customer home', async (role, expected) => {
    signedInAs(role)

    const route = await land('/my-visits')

    expect(route.name).toBe(expected)
  })
})

describe('landingFor', () => {
  it.each(['owner', 'admin', 'dispatcher', 'cleaner', 'customer'] as Role[])(
    'names a route the guard admits for a %s',
    async role => {
      signedInAs(role)
      setActivePinia(createPinia())
      const session = useSessionStore()
      await session.boot()

      const name = landingFor(session)
      const record = routes.find(route => route.name === name)

      expect(record).toBeDefined()
      expect(admits(record?.meta?.access, session)).toBe(true)
    },
  )
})
