/**
 * Named aliases over the generated schema.
 *
 * `schema.d.ts` is generated and must never be hand-edited (ADR-019); this
 * file is the hand-written index into it, so components import `Job` rather
 * than `components['schemas']['Job']`.
 */

import type { components } from './schema'

type Schemas = components['schemas']

export type Session = Schemas['Session']
export type Membership = Schemas['Membership']
export type OrganizationSummary = Schemas['OrganizationSummary']
export type Organization = Schemas['Organization']
export type Role = Schemas['RoleEnum']

export type Job = Schemas['Job']
export type JobStatus = Schemas['JobStatusEnum']
export type JobAssignment = Schemas['JobAssignment']
export type JobNote = Schemas['JobNote']
export type JobPhoto = Schemas['JobPhoto']
export type TimeEntry = Schemas['TimeEntry']
export type RecurringPlan = Schemas['RecurringPlan']

export type Customer = Schemas['Customer']
export type ServiceLocation = Schemas['ServiceLocation']
export type Service = Schemas['Service']

export type LocationSummary = Schemas['LocationSummary']
export type CustomerSummary = Schemas['CustomerSummary']
export type ServiceSummary = Schemas['ServiceSummary']

/** DRF's LimitOffsetPagination envelope. */
export interface Paginated<T> {
  count: number
  next: string | null
  previous: string | null
  results: T[]
}

/**
 * The body of a 409 from the job status action.
 *
 * `allowed` is what the UI renders its buttons from, rather than duplicating
 * the state machine on the client where it would drift.
 */
export interface TransitionConflict {
  detail: string
  status: JobStatus
  allowed: JobStatus[]
}
