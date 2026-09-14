"""
The membership endpoint doubles as the staff directory.

Phase 3b needed this: an assignee picker POSTs a *user* id to the job assign
action, and a membership id is not one. Without the user fields the frontend
could list colleagues but not assign any of them.
"""

import pytest
from django.urls import reverse

from users.enums import Role

LIST = reverse("users:membership-list")


@pytest.mark.django_db
class TestMembershipDirectory:
    def test_it_exposes_the_user_id_for_assignment(self, authed_client, owner):
        row = authed_client.get(LIST).json()["results"][0]

        assert row["user"] == str(owner.id)

    def test_it_exposes_a_name_to_show_in_a_picker(self, authed_client, owner):
        row = authed_client.get(LIST).json()["results"][0]

        assert row["user_name"] == owner.full_name
        assert row["user_email"] == owner.email

    def test_the_membership_id_is_still_distinct_from_the_user_id(self, authed_client, owner):
        """The two were easy to confuse, which is what prompted this."""
        row = authed_client.get(LIST).json()["results"][0]

        assert row["id"] != row["user"]

    def test_a_cleaner_can_read_the_directory(self, api_client, organization, make_member, owner):
        """They see colleague names on their own jobs already."""
        api_client.force_login(make_member(organization, role=Role.CLEANER))

        response = api_client.get(LIST)

        assert response.status_code == 200
        assert response.json()["count"] == 2

    def test_a_customer_cannot(self, api_client, organization, make_member):
        api_client.force_login(make_member(organization, role=Role.CUSTOMER))

        assert api_client.get(LIST).status_code == 403

    def test_another_organizations_staff_are_not_listed(self, authed_client, rival_owner):
        rows = authed_client.get(LIST).json()["results"]

        assert str(rival_owner.id) not in {row["user"] for row in rows}
