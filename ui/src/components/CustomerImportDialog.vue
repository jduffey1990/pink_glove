<script setup lang="ts">
  /**
   * Load a customer book from a CSV file (ADR-031). Owner and admin.
   *
   * Three things here are deliberate:
   *
   * 1. The file is uploaded, never read. The browser does not parse CSV or
   *    judge a row; the server does both with the rules the create path uses
   *    and this renders its answer, sentence for sentence (ADR-023).
   * 2. Choosing a file writes nothing. The first upload is always a dry run,
   *    and the Import button is drawn from the server's `can_commit`.
   * 3. Nothing shown here can be an access code: the server's per-row outcome
   *    has no field for one, so the codes go up in the file and never come
   *    back down (ADR-014).
   *
   * The flow itself is `useCustomerImport` in `lib/customerImport`, which has
   * the spec. This file is the markup.
   */
  import type { CustomerImportTemplate } from '@/api/types'
  import { ref, watch } from 'vue'
  import { fetchCustomerImportTemplate, importCustomers } from '@/api/endpoints'
  import {
    columnHints,
    errorText,
    importTone,
    rowHeading,
    templateSummary,
    useCustomerImport,
  } from '@/lib/customerImport'
  import { saveTextFile } from '@/lib/download'

  const model = defineModel<boolean>({ required: true })
  const emit = defineEmits<{ imported: [] }>()

  const {
    file,
    result,
    busy,
    error,
    step,
    groups,
    canPreview,
    canConfirm,
    choose,
    startOver,
    preview,
    confirm,
  } = useCustomerImport(importCustomers, () => emit('imported'))

  const template = ref<CustomerImportTemplate | null>(null)

  // `immediate`, as in RevealCodesDialog: a dialog behind a `v-if` that flips
  // in the same tick as its model mounts with the model already true.
  watch(model, async open => {
    if (!open) {
      return
    }
    // Every opening starts clean: last time's file and answer are gone.
    startOver()

    if (template.value === null) {
      // Not fatal. Without it the dialog still imports; it only loses the
      // download link and the line saying what a file needs.
      template.value = await fetchCustomerImportTemplate().catch(() => null)
    }
  }, { immediate: true })

  function downloadTemplate () {
    if (template.value) {
      saveTextFile(template.value.filename, template.value.csv)
    }
  }
</script>

<template>
  <v-dialog v-model="model" max-width="640" scrollable>
    <v-card>
      <v-card-title class="text-subtitle-1">Import customers</v-card-title>

      <v-card-text>
        <v-alert
          v-if="error"
          class="mb-3"
          density="compact"
          type="error"
          variant="tonal"
        >
          {{ error }}
        </v-alert>

        <template v-if="step === 'choose'">
          <p class="text-body-2 mb-3">
            One row per customer and service location. Nothing is saved until you have
            seen a preview and confirmed it.
          </p>

          <v-file-input
            accept=".csv,text/csv"
            density="compact"
            label="CSV file"
            :model-value="file"
            prepend-icon="mdi-file-delimited-outline"
            show-size
            @update:model-value="choose"
          />

          <!-- The server's column list and limits, not a copy of them. -->
          <p v-if="template" class="text-caption text-medium-emphasis wrap">
            {{ templateSummary(template) }}
          </p>

          <!--
            The server's hint for each column: its choices, and what a blank
            cell does. Closed by default, because thirty lines of it would
            push the file picker off a phone.
          -->
          <details v-if="template" class="text-caption text-medium-emphasis mt-2 wrap">
            <summary>What goes in each column</summary>

            <div v-for="column in columnHints(template)" :key="column.header" class="mt-1">
              <strong>{{ column.header }}</strong>: {{ column.hint }}
            </div>
          </details>

          <v-btn
            class="mt-2 px-0"
            :disabled="!template"
            prepend-icon="mdi-download"
            size="small"
            text="Download the template"
            variant="text"
            @click="downloadTemplate"
          />

          <p class="text-caption text-medium-emphasis mt-1">
            The template has one example row. Replace it with your own.
          </p>
        </template>

        <template v-else-if="result">
          <!-- The server's own summary and sentences from here down. -->
          <v-alert
            class="mb-3"
            density="compact"
            :type="importTone(result)"
            variant="tonal"
          >
            {{ result.message }}
          </v-alert>

          <v-alert
            v-if="result.ignored_columns.length > 0"
            class="mb-3 wrap"
            density="compact"
            type="warning"
            variant="tonal"
          >
            Columns in the file that are not imported: {{ result.ignored_columns.join(', ') }}
          </v-alert>

          <section v-for="group in groups" :key="group.key" class="mb-4">
            <h3 class="text-subtitle-2 mb-1">{{ group.title }}</h3>

            <div v-for="row in group.rows" :key="row.row" class="import-row wrap">
              <div class="text-body-2">{{ rowHeading(row) }}</div>
              <div class="text-caption text-medium-emphasis">{{ row.detail }}</div>

              <div
                v-for="(rowError, index) in row.errors"
                :key="index"
                class="text-caption text-error"
              >
                {{ errorText(rowError) }}
              </div>
            </div>

            <p v-if="group.hidden > 0" class="text-caption text-medium-emphasis mt-1">
              and {{ group.hidden }} more
            </p>
          </section>
        </template>
      </v-card-text>

      <v-card-actions class="flex-wrap">
        <v-spacer />

        <template v-if="step === 'choose'">
          <v-btn text="Cancel" @click="model = false" />

          <v-btn
            color="primary"
            :disabled="!canPreview"
            :loading="busy"
            text="Preview"
            variant="flat"
            @click="preview"
          />
        </template>

        <template v-else-if="step === 'preview'">
          <v-btn :disabled="busy" text="Choose another file" @click="startOver" />

          <v-btn
            color="primary"
            :disabled="!canConfirm"
            :loading="busy"
            text="Import"
            variant="flat"
            @click="confirm"
          />
        </template>

        <v-btn
          v-else
          color="primary"
          text="Done"
          variant="flat"
          @click="model = false"
        />
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<style scoped>
.import-row {
  padding: 6px 0;
  border-top: thin solid rgba(var(--v-border-color), var(--v-border-opacity));
}

/* A long email, address or column list wraps rather than widening a phone. */
.wrap {
  overflow-wrap: anywhere;
}
</style>
