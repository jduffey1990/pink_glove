import pytest
from django.contrib.auth import get_user_model

User = get_user_model()


@pytest.mark.django_db
class TestCustomUserManager:
    def test_email_is_lowercased(self):
        user = User.objects.create_user(email="MiXeD@Example.COM", password="pw-for-tests-only")
        assert user.email == "mixed@example.com"

    def test_email_is_required(self):
        with pytest.raises(ValueError, match="email"):
            User.objects.create_user(email="", password="pw-for-tests-only")

    def test_user_without_password_cannot_authenticate_by_password(self):
        """Customers sign in by magic link (Phase 1) and never set a password."""
        user = User.objects.create_user(email="customer@example.com")

        assert user.has_usable_password() is False

    def test_create_superuser_sets_flags(self):
        user = User.objects.create_superuser(email="root@example.com", password="pw-for-tests-only")

        assert user.is_staff and user.is_superuser and user.is_active

    def test_create_superuser_requires_a_password(self):
        with pytest.raises(ValueError, match="password"):
            User.objects.create_superuser(email="root@example.com", password="")

    def test_create_superuser_rejects_non_staff(self):
        with pytest.raises(ValueError, match="is_staff"):
            User.objects.create_superuser(
                email="root@example.com", password="pw-for-tests-only", is_staff=False
            )


@pytest.mark.django_db
class TestCustomUser:
    def test_uses_uuid_primary_key(self, user):
        import uuid

        assert isinstance(user.pk, uuid.UUID)

    def test_full_name(self, user):
        assert user.full_name == "Pat Rivera"

    def test_full_name_falls_back_to_email(self):
        user = User.objects.create_user(email="noname@example.com", password="pw-for-tests-only")
        assert user.full_name == "noname@example.com"

    def test_has_no_organization_or_role_field(self):
        """
        Organization membership and role live on Membership (Phase 1), not
        here. See docs/DECISIONS.md ADR-003. If this test fails, someone has
        reintroduced the single-org coupling it exists to prevent.
        """
        field_names = {f.name for f in User._meta.get_fields()}

        assert "organization" not in field_names
        assert "role" not in field_names
