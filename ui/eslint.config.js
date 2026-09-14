import vuetify from 'eslint-config-vuetify'

export default vuetify(
  { ts: true },
  {
    // Generated from the backend by `npm run api:types` (ADR-019). It is
    // committed so an API change is a reviewable diff, but it is never
    // hand-edited, so linting it would only ever produce noise nobody may fix.
    ignores: ['openapi.yaml', 'src/api/schema.d.ts'],
  },
)
