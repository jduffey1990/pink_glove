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
import { fragmentRedirect } from '@/lib/linkToken'
import { useSessionStore } from '@/stores/session'

/** Which tier a route needs. Absent means "any signed-in user". */
type Access = 'public' | 'admin' | 'dispatcher' | 'staff'

declare module 'vue-router' {
  interface RouteMeta {
    access?: Access
    title?: string
  }
}

export const routes: RouteRecordRaw[] = [
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
    // The three account pages (ADR-034). No session: the token in the email
    // is the credential, and none of them signs anyone in. The token is in
    // the fragment (`#token=...`, ADR-036); the `/:token` forms are links
    // already sent, moved to the fragment before anything renders.
    path: '/forgot-password',
    name: 'forgot-password',
    component: () => import('@/pages/ForgotPasswordPage.vue'),
    meta: { access: 'public', title: 'Reset your password' },
  },
  {
    path: '/reset-password',
    name: 'reset-password',
    component: () => import('@/pages/ResetPasswordPage.vue'),
    meta: { access: 'public', title: 'Choose a new password' },
  },
  { path: '/reset-password/:token', redirect: fragmentRedirect('reset-password') },
  {
    path: '/accept-invite',
    name: 'accept-invite',
    component: () => import('@/pages/AcceptInvitePage.vue'),
    meta: { access: 'public', title: 'Accept your invitation' },
  },
  { path: '/accept-invite/:token', redirect: fragmentRedirect('accept-invite') },
  {
    path: '/choose-organization',
    name: 'choose-organization',
    component: () => import('@/pages/ChooseOrganizationPage.vue'),
    meta: { title: 'Choose an organization' },
  },
  {
    // Signed in, and active in no organization. See the guard below.
    path: '/no-access',
    name: 'no-access',
    component: () => import('@/pages/NoAccessPage.vue'),
    meta: { title: 'No access' },
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
    // Hours and money for the whole business: owner and admin, one tier
    // above the rest of billing. The API refuses a dispatcher independently.
    path: '/reports',
    name: 'reports',
    component: () => import('@/pages/ReportsPage.vue'),
    meta: { access: 'admin', title: 'Reports' },
  },
  {
    path: '/team',
    name: 'team',
    component: () => import('@/pages/TeamPage.vue'),
    meta: { access: 'admin', title: 'Team' },
  },
  {
    // The customer's pay page (Phase 4b). No session: the signed token in the
    // invoice email is the credential, and the API decides what it opens.
    // The token is in the fragment (ADR-036), and Stripe returns to
    // `/pay?paid=1#token=...`. The `/:token` form is every invoice email sent
    // before that, and the return of any Checkout session opened before it.
    path: '/pay',
    name: 'pay',
    component: () => import('@/pages/PayInvoicePage.vue'),
    meta: { access: 'public', title: 'Pay invoice' },
  },
  { path: '/pay/:token', redirect: fragmentRedirect('pay') },
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

  // Deactivated everywhere: there is no role, and every screen needs one.
  // One page that says so, instead of `home` and `schedule` redirecting to
  // each other until the router gives up.
  if (session.hasNoOrganization) {
    return to.name === 'no-access' ? true : { name: 'no-access' }
  }
  if (to.name === 'no-access') {
    return { name: 'home' }
  }

  if (session.needsOrganizationChoice && to.name !== 'choose-organization') {
    return { name: 'choose-organization' }
  }

  if (to.meta.access === 'admin' && !session.isAdminOrHigher) {
    return { name: 'home' }
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
