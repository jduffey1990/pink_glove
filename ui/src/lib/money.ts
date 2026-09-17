/**
 * Money, in one place.
 *
 * Amounts are integer cents everywhere (CLAUDE.md invariant 5) and rates are
 * decimals to three places (ADR-009). Forms take dollars, because nobody types
 * cents. The conversion between the two is exactly the kind of one-liner that
 * gets written inline on five pages and goes wrong on one of them: `0.07 * 100`
 * is `7.000000000000001`, which the API refused and which cost an afternoon in
 * Phase 3b.
 *
 * So it lives here, with a spec, and the pages call it.
 */

/**
 * A dollar amount from a form input, as whole cents.
 *
 * Rounded once, away from zero, so a negative adjustment rounds the same way a
 * positive charge does. Returns `NaN` for something that is not a number at
 * all -- the caller decides whether that is an empty field or a typo, which is
 * a question about the form rather than about money.
 */
export function dollarsToCents (value: string | number | null | undefined): number {
  if (value === null || value === undefined) {
    return 0
  }

  const text = String(value).trim()
  if (text === '') {
    return 0
  }

  const amount = Number(text)
  if (!Number.isFinite(amount)) {
    return Number.NaN
  }

  // toFixed first: it absorbs the binary representation error before rounding,
  // which is the whole point of this function existing.
  const scaled = Number((amount * 100).toFixed(4))
  return Math.sign(scaled) * Math.round(Math.abs(scaled))
}

/** Cents back into the dollars a form field shows. */
export function centsToDollars (cents: number | null | undefined): number {
  return (cents ?? 0) / 100
}

/**
 * A dollar rate as the decimal string the API stores.
 *
 * Rates are not amounts: $0.125 per square foot is a real price, and rounding
 * the rate rather than the total loses money over a large house. `places` is
 * how many decimal places of a *cent* the field keeps -- 2 for an hourly rate,
 * 3 for a per-square-foot one.
 */
export function dollarsToRateString (
  value: string | number | null | undefined,
  places: number,
): string {
  const amount = typeof value === 'number' ? value : Number(String(value ?? 0).trim() || 0)
  return (Number.isFinite(amount) ? amount * 100 : 0).toFixed(places)
}

/** Integer cents as money: `15000` -> `"$150.00"`. */
export function formatCents (cents: number | null | undefined): string {
  if (cents === null || cents === undefined) {
    return '—'
  }
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' })
    .format(cents / 100)
}

/** A percentage rate as the API sends it (a decimal string): `"8.250"` -> `"8.25%"`. */
export function formatPercent (rate: string | number | null | undefined): string {
  if (rate === null || rate === undefined) {
    return '—'
  }
  const value = Number(String(rate).trim() || Number.NaN)
  return Number.isFinite(value) ? `${Number(value.toFixed(3))}%` : '—'
}
