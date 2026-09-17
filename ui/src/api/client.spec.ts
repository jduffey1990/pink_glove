/**
 * The client's interceptors.
 *
 * These are the three things that are invisible when they break: a missing
 * CSRF header turns every write into a 403, a stray organization header sends
 * a single-membership user somewhere they did not ask for, and a 404 treated
 * as a lost session signs people out for opening a stale link.
 */

import type { AxiosInstance } from 'axios'
import MockAdapter from 'axios-mock-adapter'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  createClient,
  readCookie,
  setOrganizationIdGetter,
  setSessionDoubtedHandler,
  setSessionLostHandler,
} from './client'

function setCookie (value: string): void {
  Object.defineProperty(document, 'cookie', {
    writable: true,
    value,
  })
}

describe('readCookie', () => {
  it('reads a cookie by name', () => {
    setCookie('csrftoken=abc123; sessionid=zzz')

    expect(readCookie('csrftoken')).toBe('abc123')
  })

  it('returns null when the cookie is absent', () => {
    setCookie('sessionid=zzz')

    expect(readCookie('csrftoken')).toBeNull()
  })

  it('does not match a cookie whose name merely ends with the one asked for', () => {
    setCookie('xcsrftoken=wrong; csrftoken=right')

    expect(readCookie('csrftoken')).toBe('right')
  })

  it('decodes an encoded value', () => {
    setCookie('csrftoken=a%20b')

    expect(readCookie('csrftoken')).toBe('a b')
  })
})

describe('the CSRF header', () => {
  let client: AxiosInstance
  let mock: MockAdapter

  beforeEach(() => {
    setCookie('csrftoken=token-from-cookie')
    setOrganizationIdGetter(() => null)
    setSessionLostHandler(() => {})
    client = createClient()
    mock = new MockAdapter(client)
  })

  afterEach(() => mock.restore())

  it('is attached to a POST', async () => {
    mock.onPost('/api/thing/').reply(200, {})

    await client.post('/api/thing/', {})

    expect(mock.history.post[0].headers?.['X-CSRFToken']).toBe('token-from-cookie')
  })

  it.each(['put', 'patch', 'delete'] as const)('is attached to a %s', async method => {
    mock.onAny('/api/thing/').reply(200, {})

    await client[method]('/api/thing/')

    expect(mock.history[method][0].headers?.['X-CSRFToken']).toBe('token-from-cookie')
  })

  it('is not attached to a GET', async () => {
    mock.onGet('/api/thing/').reply(200, {})

    await client.get('/api/thing/')

    expect(mock.history.get[0].headers?.['X-CSRFToken']).toBeUndefined()
  })

  it('is omitted when there is no cookie yet', async () => {
    setCookie('')
    mock.onPost('/api/thing/').reply(200, {})

    await client.post('/api/thing/', {})

    expect(mock.history.post[0].headers?.['X-CSRFToken']).toBeUndefined()
  })

  it('sends credentials on every request', async () => {
    mock.onGet('/api/thing/').reply(200, {})

    await client.get('/api/thing/')

    expect(mock.history.get[0].withCredentials).toBe(true)
  })
})

describe('the organization header', () => {
  let client: AxiosInstance
  let mock: MockAdapter

  beforeEach(() => {
    setCookie('csrftoken=t')
    setSessionLostHandler(() => {})
    client = createClient()
    mock = new MockAdapter(client)
  })

  afterEach(() => mock.restore())

  it('is attached when the getter supplies one', async () => {
    setOrganizationIdGetter(() => 'org-123')
    mock.onGet('/api/thing/').reply(200, {})

    await client.get('/api/thing/')

    expect(mock.history.get[0].headers?.['X-Organization']).toBe('org-123')
  })

  it('is omitted when the getter returns null', async () => {
    // Which is what a single-membership user looks like: the backend resolves
    // the tenant itself and a header it did not ask for is noise.
    setOrganizationIdGetter(() => null)
    mock.onGet('/api/thing/').reply(200, {})

    await client.get('/api/thing/')

    expect(mock.history.get[0].headers?.['X-Organization']).toBeUndefined()
  })

  it('is read per request rather than captured once', async () => {
    let current = 'org-a'
    setOrganizationIdGetter(() => current)
    mock.onGet('/api/thing/').reply(200, {})

    await client.get('/api/thing/')
    current = 'org-b'
    await client.get('/api/thing/')

    expect(mock.history.get[0].headers?.['X-Organization']).toBe('org-a')
    expect(mock.history.get[1].headers?.['X-Organization']).toBe('org-b')
  })
})

describe('losing the session', () => {
  let client: AxiosInstance
  let mock: MockAdapter
  const onLost = vi.fn()
  const onDoubted = vi.fn()

  beforeEach(() => {
    onLost.mockClear()
    onDoubted.mockClear()
    setCookie('csrftoken=t')
    setOrganizationIdGetter(() => null)
    setSessionLostHandler(onLost)
    setSessionDoubtedHandler(onDoubted)
    client = createClient()
    mock = new MockAdapter(client)
  })

  afterEach(() => mock.restore())

  it('a 401 clears the session', async () => {
    mock.onGet('/api/thing/').reply(401, {})

    await expect(client.get('/api/thing/')).rejects.toThrow()

    expect(onLost).toHaveBeenCalledOnce()
    expect(onDoubted).not.toHaveBeenCalled()
  })

  it('a 403 does NOT clear the session -- it asks whether there still is one', async () => {
    // "Signed in, not allowed" is an ordinary answer: an unassigned cleaner
    // asking for codes gets a 403 by design. Signing them out for it would
    // turn every permission rule on the backend into a logout.
    mock.onGet('/api/thing/').reply(403, {})

    await expect(client.get('/api/thing/')).rejects.toThrow()

    expect(onLost).not.toHaveBeenCalled()
    expect(onDoubted).toHaveBeenCalledOnce()
  })

  it('a 404 does NOT clear the session', async () => {
    // The API answers 404 for a cross-tenant read by design -- a 403 would
    // confirm the record exists. Signing people out for opening a stale link
    // would be the wrong reaction to that.
    mock.onGet('/api/thing/').reply(404, {})

    await expect(client.get('/api/thing/')).rejects.toThrow()

    expect(onLost).not.toHaveBeenCalled()
  })

  it('a 500 does not clear the session', async () => {
    mock.onGet('/api/thing/').reply(500, {})

    await expect(client.get('/api/thing/')).rejects.toThrow()

    expect(onLost).not.toHaveBeenCalled()
  })

  it('a success does not clear the session', async () => {
    mock.onGet('/api/thing/').reply(200, {})

    await client.get('/api/thing/')

    expect(onLost).not.toHaveBeenCalled()
  })

  it('rejects rather than swallowing the error', async () => {
    mock.onGet('/api/thing/').reply(403, { detail: 'nope' })

    await expect(client.get('/api/thing/')).rejects.toMatchObject({
      response: { status: 403 },
    })
  })
})
