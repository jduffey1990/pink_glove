/**
 * Shared by the team specs (lib, store, page). Not shipped: nothing outside
 * a `*.spec.ts` imports this.
 */

import type { OrganizationSummary, TeamMember } from '@/api/types'

/** A paid-up organization: writable, and with nothing for the banner to say. */
export const ORGANIZATION: OrganizationSummary = {
  id: 'org-a',
  name: 'Sparkle Clean',
  slug: 'sparkle-clean',
  timezone: 'America/Denver',
  primary_color: '',
  logo: null,
  is_active: true,
  billing: { state: 'active', level: null, notice: null },
}

/**
 * The same organization gone read-only (Phase 4c). Its billing state is
 * `deactivated`, which is also a word a membership's own `state` uses: the
 * two are different fields on different objects, and a spec uses this to
 * prove the team page never reads one for the other.
 */
export const READ_ONLY_ORGANIZATION: OrganizationSummary = {
  ...ORGANIZATION,
  is_active: false,
  billing: { state: 'deactivated', level: 'error', notice: 'This organization is read-only.' },
}

/**
 * A team row as the API sends it: an active cleaner the caller may do
 * nothing to, until a spec says otherwise.
 */
export function teamRow (id = 'm-1', overrides: Record<string, unknown> = {}): TeamMember {
  return {
    id,
    organization: ORGANIZATION,
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
  } as TeamMember
}

/** DRF's pagination envelope around a list of rows. */
export function page (rows: unknown[]) {
  return { count: rows.length, next: null, previous: null, results: rows }
}

/** Vuetify measures things; jsdom has nothing to measure with. */
export function stubResizeObserver (): void {
  globalThis.ResizeObserver ??= class {
    observe () {}
    unobserve () {}
    disconnect () {}
  }
}
