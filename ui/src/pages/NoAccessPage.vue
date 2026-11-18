<script setup lang="ts">
  /**
   * Signed in, and active in no organization.
   *
   * Where someone lands after their membership is switched off: the session
   * is still theirs (they may belong somewhere else one day), but the server
   * resolves no organization for it and refuses everything. Only the router
   * sends anyone here, on `session.hasNoOrganization`.
   */
  import { useRouter } from 'vue-router'
  import { useSessionStore } from '@/stores/session'

  const session = useSessionStore()
  const router = useRouter()

  async function signOut () {
    await session.logout().catch(() => {})
    router.push({ name: 'login' })
  }
</script>

<template>
  <v-container class="fill-height text-center" max-width="480">
    <div class="w-100">
      <v-icon class="mb-4" icon="mdi-account-off-outline" size="64" />
      <h1 class="text-h6 mb-2">No active organization</h1>

      <p class="text-medium-emphasis mb-2">
        You are signed in as {{ session.user?.email }}, but this account is not
        active in any organization.
      </p>

      <p class="text-medium-emphasis mb-6">
        If you expected to have access, ask an owner or admin of your
        organization to reactivate you.
      </p>

      <v-btn color="primary" prepend-icon="mdi-logout" @click="signOut">Sign out</v-btn>
    </div>
  </v-container>
</template>
