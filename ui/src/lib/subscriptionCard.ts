/**
 * What the "Your pink glove subscription" card on the settings page shows,
 * from the organization's billing block and who is looking.
 *
 * The *state*, the sentence and which buttons the API will accept are all
 * the server's (`billing.state`, `billing.summary`, `available_actions`,
 * ADR-023); this only chooses a title, an icon and whether to draw a
 * button, which is a courtesy -- the API decides who may subscribe (owner
 * only). Pure so it has a spec.
 */

import type { PlatformBilling } from '@/api/types'

export type SubscriptionAction = 'subscribe' | 'manage'

export interface SubscriptionCard {
  state: PlatformBilling['state']
  title: string
  /** The server's sentence, verbatim. */
  text: string
  /** The one button to draw, and its label. */
  action: SubscriptionAction | null
  actionLabel: string
  /** A quieter way into the portal beside a Subscribe button, for a returning customer. */
  secondary: SubscriptionAction | null
  secondaryLabel: string
  icon: string
  color: string
}

const LABELS: Record<SubscriptionAction, string> = {
  subscribe: 'Subscribe',
  manage: 'Manage billing',
}

const LOOK: Record<PlatformBilling['state'], { title: string, icon: string, color: string }> = {
  exempt: { title: 'Complimentary', icon: 'mdi-gift-outline', color: 'success' },
  trial: { title: 'Free trial', icon: 'mdi-clock-outline', color: 'primary' },
  active: { title: 'Subscribed', icon: 'mdi-check-circle', color: 'success' },
  past_due: { title: 'Payment failed', icon: 'mdi-alert-outline', color: 'warning' },
  lapsed: { title: 'Lapsed', icon: 'mdi-alert-outline', color: 'warning' },
  deactivated: { title: 'Read-only', icon: 'mdi-lock-outline', color: 'error' },
}

/**
 * `null` means no card at all: billing is not configured on the server, so
 * there is nothing to show and nothing to ask.
 */
export function subscriptionCard (
  billing: PlatformBilling,
  isOwner: boolean,
): SubscriptionCard | null {
  if (billing.state === 'exempt' && !billing.exempt) {
    return null
  }

  const can = (action: SubscriptionAction) => isOwner && billing.available_actions.includes(action)
  const action: SubscriptionAction | null = can('subscribe') ? 'subscribe' : (can('manage') ? 'manage' : null)
  const secondary: SubscriptionAction | null = action === 'subscribe' && can('manage') ? 'manage' : null

  return {
    ...LOOK[billing.state],
    state: billing.state,
    text: billing.summary,
    action,
    actionLabel: action ? LABELS[action] : '',
    secondary,
    secondaryLabel: secondary ? 'Billing portal' : '',
  }
}
