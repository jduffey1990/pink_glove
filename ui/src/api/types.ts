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

/** Action responses. Generated like everything else -- never restated by hand. */
export type PlanPreview = Schemas['PreviewResponse']
export type RegenerateResult = Schemas['RegenerateResponse']
export type MaterializeResult = Schemas['MaterializeResponse']
export type AccessWarning = Schemas['AccessWarning']
export type RevealedCodes = Schemas['RevealedCodes']

export type Invoice = Schemas['Invoice']
export type InvoiceLine = Schemas['InvoiceLine']
export type InvoiceStatus = Schemas['InvoiceStatusEnum']
export type LineKind = Schemas['LineKindEnum']
export type Payment = Schemas['Payment']
export type PaymentMethod = Schemas['PaymentMethodEnum']
export type PaymentState = Schemas['PaymentStateEnum']
export type BillableJob = Schemas['BillableJob']

/** Stripe Connect (Phase 4b). */
export type StripeStatus = Schemas['StripeStatus']
export type PublicInvoice = Schemas['PublicInvoice']
export type PublicInvoiceLine = Schemas['PublicInvoiceLine']

/**
 * What the server says this invoice will accept right now.
 *
 * A string union rather than an enum on the wire: `available_actions` is a
 * list of action names, and the server is the only place they are decided
 * (ADR-023). Narrowed here so a page cannot check for an action that does not
 * exist.
 */
export type InvoiceAction
  = | 'edit'
    | 'delete'
    | 'issue'
    | 'void'
    | 'send'
    | 'record_payment'

export type Customer = Schemas['Customer']
export type ServiceLocation = Schemas['ServiceLocation']
export type Service = Schemas['Service']

/**
 * Request bodies, which are NOT the same shape as responses.
 *
 * `SPECTACULAR_SETTINGS['COMPONENT_SPLIT_REQUEST']` generates these
 * separately, and the difference is load-bearing: a location's access codes
 * are write-only, so they exist on the request type and not on the response
 * one. Reading a code back is a logged reveal (ADR-016), never a field on a
 * detail payload -- and the types enforce that rather than relying on
 * everyone remembering it.
 */
export type ServiceLocationRequest = Schemas['PatchedServiceLocationRequest']
export type CustomerRequest = Schemas['PatchedCustomerRequest']
export type ServiceRequest = Schemas['PatchedServiceRequest']
export type JobRequest = Schemas['PatchedJobRequest']
export type InvoiceRequest = Schemas['PatchedInvoiceRequest']
export type InvoiceLineRequest = Schemas['InvoiceLineRequest']
export type PatchedInvoiceLineRequest = Schemas['PatchedInvoiceLineRequest']
export type RecordPaymentRequest = Schemas['RecordPaymentRequest']
export type OrganizationRequest = Schemas['PatchedOrganizationRequest']
export type RecurringPlanRequest = Schemas['PatchedRecurringPlanRequest']

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
