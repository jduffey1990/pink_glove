import type {
  CustomerImportResult,
  CustomerImportRow,
  CustomerImportTemplate,
} from '@/api/types'
import { AxiosError, type AxiosResponse } from 'axios'
import { describe, expect, it, vi } from 'vitest'
import {
  errorText,
  groupRows,
  importFailure,
  importStep,
  importTone,
  pickedFile,
  refusedImport,
  rowHeading,
  templateSummary,
  useCustomerImport,
} from './customerImport'

function row (overrides: Partial<CustomerImportRow> = {}): CustomerImportRow {
  return {
    row: 2,
    action: 'create_customer',
    customer: 'Dana Wu',
    address: '1 Elm St, Denver, CO, 80202',
    detail: 'New customer with this location.',
    errors: [],
    ...overrides,
  }
}

function result (
  rows: CustomerImportRow[] = [row()],
  overrides: Partial<CustomerImportResult> = {},
): CustomerImportResult {
  const count = (...actions: string[]) => rows.filter(item => actions.includes(item.action)).length
  const errors = count('error')
  const changes = count('create_customer', 'add_location')

  return {
    dry_run: true,
    committed: false,
    can_commit: errors === 0 && changes > 0,
    message: 'The server said this.',
    totals: {
      rows: rows.length,
      new_customers: count('create_customer'),
      new_locations: changes,
      skipped: count('skip'),
      errors,
    },
    ignored_columns: [],
    rows,
    ...overrides,
  }
}

function failure (status: number, data: unknown): AxiosError {
  const response = { status, data } as AxiosResponse
  return new AxiosError('failed', 'ERR_BAD_REQUEST', undefined, undefined, response)
}

const BAD_ROW = row({
  row: 3,
  action: 'error',
  detail: 'Not imported. Fix this row and upload the file again.',
  errors: [{ column: 'email', message: 'Enter a valid email address.' }],
})

const FILE = new File(['first_name\r\nDana\r\n'], 'book.csv', { type: 'text/csv' })

describe('importStep', () => {
  it('starts by choosing a file', () => {
    expect(importStep(null)).toBe('choose')
  })

  it('shows a preview for a dry run, and for an import a row stopped', () => {
    expect(importStep(result())).toBe('preview')
    expect(importStep(result([BAD_ROW], { dry_run: false, committed: false }))).toBe('preview')
  })

  it('is done once the server says it committed', () => {
    expect(importStep(result([row()], { dry_run: false, committed: true }))).toBe('done')
  })
})

describe('refusedImport', () => {
  it('reads the per-row result out of a 400', () => {
    const body = result([BAD_ROW], { dry_run: false })
    expect(refusedImport(failure(400, body))).toEqual(body)
  })

  it('is null for a refusal about the file as a whole', () => {
    expect(refusedImport(failure(400, { file: ['This file is empty.'] }))).toBeNull()
    expect(refusedImport(failure(403, { detail: 'You need admin access to do that.' }))).toBeNull()
    expect(refusedImport(new Error('offline'))).toBeNull()
  })
})

describe('importFailure', () => {
  it('uses the server\'s sentence about the file', () => {
    const error = failure(400, { file: ['This file is missing required columns: city.'] })
    expect(importFailure(error)).toBe('This file is missing required columns: city.')
  })

  it('uses the server\'s refusal of the person', () => {
    expect(importFailure(failure(403, { detail: 'You need admin access to do that.' })))
      .toBe('You need admin access to do that.')
  })

  it('has something to say when the server said nothing', () => {
    expect(importFailure(new Error('offline'))).toContain('Could not upload')
  })
})

describe('pickedFile', () => {
  it('takes the file however the input hands it over', () => {
    expect(pickedFile(FILE)).toBe(FILE)
    expect(pickedFile([FILE])).toBe(FILE)
  })

  it('is null when nothing is chosen', () => {
    expect(pickedFile(null)).toBeNull()
    expect(pickedFile(undefined)).toBeNull()
    expect(pickedFile([])).toBeNull()
  })
})

describe('importTone', () => {
  it('is an error whenever a row needs fixing', () => {
    expect(importTone(result([row(), BAD_ROW]))).toBe('error')
  })

  it('is a success only for an import that wrote something', () => {
    expect(importTone(result([row()], { dry_run: false, committed: true }))).toBe('success')
  })

  it('is neutral for a preview, and for an import with nothing to add', () => {
    expect(importTone(result([row()]))).toBe('info')
    expect(importTone(result([row({ action: 'skip' })], { dry_run: false, committed: true })))
      .toBe('info')
  })
})

describe('groupRows', () => {
  const rows = [
    row({ row: 2 }),
    BAD_ROW,
    row({ row: 4, action: 'add_location' }),
    row({ row: 5, action: 'skip' }),
  ]

  it('puts what must be fixed first, then what changes, then what is skipped', () => {
    const groups = groupRows(result(rows))

    expect(groups.map(group => group.key)).toEqual(['errors', 'changes', 'skipped'])
    expect(groups.map(group => group.rows.map(item => item.row))).toEqual([[3], [2, 4], [5]])
    expect(groups.map(group => group.title)).toEqual([
      'Needs fixing (1)',
      'Will be imported (2)',
      'Already on file (1)',
    ])
  })

  it('says "Imported" once it has happened', () => {
    const groups = groupRows(result([row()], { dry_run: false, committed: true }))
    expect(groups[0].title).toBe('Imported (1)')
  })

  it('leaves out a group with nothing in it', () => {
    expect(groupRows(result([row()])).map(group => group.key)).toEqual(['changes'])
  })

  it('caps the uneventful rows and counts the rest', () => {
    const many = Array.from({ length: 7 }, (_, index) => row({ row: index + 2 }))
    const [changes] = groupRows(result(many), 3)

    expect(changes.rows.map(item => item.row)).toEqual([2, 3, 4])
    expect(changes.hidden).toBe(4)
    expect(changes.title).toBe('Will be imported (7)')
  })

  it('never caps the rows that need fixing', () => {
    const many = Array.from({ length: 7 }, (_, index) => ({ ...BAD_ROW, row: index + 2 }))
    const [errors] = groupRows(result(many), 3)

    expect(errors.rows).toHaveLength(7)
    expect(errors.hidden).toBe(0)
  })
})

describe('rowHeading', () => {
  it('names the row, the customer and the address', () => {
    expect(rowHeading(row())).toBe('Row 2: Dana Wu, 1 Elm St, Denver, CO, 80202')
  })

  it('makes do with whichever the row has', () => {
    expect(rowHeading(row({ address: '' }))).toBe('Row 2: Dana Wu')
    expect(rowHeading(row({ customer: '', address: '' }))).toBe('Row 2')
  })
})

describe('errorText', () => {
  it('leads with the file\'s own column header', () => {
    expect(errorText({ column: 'email', message: 'Enter a valid email address.' }))
      .toBe('email: Enter a valid email address.')
  })

  it('is the message alone when it is about the whole row', () => {
    expect(errorText({ column: '', message: 'A customer needs a name.' }))
      .toBe('A customer needs a name.')
  })
})

describe('templateSummary', () => {
  const template: CustomerImportTemplate = {
    filename: 'customer-import-template.csv',
    csv: 'first_name,city\r\nSample,Lakewood\r\n',
    columns: [
      { header: 'first_name', required: false, hint: '' },
      { header: 'address_line1', required: true, hint: '' },
      { header: 'city', required: true, hint: '' },
    ],
    max_rows: 2000,
    max_megabytes: 1,
  }

  it('lists the columns the server requires and the server\'s limits', () => {
    expect(templateSummary(template)).toBe(
      'Required columns: address_line1, city. Up to 2000 rows and 1 MB, saved as CSV UTF-8.',
    )
  })

  it('says nothing about required columns when there are none', () => {
    const none = { ...template, columns: [{ header: 'first_name', required: false, hint: '' }] }
    expect(templateSummary(none)).toBe('Up to 2000 rows and 1 MB, saved as CSV UTF-8.')
  })
})

describe('useCustomerImport', () => {
  it('does nothing until a file is chosen', async () => {
    const upload = vi.fn()
    const flow = useCustomerImport(upload)

    expect(flow.canPreview.value).toBe(false)
    await flow.preview()
    await flow.confirm()

    expect(upload).not.toHaveBeenCalled()
    expect(flow.step.value).toBe('choose')
  })

  it('previews with a dry run and never writes on its own', async () => {
    const upload = vi.fn().mockResolvedValue(result())
    const imported = vi.fn()
    const flow = useCustomerImport(upload, imported)

    flow.choose([FILE])
    await flow.preview()

    expect(upload.mock.calls).toEqual([[FILE, true]])
    expect(flow.step.value).toBe('preview')
    expect(flow.canConfirm.value).toBe(true)
    expect(imported).not.toHaveBeenCalled()
  })

  it('imports the same file for real on confirm, then says so', async () => {
    const committed = result([row()], { dry_run: false, committed: true })
    const upload = vi.fn().mockResolvedValueOnce(result()).mockResolvedValueOnce(committed)
    const imported = vi.fn()
    const flow = useCustomerImport(upload, imported)

    flow.choose(FILE)
    await flow.preview()
    await flow.confirm()

    expect(upload.mock.calls).toEqual([[FILE, true], [FILE, false]])
    expect(flow.step.value).toBe('done')
    expect(flow.result.value).toEqual(committed)
    expect(imported).toHaveBeenCalledTimes(1)
  })

  it('will not import a file the server said cannot be committed', async () => {
    const upload = vi.fn().mockResolvedValue(result([row(), BAD_ROW]))
    const flow = useCustomerImport(upload)

    flow.choose(FILE)
    await flow.preview()
    expect(flow.canConfirm.value).toBe(false)
    await flow.confirm()

    expect(upload).toHaveBeenCalledTimes(1)
    expect(flow.groups.value[0].key).toBe('errors')
  })

  it('will not import a file where every row is already on file', async () => {
    const upload = vi.fn().mockResolvedValue(result([row({ action: 'skip' })]))
    const flow = useCustomerImport(upload)

    flow.choose(FILE)
    await flow.preview()
    await flow.confirm()

    expect(flow.canConfirm.value).toBe(false)
    expect(upload).toHaveBeenCalledTimes(1)
  })

  it('shows the server\'s reason when the file itself is refused', async () => {
    const upload = vi.fn().mockRejectedValue(failure(400, { file: ['This file is empty.'] }))
    const flow = useCustomerImport(upload)

    flow.choose(FILE)
    await flow.preview()

    expect(flow.error.value).toBe('This file is empty.')
    expect(flow.step.value).toBe('choose')
    expect(flow.busy.value).toBe(false)
  })

  it('shows the rows when a real import is stopped by one', async () => {
    const refused = result([row(), BAD_ROW], { dry_run: false })
    const upload = vi.fn()
      .mockResolvedValueOnce(result())
      .mockRejectedValueOnce(failure(400, refused))
    const imported = vi.fn()
    const flow = useCustomerImport(upload, imported)

    flow.choose(FILE)
    await flow.preview()
    await flow.confirm()

    expect(flow.step.value).toBe('preview')
    expect(flow.result.value).toEqual(refused)
    expect(flow.canConfirm.value).toBe(false)
    expect(flow.error.value).toBe('')
    expect(imported).not.toHaveBeenCalled()
  })

  it('keeps the preview when a real import fails for another reason', async () => {
    const upload = vi.fn()
      .mockResolvedValueOnce(result())
      .mockRejectedValueOnce(new Error('offline'))
    const flow = useCustomerImport(upload)

    flow.choose(FILE)
    await flow.preview()
    await flow.confirm()

    expect(flow.step.value).toBe('preview')
    expect(flow.error.value).toContain('Could not upload')
    expect(flow.canConfirm.value).toBe(true)
  })

  it('forgets the last answer when another file is chosen', async () => {
    const upload = vi.fn().mockResolvedValue(result())
    const flow = useCustomerImport(upload)

    flow.choose(FILE)
    await flow.preview()
    flow.choose(new File(['x'], 'other.csv'))

    expect(flow.result.value).toBeNull()
    expect(flow.step.value).toBe('choose')
    expect(flow.canConfirm.value).toBe(false)
  })

  it('starts over with no file and no answer', async () => {
    const upload = vi.fn().mockResolvedValue(result())
    const flow = useCustomerImport(upload)

    flow.choose(FILE)
    await flow.preview()
    flow.startOver()

    expect(flow.file.value).toBeNull()
    expect(flow.result.value).toBeNull()
    expect(flow.canPreview.value).toBe(false)
  })

  it('sends one upload at a time', async () => {
    let finish: (value: CustomerImportResult) => void = () => {}
    const upload = vi.fn().mockReturnValue(
      new Promise<CustomerImportResult>(resolve => {
        finish = resolve
      }),
    )
    const flow = useCustomerImport(upload)

    flow.choose(FILE)
    const first = flow.preview()
    const second = flow.preview()
    expect(flow.busy.value).toBe(true)
    expect(flow.canPreview.value).toBe(false)

    finish(result())
    await Promise.all([first, second])

    expect(upload).toHaveBeenCalledTimes(1)
    expect(flow.busy.value).toBe(false)
  })
})
