/**
 * Hand the browser a file that was fetched, not navigated to.
 *
 * A plain link to the API would not do: the request needs the session cookie
 * and, for someone in two organizations, the X-Organization header that only
 * `api/client.ts` adds. So the bytes are fetched through the client and land
 * in memory, and this is the last step: a temporary link to them, clicked.
 */

/** How long the object URL is kept after the click. */
const REVOKE_AFTER_MS = 1000

/** Save bytes fetched through the API client, such as a report's CSV. */
export function saveBlob (blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)

  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.style.display = 'none'

  // Attached for the click: some browsers ignore one on a detached element.
  document.body.append(link)
  link.click()
  link.remove()

  // Not at once. The download reads the URL after the click returns, and a
  // URL revoked under it saves an empty file in some browsers. Never is
  // wrong too: the bytes would stay in memory until the tab closed.
  setTimeout(() => URL.revokeObjectURL(url), REVOKE_AFTER_MS)
}

/**
 * Save text the API returned in a JSON body, such as the customer import
 * template.
 */
export function saveTextFile (
  filename: string,
  text: string,
  type = 'text/csv;charset=utf-8',
): void {
  saveBlob(new Blob([text], { type }), filename)
}
