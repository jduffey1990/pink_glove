/**
 * Typed helpers over the generated schema, one per endpoint the slice uses.
 *
 * Deliberately thin. The types come from `schema.d.ts` (ADR-019); these
 * functions exist so a component writes `listJobs({ mine: true })` instead of
 * assembling URLs, and so a change to a path shows up in one place.
 */

import type {
  AccessWarning,
  BillableJob,
  Collections,
  Customer,
  CustomerImportResult,
  CustomerImportTemplate,
  CustomerRequest,
  Invitation,
  InvitationAcceptRequest,
  InvitedTeamMember,
  InviteOptions,
  InviteRequest,
  Invoice,
  InvoiceLine,
  InvoiceLineRequest,
  InvoiceRequest,
  InvoiceStatus,
  Job,
  JobNote,
  JobPhoto,
  JobRequest,
  JobStatus,
  MagicLinkSent,
  MaterializeResult,
  Membership,
  NewJobRequest,
  Organization,
  OrganizationRequest,
  Paginated,
  PasswordResetSent,
  PatchedInvoiceLineRequest,
  Payment,
  PaymentState,
  PayTokenRequest,
  PlanPreview,
  PlatformBilling,
  PublicInvoice,
  Receivables,
  RecordPaymentRequest,
  RecurringPlan,
  RecurringPlanRequest,
  RegenerateResult,
  RevealedCodes,
  Service,
  ServiceLocation,
  ServiceLocationRequest,
  ServiceRequest,
  StaffRole,
  StripeStatus,
  TeamMember,
  TimeEntry,
  Timesheet,
  Url,
} from './types'
import { api } from './client'

// --- jobs ------------------------------------------------------------------

export interface JobQuery {
  /** Organization-local dates. The backend converts; the UI never does. */
  date_from?: string
  date_to?: string
  status?: JobStatus[]
  customer?: string
  location?: string
  assignee?: string
  plan?: string
  mine?: boolean
  ordering?: string
  limit?: number
  offset?: number
}

export async function listJobs (query: JobQuery = {}): Promise<Paginated<Job>> {
  const { data } = await api.get<Paginated<Job>>('/api/scheduling/jobs/', {
    params: query,
    // status is a multi-value filter; axios' default bracket syntax is not
    // what django-filter reads.
    paramsSerializer: { indexes: null },
  })
  return data
}

export async function getJob (id: string): Promise<Job> {
  const { data } = await api.get<Job>(`/api/scheduling/jobs/${id}/`)
  return data
}

export async function createJob (payload: NewJobRequest): Promise<Job> {
  const { data } = await api.post<Job>('/api/scheduling/jobs/', payload)
  return data
}

export async function updateJob (id: string, payload: JobRequest): Promise<Job> {
  const { data } = await api.patch<Job>(`/api/scheduling/jobs/${id}/`, payload)
  return data
}

export async function deleteJob (id: string): Promise<void> {
  await api.delete(`/api/scheduling/jobs/${id}/`)
}

export async function assignJob (id: string, userId: string): Promise<Job> {
  const { data } = await api.post<Job>(`/api/scheduling/jobs/${id}/assign/`, { user: userId })
  return data
}

export async function unassignJob (id: string, userId: string): Promise<Job> {
  const { data } = await api.post<Job>(`/api/scheduling/jobs/${id}/unassign/`, { user: userId })
  return data
}

/**
 * Move a job to another status.
 *
 * A refusal is a 409 whose body carries the allowed next states; callers let
 * that reach them rather than pre-judging the transition, so the server stays
 * the only place the state machine lives.
 */
export async function setJobStatus (
  id: string,
  status: JobStatus,
  reason = '',
): Promise<Job> {
  const { data } = await api.post<Job>(`/api/scheduling/jobs/${id}/status/`, { status, reason })
  return data
}

export async function clockIn (id: string): Promise<Job> {
  const { data } = await api.post<Job>(`/api/scheduling/jobs/${id}/clock-in/`)
  return data
}

export async function clockOut (id: string): Promise<Job> {
  const { data } = await api.post<Job>(`/api/scheduling/jobs/${id}/clock-out/`)
  return data
}

// --- notes and photos ------------------------------------------------------

export async function listJobNotes (jobId: string): Promise<Paginated<JobNote>> {
  const { data } = await api.get<Paginated<JobNote>>('/api/scheduling/job-notes/', {
    params: { job: jobId },
  })
  return data
}

export async function createJobNote (jobId: string, body: string): Promise<JobNote> {
  const { data } = await api.post<JobNote>('/api/scheduling/job-notes/', { job: jobId, body })
  return data
}

export async function deleteJobNote (id: string): Promise<void> {
  await api.delete(`/api/scheduling/job-notes/${id}/`)
}

export async function listJobPhotos (jobId: string): Promise<Paginated<JobPhoto>> {
  const { data } = await api.get<Paginated<JobPhoto>>('/api/scheduling/job-photos/', {
    params: { job: jobId },
  })
  return data
}

export async function uploadJobPhoto (
  jobId: string,
  file: File,
  caption = '',
): Promise<JobPhoto> {
  const form = new FormData()
  form.append('job', jobId)
  form.append('image', file)
  form.append('caption', caption)

  // No explicit Content-Type: the browser has to set the multipart boundary.
  const { data } = await api.post<JobPhoto>('/api/scheduling/job-photos/', form)
  return data
}

// --- time entries ----------------------------------------------------------

export async function listTimeEntries (
  query: { job?: string, user?: string, date_from?: string, date_to?: string } = {},
): Promise<Paginated<TimeEntry>> {
  const { data } = await api.get<Paginated<TimeEntry>>('/api/scheduling/time-entries/', {
    params: query,
  })
  return data
}

export async function correctTimeEntry (
  id: string,
  payload: { clock_in?: string, clock_out?: string },
): Promise<TimeEntry> {
  const { data } = await api.patch<TimeEntry>(`/api/scheduling/time-entries/${id}/`, payload)
  return data
}

// --- recurring plans -------------------------------------------------------

export async function listPlans (
  query: { customer?: string, location?: string, is_active?: boolean } = {},
): Promise<Paginated<RecurringPlan>> {
  const { data } = await api.get<Paginated<RecurringPlan>>('/api/scheduling/plans/', {
    params: query,
  })
  return data
}

export async function getPlan (id: string): Promise<RecurringPlan> {
  const { data } = await api.get<RecurringPlan>(`/api/scheduling/plans/${id}/`)
  return data
}

export async function createPlan (payload: RecurringPlanRequest): Promise<RecurringPlan> {
  const { data } = await api.post<RecurringPlan>('/api/scheduling/plans/', payload)
  return data
}

/**
 * Edit a plan.
 *
 * The response carries `regenerated` and `kept` when the edit touched the
 * schedule, so the caller can tell the dispatcher what happened to the visits
 * already on the board (ADR-020).
 */
export async function updatePlan (
  id: string,
  payload: RecurringPlanRequest,
): Promise<RecurringPlan & Partial<RegenerateResult>> {
  const { data } = await api.patch<RecurringPlan & Partial<RegenerateResult>>(
    `/api/scheduling/plans/${id}/`,
    payload,
  )
  return data
}

export async function deletePlan (id: string): Promise<void> {
  await api.delete(`/api/scheduling/plans/${id}/`)
}

export async function previewPlan (id: string, count = 6): Promise<PlanPreview> {
  const { data } = await api.get<PlanPreview>(`/api/scheduling/plans/${id}/preview/`, {
    params: { count },
  })
  return data
}

export async function materializePlan (id: string): Promise<MaterializeResult> {
  const { data } = await api.post<MaterializeResult>(`/api/scheduling/plans/${id}/materialize/`)
  return data
}

// --- customers and locations ----------------------------------------------

export async function listCustomers (
  query: { search?: string, status?: string, limit?: number } = {},
): Promise<Paginated<Customer>> {
  const { data } = await api.get<Paginated<Customer>>('/api/customers/customers/', {
    params: query,
  })
  return data
}

export async function getCustomer (id: string): Promise<Customer> {
  const { data } = await api.get<Customer>(`/api/customers/customers/${id}/`)
  return data
}

export async function createCustomer (payload: CustomerRequest): Promise<Customer> {
  const { data } = await api.post<Customer>('/api/customers/customers/', payload)
  return data
}

export async function updateCustomer (
  id: string,
  payload: CustomerRequest,
): Promise<Customer> {
  const { data } = await api.patch<Customer>(`/api/customers/customers/${id}/`, payload)
  return data
}

/**
 * What an import file looks like: the template to download, which columns
 * are required and the limits on a file. Served so the column list has one
 * source, the backend's (ADR-031).
 */
export async function fetchCustomerImportTemplate (): Promise<CustomerImportTemplate> {
  const { data } = await api.get<CustomerImportTemplate>(
    '/api/customers/customers/import-template/',
  )
  return data
}

/**
 * Preview (`dryRun`) or perform an import of customers from a CSV file.
 *
 * The file goes up as a file. It is never read in the browser: the server
 * validates each row with the rules the create path uses and says what it
 * would do. A real import that a row stops is a 400 whose body is the same
 * per-row result; `lib/customerImport` reads it back out of the error.
 */
export async function importCustomers (
  file: File,
  dryRun: boolean,
): Promise<CustomerImportResult> {
  const form = new FormData()
  form.append('file', file)
  // Always sent. The server reads a missing value as a dry run, but a real
  // import should never hinge on a default.
  form.append('dry_run', dryRun ? 'true' : 'false')

  // No explicit Content-Type: the browser has to set the multipart boundary.
  const { data } = await api.post<CustomerImportResult>(
    '/api/customers/customers/import/',
    form,
  )
  return data
}

export async function listLocations (
  query: { customer?: string, is_active?: boolean, limit?: number } = {},
): Promise<Paginated<ServiceLocation>> {
  const { data } = await api.get<Paginated<ServiceLocation>>('/api/customers/locations/', {
    params: query,
  })
  return data
}

export async function createLocation (
  payload: ServiceLocationRequest,
): Promise<ServiceLocation> {
  const { data } = await api.post<ServiceLocation>('/api/customers/locations/', payload)
  return data
}

export async function updateLocation (
  id: string,
  payload: ServiceLocationRequest,
): Promise<ServiceLocation> {
  const { data } = await api.patch<ServiceLocation>(`/api/customers/locations/${id}/`, payload)
  return data
}

/**
 * Fetch the warning a user must acknowledge before codes are revealed.
 *
 * Served by the backend so the words shown and the words the audit row claims
 * were shown are one string (ADR-023). Nothing is logged by asking: no reveal
 * has happened yet.
 */
export async function fetchAccessWarning (locationId: string): Promise<string> {
  const { data } = await api.get<AccessWarning>(
    `/api/customers/locations/${locationId}/access-warning/`,
  )
  return data.warning
}

/**
 * Reveal a location's access codes. Logged, always (ADR-016).
 *
 * The acknowledgement is part of the contract, not a dialog the frontend could
 * quietly stop showing: the row records that the user saw the warning.
 */
export async function revealAccessCodes (
  locationId: string,
  jobId?: string,
): Promise<RevealedCodes> {
  const { data } = await api.post<RevealedCodes>(
    `/api/customers/locations/${locationId}/reveal-access/`,
    { acknowledged: true, ...(jobId ? { job: jobId } : {}) },
  )
  return data
}

// --- catalog and organization ---------------------------------------------

export async function listServices (
  query: { is_active?: boolean, search?: string } = {},
): Promise<Paginated<Service>> {
  const { data } = await api.get<Paginated<Service>>('/api/catalog/services/', { params: query })
  return data
}

export async function createService (payload: ServiceRequest): Promise<Service> {
  const { data } = await api.post<Service>('/api/catalog/services/', payload)
  return data
}

export async function updateService (id: string, payload: ServiceRequest): Promise<Service> {
  const { data } = await api.patch<Service>(`/api/catalog/services/${id}/`, payload)
  return data
}

export async function getCurrentOrganization (): Promise<Organization> {
  const { data } = await api.get<Organization>('/api/organizations/current/')
  return data
}

/** Everyone in the current organization. */
export async function listMemberships (): Promise<Paginated<Membership>> {
  const { data } = await api.get<Paginated<Membership>>('/api/users/memberships/', {
    params: { limit: 200 },
  })
  return data
}

/**
 * Staff who can be put on a job, as { id, label } for a picker.
 *
 * `id` is the USER id, not the membership id -- the assign action takes the
 * former and the two are easy to confuse.
 */
export async function listAssignableStaff (): Promise<{ id: string, label: string }[]> {
  const page = await listMemberships()
  return page.results
    .filter(m => m.is_active && m.role !== 'customer')
    .map(m => ({
      id: m.user,
      label: `${m.user_name || m.user_email} (${m.role})`,
    }))
}

// --- billing ---------------------------------------------------------------

export interface InvoiceQuery {
  customer?: string
  status?: InvoiceStatus[]
  payment_state?: PaymentState
  overdue?: boolean
  /** Organization-local dates, as `issued_on` already is on the row. */
  issued_from?: string
  issued_to?: string
  ordering?: string
  limit?: number
  offset?: number
}

export async function listInvoices (query: InvoiceQuery = {}): Promise<Paginated<Invoice>> {
  const { data } = await api.get<Paginated<Invoice>>('/api/billing/invoices/', {
    params: query,
    paramsSerializer: { indexes: null },
  })
  return data
}

export async function getInvoice (id: string): Promise<Invoice> {
  const { data } = await api.get<Invoice>(`/api/billing/invoices/${id}/`)
  return data
}

/**
 * Visits that are finished and on no live invoice.
 *
 * `amount_cents` is what each one will bill -- the price agreed when it was
 * scheduled, or the no-access fee. The server works it out so that what the
 * dispatcher is shown before pressing the button is what lands on the line.
 */
export async function listBillableJobs (customerId?: string): Promise<BillableJob[]> {
  const { data } = await api.get<BillableJob[]>('/api/billing/invoices/billable-jobs/', {
    params: customerId ? { customer: customerId } : {},
  })
  return data
}

/** Open a draft over a customer's visits. The server builds the lines. */
export async function draftInvoice (customerId: string, jobIds: string[]): Promise<Invoice> {
  const { data } = await api.post<Invoice>('/api/billing/invoices/', {
    customer: customerId,
    jobs: jobIds,
  })
  return data
}

export async function updateInvoice (id: string, payload: InvoiceRequest): Promise<Invoice> {
  const { data } = await api.patch<Invoice>(`/api/billing/invoices/${id}/`, payload)
  return data
}

export async function deleteInvoice (id: string): Promise<void> {
  await api.delete(`/api/billing/invoices/${id}/`)
}

export async function issueInvoice (id: string): Promise<Invoice> {
  const { data } = await api.post<Invoice>(`/api/billing/invoices/${id}/issue/`)
  return data
}

export async function voidInvoice (id: string, reason: string): Promise<Invoice> {
  const { data } = await api.post<Invoice>(`/api/billing/invoices/${id}/void/`, { reason })
  return data
}

/** Queue the invoice email. 202, not 200 -- a worker does the sending. */
export async function sendInvoice (id: string): Promise<Invoice> {
  const { data } = await api.post<Invoice>(`/api/billing/invoices/${id}/send/`)
  return data
}

export async function addInvoiceLine (payload: InvoiceLineRequest): Promise<InvoiceLine> {
  const { data } = await api.post<InvoiceLine>('/api/billing/invoice-lines/', payload)
  return data
}

export async function updateInvoiceLine (
  id: string,
  payload: PatchedInvoiceLineRequest,
): Promise<InvoiceLine> {
  const { data } = await api.patch<InvoiceLine>(`/api/billing/invoice-lines/${id}/`, payload)
  return data
}

export async function deleteInvoiceLine (id: string): Promise<void> {
  await api.delete(`/api/billing/invoice-lines/${id}/`)
}

/** Re-price an hourly visit from the hours actually worked. Never automatic. */
export async function repriceInvoiceLine (id: string): Promise<InvoiceLine> {
  const { data } = await api.post<InvoiceLine>(`/api/billing/invoice-lines/${id}/reprice/`)
  return data
}

export async function listPayments (
  query: { invoice?: string, method?: string[], received_from?: string, received_to?: string } = {},
): Promise<Paginated<Payment>> {
  const { data } = await api.get<Paginated<Payment>>('/api/billing/payments/', {
    params: query,
    paramsSerializer: { indexes: null },
  })
  return data
}

export async function recordPayment (payload: RecordPaymentRequest): Promise<Payment> {
  const { data } = await api.post<Payment>('/api/billing/payments/', payload)
  return data
}

/** Void a payment. The row stays and the balance comes back (ADR-025). */
export async function voidPayment (id: string, reason: string): Promise<Payment> {
  const { data } = await api.post<Payment>(`/api/billing/payments/${id}/void/`, { reason })
  return data
}

export async function updateOrganization (payload: OrganizationRequest): Promise<Organization> {
  const { data } = await api.patch<Organization>('/api/organizations/current/', payload)
  return data
}

// --- Stripe Connect (Phase 4b) ---------------------------------------------

/** The endpoints that hand back a Stripe page to send the browser to. */
async function postForUrl (path: string, body?: object): Promise<string> {
  const { data } = await api.post<Url>(path, body)
  return data.url
}

/** Owner only. Returns where to send the browser: Stripe's hosted onboarding. */
export function startStripeOnboarding (): Promise<string> {
  return postForUrl('/api/billing/stripe/connect/')
}

/** Re-read the account from Stripe; called once on return from onboarding. */
export async function refreshStripeStatus (): Promise<StripeStatus> {
  const { data } = await api.post<StripeStatus>('/api/billing/stripe/refresh/')
  return data
}

// --- The platform subscription (Phase 4c) ----------------------------------
// All three answer on a read-only organization: the owner must be able to pay.

/** Owner only. Returns where to send the browser: Stripe Checkout, subscription mode. */
export function startSubscription (): Promise<string> {
  return postForUrl('/api/billing/subscription/checkout/')
}

/** Owner only. Returns where to send the browser: Stripe's Customer Portal. */
export function openBillingPortal (): Promise<string> {
  return postForUrl('/api/billing/subscription/portal/')
}

/** Re-read the subscription from Stripe; called once on return from Checkout. */
export async function refreshSubscription (): Promise<PlatformBilling> {
  const { data } = await api.post<PlatformBilling>('/api/billing/subscription/refresh/')
  return data
}

/**
 * The public pay page. No session: the signed token in the invoice email is
 * the credential, and the server answers 404 for a bad or expired one. The
 * token goes in the body, never in the URL of an API call (ADR-036).
 */
export async function getPublicInvoice (token: string): Promise<PublicInvoice> {
  const payload: PayTokenRequest = { token }
  const { data } = await api.post<PublicInvoice>('/api/billing/pay/invoice/', payload)
  return data
}

/** Open a Checkout session for the balance. Returns Stripe's URL. */
export function openCheckout (token: string): Promise<string> {
  const payload: PayTokenRequest = { token }
  return postForUrl('/api/billing/pay/checkout/', payload)
}

// --- reports ---------------------------------------------------------------
// Owner and admin only; the API refuses everyone else with a 403. Every
// figure is computed on the server. The page adds nothing up.

/** Organization-local dates, both inclusive. The backend converts. */
export interface ReportPeriod {
  date_from: string
  date_to: string
}

export async function getTimesheet (period: ReportPeriod): Promise<Timesheet> {
  const { data } = await api.get<Timesheet>('/api/scheduling/reports/timesheet/', {
    params: period,
  })
  return data
}

/** Aged as of the organization's own today unless `asOf` says otherwise. */
export async function getReceivables (asOf?: string): Promise<Receivables> {
  const { data } = await api.get<Receivables>('/api/billing/reports/receivables/', {
    params: asOf ? { as_of: asOf } : {},
  })
  return data
}

export async function getCollections (period: ReportPeriod): Promise<Collections> {
  const { data } = await api.get<Collections>('/api/billing/reports/collections/', {
    params: period,
  })
  return data
}

const REPORT_CSV_PATHS = {
  timesheet: '/api/scheduling/reports/timesheet/csv/',
  receivables: '/api/billing/reports/receivables/csv/',
  collections: '/api/billing/reports/collections/csv/',
} as const

export type ReportName = keyof typeof REPORT_CSV_PATHS

/**
 * A report as a CSV file.
 *
 * Fetched here rather than opened as a link: a plain navigation carries the
 * session cookie but not the X-Organization header, so someone with two
 * memberships would have no organization resolved and be refused.
 */
export async function downloadReportCsv (
  report: ReportName,
  params: Partial<ReportPeriod> & { as_of?: string } = {},
): Promise<Blob> {
  const { data } = await api.get<Blob>(REPORT_CSV_PATHS[report], {
    params,
    responseType: 'blob',
  })
  return data
}

// --- team management (ADR-034) --------------------------------------------

/**
 * Everyone who works here, with what the caller may do to each.
 *
 * `staff=true` leaves the customers out on the server, so a long customer
 * list cannot push a colleague off the end of the page.
 */
export async function listTeam (): Promise<Paginated<TeamMember>> {
  const { data } = await api.get<Paginated<TeamMember>>('/api/users/memberships/', {
    params: { staff: true, limit: 200 },
  })
  return data
}

/** The roles the caller may invite someone as. The server decides (ADR-023). */
export async function getInviteOptions (): Promise<InviteOptions> {
  const { data } = await api.get<InviteOptions>('/api/users/memberships/invite-options/')
  return data
}

/** `dev_link` is on the response only when the API runs with LOCAL = True. */
export async function inviteMember (payload: InviteRequest): Promise<InvitedTeamMember> {
  const { data } = await api.post<InvitedTeamMember>('/api/users/memberships/', payload)
  return data
}

export async function changeMemberRole (id: string, role: StaffRole): Promise<TeamMember> {
  const { data } = await api.patch<TeamMember>(`/api/users/memberships/${id}/`, { role })
  return data
}

export async function deactivateMember (id: string): Promise<TeamMember> {
  const { data } = await api.post<TeamMember>(`/api/users/memberships/${id}/deactivate/`)
  return data
}

/** A withdrawn invitation comes back as an invitation, with a fresh link. */
export async function reactivateMember (id: string): Promise<InvitedTeamMember> {
  const { data } = await api.post<InvitedTeamMember>(`/api/users/memberships/${id}/reactivate/`)
  return data
}

/** The link in the earlier email stops working. */
export async function resendInvite (id: string): Promise<InvitedTeamMember> {
  const { data } = await api.post<InvitedTeamMember>(
    `/api/users/memberships/${id}/resend-invite/`,
  )
  return data
}

// --- invitations and password reset: public, no session --------------------

/**
 * What an invitation link opens. The token goes in the body, never in the
 * URL of an API call; the page's own address carries it in the fragment,
 * which no request includes (ADR-036).
 */
export async function inspectInvitation (token: string): Promise<Invitation> {
  const { data } = await api.post<Invitation>('/api/users/invitations/inspect/', { token })
  return data
}

/** Returns the server's sentence. Nobody is signed in by this. */
export async function acceptInvitation (payload: InvitationAcceptRequest): Promise<string> {
  const { data } = await api.post<{ detail: string }>('/api/users/invitations/accept/', payload)
  return data.detail
}

/** The same answer for every address, by design. */
export async function requestPasswordReset (email: string): Promise<PasswordResetSent> {
  const { data } = await api.post<PasswordResetSent>('/api/auth/password-reset/', { email })
  return data
}

/**
 * A customer's sign-in link (ADR-008, ADR-039). The same 202 for every
 * address, staff and strangers included. Redeeming the link signs someone in,
 * so that half is the session store's (`signInWithLink`).
 */
export async function requestSignInLink (email: string): Promise<MagicLinkSent> {
  const { data } = await api.post<MagicLinkSent>('/api/users/magic-link/', { email })
  return data
}

/** Returns the server's sentence. Nobody is signed in by this either. */
export async function confirmPasswordReset (token: string, password: string): Promise<string> {
  const { data } = await api.post<{ detail: string }>(
    '/api/auth/password-reset/confirm/',
    { token, password },
  )
  return data.detail
}
