"""Shared pytest fixtures."""

import pytest
from rest_framework.test import APIClient


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def user(db):
    from users.models import CustomUser

    return CustomUser.objects.create_user(
        email="owner@example.com",
        password="pw-for-tests-only",
        first_name="Pat",
        last_name="Rivera",
    )


@pytest.fixture
def superuser(db):
    from users.models import CustomUser

    return CustomUser.objects.create_superuser(
        email="admin@example.com",
        password="pw-for-tests-only",
    )


@pytest.fixture
def authed_client(api_client, user):
    api_client.force_authenticate(user=user)
    return api_client
