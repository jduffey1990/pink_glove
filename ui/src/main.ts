/**
 * Boots the app.
 *
 * The store and the API client are joined here, once: the client cannot import
 * the store (the store imports the client), so `connectSessionToClient` hands
 * it the two things it needs -- which organization to name in a header, and
 * what to do when a session turns out to be gone.
 */

import { createApp } from 'vue'
import { registerPlugins } from '@/plugins'
import { connectSessionToClient, useSessionStore } from '@/stores/session'
import App from './App.vue'

import 'unfonts.css'

const app = createApp(App)

registerPlugins(app)

connectSessionToClient(useSessionStore())

app.mount('#app')
