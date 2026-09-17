"""
Tenant isolation.

Two layers:

1. A structural check that walks the URLconf and fails if any view over a
   TenantModel skips the scoping chokepoint. This is what makes "someone
   forgot to filter" a CI failure rather than a data leak.
2. Behavioural checks that two organizations genuinely cannot see or touch
   each other's records.

See docs/DECISIONS.md ADR-002.
"""

import pytest
from django.core.exceptions import ValidationError
from django.db import models
from django.test.utils import isolate_apps
from django.urls import URLPattern, URLResolver, get_resolver, reverse

from base.models import Base, TenantModel
from base.viewsets import TenantViewSetMixin

#: Views over a TenantModel that legitimately do not use TenantViewSetMixin.
#:
#: Every entry needs a reason. Keeping exemptions here rather than allowing
#: silent omissions means each one shows up as a reviewable line in a diff.
TENANCY_EXEMPT: frozenset[str] = frozenset(
    {
        # (none yet)
    }
)


def _iter_views(patterns, prefix=""):
    """Yield (route, view_class) for every resolvable view in the URLconf."""
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            yield from _iter_views(pattern.url_patterns, prefix + str(pattern.pattern))
        elif isinstance(pattern, URLPattern):
            callback = pattern.callback
            view_class = getattr(callback, "cls", None) or getattr(callback, "view_class", None)
            if view_class is not None:
                yield prefix + str(pattern.pattern), view_class


def _synthetic_models():
    """
    Build a tenant model that points at another tenant model.

    MUST be called inside `@isolate_apps("base", "organizations")`.

    Two things make this fiddly. `isolate_apps` builds an *empty* registry
    rather than cloning the real one, so the string reference
    `"organizations.Organization"` on `TenantModel` has nothing to resolve
    against unless a stand-in with that app_label is defined here too. And the
    lazy reference only resolves while that registry is live, so the classes
    cannot escape the context.

    Returns the stand-in Organization as well -- instances must come from it,
    not from the real model, or the FK descriptor's isinstance check fails.
    """

    class Organization(Base):
        name = models.CharField(max_length=255)

        class Meta(Base.Meta):
            app_label = "organizations"

    class Widget(TenantModel):
        class Meta(TenantModel.Meta):
            app_label = "base"

    class Gadget(TenantModel):
        widget = models.ForeignKey(Widget, on_delete=models.CASCADE)

        class Meta(TenantModel.Meta):
            app_label = "base"

    return Organization, Widget, Gadget


def _model_for(view_class):
    """
    Best-effort static resolution of the model a view operates on.

    A view that builds its queryset only inside get_queryset() is invisible
    here -- that is the known limitation TENANCY_EXEMPT exists to make
    explicit rather than silent.
    """
    queryset = getattr(view_class, "queryset", None)
    if queryset is not None:
        return queryset.model

    serializer_class = getattr(view_class, "serializer_class", None)
    meta = getattr(serializer_class, "Meta", None)
    return getattr(meta, "model", None)


class TestTenancyConformance:
    def test_every_view_over_a_tenant_model_is_scoped(self):
        offenders = []

        for route, view_class in _iter_views(get_resolver().url_patterns):
            model = _model_for(view_class)
            if model is None or not issubclass(model, TenantModel):
                continue
            if view_class.__name__ in TENANCY_EXEMPT:
                continue
            if not issubclass(view_class, TenantViewSetMixin):
                offenders.append(f"{view_class.__name__} at /{route} ({model._meta.label})")

        assert not offenders, (
            "These views expose a TenantModel without inheriting "
            "TenantViewSetMixin, so they are not scoped to an organization:\n  "
            + "\n  ".join(offenders)
            + "\n\nAdd the mixin, or add the class name to TENANCY_EXEMPT with a reason."
        )

    def test_the_check_can_actually_fail(self):
        """
        Guards the guard. If _model_for or _iter_views silently stopped
        resolving anything, the conformance test above would pass vacuously
        forever.
        """
        models_found = {
            _model_for(view_class) for _, view_class in _iter_views(get_resolver().url_patterns)
        }
        tenant_models = {m for m in models_found if m is not None and issubclass(m, TenantModel)}

        assert tenant_models, (
            "The URLconf walker found no views over any TenantModel. Either the "
            "walker is broken or no such view is registered -- both make the "
            "conformance test meaningless."
        )


@pytest.mark.django_db
class TestCrossOrganizationIsolation:
    """
    Exercised through the Membership endpoint, which is a real TenantModel
    view. Every future tenant model rides the same chokepoint.
    """

    def test_list_returns_only_the_callers_organization(self, authed_client, owner, rival_owner):
        response = authed_client.get(reverse("users:membership-list"))

        assert response.status_code == 200
        returned_users = {row["id"] for row in response.json()["results"]}
        rival_membership_ids = {str(m.id) for m in rival_owner.memberships.all()}

        assert returned_users.isdisjoint(rival_membership_ids)
        assert len(returned_users) == 1

    def test_reading_another_organizations_record_returns_404_not_403(
        self, authed_client, rival_owner
    ):
        rival_membership = rival_owner.memberships.first()

        response = authed_client.get(reverse("users:membership-detail", args=[rival_membership.id]))

        # 403 would confirm the record exists. 404 tells them nothing.
        assert response.status_code == 404

    def test_a_user_with_no_membership_gets_nothing(self, api_client, user, organization):
        api_client.force_login(user)

        response = api_client.get(reverse("users:membership-list"))

        assert response.status_code == 403

    def test_a_deactivated_membership_loses_access(self, authed_client, owner):
        """Removing someone is `is_active = False`; the session may well outlive it."""
        from users.models import Membership

        assert authed_client.get(reverse("users:membership-list")).status_code == 200

        Membership.objects.filter(user=owner).update(is_active=False)

        assert authed_client.get(reverse("users:membership-list")).status_code == 403

    def test_anonymous_is_rejected(self, api_client, organization):
        assert api_client.get(reverse("users:membership-list")).status_code == 403


class TestCrossOrganizationForeignKeys:
    """
    Queryset scoping stops a caller *reading* another tenant's rows. It does
    not stop them referencing one by id in a write -- `TenantModel.save()`
    does, via `_check_tenant_consistency`.

    Phase 1 has no tenant model pointing at another tenant model, so these use
    synthetic models under `isolate_apps` (no tables, no database). Assigning
    the related *object* rather than its id populates the instance's field
    cache, which is the path the checker takes when it can avoid a query --
    so this exercises the real logic. Phase 2 onward
    (Job -> Customer -> ServiceLocation) depends on it.
    """

    @isolate_apps("base", "organizations")
    def test_tenant_foreign_keys_are_discovered(self):
        Organization, _, Gadget = _synthetic_models()

        field_names = [f.name for f in Gadget(organization=None)._tenant_foreign_keys()]

        # `organization` itself is excluded; the tenant-to-tenant FK is found.
        assert field_names == ["widget"]

    @isolate_apps("base", "organizations")
    def test_mismatched_organization_is_reported(self):
        Organization, Widget, Gadget = _synthetic_models()
        org_a, org_b = Organization(name="A"), Organization(name="B")

        gadget = Gadget(organization=org_a, widget=Widget(organization=org_b))
        errors = gadget._check_tenant_consistency()

        assert "widget" in errors
        assert "different organization" in errors["widget"]

    @isolate_apps("base", "organizations")
    def test_matching_organization_passes(self):
        Organization, Widget, Gadget = _synthetic_models()
        org = Organization(name="A")

        gadget = Gadget(organization=org, widget=Widget(organization=org))

        assert gadget._check_tenant_consistency() == {}

    @isolate_apps("base", "organizations")
    def test_save_raises_validation_error_on_mismatch(self):
        Organization, Widget, Gadget = _synthetic_models()
        org_a, org_b = Organization(name="A"), Organization(name="B")

        gadget = Gadget(organization=org_a, widget=Widget(organization=org_b))

        with pytest.raises(ValidationError) as excinfo:
            gadget.save()

        assert "widget" in excinfo.value.message_dict

    @isolate_apps("base", "organizations")
    def test_clean_raises_validation_error_on_mismatch(self):
        Organization, Widget, Gadget = _synthetic_models()
        org_a, org_b = Organization(name="A"), Organization(name="B")

        gadget = Gadget(organization=org_a, widget=Widget(organization=org_b))

        with pytest.raises(ValidationError):
            gadget.clean()

    @isolate_apps("base", "organizations")
    def test_null_foreign_key_is_ignored(self):
        Organization, _, Gadget = _synthetic_models()

        gadget = Gadget(organization=Organization(name="A"))

        assert gadget._check_tenant_consistency() == {}

    @isolate_apps("base", "organizations")
    def test_the_check_can_be_disabled_per_model(self):
        Organization, Widget, Gadget = _synthetic_models()
        Gadget.validate_tenant_consistency = False
        org_a, org_b = Organization(name="A"), Organization(name="B")

        gadget = Gadget(organization=org_a, widget=Widget(organization=org_b))

        # save() skips the guard, but the checker itself still sees the problem.
        assert gadget._check_tenant_consistency() != {}
