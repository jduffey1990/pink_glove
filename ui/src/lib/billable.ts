/**
 * The ready-to-invoice list, grouped and totalled.
 *
 * An invoice covers one customer's visits, so the list is grouped that way
 * and each group is selected and totalled on its own. Pure functions rather
 * than logic inside the page, because the sum is money: a grouping bug bills
 * one customer for another's visit, and that is worth a spec.
 */

import type { BillableJob } from '@/api/types'

export interface BillableGroup {
  /** The customer's id. */
  id: string
  name: string
  jobs: BillableJob[]
}

/**
 * Group visits by customer, keeping the order the server sent them in --
 * oldest first, which is the order they will appear on the invoice.
 */
export function groupByCustomer (jobs: BillableJob[]): BillableGroup[] {
  const groups = new Map<string, BillableGroup>()

  for (const job of jobs) {
    const group = groups.get(job.customer)
      ?? { id: job.customer, name: job.customer_name, jobs: [] }
    group.jobs.push(job)
    groups.set(job.customer, group)
  }

  return [...groups.values()]
}

/** The ticked visits in one group. */
export function selectedIn (group: BillableGroup, selected: ReadonlySet<string>): string[] {
  return group.jobs.filter(job => selected.has(job.id)).map(job => job.id)
}

/**
 * What the ticked visits come to, in cents.
 *
 * The server priced each one, so this only adds up. Tax is not applied here:
 * it depends on which services are taxable and is computed once, on the
 * invoice, by the server (ADR-026).
 */
export function selectedTotal (group: BillableGroup, selected: ReadonlySet<string>): number {
  return group.jobs
    .filter(job => selected.has(job.id))
    .reduce((sum, job) => sum + job.amount_cents, 0)
}

/** Tick or untick one visit. Returns a new set, so watchers see the change. */
export function toggle (selected: ReadonlySet<string>, jobId: string): Set<string> {
  const next = new Set(selected)
  if (next.has(jobId)) {
    next.delete(jobId)
  } else {
    next.add(jobId)
  }
  return next
}

/**
 * Tick every visit in a group, or untick them all if they are already ticked.
 *
 * "All on" is judged over this group alone: a customer whose visits are all
 * selected untoggles, while another customer's selection is left alone.
 */
export function toggleAll (selected: ReadonlySet<string>, group: BillableGroup): Set<string> {
  const ids = group.jobs.map(job => job.id)
  const next = new Set(selected)
  const allOn = ids.length > 0 && ids.every(id => next.has(id))

  for (const id of ids) {
    if (allOn) {
      next.delete(id)
    } else {
      next.add(id)
    }
  }

  return next
}
