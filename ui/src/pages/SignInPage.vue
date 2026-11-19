<script setup lang="ts">
  /**
   * Opening a customer's sign-in link (ADR-008, ADR-039).
   *
   * Public: the token in the address's fragment is the credential (ADR-036),
   * and an address without one is refused before asking. The token is spent
   * by a press of the button, not on arrival: a mail scanner that opens the
   * link and runs the page must not use up a link that works once. The token
   * goes to the API in the body, and a success lands the customer on `/`,
   * which sends each role to its own screen.
   *
   * A refusal is the server's sentence, the same one for a spent, expired,
   * garbled or no-longer-a-customer link, with a way to ask for another.
   */
  import { ref } from 'vue'
  import { useRoute, useRouter } from 'vue-router'
  import { INCOMPLETE_LINK, tokenFromHash } from '@/lib/linkToken'
  import { failureMessage } from '@/lib/team'
  import { useSessionStore } from '@/stores/session'

  const route = useRoute()
  const router = useRouter()
  const session = useSessionStore()

  const token = tokenFromHash(route.hash)

  const busy = ref(false)
  /** Why the link did not sign anyone in, in the server's words. */
  const problem = ref(token ? '' : INCOMPLETE_LINK)

  async function signIn () {
    busy.value = true
    problem.value = ''

    try {
      await session.signInWithLink(token)
      // Replaced, not pushed: Back should not return to a spent link.
      await router.replace({ name: 'home' })
    } catch (error_) {
      problem.value = failureMessage(error_, 'Could not sign you in with that link.')
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

        <v-card-subtitle class="text-wrap">
          {{ problem ? 'That link did not work' : 'Your sign-in link is ready' }}
        </v-card-subtitle>
      </v-card-item>

      <v-card-text>
        <template v-if="problem">
          <v-alert class="mb-4" density="compact" type="error" variant="tonal">
            {{ problem }}
          </v-alert>

          <v-btn
            block
            color="primary"
            size="large"
            @click="router.push({ name: 'sign-in-link' })"
          >
            Email me a new link
          </v-btn>
        </template>

        <template v-else>
          <p class="text-body-2 text-medium-emphasis mb-4">
            The link works once. Continue to sign in on this device.
          </p>

          <v-btn
            block
            color="primary"
            :loading="busy"
            size="large"
            @click="signIn"
          >
            Sign in
          </v-btn>
        </template>
      </v-card-text>
    </v-card>
  </v-container>
</template>
