/**
 * Hand the browser some text to save as a file.
 *
 * For text the API returned in a JSON body, such as the customer import
 * template. A plain link to the API would not do: the request needs the
 * session cookie and, for someone in two organizations, the X-Organization
 * header that only `api/client.ts` adds.
 */
export function saveTextFile (
  filename: string,
  text: string,
  type = 'text/csv;charset=utf-8',
): void {
  const url = URL.createObjectURL(new Blob([text], { type }))

  const link = document.createElement('a')
  link.href = url
  link.download = filename
  // Attached before the click: Firefox ignores a click on a detached link.
  document.body.append(link)
  link.click()
  link.remove()

  URL.revokeObjectURL(url)
}
