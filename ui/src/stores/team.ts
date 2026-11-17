/**
 * The team: who works here, and the writes an owner or admin makes to it.
 *
 * The store holds what the server sent and nothing derived from a role
 * table. Every row carries the `available_actions` the server worked out for
 * the caller, and after any write the whole list is read again, because one
 * change can alter what another row offers: demote one of two owners and the
 * other becomes the last one, who can no longer be deactivated (ADR-034).
 */

import type { InvitedTeamMember, StaffRole, TeamMember } from '@/api/types'
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import {
  changeMemberRole,
  deactivateMember,
  getInviteOptions,
  inviteMember,
  listTeam,
  reactivateMember,
  resendInvite,
} from '@/api/endpoints'
import { sortTeam } from '@/lib/team'

export const useTeamStore = defineStore('team', () => {
  const members = ref<TeamMember[]>([])
  /** The roles the caller may invite someone as. Empty means they may not invite. */
  const inviteRoles = ref<StaffRole[]>([])
  const loading = ref(false)
  const loadError = ref('')

  /**
   * The invitation link, when the backend chose to hand it over.
   *
   * `dev_link` is present only when the API runs with `LOCAL = True`, which
   * is never a deployed environment: the backend gates it, not this store.
   * Same arrangement as the session store's `devCode`.
   */
  const devLink = ref<string | null>(null)

  const sorted = computed(() => sortTeam(members.value))
  const canInvite = computed(() => inviteRoles.value.length > 0)

  /** Read the team and what the caller may invite. `quiet` skips the spinner. */
  async function load (options: { quiet?: boolean } = {}): Promise<void> {
    loading.value = !options.quiet
    loadError.value = ''

    try {
      const [team, invite] = await Promise.all([listTeam(), getInviteOptions()])
      members.value = team.results
      inviteRoles.value = invite.roles
    } catch {
      loadError.value = 'Could not load the team.'
    } finally {
      loading.value = false
    }
  }

  /**
   * Put the row the server answered with in place, then read the rest again.
   * Returns that row, so the page can say what happened in terms of it.
   */
  async function settle (row: TeamMember | InvitedTeamMember): Promise<TeamMember> {
    devLink.value = ('dev_link' in row ? row.dev_link : null) ?? null

    const known = members.value.some(member => member.id === row.id)
    members.value = known
      ? members.value.map(member => (member.id === row.id ? row : member))
      : [...members.value, row]

    // The write has succeeded. `load` reports its own failure as a banner
    // and never throws, so a failed re-read cannot make the write look failed.
    await load({ quiet: true })
    return row
  }

  // Each of these lets a refusal reach the caller: the page shows the
  // server's own sentence for it (ADR-023) and the list is left untouched.

  async function invite (email: string, role: StaffRole): Promise<TeamMember> {
    return settle(await inviteMember({ email, role }))
  }

  async function changeRole (id: string, role: StaffRole): Promise<TeamMember> {
    return settle(await changeMemberRole(id, role))
  }

  async function deactivate (id: string): Promise<TeamMember> {
    return settle(await deactivateMember(id))
  }

  async function reactivate (id: string): Promise<TeamMember> {
    return settle(await reactivateMember(id))
  }

  async function resend (id: string): Promise<TeamMember> {
    return settle(await resendInvite(id))
  }

  function clearDevLink (): void {
    devLink.value = null
  }

  return {
    members,
    sorted,
    inviteRoles,
    canInvite,
    loading,
    loadError,
    devLink,
    load,
    invite,
    changeRole,
    deactivate,
    reactivate,
    resend,
    clearDevLink,
  }
})
