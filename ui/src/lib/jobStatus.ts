/**
 * How a job status is worded and coloured, in one place.
 *
 * Only presentation lives here. Which statuses a job may move to, whether it
 * is finished, and which moves need a reason are the server's to say -- they
 * arrive on the job as `next_statuses` and `is_terminal` (ADR-023). A copy of
 * those rules kept here is how My Day came to offer "clock in" on a job
 * already marked no-access.
 */

import type { JobStatus } from '@/api/types'

export const JOB_STATUS_LOOK: Record<JobStatus, { label: string, color: string, icon: string }> = {
  scheduled: { label: 'Scheduled', color: 'grey', icon: 'mdi-calendar-blank-outline' },
  en_route: { label: 'En route', color: 'info', icon: 'mdi-car' },
  in_progress: { label: 'In progress', color: 'primary', icon: 'mdi-progress-wrench' },
  complete: { label: 'Complete', color: 'success', icon: 'mdi-check-circle-outline' },
  cancelled: { label: 'Cancelled', color: 'error', icon: 'mdi-close-circle-outline' },
  // Not a cancellation and not a completion: the crew turned up and could
  // not get in. It bills differently from both.
  no_access: { label: 'No access', color: 'warning', icon: 'mdi-door-closed-lock' },
}

export function statusLabel (status: JobStatus): string {
  return JOB_STATUS_LOOK[status].label
}

/** Every status with its label, in workflow order -- for filters and selects. */
export const JOB_STATUS_OPTIONS = (Object.keys(JOB_STATUS_LOOK) as JobStatus[]).map(value => ({
  value,
  label: statusLabel(value),
}))

/** Whether the server offers `status` as a next move for this job. */
export function canMoveTo (job: { next_statuses: { status: JobStatus }[] }, status: JobStatus): boolean {
  return job.next_statuses.some(next => next.status === status)
}
