<script setup lang="ts">
  /**
   * Which organization am I acting in?
   *
   * Only reached when someone holds more than one membership. The choice is
   * held in the store and sent as `X-Organization` on every request; the
   * backend validates it against the user's own memberships, so this is a
   * convenience rather than a grant.
   */
  import { useRouter } from 'vue-router'
  import { useSessionStore } from '@/stores/session'

  const session = useSessionStore()
  const router = useRouter()

  async function choose (organizationId: string) {
    session.chooseOrganization(organizationId)
    // Re-fetch so current_role reflects the new organization: the same person
    // can be a dispatcher in one and a cleaner in another (ADR-003).
    await session.refresh().catch(() => {})
    router.push({ name: 'home' })
  }
</script>

<template>
  <v-container class="fill-height" max-width="520">
    <v-card class="w-100 pa-2" flat>
      <v-card-item>
        <v-card-title class="text-h6">Choose an organization</v-card-title>
        <v-card-subtitle>You belong to more than one.</v-card-subtitle>
      </v-card-item>

      <v-list>
        <v-list-item
          v-for="membership in session.memberships"
          :key="membership.id"
          border
          class="mb-2"
          rounded
          :subtitle="membership.role"
          :title="membership.organization.name"
          @click="choose(membership.organization.id)"
        >
          <template #prepend>
            <v-avatar color="primary" size="36" variant="tonal">
              {{ membership.organization.name.charAt(0) }}
            </v-avatar>
          </template>

          <template #append>
            <v-icon icon="mdi-chevron-right" />
          </template>
        </v-list-item>
      </v-list>
    </v-card>
  </v-container>
</template>
