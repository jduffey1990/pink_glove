/**
 * How the team page words and orders things, in one place.
 *
 * Presentation only, as with `jobStatus.ts` and `invoiceStatus.ts`. Who may
 * manage whom is the server's to say: each row arrives with the
 * `available_actions` and `assignable_roles` worked out for the caller, and
 * the roles they may invite come from their own endpoint (ADR-023, ADR-028).
 * There is deliberately no table here of which role outranks which.
 */

import type { MembershipState, Role, TeamAction, TeamMember } from '@/api/types'
import { errorDetail, statusOf } from '@/api/errors'

export const ROLE_LABEL: Record<Role, string> = {
  owner: 'Owner',
  admin: 'Admin',
  dispatcher: 'Dispatcher',
  cleaner: 'Cleaner',
  customer: 'Customer',
}

export function roleLabel (role: Role): string {
  return ROLE_LABEL[role]
}

/** A list of roles from the server, shaped for a select. Order is the server's. */
export function roleOptions<R extends Role> (roles: readonly R[]): { value: R, title: string }[] {
  return roles.map(value => ({ value, title: roleLabel(value) }))
}

export const MEMBER_STATE_LOOK: Record<MembershipState, { label: string, color: string }> = {
  active: { label: 'Active', color: 'success' },
  invited: { label: 'Invited', color: 'info' },
  deactivated: { label: 'Deactivated', color: 'grey' },
}

/**
 * How each action is worded on its button.
 *
 * Typed as a full record so that a new action the server starts offering
 * fails the type-check here instead of rendering as a blank menu item.
 */
export const TEAM_ACTION_LOOK: Record<TeamAction, { label: string, icon: string }> = {
  change_role: { label: 'Change role', icon: 'mdi-account-convert-outline' },
  resend_invite: { label: 'Resend invitation', icon: 'mdi-email-fast-outline' },
  deactivate: { label: 'Deactivate', icon: 'mdi-account-off-outline' },
  reactivate: { label: 'Reactivate', icon: 'mdi-account-check-outline' },
}

/** Whether the server is currently offering this action on this row. */
export function offers (member: Pick<TeamMember, 'available_actions'>, action: TeamAction): boolean {
  return member.available_actions.includes(action)
}

/**
 * The actions to draw for a row: the server's list, in a fixed menu order.
 *
 * Anything the server offers that this build has no wording for is dropped
 * rather than drawn blank; anything it does not offer is never added.
 */
export function actionsFor (member: Pick<TeamMember, 'available_actions'>): TeamAction[] {
  return (Object.keys(TEAM_ACTION_LOOK) as TeamAction[]).filter(action => offers(member, action))
}

const STATE_ORDER: Record<MembershipState, number> = { active: 0, invited: 1, deactivated: 2 }

/**
 * Working people first, then invitations still out, then the switched off;
 * by name within each. A new array: the store's own list is left as the
 * server sent it.
 */
export function sortTeam<T extends Pick<TeamMember, 'state' | 'user_name'>> (members: readonly T[]): T[] {
  return members.toSorted((a, b) =>
    STATE_ORDER[a.state] - STATE_ORDER[b.state]
    || a.user_name.localeCompare(b.user_name, undefined, { sensitivity: 'base' }),
  )
}

/**
 * The second line of a row. An invitation shows its address as its name
 * (the server sends it that way until the person accepts), so repeating the
 * address underneath would say the same thing twice.
 */
export function secondLine (member: Pick<TeamMember, 'user_name' | 'user_email'>): string {
  return member.user_name === member.user_email ? '' : member.user_email
}

/**
 * What to say once a write has gone through, read off the row the server
 * answered with rather than assumed: reactivating a withdrawn invitation
 * comes back `invited`, not `active`, and the sentence has to agree.
 */
export function doneMessage (
  action: TeamAction | 'invite',
  row: Pick<TeamMember, 'user_name' | 'state' | 'role'>,
): string {
  const name = row.user_name
  switch (action) {
    case 'invite': {
      return `Invitation sent to ${name}.`
    }
    case 'resend_invite': {
      return `A new invitation was sent to ${name}. The earlier link no longer works.`
    }
    case 'deactivate': {
      return `${name} has been deactivated.`
    }
    case 'reactivate': {
      return row.state === 'invited'
        ? `${name} has been invited again.`
        : `${name} is active again.`
    }
    case 'change_role': {
      return `${name} now has the role ${roleLabel(row.role)}.`
    }
  }
}

/**
 * What to tell someone when a call failed.
 *
 * The server's own sentence when it sent one (ADR-023): "You cannot
 * deactivate yourself" is better than anything written here. Only the two
 * cases with no sentence worth showing are worded locally.
 */
export function failureMessage (error: unknown, fallback: string): string {
  const status = statusOf(error)
  if (status === 429) {
    return 'Too many attempts. Wait a while and try again.'
  }
  if (status === null) {
    return 'Could not reach the server. Check your connection and try again.'
  }
  return errorDetail(error) ?? fallback
}
