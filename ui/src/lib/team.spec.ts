/**
 * The team page's wording and ordering.
 *
 * The one thing worth pinning hardest: the buttons a row gets are exactly
 * the server's `available_actions`, and nothing here adds one from a role.
 */

import type { TeamMember } from '@/api/types'
import { AxiosError, type AxiosResponse } from 'axios'
import { describe, expect, it } from 'vitest'
import {
  actionsFor,
  doneMessage,
  failureMessage,
  MEMBER_STATE_LOOK,
  offers,
  roleLabel,
  roleOptions,
  secondLine,
  sortTeam,
  TEAM_ACTION_LOOK,
} from './team'
import { teamRow } from './team.fixtures'

function row (overrides: Record<string, unknown> = {}): TeamMember {
  return teamRow('m-1', {
    user_name: 'Maria Lopez',
    user_email: 'maria@example.test',
    ...overrides,
  })
}

function failure (status: number, data: unknown): AxiosError {
  const response = { status, data } as AxiosResponse
  return new AxiosError('failed', 'ERR_BAD_REQUEST', undefined, undefined, response)
}

describe('actionsFor', () => {
  it('draws exactly what the server offers', () => {
    const member = row({ available_actions: ['deactivate', 'change_role'] })

    expect(actionsFor(member)).toEqual(['change_role', 'deactivate'])
  })

  it('draws nothing when the server offers nothing, whatever the role', () => {
    // An owner's own row, a customer, anyone seen by a dispatcher: the server
    // says no by sending an empty list, and the page must not know better.
    for (const role of ['owner', 'admin', 'dispatcher', 'cleaner', 'customer'] as const) {
      expect(actionsFor(row({ role, available_actions: [] }))).toEqual([])
    }
  })

  it('offers resend only on a row the server says may be resent', () => {
    expect(actionsFor(row({ state: 'invited', available_actions: [] }))).toEqual([])
    expect(actionsFor(row({ state: 'invited', available_actions: ['resend_invite'] })))
      .toEqual(['resend_invite'])
  })

  it('drops an action this build has no wording for instead of drawing it blank', () => {
    expect(actionsFor(row({ available_actions: ['teleport', 'reactivate'] })))
      .toEqual(['reactivate'])
  })

  it('has wording for every action it can draw', () => {
    for (const look of Object.values(TEAM_ACTION_LOOK)) {
      expect(look.label).not.toBe('')
      expect(look.icon).toMatch(/^mdi-/)
    }
  })
})

describe('offers', () => {
  it('is the server\'s answer', () => {
    const member = row({ available_actions: ['reactivate'] })

    expect(offers(member, 'reactivate')).toBe(true)
    expect(offers(member, 'deactivate')).toBe(false)
  })
})

describe('roleOptions', () => {
  it('keeps the server\'s roles and the server\'s order', () => {
    expect(roleOptions(['dispatcher', 'cleaner'])).toEqual([
      { value: 'dispatcher', title: 'Dispatcher' },
      { value: 'cleaner', title: 'Cleaner' },
    ])
  })

  it('is empty when the server allows no role', () => {
    expect(roleOptions([])).toEqual([])
  })

  it('labels every role', () => {
    expect(roleLabel('owner')).toBe('Owner')
    expect(roleLabel('customer')).toBe('Customer')
  })
})

describe('sortTeam', () => {
  it('puts working people first, then invitations, then the deactivated', () => {
    const sorted = sortTeam([
      row({ id: 'off', state: 'deactivated', user_name: 'Aaron' }),
      row({ id: 'invited', state: 'invited', user_name: 'bea@example.test' }),
      row({ id: 'active', state: 'active', user_name: 'Zed' }),
    ])

    expect(sorted.map(member => member.id)).toEqual(['active', 'invited', 'off'])
  })

  it('orders by name within a state, ignoring case', () => {
    const sorted = sortTeam([
      row({ id: '3', user_name: 'zoe' }),
      row({ id: '1', user_name: 'Adam' }),
      row({ id: '2', user_name: 'bea' }),
    ])

    expect(sorted.map(member => member.id)).toEqual(['1', '2', '3'])
  })

  it('leaves the list it was given alone', () => {
    const members = [row({ id: 'b', user_name: 'B' }), row({ id: 'a', user_name: 'A' })]

    sortTeam(members)

    expect(members.map(member => member.id)).toEqual(['b', 'a'])
  })

  it('has a look for every state', () => {
    expect(Object.keys(MEMBER_STATE_LOOK).toSorted()).toEqual(['active', 'deactivated', 'invited'])
  })
})

describe('secondLine', () => {
  it('shows the address under a name', () => {
    expect(secondLine(row())).toBe('maria@example.test')
  })

  it('does not repeat the address under itself for an invitation', () => {
    expect(secondLine(row({ user_name: 'new@example.test', user_email: 'new@example.test' })))
      .toBe('')
  })
})

describe('doneMessage', () => {
  it('reads a reactivation off the row that came back', () => {
    expect(doneMessage('reactivate', row({ state: 'active' }))).toBe('Maria Lopez is active again.')
    // A withdrawn invitation comes back as an invitation, not as access.
    expect(doneMessage('reactivate', row({ state: 'invited', user_name: 'new@example.test' })))
      .toBe('new@example.test has been invited again.')
  })

  it('names the new role', () => {
    expect(doneMessage('change_role', row({ role: 'dispatcher' })))
      .toBe('Maria Lopez now has the role Dispatcher.')
  })

  it('says the earlier link is dead after a resend', () => {
    expect(doneMessage('resend_invite', row())).toContain('The earlier link no longer works.')
  })

  it('has a sentence for the rest', () => {
    expect(doneMessage('invite', row({ user_name: 'new@example.test' })))
      .toBe('Invitation sent to new@example.test.')
    expect(doneMessage('deactivate', row())).toBe('Maria Lopez has been deactivated.')
  })
})

describe('failureMessage', () => {
  it('shows the server\'s own sentence', () => {
    expect(failureMessage(failure(403, { detail: 'You cannot deactivate yourself.' }), 'nope'))
      .toBe('You cannot deactivate yourself.')
    expect(failureMessage(failure(400, { email: ['That person already belongs here.'] }), 'nope'))
      .toBe('That person already belongs here.')
  })

  it('does not show the throttle\'s countdown', () => {
    const throttled = failure(429, { detail: 'Request was throttled. Expected available in 3412 seconds.' })

    expect(failureMessage(throttled, 'nope')).toBe('Too many attempts. Wait a while and try again.')
  })

  it('says so when the server could not be reached', () => {
    expect(failureMessage(new AxiosError('network down'), 'nope'))
      .toBe('Could not reach the server. Check your connection and try again.')
  })

  it('falls back when the server said nothing usable', () => {
    expect(failureMessage(failure(500, '<html>Server Error</html>'), 'Could not do that.'))
      .toBe('Could not do that.')
  })
})
