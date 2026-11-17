/**
 * Reading what the API said went wrong.
 *
 * The server's own sentence is nearly always better than one invented in a
 * page ("You are already clocked in to this job"), and ADR-023 wants its
 * wording shown rather than restated. DRF sends either `{detail: "..."}` or,
 * for a validation failure, `{field: ["...", ...]}`.
 */

import axios from 'axios'

/** Narrow an axios error to its status, for callers that branch on it. */
export function statusOf (error: unknown): number | null {
  return axios.isAxiosError(error) ? (error.response?.status ?? null) : null
}

/** The response body, if there was a response and it was an object. */
export function bodyOf<T = Record<string, unknown>> (error: unknown): T | null {
  if (!axios.isAxiosError(error)) {
    return null
  }
  const data = error.response?.data
  return data !== null && typeof data === 'object' ? (data as T) : null
}

/** The server's explanation: `detail`, else the first field error, else null. */
export function errorDetail (error: unknown): string | null {
  const body = bodyOf(error)
  if (body === null) {
    return null
  }
  if (typeof body.detail === 'string') {
    return body.detail
  }
  for (const value of Object.values(body)) {
    const first = Array.isArray(value) ? value[0] : value
    if (typeof first === 'string') {
      return first
    }
  }
  return null
}

/**
 * A validation failure, field by field: `{ field: ["message", ...] }`.
 *
 * For a form that shows each message under the input it is about. DRF sends a
 * list per field, sometimes a bare string, and `detail` or
 * `non_field_errors` for what belongs to no field; all come back as lists
 * under the key the server used, so the caller decides where each one goes.
 * Empty when there was no response body to read.
 */
export function fieldErrors (error: unknown): Record<string, string[]> {
  const body = bodyOf(error)
  if (body === null) {
    return {}
  }

  const errors: Record<string, string[]> = {}
  for (const [field, value] of Object.entries(body)) {
    const messages = (Array.isArray(value) ? value : [value])
      .filter((message): message is string => typeof message === 'string')
    if (messages.length > 0) {
      errors[field] = messages
    }
  }
  return errors
}
