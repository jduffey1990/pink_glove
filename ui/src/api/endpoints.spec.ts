/**
 * The customer import calls (ADR-031).
 *
 * Only these two have a spec here, because of what is at stake in one form
 * field: `dry_run` is the difference between a preview and a write, so what
 * goes on the wire for each is pinned.
 */

import MockAdapter from 'axios-mock-adapter'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { api } from './client'
import { fetchCustomerImportTemplate, importCustomers } from './endpoints'

const IMPORT_URL = '/api/customers/customers/import/'
const FILE = new File(['first_name\r\nDana\r\n'], 'book.csv', { type: 'text/csv' })

describe('importCustomers', () => {
  let mock: MockAdapter

  beforeEach(() => {
    mock = new MockAdapter(api)
    mock.onPost(IMPORT_URL).reply(200, { rows: [] })
  })

  afterEach(() => {
    mock.restore()
  })

  function sentForm (): FormData {
    expect(mock.history.post).toHaveLength(1)
    const form = mock.history.post[0].data
    expect(form).toBeInstanceOf(FormData)
    return form as FormData
  }

  it('uploads the file itself, as a file', async () => {
    await importCustomers(FILE, true)

    const sent = sentForm().get('file')
    expect(sent).toBeInstanceOf(File)
    expect((sent as File).name).toBe('book.csv')
  })

  it('asks for a dry run when previewing', async () => {
    await importCustomers(FILE, true)

    expect(sentForm().get('dry_run')).toBe('true')
  })

  it('asks for a real import only when told to', async () => {
    await importCustomers(FILE, false)

    expect(sentForm().get('dry_run')).toBe('false')
  })

  it('returns the server\'s result', async () => {
    expect(await importCustomers(FILE, true)).toEqual({ rows: [] })
  })
})

describe('fetchCustomerImportTemplate', () => {
  it('reads the template from the API', async () => {
    const mock = new MockAdapter(api)
    const template = { filename: 't.csv', csv: 'a\r\n', columns: [], max_rows: 2000, max_megabytes: 1 }
    mock.onGet('/api/customers/customers/import-template/').reply(200, template)

    expect(await fetchCustomerImportTemplate()).toEqual(template)

    mock.restore()
  })
})
