import base64
import hashlib
from urllib.parse import parse_qs, urlparse

from django.contrib.auth.backends import AllowAllUsersModelBackend, ModelBackend
from django.contrib.auth.models import User
from django.test import override_settings

import pytest

from ..constants import TEST_OTP
from ..models import Organization


@override_settings(
    AUTHENTICATION_BACKENDS=[
        f"{ModelBackend.__module__}.{ModelBackend.__name__}",
        f"{AllowAllUsersModelBackend.__module__}.{AllowAllUsersModelBackend.__name__}",
    ]
)
@pytest.mark.django_db
def test_email_login_works_with_multiple_authentication_backends(expect, client):
    Organization.objects.create(email_domain="example.com")

    client.post("/accounts/login/", {"email": "test@example.com"})
    response = client.post("/accounts/verify/", {"otp": TEST_OTP}, follow=True)

    expect(response.status_code) == 200
    expect(response.content.decode("utf-8")).contains("Logout")


@pytest.mark.django_db
def test_admin_password_login_uses_model_backend(expect, client):
    user = User.objects.create_superuser(
        username="admin", email="admin@example.com", password="password"
    )

    response = client.post(
        "/admin/login/?next=/admin/",
        {"username": user.username, "password": "password"},
    )

    expect(response.status_code) == 302
    expect(response["Location"]) == "/admin/"
    expect(client.session["_auth_user_id"]) == str(user.id)


@pytest.mark.django_db
def test_inactive_oidc_users_are_removed_from_their_session(expect, client):
    user = User.objects.create_user(username="inactive", email="inactive@example.com")
    client.force_login(user, backend="tab.core.auth.AuthentikOIDCBackend")
    user.is_active = False
    user.save(update_fields=["is_active"])

    response = client.get("/projects/")

    expect(response.status_code) == 302
    expect(response["Location"]).contains("/accounts/login/")
    expect(client.session.get("_auth_user_id")) is None


@pytest.mark.django_db
def test_expired_oidc_sessions_are_reauthorized_with_authentik(expect, client):
    user = User.objects.create_user(username="oidc", email="oidc@example.com")
    client.force_login(user, backend="tab.core.auth.AuthentikOIDCBackend")
    session = client.session
    session["oidc_id_token_expiration"] = 0
    session.save()

    response = client.get("/projects/")

    expect(response.status_code) == 302
    expect(response["Location"]).contains("https://auth.corp.zoo.dev/")
    expect(response["Location"]).contains("prompt=none")
    query = parse_qs(urlparse(response["Location"]).query)
    state = query["state"][0]
    verifier = client.session["oidc_states"][state]["code_verifier"]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=")
    expect(query["code_challenge"]) == [challenge.decode()]
    expect(query["code_challenge_method"]) == ["S256"]


@pytest.mark.django_db
def test_expired_oidc_sessions_cannot_make_post_requests(expect, client):
    user = User.objects.create_user(username="oidc", email="oidc@example.com")
    client.force_login(user, backend="tab.core.auth.AuthentikOIDCBackend")
    session = client.session
    session["oidc_id_token_expiration"] = 0
    session.save()

    response = client.post("/projects/missing")

    expect(response.status_code) == 401
    expect(response.content.decode()) == "OIDC session expired. Please sign in again."
    expect(client.session.get("_auth_user_id")) is None


@pytest.mark.django_db
def test_denied_oidc_refresh_logs_the_user_out(expect, client):
    user = User.objects.create_user(username="oidc", email="oidc@example.com")
    client.force_login(user, backend="tab.core.auth.AuthentikOIDCBackend")
    session = client.session
    session["oidc_id_token_expiration"] = 0
    session.save()

    refresh_response = client.get("/projects/")
    state = parse_qs(urlparse(refresh_response["Location"]).query)["state"][0]
    callback_response = client.get(
        "/oidc/callback/",
        {"error": "login_required", "state": state},
        follow=True,
    )

    expect(callback_response.status_code) == 200
    expect(callback_response.content.decode()).contains(
        "Your Authentik session expired."
    )
    expect(client.session.get("_auth_user_id")) is None


@pytest.mark.django_db
def test_denied_oidc_refresh_returns_to_the_original_page(expect, client):
    user = User.objects.create_user(username="oidc", email="oidc@example.com")
    client.force_login(user, backend="tab.core.auth.AuthentikOIDCBackend")
    session = client.session
    session["oidc_id_token_expiration"] = 0
    session.save()

    refresh_response = client.get("/releases/")
    state = parse_qs(urlparse(refresh_response["Location"]).query)["state"][0]
    callback_response = client.get(
        "/oidc/callback/",
        {"error": "login_required", "state": state},
        follow=True,
    )

    expect(
        callback_response.redirect_chain[-1][0]
    ) == "/accounts/login/?next=%2Freleases%2F"
    expect(callback_response.content.decode()).contains(
        "/oidc/authenticate/?next=/releases/"
    )


@pytest.mark.django_db
def test_expired_oidc_sessions_can_log_out(expect, client):
    user = User.objects.create_user(username="oidc", email="oidc@example.com")
    client.force_login(user, backend="tab.core.auth.AuthentikOIDCBackend")
    session = client.session
    session["oidc_id_token_expiration"] = 0
    session.save()

    response = client.get("/accounts/logout/")

    expect(response.status_code) == 302
    expect(response["Location"]) == "/"
    expect(client.session.get("_auth_user_id")) is None
