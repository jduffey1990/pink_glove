/**
 * Who is signed in, and which organization they are acting in.
 *
 * The boot sequence is the backend's, not an invention here:
 *
 *   1. GET /api/users/session/  -- an empty object means nobody is signed in.
 *      This call is also what sets the csrftoken cookie, so it must happen
 *      before the first POST (which is the login).
 *   2. POST /api/auth/login/    -- 202 means a 2FA code was sent, 200 means
 *      the session is already established (trusted device, or a role that is
 *      not challenged).
 *   3. POST /api/auth/verify/   -- on success, re-fetch the session.
 *   4. More than one membership and no current_organization -> the picker.
 */

import type { Membership, Role, Session } from '@/api/types'
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import {
  api,
  setOrganizationIdGetter,
  setSessionDoubtedHandler,
  setSessionLostHandler,
} from '@/api/client'

/** Mirrors users.enums.DISPATCHER_ROLES on the backend. */
const DISPATCHER_ROLES = new Set<Role>(['owner', 'admin', 'dispatcher'])

/** Mirrors users.enums.ADMIN_ROLES. */
const ADMIN_ROLES = new Set<Role>(['owner', 'admin'])

/** Where the organization choice is remembered across a page reload. */
const ORGANIZATION_STORAGE_KEY = 'pink_glove.organization'

export type LoginOutcome = 'signed-in' | 'code-required'

function readStoredOrganization (): string | null {
  try {
    return globalThis.localStorage?.getItem(ORGANIZATION_STORAGE_KEY) ?? null
  } catch {
    // Private windows and blocked site data both throw here. A remembered
    // organization is a convenience; losing it just means picking again.
    return null
  }
}

function writeStoredOrganization (id: string | null): void {
  try {
    if (id === null) {
      globalThis.localStorage?.removeItem(ORGANIZATION_STORAGE_KEY)
    } else {
      globalThis.localStorage?.setItem(ORGANIZATION_STORAGE_KEY, id)
    }
  } catch {
    // See above -- not worth failing a sign-in over.
  }
}

export const useSessionStore = defineStore('session', () => {
  const user = ref<Session | null>(null)
  const ready = ref(false)
  const organizationId = ref<string | null>(readStoredOrganization())

  /**
   * The code, when the backend chose to hand it over.
   *
   * `dev_code` is present only when the API runs with `LOCAL = True`, which is
   * never a deployed environment -- the backend gates it, not this store. It
   * exists so a developer with no mail server can still sign in, and surfacing
   * it here saves digging through DevTools for something the response already
   * contains.
   */
  const devCode = ref<string | null>(null)

  const isAuthenticated = computed(() => user.value !== null)
  const memberships = computed<Membership[]>(() => user.value?.memberships ?? [])

  /**
   * The role the caller holds in the organization they are acting in.
   *
   * Taken from the membership list when the server has not resolved one yet,
   * so the picker and the first render agree about what this person can do.
   */
  const role = computed(() => {
    if (user.value?.current_role) {
      return user.value.current_role
    }
    const chosen = memberships.value.find(m => m.organization.id === organizationId.value)
    return chosen?.role ?? null
  })

  const organization = computed(() => {
    if (user.value?.current_organization) {
      return user.value.current_organization
    }
    return memberships.value.find(m => m.organization.id === organizationId.value)?.organization ?? null
  })

  const isStaff = computed(() => role.value !== null && role.value !== 'customer')
  const isDispatcherOrHigher = computed(
    () => role.value !== null && DISPATCHER_ROLES.has(role.value),
  )
  const isAdminOrHigher = computed(
    () => role.value !== null && ADMIN_ROLES.has(role.value),
  )
  const isCleaner = computed(() => role.value === 'cleaner')
  const isCustomer = computed(() => role.value === 'customer')

  /**
   * The zone every date on every screen is reckoned in.
   *
   * Never the browser's: a dispatcher in Denver looking at "Monday" must see
   * the day the API filed the job under (CLAUDE.md invariant 8). Six pages
   * were each deriving this the same way; it belongs here, once.
   */
  const timeZone = computed(() => organization.value?.timezone ?? 'UTC')

  /** True when the user must choose before anything else can be scoped. */
  const needsOrganizationChoice = computed(
    () => isAuthenticated.value && memberships.value.length > 1 && organization.value === null,
  )

  function clear (): void {
    user.value = null
    devCode.value = null
    organizationId.value = null
    writeStoredOrganization(null)
  }

  function chooseOrganization (id: string): void {
    organizationId.value = id
    writeStoredOrganization(id)
  }

  /**
   * Fetch the session. Safe to call when signed out -- the endpoint answers
   * 200 with an empty object rather than 401, and sets the CSRF cookie either
   * way, which is the point of calling it first.
   */
  async function refresh (): Promise<Session | null> {
    const { data } = await api.get<Session | Record<string, never>>('/api/users/session/')
    user.value = 'id' in data && data.id ? (data as Session) : null

    if (user.value === null) {
      organizationId.value = null
    } else if (user.value.memberships.length === 1) {
      // With a single membership the backend resolves the tenant itself; hold
      // the id anyway so the rest of the app can read it uniformly.
      chooseOrganization(user.value.memberships[0].organization.id)
    } else if (organizationId.value !== null) {
      // A remembered choice the user no longer holds must not be sent.
      const stillAMember = user.value.memberships.some(
        m => m.organization.id === organizationId.value,
      )
      if (!stillAMember) {
        chooseOrganization(user.value.memberships[0]?.organization.id ?? '')
      }
    }

    return user.value
  }

  async function boot (): Promise<void> {
    try {
      await refresh()
    } catch {
      // A failed boot means signed out, not broken. The router sends them to
      // the login page and the CSRF cookie is set by then regardless.
      user.value = null
    } finally {
      ready.value = true
    }
  }

  async function login (email: string, password: string): Promise<LoginOutcome> {
    const response = await api.post('/api/auth/login/', { email, password })

    if (response.status === 202) {
      devCode.value = response.data?.dev_code ?? null
      return 'code-required'
    }

    await refresh()
    return 'signed-in'
  }

  async function verify (code: string, rememberDevice = false): Promise<void> {
    await api.post('/api/auth/verify/', { code, remember_device: rememberDevice })
    devCode.value = null
    await refresh()
  }

  async function resendCode (): Promise<void> {
    const response = await api.post('/api/auth/resend/')
    devCode.value = response.data?.dev_code ?? null
  }

  async function logout (): Promise<void> {
    try {
      await api.post('/api/users/logout/')
    } finally {
      clear()
    }
  }

  return {
    user,
    ready,
    organizationId,
    devCode,
    isAuthenticated,
    memberships,
    role,
    organization,
    isStaff,
    isDispatcherOrHigher,
    isAdminOrHigher,
    isCleaner,
    isCustomer,
    timeZone,
    needsOrganizationChoice,
    boot,
    refresh,
    login,
    verify,
    resendCode,
    logout,
    chooseOrganization,
    clear,
  }
})

/**
 * Wire the store into the client.
 *
 * The client cannot import the store (the store imports the client), so the
 * two are joined here, once, at startup.
 */
export function connectSessionToClient (store: ReturnType<typeof useSessionStore>): void {
  setOrganizationIdGetter(() =>
    // Only meaningful with more than one membership; see client.ts.
    store.memberships.length > 1 ? store.organizationId : null,
  )

  setSessionLostHandler(() => {
    if (store.isAuthenticated) {
      store.clear()
    }
  })

  // A 403 might be an expired session or might be a plain "no". Ask, and sign
  // out only if the server says nobody is signed in. One probe at a time: a
  // page that fires five requests gets five 403s.
  let probing = false
  setSessionDoubtedHandler(() => {
    if (!store.isAuthenticated || probing) {
      return
    }
    probing = true
    store.refresh()
      .then(user => {
        if (user === null) {
          store.clear()
        }
      })
      .catch(() => {
        // Could not ask. Leave the session alone; the next request will tell.
      })
      .finally(() => {
        probing = false
      })
  })
}

// Lives with the other error readers now; re-exported for its existing callers.
export { statusOf } from '@/api/errors'
