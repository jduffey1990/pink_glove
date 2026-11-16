/**
 * The import dialog, mounted.
 *
 * The flow has its own spec in `lib/customerImport.spec.ts`. This one exists
 * because no browser was pointed at the dialog when it was written: it proves
 * the markup is wired to that flow, that what is on screen is the server's
 * wording, and that the Import button follows the server's `can_commit`. It
 * says nothing about how the dialog looks.
 */

import type { CustomerImportResult, CustomerImportTemplate } from '@/api/types'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createVuetify } from 'vuetify'
import { fetchCustomerImportTemplate, importCustomers } from '@/api/endpoints'
import { saveTextFile } from '@/lib/download'
import CustomerImportDialog from './CustomerImportDialog.vue'

vi.mock('@/api/endpoints', () => ({
  fetchCustomerImportTemplate: vi.fn(),
  importCustomers: vi.fn(),
}))
vi.mock('@/lib/download', () => ({ saveTextFile: vi.fn() }))

const TEMPLATE: CustomerImportTemplate = {
  filename: 'customer-import-template.csv',
  csv: 'first_name,address_line1\r\nSample,100 Example St\r\n',
  columns: [
    { header: 'first_name', required: false, hint: '' },
    { header: 'address_line1', required: true, hint: '' },
  ],
  max_rows: 2000,
  max_megabytes: 1,
}

const FILE = new File(['first_name\r\nDana\r\n'], 'book.csv', { type: 'text/csv' })

function result (overrides: Partial<CustomerImportResult> = {}): CustomerImportResult {
  return {
    dry_run: true,
    committed: false,
    can_commit: true,
    message: 'Ready to import 1 customer and 1 location. Nothing has been saved yet.',
    totals: { rows: 1, new_customers: 1, new_locations: 1, skipped: 0, errors: 0 },
    ignored_columns: [],
    rows: [
      {
        row: 2,
        action: 'create_customer',
        customer: 'Dana Wu',
        address: '1 Elm St, Denver, CO, 80202',
        detail: 'New customer with this location.',
        errors: [],
      },
    ],
    ...overrides,
  }
}

/** The dialog teleports to the body, so that is where its text and buttons are. */
function screen (): string {
  return document.body.textContent ?? ''
}

function button (label: string): HTMLButtonElement {
  const found = [...document.body.querySelectorAll('button')]
    .find(candidate => candidate.textContent?.trim() === label)
  if (!found) {
    throw new Error(`No "${label}" button on screen.`)
  }
  return found
}

async function open () {
  const wrapper = mount(CustomerImportDialog, {
    props: { modelValue: true },
    global: { plugins: [createVuetify()] },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
}

async function chooseFile (wrapper: Awaited<ReturnType<typeof open>>) {
  wrapper.findComponent({ name: 'VFileInput' }).vm.$emit('update:modelValue', FILE)
  await flushPromises()
}

async function press (label: string) {
  button(label).click()
  await flushPromises()
}

describe('CustomerImportDialog', () => {
  beforeEach(() => {
    // Two browser APIs Vuetify's overlay reaches for and jsdom does not have.
    // Stand-ins that do nothing: positioning is not what is under test.
    vi.stubGlobal('visualViewport', Object.assign(new EventTarget(), {
      width: 390,
      height: 844,
      offsetLeft: 0,
      offsetTop: 0,
      scale: 1,
    }))
    vi.stubGlobal('ResizeObserver', class {
      observe () {}
      unobserve () {}
      disconnect () {}
    })

    vi.mocked(fetchCustomerImportTemplate).mockResolvedValue(TEMPLATE)
    vi.mocked(importCustomers).mockResolvedValue(result())
  })

  afterEach(() => {
    vi.clearAllMocks()
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
  })

  it('opens on the file step, with the server\'s columns and limits', async () => {
    await open()

    expect(screen()).toContain('Import customers')
    expect(screen()).toContain('Required columns: address_line1.')
    expect(screen()).toContain('Up to 2000 rows and 1 MB')
    expect(button('Preview').disabled).toBe(true)
    expect(importCustomers).not.toHaveBeenCalled()
  })

  it('downloads the template the server sent', async () => {
    await open()
    await press('Download the template')

    expect(saveTextFile).toHaveBeenCalledWith(TEMPLATE.filename, TEMPLATE.csv)
  })

  it('still opens when the template cannot be fetched', async () => {
    vi.mocked(fetchCustomerImportTemplate).mockRejectedValue(new Error('offline'))
    await open()

    expect(screen()).toContain('Import customers')
    expect(button('Download the template').disabled).toBe(true)
  })

  it('previews the chosen file as a dry run and shows what the server said', async () => {
    const wrapper = await open()
    await chooseFile(wrapper)
    await press('Preview')

    expect(vi.mocked(importCustomers).mock.calls).toEqual([[FILE, true]])
    expect(screen()).toContain('Ready to import 1 customer and 1 location.')
    expect(screen()).toContain('Will be imported (1)')
    expect(screen()).toContain('Row 2: Dana Wu, 1 Elm St, Denver, CO, 80202')
    expect(screen()).toContain('New customer with this location.')
    expect(button('Import').disabled).toBe(false)
    expect(wrapper.emitted('imported')).toBeUndefined()
  })

  it('imports on confirm, shows the result and tells the page', async () => {
    vi.mocked(importCustomers)
      .mockResolvedValueOnce(result())
      .mockResolvedValueOnce(result({
        dry_run: false,
        committed: true,
        can_commit: false,
        message: 'Imported 1 customer and 1 location.',
      }))
    const wrapper = await open()
    await chooseFile(wrapper)
    await press('Preview')
    await press('Import')

    expect(vi.mocked(importCustomers).mock.calls).toEqual([[FILE, true], [FILE, false]])
    expect(screen()).toContain('Imported 1 customer and 1 location.')
    expect(screen()).toContain('Imported (1)')
    expect(wrapper.emitted('imported')).toHaveLength(1)

    await press('Done')
    expect(wrapper.emitted('update:modelValue')).toEqual([[false]])
  })

  it('renders a row\'s errors as sent and offers nothing to import', async () => {
    vi.mocked(importCustomers).mockResolvedValue(result({
      can_commit: false,
      message: 'Nothing can be imported yet: 1 row needs fixing.',
      totals: { rows: 1, new_customers: 0, new_locations: 0, skipped: 0, errors: 1 },
      ignored_columns: ['Balance Due'],
      rows: [
        {
          row: 2,
          action: 'error',
          customer: 'Dana Wu',
          address: '',
          detail: 'Not imported. Fix this row and upload the file again.',
          errors: [
            { column: 'email', message: 'Enter a valid email address.' },
            { column: '', message: 'This row has more cells than the header has columns.' },
          ],
        },
      ],
    }))
    const wrapper = await open()
    await chooseFile(wrapper)
    await press('Preview')

    expect(screen()).toContain('Nothing can be imported yet: 1 row needs fixing.')
    expect(screen()).toContain('Needs fixing (1)')
    expect(screen()).toContain('email: Enter a valid email address.')
    expect(screen()).toContain('This row has more cells than the header has columns.')
    expect(screen()).toContain('Balance Due')
    expect(button('Import').disabled).toBe(true)
  })

  it('goes back to the file step for another file', async () => {
    const wrapper = await open()
    await chooseFile(wrapper)
    await press('Preview')
    await press('Choose another file')

    expect(button('Preview').disabled).toBe(true)
    expect(screen()).not.toContain('Will be imported')
  })
})
