/**
 * Whether the shell draws the subscription banner, and how.
 *
 * The *sentence* is the server's (`billing.notice`, ADR-023), written for
 * the reader's role; the server also says how loudly (`level`). This only
 * maps that to a Vuetify alert and decides whether to offer the link to
 * the settings page, which is a navigation courtesy -- the API decides who
 * may subscribe. Pure so it has a spec.
 */

import type { BillingNotice } from '@/api/types'

export interface BillingBanner {
  level: 'info' | 'warning' | 'error'
  icon: string
  text: string
  /** Admin and above can act on it from the settings page; everyone else only reads it. */
  showSettingsLink: boolean
}

const ICONS = {
  info: 'mdi-information-outline',
  warning: 'mdi-alert-outline',
  error: 'mdi-lock-outline',
} as const

export function billingBanner (
  notice: BillingNotice | null,
  isAdminOrHigher: boolean,
): BillingBanner | null {
  if (!notice || !notice.notice || !notice.level) {
    return null
  }
  return {
    level: notice.level,
    icon: ICONS[notice.level],
    text: notice.notice,
    showSettingsLink: isAdminOrHigher,
  }
}
