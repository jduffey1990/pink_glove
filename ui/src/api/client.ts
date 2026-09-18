/**
 * The one place that knows how to talk to the API.
 *
 * Everything the backend's auth model needs lives here rather than being
 * repeated per call (ADR-018): credentials on every request, the CSRF token
 * echoed back from its cookie on every unsafe one, and the organization header
 * when the signed-in user belongs to more than one.
 *
 * Deliberately a thin wrapper over axios rather than a generated client
 * (ADR-019): the generated *types* are the contract, but the CSRF and
 * organization logic has to stay in one hand-written place.
 */

import type { AxiosError, AxiosInstance, InternalAxiosRequestConfig } from 'axios'
import axios from 'axios'

/**
 * Where the API is. A production build is served BY the API (ADR-027), so it
 * calls back to its own origin with relative URLs; the dev server runs on a
 * different port and needs the address. VITE_API_BASE_URL overrides both,
 * for a frontend hosted somewhere other than the API.
 */
export const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL
  ?? (import.meta.env.DEV ? 'http://localhost:8000' : '')

const CSRF_COOKIE = 'csrftoken'
const UNSAFE_METHODS = new Set(['post', 'put', 'patch', 'delete'])

/** Methods that need a CSRF token. GET/HEAD/OPTIONS do not. */
function isUnsafe (method?: string): boolean {
  return UNSAFE_METHODS.has((method ?? 'get').toLowerCase())
}

export function readCookie (name: string): string | null {
  // document.cookie is the only way to read this: the backend sets csrftoken
  // with HttpOnly off precisely so the SPA can echo it back.
  const match = document.cookie.match(new RegExp(String.raw`(^|;\s*)${name}=([^;]*)`))
  return match ? decodeURIComponent(match[2]) : null
}

/** Called on a 401: the server says there is no session. */
type SessionLostHandler = () => void

let onSessionLost: SessionLostHandler = () => {}

export function setSessionLostHandler (handler: SessionLostHandler): void {
  onSessionLost = handler
}

/**
 * Called on a 403, which does not say whether there is a session.
 *
 * DRF answers 403 both for "signed in, not allowed" and for an expired session
 * (session auth has no challenge header to put on a 401). The first is an
 * ordinary answer -- an unassigned cleaner asking for codes gets one by design
 * (ADR-017) -- so a 403 must not sign anyone out on its own. The handler asks
 * the server which it was.
 */
type SessionDoubtedHandler = () => void

let onSessionDoubted: SessionDoubtedHandler = () => {}

export function setSessionDoubtedHandler (handler: SessionDoubtedHandler): void {
  onSessionDoubted = handler
}

/**
 * Which organization the caller is acting in.
 *
 * Only sent when the user holds more than one membership -- with a single
 * membership the backend resolves the tenant on its own, and sending a header
 * it did not ask for is noise. Supplied as a getter so the client does not
 * import the store (which imports the client).
 */
type OrganizationIdGetter = () => string | null

let getOrganizationId: OrganizationIdGetter = () => null

export function setOrganizationIdGetter (getter: OrganizationIdGetter): void {
  getOrganizationId = getter
}

export function createClient (): AxiosInstance {
  const instance = axios.create({
    baseURL: API_BASE_URL,
    // Session cookie auth, so every request must carry credentials.
    withCredentials: true,
    headers: { Accept: 'application/json' },
  })

  instance.interceptors.request.use((config: InternalAxiosRequestConfig) => {
    if (isUnsafe(config.method)) {
      const token = readCookie(CSRF_COOKIE)
      if (token) {
        config.headers.set('X-CSRFToken', token)
      }
    }

    const organizationId = getOrganizationId()
    if (organizationId) {
      config.headers.set('X-Organization', organizationId)
    }

    return config
  })

  instance.interceptors.response.use(
    response => response,
    (error: AxiosError) => {
      const status = error.response?.status

      // 401 is "you are not signed in". 403 is either "signed in but not
      // allowed" or an expired session DRF reports as a permission failure;
      // only the session endpoint can tell them apart, so it is asked.
      //
      // 404 is NOT treated as a wrong-organization signal: the API returns 404
      // for a cross-tenant read by design (a 403 would confirm the record
      // exists), so reacting to it would sign people out for opening a stale
      // link.
      if (status === 401) {
        onSessionLost()
      } else if (status === 403) {
        onSessionDoubted()
      }

      return Promise.reject(error)
    },
  )

  return instance
}

export const api = createClient()
