/**
 * Routes and the guard that decides who may see what.
 *
 * The guard mirrors the backend's role tiers rather than inventing its own.
 * It is a *navigation* convenience, not a security boundary -- the API refuses
 * the same things independently, and it is the one that counts. Hiding a route
 * a user could not use anyway is the point; nothing here is load-bearing.
 */

import type { RouteRecordRaw } from 'vue-router'
import { createRouter, createWebHistory } from 'vue-router'
import { useSessionStore } from '@/stores/session'

/** Which tier a route needs. Absent means "any signed-in user". */
type Access = 'public' | 'dispatcher' | 'staff'

declare module 'vue-router' {
  interface RouteMeta {
    access?: Access
    title?: string
  }
}

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/pages/LoginPage.vue'),
    meta: { access: 'public', title: 'Sign in' },
  },
  {
    path: '/verify',
    name: 'verify',
    component: () => import('@/pages/VerifyPage.vue'),
    meta: { access: 'public', title: 'Enter your code' },
  },
  {
    path: '/choose-organization',
    name: 'choose-organization',
    component: () => import('@/pages/ChooseOrganizationPage.vue'),
    meta: { title: 'Choose an organization' },
  },
  {
    path: '/',
    name: 'home',
    // Sends each role to the screen they actually work from.
    redirect: () => {
      const session = useSessionStore()
      return session.isCleaner ? { name: 'my-day' } : { name: 'schedule' }
    },
  },
  {
    path: '/schedule',
    name: 'schedule',
    component: () => import('@/pages/SchedulePage.vue'),
    meta: { access: 'dispatcher', title: 'Schedule' },
  },
  {
    path: '/my-day',
    name: 'my-day',
    component: () => import('@/pages/MyDayPage.vue'),
    meta: { access: 'staff', title: 'My day' },
  },
  {
    path: '/jobs/:id',
    name: 'job',
    component: () => import('@/pages/JobDetailPage.vue'),
    meta: { title: 'Job' },
  },
  {
    path: '/customers',
    name: 'customers',
    component: () => import('@/pages/CustomersPage.vue'),
    meta: { access: 'dispatcher', title: 'Customers' },
  },
  {
    path: '/customers/:id',
    name: 'customer',
    component: () => import('@/pages/CustomerDetailPage.vue'),
    meta: { access: 'dispatcher', title: 'Customer' },
  },
  {
    path: '/services',
    name: 'services',
    component: () => import('@/pages/ServicesPage.vue'),
    meta: { access: 'staff', title: 'Services' },
  },
  {
    path: '/billing',
    name: 'billing',
    component: () => import('@/pages/BillingPage.vue'),
    meta: { access: 'dispatcher', title: 'Billing' },
  },
  {
    path: '/billing/settings',
    name: 'billing-settings',
    component: () => import('@/pages/BillingSettingsPage.vue'),
    meta: { access: 'dispatcher', title: 'Billing settings' },
  },
  {
    path: '/invoices/:id',
    name: 'invoice',
    component: () => import('@/pages/InvoiceDetailPage.vue'),
    meta: { access: 'dispatcher', title: 'Invoice' },
  },
  {
    path: '/plans',
    name: 'plans',
    component: () => import('@/pages/PlansPage.vue'),
    meta: { access: 'dispatcher', title: 'Recurring plans' },
  },
  {
    // The customer's pay page (Phase 4b). No session: the signed token in the
    // invoice email is the credential, and the API decides what it opens.
    path: '/pay/:token',
    name: 'pay',
    component: () => import('@/pages/PayInvoicePage.vue'),
    meta: { access: 'public', title: 'Pay invoice' },
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('@/pages/NotFoundPage.vue'),
    meta: { access: 'public', title: 'Not found' },
  },
]

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes,
})

router.beforeEach(async to => {
  const session = useSessionStore()

  // One session fetch per page load, before the first route resolves. It is
  // also what sets the CSRF cookie, so it must happen before any POST.
  if (!session.ready) {
    await session.boot()
  }

  if (to.meta.access === 'public') {
    // Someone already signed in has no business back on the login page.
    return session.isAuthenticated && to.name === 'login' ? { name: 'home' } : true
  }

  if (!session.isAuthenticated) {
    return { name: 'login', query: { next: to.fullPath } }
  }

  if (session.needsOrganizationChoice && to.name !== 'choose-organization') {
    return { name: 'choose-organization' }
  }

  if (to.meta.access === 'dispatcher' && !session.isDispatcherOrHigher) {
    return { name: 'home' }
  }

  if (to.meta.access === 'staff' && !session.isStaff) {
    return { name: 'home' }
  }

  return true
})

router.afterEach(to => {
  document.title = to.meta.title ? `${to.meta.title} · pink glove` : 'pink glove'
})

export default router
