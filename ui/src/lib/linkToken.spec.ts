import { describe, expect, it } from 'vitest'
import { tokenFromHash, tokenHash } from './linkToken'

describe('tokenFromHash', () => {
  it('reads the token after #token=', () => {
    expect(tokenFromHash('#token=abc_DEF-123')).toBe('abc_DEF-123')
  })

  it('keeps the colons of a signed pay token', () => {
    expect(tokenFromHash('#token=ImFiYyI:1xTixv:818Sbpq')).toBe('ImFiYyI:1xTixv:818Sbpq')
  })

  it('reads it among other keys, and without the leading #', () => {
    expect(tokenFromHash('#a=1&token=tok&b=2')).toBe('tok')
    expect(tokenFromHash('token=tok')).toBe('tok')
  })

  it.each(['', '#', '#token=', '#token=%20', '#tok', '#other=tok'])(
    'is empty for a missing or garbled fragment: %s',
    hash => {
      expect(tokenFromHash(hash)).toBe('')
    },
  )
})

describe('tokenHash', () => {
  it('round-trips through tokenFromHash', () => {
    expect(tokenFromHash(tokenHash('ImFiYyI:1xTixv:818Sbpq'))).toBe('ImFiYyI:1xTixv:818Sbpq')
  })
})
