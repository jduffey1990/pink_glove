<script setup lang="ts">
  /**
   * Following an invitation link.
   *
   * Public: the token in the address is the credential. The page first asks
   * the server what the link opens, which also says whether this address has
   * ever had a password. If not, accepting sets the account up (a name and a
   * password); if so, accepting only joins, and the password is not touched.
   *
   * Accepting signs nobody in. The server answers with a sentence, and the
   * person then signs in the ordinary way and meets the ordinary code
   * (ADR-008, ADR-028). Every message shown here is the server's own.
   */
  import type { Invitation } from '@/api/types'
  import { ref } from 'vue'
  import { useRoute, useRouter } from 'vue-router'
  import { acceptInvitation, inspectInvitation } from '@/api/endpoints'
  import { fieldErrors } from '@/api/errors'
  import { failureMessage, roleLabel } from '@/lib/team'
  import { useSessionStore } from '@/stores/session'

  const route = useRoute()
  const router = useRouter()
  const session = useSessionStore()

  const token = String(route.params.token ?? '')

  const invitation = ref<Invitation | null>(null)
  const loading = ref(true)
  /** Why the link could not be opened, in the server's words. */
  const problem = ref('')

  const firstName = ref('')
  const lastName = ref('')
  const password = ref('')
  const showPassword = ref(false)

  const busy = ref(false)
  const error = ref('')
  const firstNameErrors = ref<string[]>([])
  const passwordErrors = ref<string[]>([])
  /** The server's sentence once the invitation is accepted. */
  const done = ref('')

  async function open () {
    try {
      invitation.value = await inspectInvitation(token)
      firstName.value = invitation.value.first_name
      lastName.value = invitation.value.last_name
    } catch (error_) {
      problem.value = failureMessage(error_, 'Could not open that invitation.')
    } finally {
      loading.value = false
    }
  }

  open()

  async function submit () {
    if (invitation.value === null) {
      return
    }
    busy.value = true
    error.value = ''
    firstNameErrors.value = []
    passwordErrors.value = []

    try {
      done.value = await acceptInvitation(
        invitation.value.must_set_password
          ? {
            token,
            password: password.value,
            first_name: firstName.value.trim(),
            last_name: lastName.value.trim(),
          }
          : { token },
      )
      // Someone already signed in on this browser has just gained an
      // organization; let the shell find out. Nobody else is signed in here.
      if (session.isAuthenticated) {
        await session.refresh().catch(() => {})
      }
    } catch (error_) {
      firstNameErrors.value = fieldErrors(error_).first_name ?? []
      passwordErrors.value = fieldErrors(error_).password ?? []
      if (firstNameErrors.value.length === 0 && passwordErrors.value.length === 0) {
        error.value = failureMessage(error_, 'Could not accept that invitation.')
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
        <v-card-title class="text-h5">
          <span class="text-primary">pink</span> glove
        </v-card-title>

        <v-card-subtitle>Accept your invitation</v-card-subtitle>
      </v-card-item>

      <v-card-text>
        <v-progress-linear v-if="loading" indeterminate />

        <template v-else-if="problem">
          <v-alert class="mb-4" density="compact" type="error" variant="tonal">
            {{ problem }}
          </v-alert>

          <v-btn block variant="text" @click="router.push({ name: 'login' })">
            Back to sign in
          </v-btn>
        </template>

        <template v-else-if="done">
          <v-alert class="mb-4" density="compact" type="success" variant="tonal">
            {{ done }}
          </v-alert>

          <v-btn
            block
            color="primary"
            size="large"
            @click="router.push(session.isAuthenticated ? { name: 'home' } : { name: 'login' })"
          >
            {{ session.isAuthenticated ? 'Continue' : 'Sign in' }}
          </v-btn>
        </template>

        <template v-else-if="invitation">
          <p class="text-body-1 mb-1">
            You have been invited to join
            <strong>{{ invitation.organization_name }}</strong>.
          </p>

          <p class="text-body-2 text-medium-emphasis mb-4 text-break">
            {{ invitation.email }} · {{ roleLabel(invitation.role) }}
          </p>

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
            <template v-if="invitation.must_set_password">
              <v-text-field
                v-model="firstName"
                autocomplete="given-name"
                autofocus
                class="mb-2"
                :error-messages="firstNameErrors"
                label="First name"
              />

              <v-text-field
                v-model="lastName"
                autocomplete="family-name"
                class="mb-2"
                label="Last name"
              />

              <v-text-field
                v-model="password"
                :append-inner-icon="showPassword ? 'mdi-eye-off' : 'mdi-eye'"
                autocomplete="new-password"
                :error-messages="passwordErrors"
                label="Choose a password"
                :max-errors="6"
                prepend-inner-icon="mdi-lock-outline"
                :type="showPassword ? 'text' : 'password'"
                @click:append-inner="showPassword = !showPassword"
              />
            </template>

            <p v-else class="text-body-2 mb-2">
              You already have an account. Accepting adds this organization to
              it; your password stays as it is.
            </p>

            <v-btn
              block
              class="mt-4"
              color="primary"
              :disabled="invitation.must_set_password && (!firstName.trim() || !password)"
              :loading="busy"
              size="large"
              type="submit"
            >
              Accept invitation
            </v-btn>
          </v-form>
        </template>
      </v-card-text>
    </v-card>
  </v-container>
</template>
