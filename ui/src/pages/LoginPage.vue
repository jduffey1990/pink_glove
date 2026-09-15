<script setup lang="ts">
  /**
   * Step 1 of the backend's sign-in sequence.
   *
   * A 202 means a code was sent and the next screen collects it; a 200 means
   * the session is already established (a trusted device, or a role that is
   * not challenged). The store decides which; this page only routes on it.
   */
  import { ref } from 'vue'
  import { useRoute, useRouter } from 'vue-router'
  import { API_BASE_URL } from '@/api/client'
  import { statusOf, useSessionStore } from '@/stores/session'

  const session = useSessionStore()
  const router = useRouter()
  const route = useRoute()

  const email = ref('')
  const password = ref('')
  const busy = ref(false)
  const error = ref('')
  const showPassword = ref(false)

  /**
   * Why a sign-in failed, in terms the person can act on.
   *
   * The 403 case is the one worth spelling out. DRF only enforces CSRF on
   * requests it authenticates, so posting credentials while an OLD session
   * cookie is still in the browser gets authenticated, then fails the CSRF
   * check against a token belonging to a different session. "Please try
   * again" is exactly wrong there: retrying fails identically forever.
   */
  function messageFor (status: number | null): string {
    switch (status) {
      case 401: {
        // The backend's own wording, identical for "no such user" and "wrong
        // password" so this cannot be used to enumerate accounts.
        return 'Email and password do not match an active account.'
      }
      case 403: {
        return 'Your browser is holding an old session. Clear this site\'s data '
          + '(or use a private window) and sign in again.'
      }
      case 429: {
        return 'Too many attempts. Wait a few minutes and try again.'
      }
      case null: {
        // No response at all: the request never reached the API, or CORS
        // refused it. Commonest cause is a dev server on an unexpected port.
        return `Could not reach the API at ${API_BASE_URL}. Check it is running, `
          + 'and that this page is served from an origin the API allows.'
      }
      default: {
        return 'Could not sign in. Please try again.'
      }
    }
  }

  async function submit () {
    busy.value = true
    error.value = ''

    try {
      const outcome = await session.login(email.value.trim(), password.value)

      if (outcome === 'code-required') {
        router.push({ name: 'verify', query: route.query })
        return
      }

      router.push((route.query.next as string) || { name: 'home' })
    } catch (error_) {
      error.value = messageFor(statusOf(error_))
    } finally {
      busy.value = false
    }
  }
</script>

<template>
  <v-container class="fill-height" max-width="440">
    <v-card class="w-100 pa-2" flat>
      <v-card-item>
        <v-card-title class="text-h5">
          <span class="text-primary">pink</span> glove
        </v-card-title>

        <v-card-subtitle>Sign in to your organization</v-card-subtitle>
      </v-card-item>

      <v-card-text>
        <v-alert
          v-if="error"
          class="mb-4"
          density="compact"
          type="error"
          variant="tonal"
        >
          {{ error }}
        </v-alert>

        <v-form @submit.prevent="submit">
          <v-text-field
            v-model="email"
            autocomplete="email"
            autofocus
            class="mb-2"
            label="Email"
            prepend-inner-icon="mdi-email-outline"
            type="email"
          />

          <v-text-field
            v-model="password"
            :append-inner-icon="showPassword ? 'mdi-eye-off' : 'mdi-eye'"
            autocomplete="current-password"
            label="Password"
            prepend-inner-icon="mdi-lock-outline"
            :type="showPassword ? 'text' : 'password'"
            @click:append-inner="showPassword = !showPassword"
          />

          <v-btn
            block
            class="mt-4"
            color="primary"
            :disabled="!email || !password"
            :loading="busy"
            size="large"
            type="submit"
          >
            Sign in
          </v-btn>
        </v-form>
      </v-card-text>
    </v-card>
  </v-container>
</template>
