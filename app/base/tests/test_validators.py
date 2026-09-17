import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

from base.validators import MaxFileSize


class TestMaxFileSize:
    def test_a_file_at_the_limit_passes(self):
        MaxFileSize(1)(SimpleUploadedFile("ok.png", b"x" * 1024 * 1024))

    def test_a_file_over_the_limit_is_refused_with_both_numbers(self):
        with pytest.raises(ValidationError) as caught:
            MaxFileSize(1)(SimpleUploadedFile("big.png", b"x" * (1024 * 1024 + 1)))

        assert "1.0 MB" in caught.value.messages[0]
        assert "limit is 1 MB" in caught.value.messages[0]

    def test_the_real_limits_are_on_the_fields(self):
        """So a migration squash or a field rewrite cannot quietly drop them."""
        from organizations.models import Organization
        from scheduling.models import JobPhoto

        assert MaxFileSize(10) in JobPhoto._meta.get_field("image").validators
        assert MaxFileSize(2) in Organization._meta.get_field("logo").validators


class TestCloudMediaIsPrivate:
    """
    Job photos are the inside of people's homes. Privacy must not depend on
    whoever creates the bucket remembering to lock it.
    """

    def test_s3_writes_private_and_reads_through_expiring_urls(self):
        from app.settings import base

        options = base._MEDIA_BACKENDS["s3"]["OPTIONS"]

        assert options["default_acl"] == "private"
        assert options["querystring_auth"] is True
        assert options["querystring_expire"] <= 900
        assert options["file_overwrite"] is False

    def test_gcs_reads_through_expiring_urls(self):
        from app.settings import base

        options = base._MEDIA_BACKENDS["gcs"]["OPTIONS"]

        assert options["querystring_auth"] is True
        assert options["expiration"].total_seconds() <= 900
        assert options["file_overwrite"] is False
