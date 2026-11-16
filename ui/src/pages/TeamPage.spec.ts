/**
 * The team page, mounted.
 *
 * No browser has looked at this page, so this is the nearest thing: the real
 * component, real Vuetify, in jsdom. It cannot see layout. What it can pin
 * is the rule the page exists to obey: a row gets a menu, and the header
 * gets an Invite button, only when the server's own answer says so. The
 * page is given no role table to consult, and these would fail if it grew
 * one.
 */

import { flushPromises, mount } from '@vue/test-utils'
import MockAdapter from 'axios-mock-adapter'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { createVuetify } from 'vuetify'
import { api } from '@/api/client'
import { useSessionStore } from '@/stores/session'
import TeamPage from './TeamPage.vue'

const LIST = '/api/users/memberships/'
const OPTIONS = '/api/users/memberships/invite-options/'

let mock: MockAdapter

const organization = {
  id: 'org-a',
  name: 'Sparkle Clean',
  slug: 'sparkle-clean',
  timezone: 'America/Denver',
  primary_color: '',
  logo: null,
}

function row (id: string, overrides: Record<string, unknown> = {}) {
  return {
    id,
    organization,
    user: `user-${id}`,
    user_name: `Person ${id}`,
    user_email: `${id}@example.test`,
    role: 'cleaner',
    is_active: true,
    state: 'active',
    invited_at: null,
    accepted_at: null,
    deactivated_at: null,
    available_actions: [],
    assignable_roles: [],
    ...overrides,
  }
}

async function mountPage (rows: unknown[], roles: string[], role = 'owner') {
  mock.onGet(LIST).reply(200, { count: rows.length, next: null, previous: null, results: rows })
  mock.onGet(OPTIONS).reply(200, { roles })
  mock.onGet('/api/users/session/').reply(200, {
    id: 'user-me',
    email: 'me@example.test',
    first_name: 'Me',
    last_name: '',
    full_name: 'Me',
    phone: '',
    is_superuser: false,
    memberships: [{ id: 'm-me', organization, role, is_active: true }],
    current_organization: organization,
    current_role: role,
  })

  const pinia = createPinia()
  setActivePinia(pinia)
  await useSessionStore().boot()

  const wrapper = mount(TeamPage, { global: { plugins: [pinia, createVuetify()] } })
  await flushPromises()
  return wrapper
}

function menuButtons (wrapper: Awaited<ReturnType<typeof mountPage>>) {
  return wrapper.findAll('button[aria-label^="Actions for"]')
}

beforeEach(() => {
  // Vuetify measures things; jsdom has nothing to measure with.
  globalThis.ResizeObserver ??= class {
    observe () {}
    unobserve () {}
    disconnect () {}
  }
  mock = new MockAdapter(api)
})

afterEach(() => mock.restore())

describe('TeamPage', () => {
  it('lists everyone with their role and state', async () => {
    const wrapper = await mountPage([
      row('a', { user_name: 'Maria Lopez', role: 'dispatcher' }),
      row('b', { user_name: 'new@example.test', user_email: 'new@example.test', state: 'invited' }),
      row('c', { user_name: 'Sam Old', state: 'deactivated' }),
    ], [])

    const text = wrapper.text()
    expect(text).toContain('Maria Lopez')
    expect(text).toContain('Dispatcher')
    expect(text).toContain('Invited')
    expect(text).toContain('Deactivated')
    // An active row needs no chip to say so.
    expect(text).not.toContain('Active')
  })

  it('gives a row a menu only when the server offers it an action', async () => {
    const wrapper = await mountPage([
      row('a', { user_name: 'Has Actions', available_actions: ['change_role', 'deactivate'] }),
      row('b', { user_name: 'Has None', available_actions: [] }),
    ], ['cleaner'])

    const labels = menuButtons(wrapper).map(button => button.attributes('aria-label'))

    expect(labels).toEqual(['Actions for Has Actions'])
  })

  it('offers an owner nothing the server did not, even on a cleaner', async () => {
    // The caller is an owner and the row is a cleaner: a client-side role
    // table would say "manageable". The server said otherwise, and it wins.
    const wrapper = await mountPage([row('a', { role: 'cleaner', available_actions: [] })], [], 'owner')

    expect(menuButtons(wrapper)).toHaveLength(0)
  })

  it('offers a dispatcher what the server offers, whatever their role suggests', async () => {
    // The mirror image: nothing on this page second-guesses the server by
    // looking at the caller's role. (The route is admin tier; this is the
    // page itself.)
    const wrapper = await mountPage(
      [row('a', { available_actions: ['deactivate'] })],
      [],
      'dispatcher',
    )

    expect(menuButtons(wrapper)).toHaveLength(1)
  })

  it('shows Invite only when the server allows a role to invite', async () => {
    const without = await mountPage([row('a')], [])
    expect(without.text()).not.toContain('Invite')

    const withRoles = await mountPage([row('a')], ['dispatcher', 'cleaner'])
    expect(withRoles.text()).toContain('Invite')
  })

  it('marks the caller\'s own row', async () => {
    const wrapper = await mountPage([
      row('me', { user: 'user-me', user_name: 'Me' }),
      row('other'),
    ], [])

    expect(wrapper.text().match(/You/g)).toHaveLength(1)
  })

  it('says so when the team cannot be loaded', async () => {
    mock.onGet(LIST).reply(500)
    mock.onGet(OPTIONS).reply(500)
    mock.onGet('/api/users/session/').reply(200, {})
    const pinia = createPinia()
    setActivePinia(pinia)

    const wrapper = mount(TeamPage, { global: { plugins: [pinia, createVuetify()] } })
    await flushPromises()

    expect(wrapper.text()).toContain('Could not load the team.')
    expect(wrapper.text()).not.toContain('Nobody here yet')
  })
})
