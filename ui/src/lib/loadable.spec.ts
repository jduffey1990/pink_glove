import { AxiosError, AxiosHeaders } from 'axios'
import { describe, expect, it } from 'vitest'
import { useLoadable } from './loadable'

/** A promise and the handles to settle it, so a test decides the order. */
function deferred<T> () {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((resolve_, reject_) => {
    resolve = resolve_
    reject = reject_
  })
  return { promise, resolve, reject }
}

function apiError (status: number, data: unknown): AxiosError {
  const headers = new AxiosHeaders()
  return new AxiosError('Request failed', 'ERR_BAD_REQUEST', undefined, undefined, {
    status,
    statusText: '',
    headers,
    config: { headers },
    data,
  })
}

describe('useLoadable', () => {
  it('starts empty and idle', () => {
    const section = useLoadable<number>('Could not load.')

    expect(section.data.value).toBeNull()
    expect(section.loading.value).toBe(false)
    expect(section.error.value).toBe('')
  })

  it('is loading until the answer arrives, then holds it', async () => {
    const section = useLoadable<number>('Could not load.')
    const request = deferred<number>()

    const loading = section.load(() => request.promise)
    expect(section.loading.value).toBe(true)

    request.resolve(7200)
    await loading

    expect(section.loading.value).toBe(false)
    expect(section.data.value).toBe(7200)
  })

  it('shows the server\'s own sentence when it sent one', async () => {
    const section = useLoadable<number>('Could not load.')

    await section.load(() =>
      Promise.reject(apiError(400, { date_to: ['date_to cannot fall before date_from.'] })),
    )

    expect(section.error.value).toBe('date_to cannot fall before date_from.')
  })

  it('falls back when there is nothing to quote', async () => {
    const section = useLoadable<number>('Could not load the timesheet.')

    await section.load(() => Promise.reject(new Error('Network Error')))

    expect(section.error.value).toBe('Could not load the timesheet.')
    expect(section.loading.value).toBe(false)
  })

  it('a failure clears the figures it could not replace', async () => {
    const section = useLoadable<number>('Could not load.')
    await section.load(() => Promise.resolve(7200))

    await section.load(() => Promise.reject(new Error('boom')))

    // Last period's hours under this period's heading would be a wrong number.
    expect(section.data.value).toBeNull()
  })

  it('a new load clears the previous error', async () => {
    const section = useLoadable<number>('Could not load.')
    await section.load(() => Promise.reject(new Error('boom')))

    await section.load(() => Promise.resolve(1))

    expect(section.error.value).toBe('')
    expect(section.data.value).toBe(1)
  })

  it('drops a slow answer that lands after a later one', async () => {
    const section = useLoadable<string>('Could not load.')
    const lastWeek = deferred<string>()
    const thisWeek = deferred<string>()

    const first = section.load(() => lastWeek.promise)
    const second = section.load(() => thisWeek.promise)

    thisWeek.resolve('this week')
    await second
    lastWeek.resolve('last week')
    await first

    expect(section.data.value).toBe('this week')
    expect(section.loading.value).toBe(false)
  })

  it('drops a stale failure too, and keeps loading until the newest settles', async () => {
    const section = useLoadable<string>('Could not load.')
    const stale = deferred<string>()
    const newest = deferred<string>()

    const first = section.load(() => stale.promise)
    const second = section.load(() => newest.promise)

    stale.reject(new Error('boom'))
    await first

    expect(section.error.value).toBe('')
    expect(section.loading.value).toBe(true)

    newest.resolve('this week')
    await second

    expect(section.data.value).toBe('this week')
    expect(section.loading.value).toBe(false)
  })

  it('clear forgets what is shown and whatever is still in the air', async () => {
    const section = useLoadable<string>('Could not load.')
    await section.load(() => Promise.resolve('shown'))
    const inFlight = deferred<string>()
    const pending = section.load(() => inFlight.promise)

    section.clear()
    inFlight.resolve('too late')
    await pending

    expect(section.data.value).toBeNull()
    expect(section.loading.value).toBe(false)
    expect(section.error.value).toBe('')
  })
})
