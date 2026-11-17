<script setup lang="ts">
  /** The book of business. Dispatcher and above. */
  import type { Customer } from '@/api/types'
  import { ref, watch } from 'vue'
  import { useRouter } from 'vue-router'
  import { useDisplay } from 'vuetify'
  import { createCustomer, listCustomers } from '@/api/endpoints'
  import CustomerImportDialog from '@/components/CustomerImportDialog.vue'
  import { useSessionStore } from '@/stores/session'

  const router = useRouter()
  const session = useSessionStore()

  // Owner and admin. The API enforces this independently (ADR-031); hiding
  // the button is a courtesy.
  const importOpen = ref(false)
  // On a phone Import is icon-only and the search takes its own full-width
  // row, so New stays on the title row and the search is not squeezed.
  const { xs } = useDisplay()

  const customers = ref<Customer[]>([])
  const search = ref('')
  const loading = ref(false)
  const error = ref('')

  const dialog = ref(false)
  const saving = ref(false)
  const draft = ref<Partial<Customer>>({})
  const formError = ref('')

  async function load () {
    loading.value = true
    error.value = ''

    try {
      const page = await listCustomers({
        ...(search.value ? { search: search.value } : {}),
        limit: 100,
      })
      customers.value = page.results
    } catch {
      error.value = 'Could not load customers.'
    } finally {
      loading.value = false
    }
  }

  let searchTimer: ReturnType<typeof setTimeout> | undefined
  watch(search, () => {
    clearTimeout(searchTimer)
    searchTimer = setTimeout(load, 250)
  })

  load()

  function openNew () {
    draft.value = { preferred_contact_method: 'email', status: 'lead' }
    formError.value = ''
    dialog.value = true
  }

  async function save () {
    saving.value = true
    formError.value = ''

    try {
      const created = await createCustomer(draft.value)
      dialog.value = false
      router.push({ name: 'customer', params: { id: created.id } })
    } catch (error_) {
      // The backend requires at least one of first/last/company name; it
      // mirrors a database constraint, so it comes back as a 400 rather than
      // being duplicated here.
      const data = (error_ as { response?: { data?: Record<string, unknown> } }).response?.data
      formError.value = typeof data === 'object' && data !== null
        ? Object.values(data).flat().join(' ')
        : 'Could not save that customer.'
    } finally {
      saving.value = false
    }
  }
</script>

<template>
  <v-container max-width="1000">
    <div class="d-flex flex-wrap align-center ga-2 mb-4">
      <h1 class="text-h6">Customers</h1>
      <v-spacer />

      <v-text-field
        v-model="search"
        class="search"
        :class="{ 'search--own-row': xs }"
        clearable
        density="compact"
        hide-details
        label="Search"
        prepend-inner-icon="mdi-magnify"
        variant="outlined"
      />

      <v-btn
        v-if="session.isAdminOrHigher && xs"
        aria-label="Import"
        icon="mdi-upload"
        size="small"
        variant="tonal"
        @click="importOpen = true"
      />

      <v-btn
        v-else-if="session.isAdminOrHigher"
        prepend-icon="mdi-upload"
        variant="tonal"
        @click="importOpen = true"
      >
        Import
      </v-btn>

      <v-btn color="primary" prepend-icon="mdi-plus" @click="openNew">New</v-btn>
    </div>

    <v-alert v-if="error" class="mb-4" type="error" variant="tonal">{{ error }}</v-alert>

    <v-progress-linear v-if="loading" class="mb-2" indeterminate />

    <v-card border flat>
      <v-list>
        <v-list-item
          v-for="customer in customers"
          :key="customer.id"
          :subtitle="customer.email || customer.phone || '—'"
          :title="customer.display_name"
          @click="router.push({ name: 'customer', params: { id: customer.id } })"
        >
          <template #append>
            <v-chip class="mr-2" size="x-small" variant="tonal">{{ customer.status }}</v-chip>

            <v-chip size="x-small" variant="text">
              {{ customer.locations.length }} location(s)
            </v-chip>
          </template>
        </v-list-item>

        <v-list-item v-if="!loading && customers.length === 0" title="No customers yet" />
      </v-list>
    </v-card>

    <v-dialog v-model="dialog" max-width="520">
      <v-card>
        <v-card-title class="text-subtitle-1">New customer</v-card-title>

        <v-card-text>
          <v-alert
            v-if="formError"
            class="mb-3"
            density="compact"
            type="error"
            variant="tonal"
          >
            {{ formError }}
          </v-alert>

          <v-text-field v-model="draft.first_name" density="compact" label="First name" />
          <v-text-field v-model="draft.last_name" density="compact" label="Last name" />
          <v-text-field v-model="draft.company_name" density="compact" label="Company" />
          <v-text-field v-model="draft.email" density="compact" label="Email" />
          <v-text-field v-model="draft.phone" density="compact" label="Phone" />
        </v-card-text>

        <v-card-actions>
          <v-spacer />
          <v-btn text="Cancel" @click="dialog = false" />

          <v-btn
            color="primary"
            :loading="saving"
            text="Create"
            variant="flat"
            @click="save"
          />
        </v-card-actions>
      </v-card>
    </v-dialog>

    <CustomerImportDialog
      v-if="session.isAdminOrHigher"
      v-model="importOpen"
      @imported="load"
    />
  </v-container>
</template>

<style scoped>
.search {
  max-width: 280px;
}

.search--own-row {
  order: 1;
  flex-basis: 100%;
  max-width: none;
}
</style>
