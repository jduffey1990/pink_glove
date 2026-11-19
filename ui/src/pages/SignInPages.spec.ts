/**
 * A customer's two sign-in pages (ADR-039): asking for a link, and opening
 * it. What this pins: the token is read from the address's fragment and
 * travels only in a request body; nothing is spent until the button is
 * pressed; a success re-reads the session and lands on `/`; and a missing,
 * garbled or dead token shows the invalid state with a way to a new link.
 *
 * jsdom, not a browser: nothing here says how the pages look.
 */

import type { Component } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import MockAdapter from 'axios-mock-adapter'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'
import { createVuetify } from 'vuetify'
import { api } from '@/api/client'
import { INCOMPLETE_LINK } from '@/lib/linkToken'
import { ORGANIZATION, stubResizeObserver } from '@/lib/team.fixtures'
import { useSessionStore } from '@/stores/session'
import SignInLinkPage from './SignInLinkPage.vue'
import SignInPage from './SignInPage.vue'

const REQUEST = '/api/users/magic-link/'
const CONSUME = '/api/users/magic-link/consume/'
const SESSION = '/api/users/session/'
const DEAD = 'That sign-in link is invalid or has expired.'

let mock: MockAdapter

const Blank = { template: '<div />' }

async function mountAt (component: Component, path: string) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/login', name: 'login', component: Blank },
      { path: '/', name: 'home', component: Blank },
      { path: '/sign-in', name: 'sign-in', component: SignInPage },
      { path: '/sign-in-link', name: 'sign-in-link', component: SignInLinkPage },
    ],
  })
  router.push(path)
  await router.isReady()

  const pinia = createPinia()
  setActivePinia(pinia)

  const wrapper = mount(component, { global: { plugins: [pinia, router, createVuetify()] } })
  await flushPromises()
  return { wrapper, router }
}

function sent (url: string): Record<string, unknown> {
  return JSON.parse(mock.history.post.findLast(call => call.url === url)?.data ?? '{}')
}

function button (wrapper: Awaited<ReturnType<typeof mountAt>>['wrapper'], label: string) {
  const found = wrapper.findAll('button').find(b => b.text().includes(label))
  if (!found) {
    throw new Error(`No button "${label}" in: ${wrapper.text()}`)
  }
  return found
}

beforeEach(() => {
  stubResizeObserver()
  mock = new MockAdapter(api)
})

afterEach(() => mock.restore())

describe('SignInPage', () => {
  it('reads the fragment and spends nothing until asked', async () => {
    const { wrapper } = await mountAt(SignInPage, '/sign-in#token=tok-123')

    expect(wrapper.text()).toContain('Your sign-in link is ready')
    expect(mock.history.post).toHaveLength(0)
  })

  it('sends the token in the body, re-reads the session and lands on /', async () => {
    mock.onPost(CONSUME).reply(200, {})
    mock.onGet(SESSION).reply(200, {
      id: 'user-1',
      email: 'homeowner@example.test',
      first_name: 'Dana',
      last_name: 'Henderson',
      full_name: 'Dana Henderson',
      phone: '',
      is_superuser: false,
      memberships: [{ id: 'm-1', organization: ORGANIZATION, role: 'customer', is_active: true }],
      current_organization: ORGANIZATION,
      current_role: 'customer',
    })
    const { wrapper, router } = await mountAt(SignInPage, '/sign-in#token=tok-123')

    await button(wrapper, 'Sign in').trigger('click')
    await flushPromises()

    expect(sent(CONSUME)).toEqual({ token: 'tok-123' })
    expect(mock.history.post.every(call => !call.url?.includes('tok-123'))).toBe(true)
    expect(mock.history.get.every(call => !call.url?.includes('tok-123'))).toBe(true)
    expect(useSessionStore().isCustomer).toBe(true)
    expect(router.currentRoute.value.name).toBe('home')
  })

  it('shows the server\'s sentence for a garbled or dead link, and a way to a new one', async () => {
    mock.onPost(CONSUME).reply(401, { detail: DEAD })
    const { wrapper, router } = await mountAt(SignInPage, '/sign-in#token=not-a-real-token')

    await button(wrapper, 'Sign in').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain(DEAD)
    expect(router.currentRoute.value.name).toBe('sign-in')

    await button(wrapper, 'Email me a new link').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('sign-in-link')
  })

  it.each(['/sign-in', '/sign-in#token=', '/sign-in#other=tok-123'])(
    'says a link with no token is incomplete and asks the server nothing: %s',
    async path => {
      const { wrapper } = await mountAt(SignInPage, path)

      expect(wrapper.text()).toContain(INCOMPLETE_LINK)
      expect(wrapper.text()).toContain('Email me a new link')
      expect(wrapper.findAll('button').some(b => b.text() === 'Sign in')).toBe(false)
      expect(mock.history.post).toHaveLength(0)
    },
  )
})

describe('SignInLinkPage', () => {
  it('sends the address and shows the server\'s sentence word for word', async () => {
    const detail = 'If that address has an account, a sign-in link is on its way.'
    mock.onPost(REQUEST).reply(202, { detail })
    const { wrapper } = await mountAt(SignInLinkPage, '/sign-in-link')

    await wrapper.find('input').setValue('  homeowner@example.test ')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(sent(REQUEST)).toEqual({ email: 'homeowner@example.test' })
    expect(wrapper.text()).toContain(detail)
    expect(wrapper.text()).not.toContain('Dev link')
    expect(wrapper.find('form').exists()).toBe(false)
  })

  it('shows the link only when the API handed one back', async () => {
    mock.onPost(REQUEST).reply(202, {
      detail: 'If that address has an account, a sign-in link is on its way.',
      dev_link: 'http://localhost:3000/sign-in#token=tok-9',
    })
    const { wrapper } = await mountAt(SignInLinkPage, '/sign-in-link')

    await wrapper.find('input').setValue('homeowner@example.test')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(wrapper.find('a').attributes('href')).toBe('http://localhost:3000/sign-in#token=tok-9')
  })

  it('does not show the throttle\'s countdown', async () => {
    mock.onPost(REQUEST).reply(429, {
      detail: 'Request was throttled. Expected available in 3412 seconds.',
    })
    const { wrapper } = await mountAt(SignInLinkPage, '/sign-in-link')

    await wrapper.find('input').setValue('homeowner@example.test')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('Too many attempts. Wait a while and try again.')
    expect(wrapper.text()).not.toContain('3412')
  })
})
