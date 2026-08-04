"""
Abstract model foundations.

Every concrete model in this project inherits `Base`. Models that hold
tenant-owned data inherit `TenantModel` instead (added in Phase 1), which layers
an organization FK on top.
"""

import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class SoftDeleteQuerySet(models.QuerySet):
    """Queryset whose `.delete()` stamps `deleted_at` instead of issuing DELETE."""

    def alive(self):
        return self.filter(deleted_at__isnull=True)

    def dead(self):
        return self.filter(deleted_at__isnull=False)

    def delete(self):
        return self.update(deleted_at=timezone.now())

    def hard_delete(self):
        """Actually remove the rows. Deliberately not the default."""
        return super().delete()

    def undelete(self):
        return self.update(deleted_at=None)


class AllObjectsManager(models.Manager.from_queryset(SoftDeleteQuerySet)):
    """
    Unfiltered manager -- soft-deleted rows included. Also serves as
    `base_manager_name`, which is what related-object traversal uses.
    """


class SoftDeleteManager(AllObjectsManager):
    """Default manager. Soft-deleted rows are invisible unless you ask for them."""

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class Base(models.Model):
    """
    UUID primary key plus audit timestamps.

    UUIDs rather than sequential integers because job and invoice IDs end up in
    URLs that customers can see, and sequential IDs leak business volume.

    NOTE: concrete subclasses that declare their own `Meta` must inherit this
    one (`class Meta(Base.Meta):`) so that `base_manager_name` survives.
    `base.tests.test_models` enforces this across every concrete subclass.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, editable=False)
    deleted_at = models.DateTimeField(null=True, blank=True, editable=False, db_index=True)

    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()

    class Meta:
        abstract = True
        # Related-object traversal and cascade collection must see soft-deleted
        # rows, otherwise a soft-deleted FK target raises DoesNotExist.
        base_manager_name = "all_objects"
        # Managers inherited from an abstract base sort ahead of ones declared
        # on the concrete model, so `_default_manager` would otherwise resolve
        # to `all_objects` on any model that redefines `objects`.
        default_manager_name = "objects"

    def delete(self, using=None, keep_parents=False):
        self.deleted_at = timezone.now()
        self.save(using=using, update_fields=["deleted_at", "updated_at"])

    def hard_delete(self, using=None, keep_parents=False):
        return super().delete(using=using, keep_parents=keep_parents)

    def undelete(self):
        self.deleted_at = None
        self.save(update_fields=["deleted_at", "updated_at"])

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class TenantModel(Base):
    """
    Base for anything owned by one organization.

    Scoping to the current tenant happens at the viewset chokepoint
    (`base.viewsets.TenantViewSetMixin`), NOT here -- this model's manager is
    deliberately unscoped so migrations, the admin, management commands, and
    related-object traversal all keep working. See docs/DECISIONS.md ADR-002
    before changing that.

    What this class *does* enforce is referential consistency: a record may not
    point at a record belonging to a different organization.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="+",
        db_index=True,
    )

    #: Set False on a model where the cross-organization FK check is not worth
    #: the extra queries per save (bulk ingest paths, for example).
    validate_tenant_consistency = True

    class Meta(Base.Meta):
        abstract = True
        indexes = [
            models.Index(fields=["organization", "created_at"]),
        ]

    def save(self, *args, **kwargs):
        # Called here as well as in clean() because DRF serializers do not run
        # full_clean(), and this guard is worthless if the API path skips it.
        if self.validate_tenant_consistency:
            errors = self._check_tenant_consistency()
            if errors:
                raise ValidationError(errors)
        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        errors = self._check_tenant_consistency()
        if errors:
            raise ValidationError(errors)

    def _tenant_foreign_keys(self):
        for field in self._meta.concrete_fields:
            if not field.many_to_one or field.name == "organization":
                continue
            related_model = field.related_model
            if isinstance(related_model, type) and issubclass(related_model, TenantModel):
                yield field

    def _check_tenant_consistency(self) -> dict[str, str]:
        """
        Return {field_name: message} for any FK pointing outside this record's
        organization.

        Viewset scoping stops a user reading another tenant's rows, but it does
        not stop them *referencing* one by id in a write. This does.

        Cost: one narrow indexed query per tenant FK whose target isn't already
        cached on the instance.
        """
        errors: dict[str, str] = {}
        if self.organization_id is None:
            return errors

        for field in self._tenant_foreign_keys():
            related_id = getattr(self, field.attname)
            if related_id is None:
                continue

            cached = self._state.fields_cache.get(field.name)
            if cached is not None:
                related_org_id = cached.organization_id
            else:
                related_org_id = (
                    field.related_model.all_objects.filter(pk=related_id)
                    .values_list("organization_id", flat=True)
                    .first()
                )

            if related_org_id is not None and related_org_id != self.organization_id:
                errors[field.name] = (
                    f"{field.related_model._meta.verbose_name} belongs to a different organization."
                )

        return errors
