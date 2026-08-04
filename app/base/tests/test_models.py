"""Tests for the soft-delete foundation in `base.models`."""

import pytest
from django.apps import apps

from base.models import Base


class TestBaseMetaConformance:
    """
    `Base.Meta` carries `base_manager_name` and `default_manager_name`, and a
    subclass that declares its own `Meta` without inheriting `Base.Meta`
    silently loses both. Losing them breaks related-object traversal for
    soft-deleted rows and can flip `_default_manager` to the unfiltered
    manager. Catch it here rather than in production.
    """

    def test_every_concrete_subclass_inherits_base_meta(self):
        offenders = []
        for model in apps.get_models():
            if not issubclass(model, Base) or model._meta.abstract:
                continue
            if model._meta.base_manager_name != "all_objects":
                offenders.append(f"{model._meta.label}: base_manager_name")
            if model._meta.default_manager_name != "objects":
                offenders.append(f"{model._meta.label}: default_manager_name")

        assert not offenders, (
            "These models declare a Meta that does not inherit Base.Meta. "
            "Use `class Meta(Base.Meta):`. Offenders: " + ", ".join(offenders)
        )


@pytest.mark.django_db
class TestSoftDelete:
    """Uses CustomUser as the concrete stand-in for any Base subclass."""

    @pytest.fixture
    def model(self):
        from users.models import CustomUser

        return CustomUser

    def test_delete_stamps_deleted_at_and_keeps_the_row(self, model, user):
        user.delete()

        assert user.deleted_at is not None
        assert model.all_objects.filter(pk=user.pk).exists()

    def test_default_manager_hides_deleted_rows(self, model, user):
        user.delete()

        assert not model.objects.filter(pk=user.pk).exists()
        assert model.all_objects.filter(pk=user.pk).exists()

    def test_queryset_delete_is_also_soft(self, model, user):
        model.objects.filter(pk=user.pk).delete()

        user.refresh_from_db()
        assert user.deleted_at is not None

    def test_hard_delete_actually_removes_the_row(self, model, user):
        user.hard_delete()

        assert not model.all_objects.filter(pk=user.pk).exists()

    def test_undelete_restores_visibility(self, model, user):
        user.delete()
        user.undelete()

        assert user.deleted_at is None
        assert model.objects.filter(pk=user.pk).exists()

    def test_alive_and_dead_partition_the_rows(self, model, user):
        other = model.objects.create_user(email="two@example.com", password="pw-for-tests-only")
        user.delete()

        assert list(model.all_objects.dead()) == [user]
        assert list(model.all_objects.alive()) == [other]

    def test_is_deleted_property(self, user):
        assert user.is_deleted is False
        user.delete()
        assert user.is_deleted is True
