/**
 * Typed helpers over the generated schema, one per endpoint the slice uses.
 *
 * Deliberately thin. The types come from `schema.d.ts` (ADR-019); these
 * functions exist so a component writes `listJobs({ mine: true })` instead of
 * assembling URLs, and so a change to a path shows up in one place.
 */

import type {
  Customer,
  CustomerRequest,
  Job,
  JobNote,
  JobPhoto,
  JobRequest,
  JobStatus,
  Membership,
  Organization,
  Paginated,
  RecurringPlan,
  RecurringPlanRequest,
  Service,
  ServiceLocation,
  ServiceLocationRequest,
  ServiceRequest,
  TimeEntry,
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

export async function createJob (payload: JobRequest): Promise<Job> {
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

export interface PlanPreview {
  timezone: string
  occurrences: { local: string, utc: string }[]
}

export interface RegenerateResult {
  regenerated: number
  kept: number
}

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

export async function materializePlan (id: string): Promise<{ created: number }> {
  const { data } = await api.post<{ created: number }>(`/api/scheduling/plans/${id}/materialize/`)
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

export interface RevealedCodes {
  gate_code: string
  alarm_code: string
  key_location: string
  reveal_id: string
  job: string | null
}

/**
 * Fetch the warning a user must acknowledge before codes are revealed.
 *
 * Obtained by deliberately asking *without* acknowledgement: the backend
 * answers 400 carrying `audit.models.ACCESS_WARNING`. Rendering the server's
 * own words means the warning and the thing it warns about cannot drift, and
 * nothing is logged by this call -- no reveal happened.
 */
export async function fetchAccessWarning (locationId: string): Promise<string> {
  try {
    await api.post(`/api/customers/locations/${locationId}/reveal-access/`, {
      acknowledged: false,
    })
  } catch (error) {
    const detail = (error as { response?: { data?: { detail?: string } } })
      .response
      ?.data
      ?.detail
    if (detail) {
      return detail
    }
    throw error
  }

  // A 200 here would mean the backend stopped requiring acknowledgement.
  throw new Error('The API did not ask for an acknowledgement.')
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
