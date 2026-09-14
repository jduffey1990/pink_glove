<script setup lang="ts">
  import { useRouter } from 'vue-router'

  const router = useRouter()
</script>

<template>
  <v-container class="fill-height text-center" max-width="480">
    <div class="w-100">
      <v-icon class="mb-4" icon="mdi-compass-off-outline" size="64" />
      <h1 class="text-h6 mb-2">Nothing here</h1>

      <p class="text-medium-emphasis mb-6">
        That page does not exist, or it belongs to another organization.
      </p>

      <v-btn color="primary" @click="router.push({ name: 'home' })">Go back</v-btn>
    </div>
  </v-container>
</template>
