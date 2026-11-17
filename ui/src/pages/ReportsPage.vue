<script setup lang="ts">
  /**
   * Reports: what the crew worked, who owes the organization, and what came in.
   *
   * Owner and admin only. The route says so, and the API says so
   * independently, which is the one that counts.
   *
   * Every figure on this page was computed by the server. Nothing here adds
   * up hours or money, decides which day an entry belongs to, or works out a
   * balance (ADR-023, ADR-030): the page picks a period, asks, and prints.
   * The period is reckoned in the organization's timezone, never the
   * browser's, and goes to the API as two local dates.
   *
   * The three reports load and fail independently. Receivables are not for
   * the period at all: they are what is owed now.
   */
  import type { ReportName } from '@/api/endpoints'
  import type { Collections, Receivables, Timesheet } from '@/api/types'
  import type { Period, PeriodPreset } from '@/lib/reports'
  import { computed, ref, watch } from 'vue'
  import {
    downloadReportCsv,
    getCollections,
    getReceivables,
    getTimesheet,
  } from '@/api/endpoints'
  import { formatDayLabel, formatHoursMinutes, formatTime, todayIn } from '@/lib/datetime'
  import { saveBlob } from '@/lib/download'
  import { useLoadable } from '@/lib/loadable'
  import { formatCents } from '@/lib/money'
  import {
    AGING_COLUMNS,
    csvFilename,
    feesPendingNote,
    formatPeriod,
    isComplete,
    openEntriesNote,
    PERIOD_OPTIONS,
    presetPeriod,
  } from '@/lib/reports'
  import { useSessionStore } from '@/stores/session'

  const session = useSessionStore()

  const preset = ref<PeriodPreset>('this-week')
  /** The two date fields, used only while the preset is "custom". */
  const custom = ref<Partial<Period>>({})

  const period = computed<Partial<Period>>(() =>
    preset.value === 'custom'
      ? custom.value
      : presetPeriod(preset.value, todayIn(session.timeZone)),
  )

  /**
   * The period, once there are two dates to ask with.
   *
   * A fresh object each time, on purpose: the custom fields are edited in
   * place, and a watcher handed the same object back would not fire.
   */
  const asked = computed<Period | null>(() => {
    const candidate = { ...period.value }
    return isComplete(candidate) ? candidate : null
  })

  const {
    data: timesheet,
    loading: timesheetLoading,
    error: timesheetError,
    load: loadTimesheet,
    clear: clearTimesheet,
  } = useLoadable<Timesheet>('Could not load the timesheet.')

  const {
    data: collections,
    loading: collectionsLoading,
    error: collectionsError,
    load: loadCollections,
    clear: clearCollections,
  } = useLoadable<Collections>('Could not load what was received.')

  const {
    data: receivables,
    loading: receivablesLoading,
    error: receivablesError,
    load: loadReceivables,
  } = useLoadable<Receivables>('Could not load who owes you.')

  const openNote = computed(() => openEntriesNote(timesheet.value?.open_entry_count ?? 0))
  const feesNote = computed(() => feesPendingNote(collections.value?.totals.fees_pending ?? 0))

  // Switching to "custom" starts from the dates already on screen rather
  // than from two empty fields.
  watch(preset, (now, before) => {
    if (now === 'custom' && before !== 'custom') {
      custom.value = presetPeriod(before, todayIn(session.timeZone))
    }
  })

  watch(
    asked,
    (now, before) => {
      if (now === null) {
        // Half a period: nothing to ask, and nothing on screen to mistake
        // for the answer.
        clearTimesheet()
        clearCollections()
        return
      }
      if (before && before.date_from === now.date_from && before.date_to === now.date_to) {
        return
      }
      loadTimesheet(() => getTimesheet(now))
      loadCollections(() => getCollections(now))
    },
    { immediate: true },
  )

  loadReceivables(() => getReceivables())

  const downloading = ref<ReportName | null>(null)
  const downloadError = ref('')

  async function download (report: ReportName): Promise<void> {
    downloading.value = report
    downloadError.value = ''

    try {
      if (report === 'receivables') {
        // The date the table on screen was aged from, so the file matches it.
        const asOf = receivables.value?.as_of ?? todayIn(session.timeZone)
        saveBlob(
          await downloadReportCsv(report, { as_of: asOf }),
          csvFilename(report, asOf),
        )
      } else if (asked.value) {
        saveBlob(
          await downloadReportCsv(report, asked.value),
          csvFilename(report, asked.value.date_from, asked.value.date_to),
        )
      }
    } catch {
      // The body of a failed file request is a blob, not a sentence to quote.
      downloadError.value = 'Could not download that report.'
    } finally {
      downloading.value = null
    }
  }
</script>

<template>
  <v-container max-width="1100">
    <div class="d-flex flex-wrap align-center ga-2 mb-2">
      <h1 class="text-h6">Reports</h1>
      <v-spacer />

      <v-select
        v-model="preset"
        density="compact"
        hide-details
        :items="PERIOD_OPTIONS"
        label="Period"
        max-width="180"
        variant="outlined"
      />
    </div>

    <div v-if="preset === 'custom'" class="d-flex flex-wrap ga-2 mb-2">
      <v-text-field
        v-model="custom.date_from"
        density="compact"
        hide-details
        label="From"
        max-width="200"
        type="date"
        variant="outlined"
      />

      <v-text-field
        v-model="custom.date_to"
        density="compact"
        hide-details
        label="To"
        max-width="200"
        type="date"
        variant="outlined"
      />
    </div>

    <p class="text-body-2 text-medium-emphasis mb-4">
      <template v-if="asked">
        {{ formatPeriod(asked) }} · {{ session.timeZone }} time
      </template>

      <template v-else>Choose both dates.</template>
    </p>

    <v-alert
      v-if="downloadError"
      class="mb-4"
      closable
      type="error"
      variant="tonal"
      @click:close="downloadError = ''"
    >
      {{ downloadError }}
    </v-alert>

    <!-- Timesheet -->
    <div class="d-flex flex-wrap align-center ga-2 mb-2">
      <h2 class="text-subtitle-1 font-weight-medium">Timesheet</h2>
      <v-spacer />

      <v-btn
        :disabled="!asked"
        :loading="downloading === 'timesheet'"
        prepend-icon="mdi-download"
        size="small"
        text="CSV"
        variant="text"
        @click="download('timesheet')"
      />
    </div>

    <v-alert v-if="timesheetError" class="mb-3" type="error" variant="tonal">
      {{ timesheetError }}
    </v-alert>

    <v-progress-linear v-if="timesheetLoading" class="mb-2" indeterminate />

    <template v-if="timesheet">
      <v-card border class="mb-3" flat>
        <v-card-text class="d-flex flex-wrap ga-8">
          <div>
            <div class="text-caption text-medium-emphasis">Hours worked</div>
            <div class="text-h6">{{ formatHoursMinutes(timesheet.total_seconds) }}</div>
          </div>

          <div>
            <div class="text-caption text-medium-emphasis">Entries</div>
            <div class="text-h6">{{ timesheet.entry_count }}</div>
          </div>

          <div>
            <div class="text-caption text-medium-emphasis">People</div>
            <div class="text-h6">{{ timesheet.staff.length }}</div>
          </div>
        </v-card-text>
      </v-card>

      <v-alert
        v-if="openNote"
        class="mb-3"
        density="compact"
        type="warning"
        variant="tonal"
      >
        {{ openNote }}
      </v-alert>

      <v-card v-if="timesheet.staff.length === 0" border flat>
        <v-card-text class="text-medium-emphasis">
          Nobody clocked in during this period.
        </v-card-text>
      </v-card>

      <v-expansion-panels v-else multiple variant="accordion">
        <v-expansion-panel v-for="person in timesheet.staff" :key="person.user">
          <v-expansion-panel-title>
            <span class="person-name text-truncate">{{ person.name }}</span>
            <v-spacer />

            <v-chip
              v-if="person.open_entry_count > 0"
              class="mr-2 flex-shrink-0"
              color="warning"
              size="x-small"
              variant="tonal"
            >
              {{ person.open_entry_count }} open
            </v-chip>

            <span class="text-body-2 text-no-wrap mr-2">
              {{ formatHoursMinutes(person.total_seconds) }}
            </span>
          </v-expansion-panel-title>

          <v-expansion-panel-text>
            <v-list density="compact">
              <v-list-item
                v-for="entry in person.entries"
                :key="entry.id"
                :to="{ name: 'job', params: { id: entry.job } }"
              >
                <v-list-item-title>{{ entry.customer_name }}</v-list-item-title>

                <v-list-item-subtitle>
                  {{ formatDayLabel(entry.local_date) }} ·
                  {{ formatTime(entry.clock_in, timesheet.timezone) }}
                  <template v-if="entry.clock_out">
                    to {{ formatTime(entry.clock_out, timesheet.timezone) }}
                  </template>
                </v-list-item-subtitle>

                <template #append>
                  <v-chip
                    v-if="entry.is_open"
                    color="warning"
                    size="x-small"
                    variant="tonal"
                  >
                    Still clocked in
                  </v-chip>

                  <span v-else-if="entry.seconds !== null" class="text-body-2 text-no-wrap">
                    {{ formatHoursMinutes(entry.seconds) }}
                  </span>
                </template>
              </v-list-item>
            </v-list>
          </v-expansion-panel-text>
        </v-expansion-panel>
      </v-expansion-panels>
    </template>

    <!-- Money -->
    <h2 class="text-subtitle-1 font-weight-medium mt-8 mb-2">Money</h2>

    <!-- Receivables -->
    <v-card border class="mb-4" flat>
      <v-card-title class="d-flex flex-wrap align-center ga-2 text-subtitle-2">
        <span>Who owes you</span>

        <span v-if="receivables" class="text-body-2 text-medium-emphasis text-wrap">
          as of {{ formatDayLabel(receivables.as_of) }}, by due date
        </span>

        <v-spacer />

        <v-btn
          :loading="downloading === 'receivables'"
          prepend-icon="mdi-download"
          size="small"
          text="CSV"
          variant="text"
          @click="download('receivables')"
        />
      </v-card-title>

      <v-alert v-if="receivablesError" class="mx-4 mb-3" type="error" variant="tonal">
        {{ receivablesError }}
      </v-alert>

      <v-progress-linear v-if="receivablesLoading" indeterminate />

      <v-card-text v-if="receivables && receivables.customers.length === 0" class="text-medium-emphasis">
        Nobody owes anything.
      </v-card-text>

      <!-- Seven columns of money do not fit a phone. The table scrolls
           inside this box; the page never scrolls sideways. -->
      <div v-else-if="receivables" class="table-scroll">
        <v-table density="compact">
          <thead>
            <tr>
              <th>Customer</th>

              <th v-for="column in AGING_COLUMNS" :key="column.key" class="text-right text-no-wrap">
                {{ column.label }}
              </th>

              <th class="text-right">Total</th>
            </tr>
          </thead>

          <tbody>
            <tr v-for="row in receivables.customers" :key="row.customer">
              <td class="text-no-wrap">{{ row.customer_name }}</td>

              <td v-for="column in AGING_COLUMNS" :key="column.key" class="text-right text-no-wrap">
                {{ formatCents(row[column.key]) }}
              </td>

              <td class="text-right text-no-wrap font-weight-medium">
                {{ formatCents(row.total_cents) }}
              </td>
            </tr>
          </tbody>

          <tfoot>
            <tr class="font-weight-medium">
              <td>Total</td>

              <td v-for="column in AGING_COLUMNS" :key="column.key" class="text-right text-no-wrap">
                {{ formatCents(receivables.totals[column.key]) }}
              </td>

              <td class="text-right text-no-wrap">
                {{ formatCents(receivables.totals.total_cents) }}
              </td>
            </tr>
          </tfoot>
        </v-table>
      </div>
    </v-card>

    <!-- Collections -->
    <v-card border flat>
      <v-card-title class="d-flex flex-wrap align-center ga-2 text-subtitle-2">
        <span>Received</span>

        <span v-if="asked" class="text-body-2 text-medium-emphasis text-wrap">
          {{ formatPeriod(asked) }}, by the date on the payment
        </span>

        <v-spacer />

        <v-btn
          :disabled="!asked"
          :loading="downloading === 'collections'"
          prepend-icon="mdi-download"
          size="small"
          text="CSV"
          variant="text"
          @click="download('collections')"
        />
      </v-card-title>

      <v-alert v-if="collectionsError" class="mx-4 mb-3" type="error" variant="tonal">
        {{ collectionsError }}
      </v-alert>

      <v-progress-linear v-if="collectionsLoading" indeterminate />

      <v-card-text v-if="collections && collections.methods.length === 0" class="text-medium-emphasis">
        Nothing was received in this period.
      </v-card-text>

      <template v-else-if="collections">
        <div class="table-scroll">
          <v-table density="compact">
            <thead>
              <tr>
                <th>Method</th>
                <th class="text-right">Payments</th>
                <th class="text-right">Amount</th>
                <th class="text-right">Tips</th>
                <th class="text-right">Fees</th>
                <th class="text-right">Net</th>
              </tr>
            </thead>

            <tbody>
              <tr v-for="row in collections.methods" :key="row.method">
                <td class="text-no-wrap">{{ row.label }}</td>
                <td class="text-right">{{ row.payment_count }}</td>
                <td class="text-right text-no-wrap">{{ formatCents(row.amount_cents) }}</td>
                <td class="text-right text-no-wrap">{{ formatCents(row.tip_cents) }}</td>
                <td class="text-right text-no-wrap">{{ formatCents(row.fee_cents) }}</td>

                <td class="text-right text-no-wrap font-weight-medium">
                  {{ formatCents(row.net_cents) }}
                </td>
              </tr>
            </tbody>

            <tfoot>
              <tr class="font-weight-medium">
                <td>Total</td>
                <td class="text-right">{{ collections.totals.payment_count }}</td>

                <td class="text-right text-no-wrap">
                  {{ formatCents(collections.totals.amount_cents) }}
                </td>

                <td class="text-right text-no-wrap">
                  {{ formatCents(collections.totals.tip_cents) }}
                </td>

                <td class="text-right text-no-wrap">
                  {{ formatCents(collections.totals.fee_cents) }}
                </td>

                <td class="text-right text-no-wrap">
                  {{ formatCents(collections.totals.net_cents) }}
                </td>
              </tr>
            </tfoot>
          </v-table>
        </div>

        <v-card-text class="text-body-2 text-medium-emphasis">
          Net is amount plus tips, less card fees. Tips are not revenue.
          <template v-if="feesNote"> {{ feesNote }}</template>
        </v-card-text>
      </template>
    </v-card>
  </v-container>
</template>

<style scoped>
  /* A wide table scrolls inside its own box, so the page itself never has
     to. `min-width: 0` on the name lets a long one truncate instead of
     pushing the hours off the right edge of a phone. */
  .table-scroll {
    max-width: 100%;
    overflow-x: auto;
  }

  .person-name {
    min-width: 0;
  }
</style>
