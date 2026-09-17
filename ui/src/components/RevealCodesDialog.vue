<script setup lang="ts">
  /**
   * The audited access-code reveal (ADR-016, ADR-017).
   *
   * Two things here are deliberate and should not be "simplified":
   *
   * 1. The warning text is fetched from the backend, never hardcoded. The API
   *    serves it from `access-warning`, so the words shown and the words the
   *    audit row claims were shown are the same string.
   * 2. The codes are held in memory and dropped when the dialog closes. Every
   *    look costs another logged reveal, which is the entire point: the log is
   *    a record of doors opened, not of pages loaded.
   */
  import type { RevealedCodes } from '@/api/types'
  import { ref, watch } from 'vue'
  import { fetchAccessWarning, revealAccessCodes } from '@/api/endpoints'
  import { statusOf } from '@/stores/session'

  const props = defineProps<{ locationId: string, jobId?: string | null }>()
  const model = defineModel<boolean>({ required: true })

  const warning = ref('')
  const codes = ref<RevealedCodes | null>(null)
  const loading = ref(false)
  const error = ref('')

  // `immediate` matters: callers render this dialog behind a `v-if` that
  // becomes true in the same tick as the model, so the component mounts with
  // `model` ALREADY true and a plain watcher never sees a change -- the
  // warning would then never be fetched and the dialog would open blank.
  watch(model, async open => {
    if (!open) {
      // Dropped on close. Reopening means another reveal, and another row.
      codes.value = null
      error.value = ''
      return
    }

    loading.value = true
    try {
      warning.value = await fetchAccessWarning(props.locationId)
    } catch (error_) {
      error.value = statusOf(error_) === 403
        ? 'You are not assigned to a job at this location.'
        : 'Could not reach the access log.'
    } finally {
      loading.value = false
    }
  }, { immediate: true })

  async function confirm () {
    loading.value = true
    error.value = ''

    try {
      codes.value = await revealAccessCodes(props.locationId, props.jobId ?? undefined)
    } catch (error_) {
      error.value = statusOf(error_) === 403
        ? 'You are not assigned to a job at this location.'
        : 'Could not reveal the codes.'
    } finally {
      loading.value = false
    }
  }

  const FIELDS = [
    { key: 'gate_code', label: 'Gate code' },
    { key: 'alarm_code', label: 'Alarm code' },
    { key: 'key_location', label: 'Key location' },
  ] as const
</script>

<template>
  <v-dialog v-model="model" max-width="460">
    <v-card>
      <v-card-item>
        <v-card-title class="text-h6">
          <v-icon class="mr-1" icon="mdi-shield-key-outline" />
          Access codes
        </v-card-title>
      </v-card-item>

      <v-card-text>
        <v-alert v-if="error" density="compact" type="error" variant="tonal">
          {{ error }}
        </v-alert>

        <template v-else-if="codes">
          <v-list density="compact">
            <v-list-item
              v-for="field in FIELDS"
              :key="field.key"
              :subtitle="codes[field.key] || 'Not on file'"
              :title="field.label"
            />
          </v-list>

          <p class="text-caption text-medium-emphasis mt-2">
            This reveal has been recorded against your name.
          </p>
        </template>

        <!-- The backend's own wording, not ours. -->
        <p v-else class="text-body-2">{{ warning }}</p>
      </v-card-text>

      <v-card-actions>
        <v-spacer />

        <v-btn :text="codes ? 'Done' : 'Cancel'" @click="model = false" />

        <v-btn
          v-if="!codes && !error"
          color="primary"
          :loading="loading"
          text="Continue"
          variant="flat"
          @click="confirm"
        />
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>
