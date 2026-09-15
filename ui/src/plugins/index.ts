/**
 * plugins/index.ts
 *
 * Automatically included in `./src/main.ts`
 */

// Types
import type { App } from 'vue'
import { createPinia } from 'pinia'
import router from '../router'
// Plugins
import vuetify from './vuetify'

export function registerPlugins (app: App) {
  app.use(vuetify)
  // Pinia first: the router's navigation guard reads the session store, and
  // `main.ts` connects that store to the API client immediately after this.
  app.use(createPinia())
  app.use(router)
}
