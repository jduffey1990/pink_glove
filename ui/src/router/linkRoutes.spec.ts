/**
 * The emailed links' routes (ADR-036), against the real route table and a
 * real browser history (jsdom's), because what matters is what ends up in
 * the address bar: the token in the fragment, and nothing of it in the path
 * or the query, with the old address replaced rather than kept behind it.
 */

import { afterEach, describe, expect, it } from 'vitest'
import { createRouter, createWebHistory } from 'vue-router'
import { routes } from './index'

/** Load `url` the way a click in a mail client does: a fresh page. */
async function open (url: string) {
  window.history.replaceState(null, '', url)
  const before = window.history.length
  const history = createWebHistory()
  const router = createRouter({ history, routes })
  // What `app.use(router)` does on a page load.
  await router.push(history.location)
  await router.isReady()
  return { router, before }
}

afterEach(() => window.history.replaceState(null, '', '/'))

describe.each([
  ['pay', '/pay'],
  ['accept-invite', '/accept-invite'],
  ['reset-password', '/reset-password'],
])('%s', (name, path) => {
  it('opens the fragment form as it is', async () => {
    const { router } = await open(`${path}#token=tok-new`)

    expect(router.currentRoute.value.name).toBe(name)
    expect(router.currentRoute.value.hash).toBe('#token=tok-new')
    expect(window.location.pathname).toBe(path)
  })

  it('rewrites the old path form to the fragment form, in place', async () => {
    const { router, before } = await open(`${path}/tok-old_1-2`)

    expect(router.currentRoute.value.name).toBe(name)
    expect(window.location.pathname).toBe(path)
    expect(window.location.search).toBe('')
    expect(window.location.hash).toBe('#token=tok-old_1-2')
    expect(window.location.pathname + window.location.search).not.toContain('tok-old')
    // Replaced, not pushed: Back does not lead to the old address.
    expect(window.history.length).toBe(before)
  })
})

describe('the pay page back from Stripe', () => {
  it('keeps ?paid=1 and moves a signed token to the fragment', async () => {
    const token = 'ImFiYyI:1xTixv:818Sbpq'

    const { router } = await open(`/pay/${token}?paid=1`)

    expect(router.currentRoute.value.query).toEqual({ paid: '1' })
    expect(window.location.pathname).toBe('/pay')
    expect(window.location.search).toBe('?paid=1')
    expect(window.location.hash).toBe(`#token=${token}`)
  })
})
