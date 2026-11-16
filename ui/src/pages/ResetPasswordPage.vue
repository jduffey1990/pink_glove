<script setup lang="ts">
  /**
   * Choose a new password with a reset link.
   *
   * Public: the token in the address is the credential. A success signs
   * nobody in. It ends every session the account had, forgets every trusted
   * device, and the person then signs in with the new password and a code
   * like any other sign-in (ADR-008, ADR-028).
   *
   * The validators' answers are shown as the server sent them, all of them
   * at once.
   */
  import { ref } from 'vue'
  import { useRoute, useRouter } from 'vue-router'
  import { confirmPasswordReset } from '@/api/endpoints'
  import { fieldErrors } from '@/api/errors'
  import { failureMessage } from '@/lib/team'
  import { useSessionStore } from '@/stores/session'

  const route = useRoute()
  const router = useRouter()
  const session = useSessionStore()

  const token = String(route.params.token ?? '')

  const password = ref('')
  const showPassword = ref(false)
  const busy = ref(false)
  const error = ref('')
  const passwordErrors = ref<string[]>([])
  /** The server's sentence once the password is changed. */
  const done = ref('')

  async function submit () {
    busy.value = true
    error.value = ''
    passwordErrors.value = []

    try {
      done.value = await confirmPasswordReset(token, password.value)
      // If this browser was signed in to that account, the server has just
      // ended the session. Ask, so the shell does not go on believing in it
      // and bounce the person off the login page.
      if (session.isAuthenticated) {
        await session.refresh().catch(() => {})
      }
    } catch (error_) {
      passwordErrors.value = fieldErrors(error_).password ?? []
      if (passwordErrors.value.length === 0) {
        error.value = failureMessage(error_, 'Could not change that password.')
      }
    } finally {
      busy.value = false
    }
  }
</script>

<template>
  <v-container class="fill-height" max-width="440">
    <v-card class="w-100 pa-2" flat>
      <v-card-item>
        <v-card-title class="text-h6">Choose a new password</v-card-title>
      </v-card-item>

      <v-card-text>
        <template v-if="done">
          <v-alert class="mb-4" density="compact" type="success" variant="tonal">
            {{ done }}
          </v-alert>

          <v-btn block color="primary" size="large" @click="router.push({ name: 'login' })">
            Sign in
          </v-btn>
        </template>

        <template v-else>
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
              v-model="password"
              :append-inner-icon="showPassword ? 'mdi-eye-off' : 'mdi-eye'"
              autocomplete="new-password"
              autofocus
              :error-messages="passwordErrors"
              label="New password"
              :max-errors="6"
              prepend-inner-icon="mdi-lock-outline"
              :type="showPassword ? 'text' : 'password'"
              @click:append-inner="showPassword = !showPassword"
            />

            <v-btn
              block
              class="mt-4"
              color="primary"
              :disabled="!password"
              :loading="busy"
              size="large"
              type="submit"
            >
              Change password
            </v-btn>
          </v-form>

          <v-btn
            block
            class="mt-2"
            variant="text"
            @click="router.push({ name: 'forgot-password' })"
          >
            Ask for a new link
          </v-btn>
        </template>
      </v-card-text>
    </v-card>
  </v-container>
</template>
