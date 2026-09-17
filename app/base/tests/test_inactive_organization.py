"""
A deactivated organization is read-only.

Not locked out: a tenant suspended for non-payment still has to be able to see
what they have and what they owe. Enforced in `IsOrgMember`, which every role
permission inherits, so the tests reach it through several unrelated endpoints
rather than one -- the point is that there is no way round it.
"""

import pytest
from django.urls import reverse

from base.permissions import ORGANIZATION_INACTIVE
from scheduling.tests.factories import JobFactory


@pytest.fixture
def deactivated(organization):
    organization.is_active = False
    organization.save()
    return organization


@pytest.mark.django_db
class TestADeactivatedOrganizationIsReadOnly:
    def test_reads_still_work(self, authed_client, deactivated):
        JobFactory(organization=deactivated)

        assert authed_client.get(reverse("scheduling:job-list")).json()["count"] == 1
        assert authed_client.get(reverse("customers:customer-list")).status_code == 200
        assert authed_client.get(reverse("organizations:current")).status_code == 200

    @pytest.mark.parametrize(
        "name", ["customers:customer-list", "catalog:service-list", "scheduling:job-list"]
    )
    def test_nothing_can_be_created(self, authed_client, deactivated, name):
        response = authed_client.post(reverse(name), {}, format="json")

        assert response.status_code == 403
        assert response.json()["detail"] == ORGANIZATION_INACTIVE

    def test_a_job_cannot_be_moved_along(self, authed_client, deactivated):
        job = JobFactory(organization=deactivated)

        response = authed_client.post(
            reverse("scheduling:job-status", args=[job.id]), {"status": "en_route"}, format="json"
        )

        assert response.status_code == 403

    def test_codes_cannot_be_revealed(self, authed_client, deactivated):
        """The composed permission (`A | B`) still carries the reason."""
        job = JobFactory(organization=deactivated)
        url = reverse("customers:location-reveal-access", args=[job.location_id])

        response = authed_client.post(url, {"acknowledged": True}, format="json")

        assert response.status_code == 403
        assert response.json()["detail"] == ORGANIZATION_INACTIVE

    def test_the_organization_itself_cannot_be_edited(self, authed_client, deactivated):
        response = authed_client.patch(
            reverse("organizations:current"), {"name": "Back in business"}, format="json"
        )

        assert response.status_code == 403

    def test_signing_out_still_works(self, authed_client, deactivated):
        assert authed_client.post(reverse("users:logout")).status_code == 200

    def test_a_superuser_can_still_put_things_right(self, api_client, superuser, deactivated):
        from app.middleware.tenant import ORGANIZATION_HEADER

        api_client.force_login(superuser)

        response = api_client.patch(
            reverse("organizations:current"),
            {"name": "Reinstated"},
            format="json",
            headers={ORGANIZATION_HEADER: str(deactivated.id)},
        )

        assert response.status_code == 200

    def test_an_active_organization_is_unaffected(self, authed_client, organization):
        response = authed_client.post(
            reverse("customers:customer-list"), {"first_name": "Dana"}, format="json"
        )

        assert response.status_code == 201
