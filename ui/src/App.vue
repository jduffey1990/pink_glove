<script setup lang="ts">
  /**
   * The shell: navigation, the organization the caller is acting in, and sign
   * out. The auth screens render without it, because there is nothing to
   * navigate to yet.
   */
  import { computed, ref, watchEffect } from 'vue'
  import { useRoute, useRouter } from 'vue-router'
  import { useDisplay } from 'vuetify'
  import { useSessionStore } from '@/stores/session'

  const session = useSessionStore()
  const router = useRouter()
  const route = useRoute()

  const { mdAndUp } = useDisplay()
  const drawer = ref(false)

  // Open on desktop, closed on a phone. The cleaner's screen is a phone
  // screen; the dispatcher's is not.
  watchEffect(() => {
    drawer.value = mdAndUp.value
  })

  const bare = computed(() => route.meta.access === 'public' || !session.isAuthenticated)

  const navigation = computed(() => {
    const items: { title: string, icon: string, to: string }[] = []

    if (session.isDispatcherOrHigher) {
      items.push(
        { title: 'Schedule', icon: 'mdi-calendar-week', to: '/schedule' },
        { title: 'My day', icon: 'mdi-clipboard-check-outline', to: '/my-day' },
        { title: 'Customers', icon: 'mdi-account-group-outline', to: '/customers' },
        { title: 'Recurring plans', icon: 'mdi-repeat', to: '/plans' },
        { title: 'Billing', icon: 'mdi-receipt-text-outline', to: '/billing' },
        { title: 'Services', icon: 'mdi-tag-outline', to: '/services' },
      )
    } else if (session.isCleaner) {
      items.push(
        { title: 'My day', icon: 'mdi-clipboard-check-outline', to: '/my-day' },
        { title: 'Services', icon: 'mdi-tag-outline', to: '/services' },
      )
    }

    return items
  })

  async function signOut () {
    await session.logout().catch(() => {})
    router.push({ name: 'login' })
  }
</script>

<template>
  <v-app>
    <template v-if="!bare">
      <v-app-bar border="b" color="surface" flat>
        <v-app-bar-nav-icon class="d-md-none" @click="drawer = !drawer" />

        <v-app-bar-title class="font-weight-medium">
          <span class="text-primary">pink</span> glove
        </v-app-bar-title>

        <v-spacer />

        <v-chip
          v-if="session.organization"
          class="mr-2 d-none d-sm-flex"
          size="small"
          variant="tonal"
        >
          {{ session.organization.name }}
        </v-chip>

        <v-menu>
          <template #activator="{ props }">
            <v-btn icon="mdi-account-circle-outline" v-bind="props" />
          </template>

          <v-list density="compact">
            <v-list-item :subtitle="session.role ?? ''" :title="session.user?.full_name ?? ''" />
            <v-divider />

            <v-list-item
              v-if="session.memberships.length > 1"
              prepend-icon="mdi-swap-horizontal"
              title="Switch organization"
              @click="router.push({ name: 'choose-organization' })"
            />

            <v-list-item prepend-icon="mdi-logout" title="Sign out" @click="signOut" />
          </v-list>
        </v-menu>
      </v-app-bar>

      <v-navigation-drawer v-model="drawer" :permanent="mdAndUp">
        <v-list density="compact" nav>
          <v-list-item
            v-for="item in navigation"
            :key="item.to"
            :prepend-icon="item.icon"
            :title="item.title"
            :to="item.to"
          />
        </v-list>
      </v-navigation-drawer>
    </template>

    <v-main>
      <router-view />
    </v-main>
  </v-app>
</template>
