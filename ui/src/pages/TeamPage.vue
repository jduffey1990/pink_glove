<script setup lang="ts">
  /**
   * The team: who works here, and what the caller may do about each of them.
   *
   * There is no role table on this page. Every row arrives with the
   * `available_actions` and `assignable_roles` the server worked out for
   * whoever is asking, and the roles on offer in the invite dialog come from
   * the server too (ADR-023, ADR-028). An admin sees no menu on an owner's
   * row because the server sent that row no actions, not because this file
   * knows what an admin is. Refusals are shown in the server's own words.
   */
  import type { StaffRole, TeamAction, TeamMember } from '@/api/types'
  import { computed, ref } from 'vue'
  import { fieldErrors } from '@/api/errors'
  import {
    actionsFor,
    doneMessage,
    failureMessage,
    MEMBER_STATE_LOOK,
    roleLabel,
    roleOptions,
    secondLine,
    TEAM_ACTION_LOOK,
  } from '@/lib/team'
  import { useSessionStore } from '@/stores/session'
  import { useTeamStore } from '@/stores/team'

  const session = useSessionStore()
  const team = useTeamStore()

  const notice = ref('')
  const error = ref('')
  /** The row a write is in flight for, so its menu button can show it. */
  const busyId = ref<string | null>(null)

  const inviteDialog = ref(false)
  const inviteEmail = ref('')
  const inviteRole = ref<StaffRole | null>(null)
  const inviting = ref(false)
  const inviteError = ref('')
  const inviteEmailErrors = ref<string[]>([])

  const roleDialog = ref(false)
  const roleTarget = ref<TeamMember | null>(null)
  const newRole = ref<StaffRole | null>(null)
  const roleError = ref('')

  const deactivateDialog = ref(false)
  const deactivateTarget = ref<TeamMember | null>(null)

  const inviteRoleOptions = computed(() => roleOptions(team.inviteRoles))
  const newRoleOptions = computed(() => roleOptions(roleTarget.value?.assignable_roles ?? []))

  team.load()

  function isSelf (member: TeamMember): boolean {
    return member.user === session.user?.id
  }

  function openInvite () {
    inviteEmail.value = ''
    // Nothing preselected: handing out a role should be a choice, not a default.
    inviteRole.value = null
    inviteError.value = ''
    inviteEmailErrors.value = []
    inviteDialog.value = true
  }

  async function sendInvite () {
    if (inviteRole.value === null) {
      return
    }
    inviting.value = true
    inviteError.value = ''
    inviteEmailErrors.value = []

    try {
      const row = await team.invite(inviteEmail.value.trim(), inviteRole.value)
      inviteDialog.value = false
      error.value = ''
      notice.value = doneMessage('invite', row)
    } catch (error_) {
      inviteEmailErrors.value = fieldErrors(error_).email ?? []
      if (inviteEmailErrors.value.length === 0) {
        inviteError.value = failureMessage(error_, 'Could not send that invitation.')
      }
    } finally {
      inviting.value = false
    }
  }

  /** A menu choice. Two of them ask first; the other two just happen. */
  function choose (member: TeamMember, action: TeamAction) {
    if (action === 'change_role') {
      roleTarget.value = member
      newRole.value = null
      roleError.value = ''
      roleDialog.value = true
      return
    }
    if (action === 'deactivate') {
      deactivateTarget.value = member
      deactivateDialog.value = true
      return
    }
    run(member, action)
  }

  async function run (member: TeamMember, action: Exclude<TeamAction, 'change_role'>) {
    busyId.value = member.id
    error.value = ''
    notice.value = ''

    try {
      const row = await (action === 'deactivate'
        ? team.deactivate(member.id)
        : (action === 'reactivate' ? team.reactivate(member.id) : team.resend(member.id)))
      notice.value = doneMessage(action, row)
    } catch (error_) {
      error.value = failureMessage(error_, 'Could not do that.')
      // The row may have changed under us (a 409 says so); show what is true.
      team.load({ quiet: true })
    } finally {
      busyId.value = null
    }
  }

  async function saveRole () {
    const member = roleTarget.value
    if (member === null || newRole.value === null) {
      return
    }
    busyId.value = member.id
    roleError.value = ''

    try {
      const row = await team.changeRole(member.id, newRole.value)
      roleDialog.value = false
      error.value = ''
      notice.value = doneMessage('change_role', row)
    } catch (error_) {
      roleError.value = failureMessage(error_, 'Could not change that role.')
    } finally {
      busyId.value = null
    }
  }

  function confirmDeactivate () {
    const member = deactivateTarget.value
    deactivateDialog.value = false
    if (member !== null) {
      run(member, 'deactivate')
    }
  }
</script>

<template>
  <v-container max-width="900">
    <div class="d-flex align-center ga-2 mb-4">
      <h1 class="text-h6">Team</h1>
      <v-spacer />

      <v-btn
        v-if="team.canInvite"
        color="primary"
        prepend-icon="mdi-account-plus-outline"
        @click="openInvite"
      >
        Invite
      </v-btn>
    </div>

    <v-alert
      v-if="team.loadError"
      class="mb-4"
      type="error"
      variant="tonal"
    >
      {{ team.loadError }}
    </v-alert>

    <v-alert
      v-if="error"
      class="mb-4"
      closable
      density="compact"
      type="error"
      variant="tonal"
      @click:close="error = ''"
    >
      {{ error }}
    </v-alert>

    <v-alert
      v-if="notice"
      class="mb-4"
      closable
      density="compact"
      type="success"
      variant="tonal"
      @click:close="notice = ''"
    >
      {{ notice }}
    </v-alert>

    <!-- Local development only. The API returns `dev_link` just when it is
         running with LOCAL = True, so this cannot appear in a deployed
         environment: there is nothing for it to render. -->
    <v-alert
      v-if="team.devLink"
      class="mb-4"
      closable
      density="compact"
      icon="mdi-wrench-outline"
      type="info"
      variant="tonal"
      @click:close="team.clearDevLink()"
    >
      Dev link:
      <a class="text-break" :href="team.devLink">{{ team.devLink }}</a>
    </v-alert>

    <v-progress-linear v-if="team.loading" class="mb-2" indeterminate />

    <v-card border flat>
      <v-list>
        <template v-for="(member, index) in team.sorted" :key="member.id">
          <v-divider v-if="index > 0" />

          <v-list-item class="py-3">
            <div class="d-flex align-center ga-2">
              <!-- min-width: 0 lets a long address truncate instead of
                   pushing the menu button off a 390px screen. -->
              <div class="flex-grow-1" style="min-width: 0">
                <div class="text-body-1 text-truncate">{{ member.user_name }}</div>

                <div
                  v-if="secondLine(member)"
                  class="text-body-2 text-medium-emphasis text-truncate"
                >
                  {{ secondLine(member) }}
                </div>

                <div class="d-flex flex-wrap ga-1 mt-1">
                  <v-chip size="x-small" variant="tonal">{{ roleLabel(member.role) }}</v-chip>

                  <v-chip
                    v-if="member.state !== 'active'"
                    :color="MEMBER_STATE_LOOK[member.state].color"
                    size="x-small"
                    variant="tonal"
                  >
                    {{ MEMBER_STATE_LOOK[member.state].label }}
                  </v-chip>

                  <v-chip v-if="isSelf(member)" size="x-small" variant="outlined">You</v-chip>
                </div>
              </div>

              <v-menu v-if="actionsFor(member).length > 0">
                <template #activator="{ props }">
                  <v-btn
                    v-bind="props"
                    :aria-label="`Actions for ${member.user_name}`"
                    icon="mdi-dots-vertical"
                    :loading="busyId === member.id"
                    size="small"
                    variant="text"
                  />
                </template>

                <v-list density="compact">
                  <v-list-item
                    v-for="action in actionsFor(member)"
                    :key="action"
                    :prepend-icon="TEAM_ACTION_LOOK[action].icon"
                    :title="TEAM_ACTION_LOOK[action].label"
                    @click="choose(member, action)"
                  />
                </v-list>
              </v-menu>
            </div>
          </v-list-item>
        </template>

        <v-list-item
          v-if="!team.loading && !team.loadError && team.sorted.length === 0"
          title="Nobody here yet"
        />
      </v-list>
    </v-card>

    <v-dialog v-model="inviteDialog" max-width="480">
      <v-card>
        <v-card-title class="text-subtitle-1">Invite someone</v-card-title>

        <v-card-text>
          <v-alert
            v-if="inviteError"
            class="mb-3"
            density="compact"
            type="error"
            variant="tonal"
          >
            {{ inviteError }}
          </v-alert>

          <v-form @submit.prevent="sendInvite">
            <v-text-field
              v-model="inviteEmail"
              autocomplete="off"
              autofocus
              class="mb-2"
              density="compact"
              :error-messages="inviteEmailErrors"
              label="Email"
              type="email"
            />

            <v-select
              v-model="inviteRole"
              density="compact"
              hint="They choose their own name and password when they accept."
              :items="inviteRoleOptions"
              label="Role"
              persistent-hint
            />
          </v-form>
        </v-card-text>

        <v-card-actions>
          <v-spacer />
          <v-btn text="Cancel" @click="inviteDialog = false" />

          <v-btn
            color="primary"
            :disabled="!inviteEmail || inviteRole === null"
            :loading="inviting"
            text="Send invitation"
            variant="flat"
            @click="sendInvite"
          />
        </v-card-actions>
      </v-card>
    </v-dialog>

    <v-dialog v-model="roleDialog" max-width="420">
      <v-card>
        <v-card-title class="text-subtitle-1">Change role</v-card-title>

        <v-card-text>
          <p v-if="roleTarget" class="text-body-2 mb-3">
            {{ roleTarget.user_name }} is currently
            <strong>{{ roleLabel(roleTarget.role) }}</strong>.
          </p>

          <v-alert
            v-if="roleError"
            class="mb-3"
            density="compact"
            type="error"
            variant="tonal"
          >
            {{ roleError }}
          </v-alert>

          <v-select
            v-model="newRole"
            density="compact"
            hide-details
            :items="newRoleOptions"
            label="New role"
          />
        </v-card-text>

        <v-card-actions>
          <v-spacer />
          <v-btn text="Cancel" @click="roleDialog = false" />

          <v-btn
            color="primary"
            :disabled="newRole === null"
            :loading="roleTarget !== null && busyId === roleTarget.id"
            text="Save"
            variant="flat"
            @click="saveRole"
          />
        </v-card-actions>
      </v-card>
    </v-dialog>

    <v-dialog v-model="deactivateDialog" max-width="420">
      <v-card>
        <v-card-title class="text-subtitle-1">Deactivate</v-card-title>

        <v-card-text v-if="deactivateTarget">
          <p v-if="deactivateTarget.state === 'invited'" class="text-body-2">
            This withdraws the invitation sent to {{ deactivateTarget.user_name }}.
            The link in their email stops working.
          </p>

          <p v-else class="text-body-2">
            {{ deactivateTarget.user_name }} loses access to this organization
            straight away, including anywhere they are already signed in. You
            can reactivate them later.
          </p>
        </v-card-text>

        <v-card-actions>
          <v-spacer />
          <v-btn text="Cancel" @click="deactivateDialog = false" />
          <v-btn color="error" text="Deactivate" variant="flat" @click="confirmDeactivate" />
        </v-card-actions>
      </v-card>
    </v-dialog>
  </v-container>
</template>
