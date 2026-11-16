/**
 * What the customer import dialog shows, from what the server said (ADR-031).
 *
 * The server decides everything that matters: what each row is, the sentence
 * against it, the error messages, the summary, and whether the file can be
 * imported at all (`can_commit`). Nothing here re-derives any of that
 * (ADR-023). This only arranges it for a narrow screen: which step the dialog
 * is on, the rows in groups, and a cap on how many of the uneventful ones are
 * drawn. Pure, so it has a spec; the dialog itself stays thin.
 */

import type {
  CustomerImportResult,
  CustomerImportRow,
  CustomerImportRowError,
  CustomerImportTemplate,
} from '@/api/types'
import { computed, ref } from 'vue'
import { bodyOf, errorDetail } from '@/api/errors'

export type ImportStep = 'choose' | 'preview' | 'done'

/** Where the dialog is: no answer yet, a plan to confirm, or an import that happened. */
export function importStep (result: CustomerImportResult | null): ImportStep {
  if (result === null) {
    return 'choose'
  }
  return result.committed ? 'done' : 'preview'
}

/**
 * The per-row result inside a refused import.
 *
 * A real import that a row stops is a 400 whose body is the same shape a dry
 * run returns, so the rows can be shown rather than a bare "it failed". A 400
 * about the file as a whole (`{file: [...]}`) is not one, and comes back null.
 */
export function refusedImport (error: unknown): CustomerImportResult | null {
  const body = bodyOf<Partial<CustomerImportResult>>(error)
  if (body === null || !Array.isArray(body.rows) || typeof body.totals !== 'object') {
    return null
  }
  return body as CustomerImportResult
}

/** The server's reason a file was refused, or a fallback when it gave none. */
export function importFailure (error: unknown): string {
  return errorDetail(error) ?? 'Could not upload that file. Choose it again and retry.'
}

/** v-file-input hands back a file, a list of them, or nothing. */
export function pickedFile (value: File | File[] | null | undefined): File | null {
  if (Array.isArray(value)) {
    return value[0] ?? null
  }
  return value ?? null
}

export type ImportTone = 'error' | 'success' | 'info'

/** The colour of the summary: something to fix, something done, or neither. */
export function importTone (result: CustomerImportResult): ImportTone {
  if (result.totals.errors > 0) {
    return 'error'
  }
  return result.committed && result.totals.new_locations > 0 ? 'success' : 'info'
}

export interface ImportRowGroup {
  key: 'errors' | 'changes' | 'skipped'
  title: string
  /** The rows drawn. Every error row, and at most `limit` of the others. */
  rows: CustomerImportRow[]
  /** How many more there are than are drawn. */
  hidden: number
}

/** How many uneventful rows a group draws before it says "and N more". */
export const ROW_LIMIT = 50

/**
 * The rows in the order a person needs them: what must be fixed, then what
 * changes, then what is skipped. Empty groups are left out.
 *
 * Error rows are never capped, because each one is something to do. The
 * others are, since a 2000-row file is 2000 near-identical lines and the
 * totals already say how many there are.
 */
export function groupRows (
  result: CustomerImportResult,
  limit: number = ROW_LIMIT,
): ImportRowGroup[] {
  const errors = result.rows.filter(row => row.action === 'error')
  const changes = result.rows.filter(
    row => row.action === 'create_customer' || row.action === 'add_location',
  )
  const skipped = result.rows.filter(row => row.action === 'skip')

  const capped = (
    key: ImportRowGroup['key'],
    title: string,
    rows: CustomerImportRow[],
    cap: number,
  ): ImportRowGroup => ({
    key,
    title: `${title} (${rows.length})`,
    rows: rows.slice(0, cap),
    hidden: Math.max(0, rows.length - cap),
  })

  return [
    capped('errors', 'Needs fixing', errors, errors.length),
    capped('changes', result.committed ? 'Imported' : 'Will be imported', changes, limit),
    capped('skipped', 'Already on file', skipped, limit),
  ].filter(group => group.rows.length > 0)
}

/** "Row 5: Dana Wu, 1 Elm St, Denver, CO, 80202", with whichever parts the row has. */
export function rowHeading (row: CustomerImportRow): string {
  const who = [row.customer, row.address].filter(Boolean).join(', ')
  return who ? `Row ${row.row}: ${who}` : `Row ${row.row}`
}

/** An error as the server sent it, led by the file's own column header when it has one. */
export function errorText (error: CustomerImportRowError): string {
  return error.column ? `${error.column}: ${error.message}` : error.message
}

/** The one line under the file picker: what a file must have and how big it may be. */
export function templateSummary (template: CustomerImportTemplate): string {
  const required = template.columns
    .filter(column => column.required)
    .map(column => column.header)
  const columns = required.length > 0 ? `Required columns: ${required.join(', ')}. ` : ''
  return `${columns}Up to ${template.max_rows} rows and ${template.max_megabytes} MB, saved as CSV UTF-8.`
}

/** `importCustomers` from `api/endpoints`, passed in so the flow has a spec. */
export type ImportUpload = (file: File, dryRun: boolean) => Promise<CustomerImportResult>

/**
 * The dialog's whole flow: choose a file, preview it, confirm, see the result.
 *
 * Two rules live here because they are the ones that must not be got wrong:
 * choosing a file never writes (a preview is always a dry run), and a real
 * import is only ever sent for a file whose last answer said `can_commit`.
 * The server enforces both regardless; this keeps the button honest.
 */
export function useCustomerImport (upload: ImportUpload, onImported: () => void = () => {}) {
  const file = ref<File | null>(null)
  const result = ref<CustomerImportResult | null>(null)
  const busy = ref(false)
  const error = ref('')

  const step = computed(() => importStep(result.value))
  const groups = computed(() => (result.value ? groupRows(result.value) : []))
  const canPreview = computed(() => file.value !== null && !busy.value)
  const canConfirm = computed(
    () => step.value === 'preview' && result.value?.can_commit === true && !busy.value,
  )

  /** A new file, or none: whatever was said about the last one no longer applies. */
  function choose (value: File | File[] | null | undefined): void {
    file.value = pickedFile(value)
    result.value = null
    error.value = ''
  }

  function startOver (): void {
    choose(null)
  }

  async function send (dryRun: boolean): Promise<void> {
    if (file.value === null || busy.value) {
      return
    }
    busy.value = true
    error.value = ''

    try {
      result.value = await upload(file.value, dryRun)
      if (result.value.committed) {
        onImported()
      }
    } catch (error_) {
      // A real import stopped by a row still says what every row is; show
      // that. Anything else is about the file or the connection.
      const refused = refusedImport(error_)
      if (refused === null) {
        error.value = importFailure(error_)
        if (dryRun) {
          result.value = null
        }
      } else {
        result.value = refused
      }
    } finally {
      busy.value = false
    }
  }

  function preview (): Promise<void> {
    return send(true)
  }

  async function confirm (): Promise<void> {
    if (canConfirm.value) {
      await send(false)
    }
  }

  return {
    file,
    result,
    busy,
    error,
    step,
    groups,
    canPreview,
    canConfirm,
    choose,
    startOver,
    preview,
    confirm,
  }
}
