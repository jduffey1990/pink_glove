<script setup lang="ts">
  /**
   * Step 2: the two-factor code.
   *
   * The pending challenge lives in the server's session, not in a query
   * parameter, so there is nothing to carry here beyond the code itself -- a
   * client cannot verify a challenge it was not issued.
   */
  import { ref } from 'vue'
  import { useRoute, useRouter } from 'vue-router'
  import { statusOf, useSessionStore } from '@/stores/session'

  const session = useSessionStore()
  const router = useRouter()
  const route = useRoute()

  const code = ref('')
  const rememberDevice = ref(false)
  const busy = ref(false)
  const resending = ref(false)
  const error = ref('')
  const notice = ref('')

  async function submit () {
    busy.value = true
    error.value = ''

    try {
      await session.verify(code.value.trim(), rememberDevice.value)
      router.push((route.query.next as string) || { name: 'home' })
    } catch (error_) {
      const status = statusOf(error_)
      error.value = status === 401
        ? 'That code is not correct, or it has expired.'
        : (status === 400
          ? 'No sign-in is in progress. Please start again.'
          : 'Could not verify that code.')
    } finally {
      busy.value = false
    }
  }

  async function resend () {
    resending.value = true
    error.value = ''
    notice.value = ''

    try {
      await session.resendCode()
      notice.value = 'A new code has been sent.'
    } catch {
      error.value = 'Could not send a new code.'
    } finally {
      resending.value = false
    }
  }
</script>

<template>
  <v-container class="fill-height" max-width="440">
    <v-card class="w-100 pa-2" flat>
      <v-card-item>
        <v-card-title class="text-h6">Enter your code</v-card-title>
        <v-card-subtitle>We sent a sign-in code to your email.</v-card-subtitle>
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

        <v-alert
          v-if="notice"
          class="mb-4"
          density="compact"
          type="success"
          variant="tonal"
        >
          {{ notice }}
        </v-alert>

        <v-form @submit.prevent="submit">
          <v-text-field
            v-model="code"
            autocomplete="one-time-code"
            autofocus
            inputmode="numeric"
            label="Six-digit code"
            prepend-inner-icon="mdi-key-outline"
          />

          <v-checkbox
            v-model="rememberDevice"
            density="compact"
            hide-details
            label="Trust this device"
          />

          <v-btn
            block
            class="mt-4"
            color="primary"
            :disabled="!code"
            :loading="busy"
            size="large"
            type="submit"
          >
            Verify
          </v-btn>

          <v-btn
            block
            class="mt-2"
            :loading="resending"
            variant="text"
            @click="resend"
          >
            Send a new code
          </v-btn>

          <v-btn block variant="text" @click="router.push({ name: 'login' })">
            Back to sign in
          </v-btn>
        </v-form>
      </v-card-text>
    </v-card>
  </v-container>
</template>
