import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { saveBlob, saveTextFile } from './download'

let blobs: Blob[]
let clicked: { href: string | null, download: string }[]

/**
 * Listens on the document, so a click is only seen if the link was in the
 * page when it was clicked: a detached element's click never bubbles here.
 */
function onClick (event: MouseEvent): void {
  // jsdom would otherwise try to navigate to the blob URL.
  event.preventDefault()
  const link = event.target as HTMLAnchorElement
  clicked.push({ href: link.getAttribute('href'), download: link.download })
}

beforeEach(() => {
  vi.useFakeTimers()
  blobs = []
  clicked = []

  // jsdom has neither of these: there is nowhere for a blob URL to point.
  URL.createObjectURL = vi.fn((blob: Blob) => {
    blobs.push(blob)
    return 'blob:file'
  })
  URL.revokeObjectURL = vi.fn()

  document.addEventListener('click', onClick)
})

afterEach(() => {
  document.removeEventListener('click', onClick)
  vi.useRealTimers()
})

describe('saveBlob', () => {
  it('clicks a link to the blob, named as asked', () => {
    const blob = new Blob(['staff,seconds\r\n'], { type: 'text/csv' })

    saveBlob(blob, 'timesheet_2027-06-14_2027-06-20.csv')

    expect(URL.createObjectURL).toHaveBeenCalledWith(blob)
    expect(clicked).toEqual([
      { href: 'blob:file', download: 'timesheet_2027-06-14_2027-06-20.csv' },
    ])
  })

  it('leaves nothing behind in the page', () => {
    saveBlob(new Blob(['x']), 'a.csv')

    expect(document.querySelectorAll('a')).toHaveLength(0)
  })

  it('keeps the URL alive for the download, then lets it go', () => {
    saveBlob(new Blob(['x']), 'a.csv')

    expect(URL.revokeObjectURL).not.toHaveBeenCalled()

    vi.advanceTimersByTime(1000)

    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:file')
  })
})

describe('saveTextFile', () => {
  it('offers the text as a download under the given name', async () => {
    saveTextFile('customer-import-template.csv', 'first_name,city\r\nSample,Lakewood\r\n')

    expect(clicked).toEqual([
      { href: 'blob:file', download: 'customer-import-template.csv' },
    ])
    expect(blobs[0].type).toBe('text/csv;charset=utf-8')
    expect(await blobs[0].text()).toBe('first_name,city\r\nSample,Lakewood\r\n')
  })

  it('cleans up after itself', () => {
    saveTextFile('a.csv', 'x')
    vi.advanceTimersByTime(1000)

    expect(document.querySelector('a[download]')).toBeNull()
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:file')
  })
})
