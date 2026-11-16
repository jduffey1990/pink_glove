import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { saveTextFile } from './download'

describe('saveTextFile', () => {
  let blobs: Blob[]
  let clicked: { download: string, href: string }[]

  // The click is caught at the document, so it only counts if the link was
  // attached when it was clicked, and jsdom is kept from trying to navigate.
  function onClick (event: MouseEvent) {
    event.preventDefault()
    const link = event.target as HTMLAnchorElement
    clicked.push({ download: link.download, href: link.href })
  }

  beforeEach(() => {
    blobs = []
    clicked = []

    // jsdom has neither; the browser has both.
    URL.createObjectURL = vi.fn((blob: Blob) => {
      blobs.push(blob)
      return 'blob:template'
    })
    URL.revokeObjectURL = vi.fn()

    document.addEventListener('click', onClick)
  })

  afterEach(() => {
    document.removeEventListener('click', onClick)
  })

  it('offers the text as a download under the given name', async () => {
    saveTextFile('customer-import-template.csv', 'first_name,city\r\nSample,Lakewood\r\n')

    expect(clicked).toEqual([
      { download: 'customer-import-template.csv', href: 'blob:template' },
    ])
    expect(blobs[0].type).toBe('text/csv;charset=utf-8')
    expect(await blobs[0].text()).toBe('first_name,city\r\nSample,Lakewood\r\n')
  })

  it('cleans up after itself', () => {
    saveTextFile('a.csv', 'x')

    expect(document.querySelector('a[download]')).toBeNull()
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:template')
  })
})
