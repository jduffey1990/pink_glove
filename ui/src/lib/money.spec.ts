import { describe, expect, it } from 'vitest'
import {
  centsToDollars,
  dollarsToCents,
  dollarsToRateString,
  formatCents,
  formatPercent,
  noAccessFeeToApi,
  noAccessFeeToForm,
} from './money'

describe('dollarsToCents', () => {
  it('converts whole and fractional dollars', () => {
    expect(dollarsToCents(150)).toBe(15_000)
    expect(dollarsToCents(1.5)).toBe(150)
    expect(dollarsToCents(0.05)).toBe(5)
  })

  it('survives the float that started all this', () => {
    // 0.07 * 100 is 7.000000000000001, which the API refuses as a rate and
    // which rounds wrong as an amount. This is the Phase 3b bug.
    expect(dollarsToCents(0.07)).toBe(7)
    expect(dollarsToCents(1.1)).toBe(110)
    expect(dollarsToCents(2.675)).toBe(268)
  })

  it('takes the string a text input actually gives it', () => {
    expect(dollarsToCents('150')).toBe(15_000)
    expect(dollarsToCents(' 12.34 ')).toBe(1234)
  })

  it('rounds away from zero, so a discount rounds like a charge', () => {
    expect(dollarsToCents(0.005)).toBe(1)
    expect(dollarsToCents(-0.005)).toBe(-1)
    expect(dollarsToCents(-25)).toBe(-2500)
  })

  it('treats an empty field as nothing, not as a typo', () => {
    expect(dollarsToCents('')).toBe(0)
    expect(dollarsToCents(null)).toBe(0)
    expect(dollarsToCents(undefined)).toBe(0)
  })

  it('reports a typo as NaN rather than silently as zero', () => {
    expect(dollarsToCents('lots')).toBeNaN()
  })
})

describe('centsToDollars', () => {
  it('fills a form field back in', () => {
    expect(centsToDollars(15_000)).toBe(150)
    expect(centsToDollars(null)).toBe(0)
  })

  it('round-trips through dollarsToCents', () => {
    for (const cents of [0, 1, 7, 150, 12_345, 99_999_999]) {
      expect(dollarsToCents(centsToDollars(cents))).toBe(cents)
    }
  })
})

describe('dollarsToRateString', () => {
  it('keeps the fractions of a cent a rate is allowed', () => {
    expect(dollarsToRateString(0.125, 3)).toBe('12.500')
    expect(dollarsToRateString(0.07, 3)).toBe('7.000')
    expect(dollarsToRateString(85, 2)).toBe('8500.00')
  })

  it('handles an empty field', () => {
    expect(dollarsToRateString('', 3)).toBe('0.000')
    expect(dollarsToRateString(null, 2)).toBe('0.00')
  })
})

describe('formatCents', () => {
  it('reads as money', () => {
    expect(formatCents(15_000)).toBe('$150.00')
    expect(formatCents(0)).toBe('$0.00')
    expect(formatCents(5)).toBe('$0.05')
    expect(formatCents(123_456_789)).toBe('$1,234,567.89')
  })

  it('shows a dash rather than $0.00 for nothing at all', () => {
    expect(formatCents(null)).toBe('—')
    expect(formatCents(undefined)).toBe('—')
  })

  it('marks a credit as negative', () => {
    expect(formatCents(-2500)).toBe('-$25.00')
  })
})

describe('formatPercent', () => {
  it('reads the decimal string the API sends', () => {
    expect(formatPercent('8.250')).toBe('8.25%')
    expect(formatPercent('0.000')).toBe('0%')
    expect(formatPercent(10)).toBe('10%')
  })

  it('shows a dash for nothing', () => {
    expect(formatPercent(null)).toBe('—')
    expect(formatPercent('')).toBe('—')
  })
})

describe('the no-access fee, which means two different things', () => {
  it('shows a flat fee in dollars and a percentage as itself', () => {
    expect(noAccessFeeToForm('flat', '2500.000')).toBe(25)
    expect(noAccessFeeToForm('percent', '50.000')).toBe(50)
  })

  it('sends a flat fee as cents and a percentage as a percentage', () => {
    expect(noAccessFeeToApi('flat', 25)).toBe('2500.000')
    expect(noAccessFeeToApi('percent', 50)).toBe('50.000')
  })

  it('does not convert a percentage as though it were dollars', () => {
    // The bug this function exists to prevent: 50 sent as 5000 is a 5000%
    // no-access fee.
    expect(noAccessFeeToApi('percent', 50)).not.toBe('5000.000')
  })

  it('does not send a flat fee as dollars', () => {
    // And the other way: $25 sent as 25 is a 25-cent fee.
    expect(noAccessFeeToApi('flat', 25)).not.toBe('25.000')
  })

  it('round-trips both types', () => {
    expect(noAccessFeeToApi('flat', noAccessFeeToForm('flat', '2500.000'))).toBe('2500.000')
    expect(noAccessFeeToApi('percent', noAccessFeeToForm('percent', '33.333'))).toBe('33.333')
  })

  it('keeps the fractions of a percent the API stores', () => {
    expect(noAccessFeeToApi('percent', 33.333)).toBe('33.333')
  })

  it('treats an empty field as nothing', () => {
    expect(noAccessFeeToForm('flat', null)).toBe(0)
    expect(noAccessFeeToApi('flat', '')).toBe('0.000')
    expect(noAccessFeeToApi('percent', '')).toBe('0.000')
  })

  it('reports a typo rather than sending a silent zero', () => {
    expect(noAccessFeeToApi('flat', 'lots')).toBe('NaN')
    expect(noAccessFeeToApi('percent', 'lots')).toBe('NaN')
  })
})
