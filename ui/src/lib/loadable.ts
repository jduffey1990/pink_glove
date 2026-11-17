/**
 * One independently loaded piece of a page: its data, whether it is loading,
 * and what went wrong.
 *
 * The reports page shows three of these side by side, and each must fail on
 * its own: a timesheet period the server refuses is no reason to hide who
 * owes money. Two rules are the reason this is here with a spec rather than
 * written three times in the page:
 *
 * - **A failure clears the data.** Figures left on screen under an error
 *   belong to the previous period, and nothing says so.
 * - **Only the newest load is shown** (`latestOnly`): an answer that arrives
 *   after a later request began is dropped, error or not.
 */

import type { Ref } from 'vue'
import { ref } from 'vue'
import { errorDetail } from '@/api/errors'
import { latestOnly } from './latest'

export interface Loadable<T> {
  data: Ref<T | null>
  loading: Ref<boolean>
  /** The server's own sentence when it sent one (ADR-023), else `fallback`. */
  error: Ref<string>
  load: (fetch: () => Promise<T>) => Promise<void>
  /** Forget what is shown, and any load still in the air. */
  clear: () => void
}

export function useLoadable<T> (fallback: string): Loadable<T> {
  const data = ref<T | null>(null) as Ref<T | null>
  const loading = ref(false)
  const error = ref('')
  const begin = latestOnly()

  async function load (fetch: () => Promise<T>): Promise<void> {
    const isCurrent = begin()
    loading.value = true
    error.value = ''

    try {
      const result = await fetch()
      if (isCurrent()) {
        data.value = result
      }
    } catch (error_) {
      if (isCurrent()) {
        data.value = null
        error.value = errorDetail(error_) ?? fallback
      }
    } finally {
      if (isCurrent()) {
        loading.value = false
      }
    }
  }

  function clear (): void {
    begin()
    data.value = null
    loading.value = false
    error.value = ''
  }

  return { data, loading, error, load, clear }
}
