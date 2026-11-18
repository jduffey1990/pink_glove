/**
 * The team store.
 *
 * What matters: the list is always what the server last said, a write's
 * refusal reaches the page untouched and changes nothing, and the dev link
 * only ever exists when the response carried one.
 */

import MockAdapter from 'axios-mock-adapter'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { api } from '@/api/client'
import { statusOf } from '@/api/errors'
import { page, teamRow } from '@/lib/team.fixtures'
import { useTeamStore } from './team'

const LIST = '/api/users/memberships/'
const OPTIONS = '/api/users/memberships/invite-options/'

let mock: MockAdapter

/** A row an owner could act on, which is what most of these start from. */
function row (id: string, overrides: Record<string, unknown> = {}) {
  return teamRow(id, {
    available_actions: ['change_role', 'deactivate'],
    assignable_roles: ['dispatcher'],
    ...overrides,
  })
}

beforeEach(() => {
  setActivePinia(createPinia())
  mock = new MockAdapter(api)
})

afterEach(() => mock.restore())

describe('load', () => {
  it('reads the staff list and the roles the caller may invite', async () => {
    mock.onGet(LIST).reply(200, page([row('a')]))
    mock.onGet(OPTIONS).reply(200, { roles: ['dispatcher', 'cleaner'] })
    const store = useTeamStore()

    await store.load()

    expect(store.members.map(member => member.id)).toEqual(['a'])
    expect(store.inviteRoles).toEqual(['dispatcher', 'cleaner'])
    expect(store.canInvite).toBe(true)
    expect(store.loading).toBe(false)
    // Customers are left out on the server, not filtered here.
    expect(mock.history.get.find(call => call.url === LIST)?.params).toMatchObject({ staff: true })
  })

  it('offers no invitation when the server allows no role', async () => {
    mock.onGet(LIST).reply(200, page([]))
    mock.onGet(OPTIONS).reply(200, { roles: [] })
    const store = useTeamStore()

    await store.load()

    expect(store.canInvite).toBe(false)
  })

  it('reports a failure without throwing', async () => {
    mock.onGet(LIST).reply(500)
    mock.onGet(OPTIONS).reply(200, { roles: [] })
    const store = useTeamStore()

    await store.load()

    expect(store.loadError).toBe('Could not load the team.')
    expect(store.loading).toBe(false)
  })

  it('sorts for display without reordering what the server sent', async () => {
    mock.onGet(LIST).reply(200, page([
      row('off', { state: 'deactivated', user_name: 'Aaron' }),
      row('on', { user_name: 'Zed' }),
    ]))
    mock.onGet(OPTIONS).reply(200, { roles: [] })
    const store = useTeamStore()

    await store.load()

    expect(store.sorted.map(member => member.id)).toEqual(['on', 'off'])
    expect(store.members.map(member => member.id)).toEqual(['off', 'on'])
  })
})

describe('invite', () => {
  it('sends the address and the role, and nothing else', async () => {
    const invited = row('new', { state: 'invited', is_active: false })
    mock.onPost(LIST).reply(201, invited)
    mock.onGet(LIST).reply(200, page([row('a'), invited]))
    mock.onGet(OPTIONS).reply(200, { roles: ['cleaner'] })
    const store = useTeamStore()

    const settled = await store.invite('new@example.test', 'cleaner')

    expect(JSON.parse(mock.history.post[0].data)).toEqual({
      email: 'new@example.test',
      role: 'cleaner',
    })
    expect(settled.state).toBe('invited')
    expect(store.members.map(member => member.id)).toEqual(['a', 'new'])
  })

  it('holds the dev link only when the response carried one', async () => {
    mock.onPost(LIST).replyOnce(201, row('one', { dev_link: 'http://localhost:3000/accept-invite/tok' }))
    mock.onPost(LIST).replyOnce(201, row('two'))
    mock.onGet(LIST).reply(200, page([]))
    mock.onGet(OPTIONS).reply(200, { roles: ['cleaner'] })
    const store = useTeamStore()

    await store.invite('one@example.test', 'cleaner')
    expect(store.devLink).toBe('http://localhost:3000/accept-invite/tok')

    // A deployed API never sends it, and a stale one must not linger.
    await store.invite('two@example.test', 'cleaner')
    expect(store.devLink).toBeNull()
  })

  it('lets the server\'s refusal through and leaves the list alone', async () => {
    mock.onGet(LIST).reply(200, page([row('a')]))
    mock.onGet(OPTIONS).reply(200, { roles: ['cleaner'] })
    mock.onPost(LIST).reply(403, { detail: 'Only an owner can manage owners and admins.' })
    const store = useTeamStore()
    await store.load()

    const attempt = store.invite('boss@example.test', 'owner')

    await expect(attempt).rejects.toSatisfy(error => statusOf(error) === 403)
    expect(store.members.map(member => member.id)).toEqual(['a'])
    expect(store.devLink).toBeNull()
  })
})

describe('writes to a row', () => {
  it('re-reads the whole team afterwards, because other rows can change', async () => {
    const store = useTeamStore()
    mock.onGet(OPTIONS).reply(200, { roles: ['cleaner'] })
    mock.onGet(LIST).replyOnce(200, page([row('a'), row('b')]))
    await store.load()

    mock.onPost(`${LIST}a/deactivate/`).reply(200, row('a', {
      state: 'deactivated',
      is_active: false,
      available_actions: ['reactivate'],
    }))
    // The server's second answer: b can no longer be deactivated.
    mock.onGet(LIST).reply(200, page([
      row('a', { state: 'deactivated', is_active: false, available_actions: ['reactivate'] }),
      row('b', { available_actions: [] }),
    ]))

    await store.deactivate('a')

    expect(store.members.find(member => member.id === 'a')?.state).toBe('deactivated')
    expect(store.members.find(member => member.id === 'b')?.available_actions).toEqual([])
  })

  it('keeps the row the server answered with if the re-read fails', async () => {
    const store = useTeamStore()
    mock.onGet(OPTIONS).reply(200, { roles: [] })
    mock.onGet(LIST).replyOnce(200, page([row('a')]))
    await store.load()
    mock.onPatch(`${LIST}a/`).reply(200, row('a', { role: 'dispatcher' }))
    mock.onGet(LIST).reply(500)

    const settled = await store.changeRole('a', 'dispatcher')

    // The write happened; a failed refresh is a banner, not a failed write.
    expect(settled.role).toBe('dispatcher')
    expect(store.members[0].role).toBe('dispatcher')
    expect(store.loadError).toBe('Could not load the team.')
    expect(JSON.parse(mock.history.patch[0].data)).toEqual({ role: 'dispatcher' })
  })

  it('calls the action the page asked for', async () => {
    const store = useTeamStore()
    mock.onGet(OPTIONS).reply(200, { roles: [] })
    mock.onGet(LIST).reply(200, page([row('a')]))
    mock.onPost(`${LIST}a/reactivate/`).reply(200, row('a'))
    mock.onPost(`${LIST}a/resend-invite/`).reply(200, row('a', { dev_link: 'http://x/accept-invite/t' }))

    await store.reactivate('a')
    await store.resend('a')

    expect(mock.history.post.map(call => call.url)).toEqual([
      `${LIST}a/reactivate/`,
      `${LIST}a/resend-invite/`,
    ])
    expect(store.devLink).toBe('http://x/accept-invite/t')

    store.clearDevLink()
    expect(store.devLink).toBeNull()
  })

  it('lets a conflict through untouched', async () => {
    const store = useTeamStore()
    mock.onGet(OPTIONS).reply(200, { roles: [] })
    mock.onGet(LIST).reply(200, page([row('a')]))
    await store.load()
    mock.onPost(`${LIST}a/deactivate/`).reply(409, {
      detail: 'An organization must keep at least one active owner.',
    })

    await expect(store.deactivate('a')).rejects.toSatisfy(error => statusOf(error) === 409)
    expect(store.members[0].state).toBe('active')
  })
})
