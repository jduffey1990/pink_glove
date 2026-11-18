/**
 * The three public account pages, mounted: accept an invitation, ask for a
 * reset, choose a new password.
 *
 * No browser has looked at these, so this is the nearest thing (see
 * TeamPage.spec.ts). What it pins: the token is read from the address's
 * fragment (ADR-036) and travels in the request body,
 * the server's sentences are shown as sent, the validators' answers are all
 * shown, and nothing here signs anyone in or sends more than the server
 * asked for.
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
import { stubResizeObserver } from '@/lib/team.fixtures'
import AcceptInvitePage from './AcceptInvitePage.vue'
import ForgotPasswordPage from './ForgotPasswordPage.vue'
import ResetPasswordPage from './ResetPasswordPage.vue'

const INSPECT = '/api/users/invitations/inspect/'
const ACCEPT = '/api/users/invitations/accept/'
const RESET = '/api/auth/password-reset/'
const CONFIRM = '/api/auth/password-reset/confirm/'

let mock: MockAdapter

const Blank = { template: '<div />' }

async function mountAt (component: Component, path: string) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/login', name: 'login', component: Blank },
      { path: '/', name: 'home', component: Blank },
      { path: '/forgot-password', name: 'forgot-password', component: ForgotPasswordPage },
      { path: '/reset-password', name: 'reset-password', component: ResetPasswordPage },
      { path: '/accept-invite', name: 'accept-invite', component: AcceptInvitePage },
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

function invitation (overrides: Record<string, unknown> = {}) {
  return {
    organization_name: 'Sparkle Clean',
    email: 'new@example.test',
    role: 'cleaner',
    must_set_password: true,
    first_name: '',
    last_name: '',
    ...overrides,
  }
}

beforeEach(() => {
  stubResizeObserver()
  mock = new MockAdapter(api)
})

afterEach(() => mock.restore())

describe('AcceptInvitePage', () => {
  it('asks the server what the link opens, with the token in the body', async () => {
    mock.onPost(INSPECT).reply(200, invitation())

    const { wrapper } = await mountAt(AcceptInvitePage, '/accept-invite#token=tok-123')

    expect(sent(INSPECT)).toEqual({ token: 'tok-123' })
    expect(mock.history.post.every(call => !call.url?.includes('tok-123'))).toBe(true)
    expect(wrapper.text()).toContain('Sparkle Clean')
    expect(wrapper.text()).toContain('new@example.test')
    expect(wrapper.text()).toContain('Cleaner')
  })

  it('asks a new account for a name and a password, and sends them', async () => {
    mock.onPost(INSPECT).reply(200, invitation())
    mock.onPost(ACCEPT).reply(200, { detail: 'You have joined Sparkle Clean. Sign in to get started.' })
    const { wrapper } = await mountAt(AcceptInvitePage, '/accept-invite#token=tok-123')

    const inputs = wrapper.findAll('input')
    expect(inputs).toHaveLength(3)
    await inputs[0].setValue('Maria')
    await inputs[1].setValue('Lopez')
    await inputs[2].setValue('a long phrase for tests only')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(sent(ACCEPT)).toEqual({
      token: 'tok-123',
      password: 'a long phrase for tests only',
      first_name: 'Maria',
      last_name: 'Lopez',
    })
    // The server's sentence, as sent, and a way on to the ordinary sign-in.
    expect(wrapper.text()).toContain('You have joined Sparkle Clean. Sign in to get started.')
    expect(wrapper.text()).toContain('Sign in')
    expect(wrapper.findAll('input')).toHaveLength(0)
  })

  it('asks an existing account for nothing, and sends only the token', async () => {
    mock.onPost(INSPECT).reply(200, invitation({ must_set_password: false }))
    mock.onPost(ACCEPT).reply(200, { detail: 'You have joined Sparkle Clean. Sign in to get started.' })
    const { wrapper } = await mountAt(AcceptInvitePage, '/accept-invite#token=tok-123')

    expect(wrapper.findAll('input')).toHaveLength(0)
    expect(wrapper.text()).toContain('your password stays as it is')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(sent(ACCEPT)).toEqual({ token: 'tok-123' })
  })

  it('shows every sentence the validators sent, and keeps the form', async () => {
    mock.onPost(INSPECT).reply(200, invitation())
    mock.onPost(ACCEPT).reply(400, {
      password: ['This password is too short.', 'This password is too common.'],
    })
    const { wrapper } = await mountAt(AcceptInvitePage, '/accept-invite#token=tok-123')

    const inputs = wrapper.findAll('input')
    await inputs[0].setValue('Maria')
    await inputs[2].setValue('pw')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('This password is too short.')
    expect(wrapper.text()).toContain('This password is too common.')
    expect(wrapper.findAll('input')).toHaveLength(3)
  })

  it('shows the server\'s sentence for a dead link and offers no form', async () => {
    mock.onPost(INSPECT).reply(400, {
      detail: 'That invitation link is invalid or has expired. Ask for a new one.',
    })

    const { wrapper } = await mountAt(AcceptInvitePage, '/accept-invite#token=dead')

    expect(wrapper.text()).toContain('That invitation link is invalid or has expired. Ask for a new one.')
    expect(wrapper.find('form').exists()).toBe(false)
    expect(mock.history.post.some(call => call.url === ACCEPT)).toBe(false)
  })

  it('shows the server\'s sentence when the link dies between looking and accepting', async () => {
    mock.onPost(INSPECT).reply(200, invitation({ must_set_password: false }))
    mock.onPost(ACCEPT).reply(400, {
      detail: 'That invitation link is invalid or has expired. Ask for a new one.',
    })
    const { wrapper } = await mountAt(AcceptInvitePage, '/accept-invite#token=tok-123')

    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('That invitation link is invalid or has expired. Ask for a new one.')
  })
})

describe('AcceptInvitePage without a token', () => {
  it.each(['/accept-invite', '/accept-invite#token=', '/accept-invite#other=tok-123'])(
    'says the link is incomplete and asks the server nothing: %s',
    async path => {
      const { wrapper } = await mountAt(AcceptInvitePage, path)

      expect(wrapper.text()).toContain(INCOMPLETE_LINK)
      expect(wrapper.findAll('input')).toHaveLength(0)
      expect(mock.history.post).toHaveLength(0)
    },
  )
})

describe('ForgotPasswordPage', () => {
  it('sends the address and shows the server\'s sentence word for word', async () => {
    const detail = 'If that address belongs to a staff account, a reset link is on its way.'
    mock.onPost(RESET).reply(202, { detail })
    const { wrapper } = await mountAt(ForgotPasswordPage, '/forgot-password')

    await wrapper.find('input').setValue('  owner@example.test ')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(sent(RESET)).toEqual({ email: 'owner@example.test' })
    expect(wrapper.text()).toContain(detail)
    // Nothing that claims more than the server did.
    expect(wrapper.text()).not.toContain('Dev link')
    expect(wrapper.find('a').exists()).toBe(false)
  })

  it('shows the link only when the API handed one back', async () => {
    mock.onPost(RESET).reply(202, {
      detail: 'If that address belongs to a staff account, a reset link is on its way.',
      dev_link: 'http://localhost:3000/reset-password#token=tok-9',
    })
    const { wrapper } = await mountAt(ForgotPasswordPage, '/forgot-password')

    await wrapper.find('input').setValue('owner@example.test')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(wrapper.find('a').attributes('href')).toBe('http://localhost:3000/reset-password#token=tok-9')
  })

  it('shows the server\'s complaint about the address under the field', async () => {
    mock.onPost(RESET).reply(400, { email: ['Enter a valid email address.'] })
    const { wrapper } = await mountAt(ForgotPasswordPage, '/forgot-password')

    await wrapper.find('input').setValue('nope')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('Enter a valid email address.')
    expect(wrapper.find('form').exists()).toBe(true)
  })

  it('does not show the throttle\'s countdown', async () => {
    mock.onPost(RESET).reply(429, {
      detail: 'Request was throttled. Expected available in 3412 seconds.',
    })
    const { wrapper } = await mountAt(ForgotPasswordPage, '/forgot-password')

    await wrapper.find('input').setValue('owner@example.test')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('Too many attempts. Wait a while and try again.')
    expect(wrapper.text()).not.toContain('3412')
  })
})

describe('ResetPasswordPage', () => {
  it('sends the token in the body with the new password', async () => {
    mock.onPost(CONFIRM).reply(200, { detail: 'Your password has been changed. Sign in with it now.' })
    const { wrapper } = await mountAt(ResetPasswordPage, '/reset-password#token=tok-456')

    await wrapper.find('input').setValue('a new phrase for tests only')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(sent(CONFIRM)).toEqual({ token: 'tok-456', password: 'a new phrase for tests only' })
    expect(mock.history.post.every(call => !call.url?.includes('tok-456'))).toBe(true)
    expect(wrapper.text()).toContain('Your password has been changed. Sign in with it now.')
    expect(wrapper.find('form').exists()).toBe(false)
    // It asked for no session and made none: the only call was the confirm.
    expect(mock.history.get).toHaveLength(0)
  })

  it('shows every sentence the validators sent, and keeps the form', async () => {
    mock.onPost(CONFIRM).reply(400, {
      password: ['This password is too short.', 'This password is entirely numeric.'],
    })
    const { wrapper } = await mountAt(ResetPasswordPage, '/reset-password#token=tok-456')

    await wrapper.find('input').setValue('123')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('This password is too short.')
    expect(wrapper.text()).toContain('This password is entirely numeric.')
    expect(wrapper.find('form').exists()).toBe(true)
  })

  it('shows the server\'s sentence for a dead link', async () => {
    mock.onPost(CONFIRM).reply(400, {
      detail: 'That reset link is invalid or has expired. Ask for a new one.',
    })
    const { wrapper } = await mountAt(ResetPasswordPage, '/reset-password#token=dead')

    await wrapper.find('input').setValue('a new phrase for tests only')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('That reset link is invalid or has expired. Ask for a new one.')
    expect(wrapper.text()).toContain('Ask for a new link')
  })
})

describe('ResetPasswordPage without a token', () => {
  it.each(['/reset-password', '/reset-password#token='])(
    'says the link is incomplete and offers no form: %s',
    async path => {
      const { wrapper } = await mountAt(ResetPasswordPage, path)

      expect(wrapper.text()).toContain(INCOMPLETE_LINK)
      expect(wrapper.find('form').exists()).toBe(false)
      expect(wrapper.text()).toContain('Ask for a new link')
      expect(mock.history.post).toHaveLength(0)
    },
  )
})
