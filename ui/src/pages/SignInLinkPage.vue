<script setup lang="ts">
  /**
   * A customer asks for a sign-in link (ADR-008, ADR-039).
   *
   * Customers have no password; this is their whole way in. The server gives
   * the same answer for every address, staff and strangers included, so this
   * page cannot be used to find out who has an account or who is staff. The
   * sentence shown afterwards is the server's, word for word, for the same
   * reason (see ForgotPasswordPage).
   */
  import { ref } from 'vue'
  import { useRouter } from 'vue-router'
  import { requestSignInLink } from '@/api/endpoints'
  import { fieldErrors } from '@/api/errors'
  import { failureMessage } from '@/lib/team'

  const router = useRouter()

  const email = ref('')
  const busy = ref(false)
  const error = ref('')
  const emailErrors = ref<string[]>([])
  /** The server's sentence once the request is in. */
  const sent = ref('')
  /** Present only when the API runs with LOCAL = True. See VerifyPage. */
  const devLink = ref<string | null>(null)

  async function submit () {
    busy.value = true
    error.value = ''
    emailErrors.value = []

    try {
      const body = await requestSignInLink(email.value.trim())
      sent.value = body.detail
      devLink.value = body.dev_link ?? null
    } catch (error_) {
      emailErrors.value = fieldErrors(error_).email ?? []
      if (emailErrors.value.length === 0) {
        error.value = failureMessage(error_, 'Could not ask for a sign-in link.')
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
        <v-card-title class="text-h6">Email me a sign-in link</v-card-title>

        <v-card-subtitle class="text-wrap">
          For customers. Enter the email your cleaning company has on file.
        </v-card-subtitle>
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

        <template v-if="sent">
          <v-alert class="mb-4" density="compact" type="success" variant="tonal">
            {{ sent }}
          </v-alert>

          <!-- Local development only: the API returns `dev_link` just when it
               runs with LOCAL = True, so nothing renders in a deployed
               environment. -->
          <v-alert
            v-if="devLink"
            class="mb-4"
            density="compact"
            icon="mdi-wrench-outline"
            type="info"
            variant="tonal"
          >
            Dev link:
            <a class="text-break" :href="devLink">{{ devLink }}</a>
          </v-alert>
        </template>

        <v-form v-else @submit.prevent="submit">
          <v-text-field
            v-model="email"
            autocomplete="email"
            autofocus
            :error-messages="emailErrors"
            label="Email"
            prepend-inner-icon="mdi-email-outline"
            type="email"
          />

          <v-btn
            block
            class="mt-4"
            color="primary"
            :disabled="!email"
            :loading="busy"
            size="large"
            type="submit"
          >
            Send sign-in link
          </v-btn>
        </v-form>

        <v-btn block class="mt-2" variant="text" @click="router.push({ name: 'login' })">
          Back to sign in
        </v-btn>
      </v-card-text>
    </v-card>
  </v-container>
</template>
