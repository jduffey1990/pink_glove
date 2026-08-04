"""
Abstract model foundations.

Every concrete model in this project inherits `Base`. Models that hold
tenant-owned data inherit `TenantModel` instead (added in Phase 1), which layers
an organization FK on top.
"""

import uuid

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
