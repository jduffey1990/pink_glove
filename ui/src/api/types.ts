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

/**
 * Team management (ADR-028). A `TeamMember` is a membership as the directory
 * endpoint returns it: with where it stands and what the caller may do to it.
 * The session's own `Membership` rows carry neither.
 */
export type TeamMember = Schemas['TeamMember']
export type InvitedTeamMember = Schemas['InvitedTeamMember']
export type MembershipState = Schemas['MembershipStateEnum']
/** Every role but customer: what the team page may invite or grant. */
export type StaffRole = Schemas['StaffRoleEnum']
export type InviteRequest = Schemas['InviteRequest']
export type InviteOptions = Schemas['InviteOptions']
export type Invitation = Schemas['Invitation']
export type InvitationAcceptRequest = Schemas['InvitationAcceptRequest']
export type PasswordResetSent = Schemas['PasswordResetSent']

/**
 * What the server says may be done to a team row right now.
 *
 * A string list on the wire, as an invoice's `available_actions` is; narrowed
 * here so a page cannot check for an action that does not exist. The server
 * is the only place they are decided (ADR-023).
 */
export type TeamAction
  = | 'change_role'
    | 'deactivate'
    | 'reactivate'
    | 'resend_invite'

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

/** Reports: computed on the server, read here and never recomputed. */
export type Timesheet = Schemas['Timesheet']
export type TimesheetStaff = Schemas['TimesheetStaff']
export type TimesheetEntry = Schemas['TimesheetEntry']
export type Receivables = Schemas['Receivables']
export type CustomerReceivable = Schemas['CustomerReceivable']
export type Aging = Schemas['Aging']
export type Collections = Schemas['Collections']
export type MethodCollections = Schemas['MethodCollections']
export type CollectionsTotals = Schemas['CollectionsTotals']

/** Stripe Connect (Phase 4b). */
export type StripeStatus = Schemas['StripeStatus']

/** The platform subscription (Phase 4c). The state and the notice are the server's. */
export type BillingNotice = Schemas['BillingNotice']
export type PlatformBilling = Schemas['PlatformBilling']
export type BillingState = Schemas['BillingStateEnum']
export type NoticeLevel = Schemas['NoticeLevelEnum']
/** What every "send the browser to Stripe" endpoint answers. */
export type Url = Schemas['Url']
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

/**
 * What the server says a dispatcher may do to this visit besides move its
 * status. Same arrangement as `InvoiceAction`: the list arrives on the job as
 * `available_actions` and is decided nowhere else (ADR-023, ADR-032).
 */
export type JobAction = 'edit' | 'delete'

export type Customer = Schemas['Customer']
export type ServiceLocation = Schemas['ServiceLocation']

/** Customer import (ADR-031). A row's outcome never carries an access code. */
export type CustomerImportResult = Schemas['CustomerImportResult']
export type CustomerImportRow = Schemas['CustomerImportRow']
export type CustomerImportRowError = Schemas['CustomerImportRowError']
export type CustomerImportTemplate = Schemas['CustomerImportTemplate']
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
/** Booking a visit: the same fields, with the ones a new job cannot lack required. */
export type NewJobRequest = Schemas['JobRequest']
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
