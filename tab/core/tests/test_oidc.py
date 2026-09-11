import base64
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from urllib.parse import parse_qs, urlparse

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import SuspiciousOperation
from django.db import close_old_connections
from django.test import override_settings

import pytest

from ..auth import AuthentikOIDCBackend
from ..models import OIDCIdentity, Organization
from .oidc_provider import LocalOIDCProvider


def describe_authentik_login(expect, client):
    @pytest.fixture
    def organization():
        return Organization.objects.create(email_domain="example.com")

    @pytest.fixture
    def oidc_provider():
        provider = LocalOIDCProvider()
        yield provider
        provider.close()

    def it_is_available_from_the_login_page():
        response = client.get("/accounts/login/?next=/projects/")
        expect(response.status_code) == 200

        html = response.content.decode("utf-8")
        expect(html).contains("Single Sign-On")
        expect(html).contains("Continue with Authentik")
        expect(html).contains("/oidc/authenticate/?next=/projects/")

    @pytest.mark.django_db
    def it_starts_a_pkce_authorization_code_flow():
        response = client.get("/oidc/authenticate/?next=/projects/")
        expect(response.status_code) == 302

        authorization_url = urlparse(response["Location"])
        query = parse_qs(authorization_url.query)
        expect(authorization_url.scheme) == "https"
        expect(authorization_url.netloc) == "auth.corp.zoo.dev"
        expect(authorization_url.path) == "/application/o/authorize/"
        expect(query["response_type"]) == ["code"]
        expect(query["scope"]) == ["openid email"]
        expect(query["client_id"]) == [settings.OIDC_RP_CLIENT_ID]
        expect(query["redirect_uri"]) == ["http://testserver/oidc/callback/"]
        expect(query["code_challenge_method"]) == ["S256"]
        state = query["state"][0]
        verifier = client.session["oidc_states"][state]["code_verifier"]
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).rstrip(b"=")
        expect(query["code_challenge"]) == [challenge.decode()]
        expect(query["nonce"][0])
        expect(state)
        expect(client.session["oidc_login_next"]) == "/projects/"

    @override_settings(
        ALLOWED_HOSTS=[
            "test-analysis-bot.corp.zoo.dev",
            "test-analysis-bot.hawk-dinosaur.ts.net",
        ],
        SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
    )
    @pytest.mark.django_db
    def it_uses_the_public_https_callback_behind_the_proxy():
        response = client.get(
            "/oidc/authenticate/",
            HTTP_HOST="test-analysis-bot.corp.zoo.dev",
            HTTP_X_FORWARDED_PROTO="https",
        )

        query = parse_qs(urlparse(response["Location"]).query)
        expect(query["redirect_uri"]) == [
            "https://test-analysis-bot.corp.zoo.dev/oidc/callback/"
        ]

    @pytest.mark.parametrize(
        "next_url", ["https://attacker.example/", "//attacker.example/"]
    )
    @pytest.mark.django_db
    def it_rejects_external_oidc_next_urls(next_url):
        response = client.get("/oidc/authenticate/", {"next": next_url})

        expect(response.status_code) == 302
        expect(client.session["oidc_login_next"]) is None

    @pytest.mark.django_db
    def it_completes_a_signed_oidc_callback(
        organization, oidc_provider: LocalOIDCProvider
    ):
        existing_user = User.objects.create_user(
            username="person", email=oidc_provider.email, is_staff=True
        )

        with override_settings(**oidc_provider.django_settings):
            authorization_response = client.get("/oidc/authenticate/?next=/projects/")
            query = oidc_provider.capture_authorization(
                authorization_response["Location"]
            )

            callback_response = client.get(
                "/oidc/callback/",
                {"code": "valid-code", "state": query["state"][0]},
            )

        expect(callback_response.status_code) == 302
        expect(callback_response["Location"]) == "/projects/"
        expect(client.session["_auth_user_id"]) == str(existing_user.id)
        expect(User.objects.get(pk=existing_user.id).is_staff) is True
        expect(
            OIDCIdentity.objects.get(
                issuer=oidc_provider.issuer,
                subject=oidc_provider.id_token_subject,
            ).user_id
        ) == existing_user.id
        expect(oidc_provider.token_request["code_verifier"])

    @pytest.mark.django_db
    def it_does_not_follow_an_external_next_after_callback(
        organization, oidc_provider: LocalOIDCProvider
    ):
        with override_settings(**oidc_provider.django_settings):
            authorization_response = client.get(
                "/oidc/authenticate/", {"next": "https://attacker.example/"}
            )
            query = oidc_provider.capture_authorization(
                authorization_response["Location"]
            )
            callback_response = client.get(
                "/oidc/callback/",
                {"code": "valid-code", "state": query["state"][0]},
            )

        expect(callback_response.status_code) == 302
        expect(callback_response["Location"]) == "/"
        expect(client.session["_auth_user_id"])

    @pytest.mark.django_db
    def it_rejects_mismatched_userinfo_in_a_real_callback(
        organization, oidc_provider: LocalOIDCProvider
    ):
        oidc_provider.userinfo_subject = "different-user"

        with override_settings(**oidc_provider.django_settings):
            authorization_response = client.get("/oidc/authenticate/")
            query = oidc_provider.capture_authorization(
                authorization_response["Location"]
            )
            callback_response = client.get(
                "/oidc/callback/",
                {"code": "valid-code", "state": query["state"][0]},
                follow=True,
            )

        expect(callback_response.status_code) == 200
        expect(callback_response.content.decode()).contains(
            "Unable to verify Authentik&#x27;s response."
        )
        expect(User.objects.filter(email=oidc_provider.email).exists()) is False
        expect(OIDCIdentity.objects.exists()) is False

    @pytest.mark.django_db
    def it_rejects_unknown_domains_in_a_real_callback(
        oidc_provider: LocalOIDCProvider,
    ):
        oidc_provider.email = "person@unknown.example"

        with override_settings(**oidc_provider.django_settings):
            authorization_response = client.get("/oidc/authenticate/")
            query = oidc_provider.capture_authorization(
                authorization_response["Location"]
            )
            callback_response = client.get(
                "/oidc/callback/",
                {"code": "valid-code", "state": query["state"][0]},
                follow=True,
            )

        expect(callback_response.status_code) == 200
        expect(callback_response.content.decode()).contains(
            "Your Authentik email domain is not configured."
        )
        expect(User.objects.exists()) is False
        expect(OIDCIdentity.objects.exists()) is False

    @pytest.mark.django_db
    def it_accepts_an_exact_email_address_in_a_real_callback(
        oidc_provider: LocalOIDCProvider,
    ):
        Organization.objects.create(email_domain="jacebrowning@gmail.com")
        oidc_provider.email = "jacebrowning@gmail.com"

        with override_settings(**oidc_provider.django_settings):
            authorization_response = client.get("/oidc/authenticate/")
            query = oidc_provider.capture_authorization(
                authorization_response["Location"]
            )
            callback_response = client.get(
                "/oidc/callback/",
                {"code": "valid-code", "state": query["state"][0]},
            )

        expect(callback_response.status_code) == 302
        expect(User.objects.filter(email="jacebrowning@gmail.com").exists()) is True

    @pytest.mark.django_db
    def it_rejects_other_gmail_addresses_when_only_an_exact_email_is_configured(
        oidc_provider: LocalOIDCProvider,
    ):
        Organization.objects.create(email_domain="jacebrowning@gmail.com")
        oidc_provider.email = "other@gmail.com"

        with override_settings(**oidc_provider.django_settings):
            authorization_response = client.get("/oidc/authenticate/")
            query = oidc_provider.capture_authorization(
                authorization_response["Location"]
            )
            callback_response = client.get(
                "/oidc/callback/",
                {"code": "valid-code", "state": query["state"][0]},
                follow=True,
            )

        expect(callback_response.status_code) == 200
        expect(callback_response.content.decode()).contains(
            "Your Authentik email domain is not configured."
        )
        expect(User.objects.exists()) is False
        expect(OIDCIdentity.objects.exists()) is False

    @pytest.mark.django_db
    def it_explains_unverified_email_claims(
        organization, oidc_provider: LocalOIDCProvider
    ):
        oidc_provider.email_verified = False

        with override_settings(**oidc_provider.django_settings):
            authorization_response = client.get("/oidc/authenticate/")
            query = oidc_provider.capture_authorization(
                authorization_response["Location"]
            )
            callback_response = client.get(
                "/oidc/callback/",
                {"code": "valid-code", "state": query["state"][0]},
                follow=True,
            )

        expect(callback_response.status_code) == 200
        expect(callback_response.content.decode()).contains(
            "Authentik could not verify your email address."
        )
        expect(User.objects.exists()) is False
        expect(OIDCIdentity.objects.exists()) is False

    @pytest.mark.django_db
    def it_explains_invalid_email_claims(
        organization, oidc_provider: LocalOIDCProvider
    ):
        oidc_provider.email = "not-an-email"

        with override_settings(**oidc_provider.django_settings):
            authorization_response = client.get("/oidc/authenticate/")
            query = oidc_provider.capture_authorization(
                authorization_response["Location"]
            )
            callback_response = client.get(
                "/oidc/callback/",
                {"code": "valid-code", "state": query["state"][0]},
                follow=True,
            )

        expect(callback_response.status_code) == 200
        expect(callback_response.content.decode()).contains(
            "Authentik did not provide a valid email address."
        )
        expect(User.objects.exists()) is False
        expect(OIDCIdentity.objects.exists()) is False

    @pytest.mark.django_db
    def it_rejects_tokens_for_another_client(
        organization, oidc_provider: LocalOIDCProvider
    ):
        oidc_provider.id_token_claims["aud"] = "another-client"

        with override_settings(**oidc_provider.django_settings):
            authorization_response = client.get("/oidc/authenticate/")
            query = oidc_provider.capture_authorization(
                authorization_response["Location"]
            )
            callback_response = client.get(
                "/oidc/callback/",
                {"code": "valid-code", "state": query["state"][0]},
                follow=True,
            )

        expect(callback_response.status_code) == 200
        expect(callback_response.content.decode()).contains(
            "Unable to verify Authentik&#x27;s response."
        )
        expect(User.objects.exists()) is False
        expect(OIDCIdentity.objects.exists()) is False

    @pytest.mark.django_db
    def it_explains_expired_identity_tokens(
        organization, oidc_provider: LocalOIDCProvider
    ):
        oidc_provider.id_token_claims["exp"] = int(time.time()) - 1

        with override_settings(**oidc_provider.django_settings):
            authorization_response = client.get("/oidc/authenticate/")
            query = oidc_provider.capture_authorization(
                authorization_response["Location"]
            )
            callback_response = client.get(
                "/oidc/callback/",
                {"code": "valid-code", "state": query["state"][0]},
                follow=True,
            )

        expect(callback_response.status_code) == 200
        expect(callback_response.content.decode()).contains(
            "Unable to verify Authentik&#x27;s response."
        )
        expect(User.objects.exists()) is False
        expect(OIDCIdentity.objects.exists()) is False

    @pytest.mark.django_db
    def it_handles_token_exchange_errors(
        organization, oidc_provider: LocalOIDCProvider
    ):
        oidc_provider.token_error = "invalid_client"

        with override_settings(**oidc_provider.django_settings):
            authorization_response = client.get("/oidc/authenticate/")
            query = oidc_provider.capture_authorization(
                authorization_response["Location"]
            )
            callback_response = client.get(
                "/oidc/callback/",
                {"code": "valid-code", "state": query["state"][0]},
                follow=True,
            )

        expect(callback_response.status_code) == 200
        expect(callback_response.content.decode()).contains(
            "Unable to complete sign-in with Authentik."
        )
        expect(User.objects.exists()) is False
        expect(OIDCIdentity.objects.exists()) is False

    @pytest.mark.django_db
    def it_explains_access_denied_by_authentik():
        authorization_response = client.get("/oidc/authenticate/")
        query = parse_qs(urlparse(authorization_response["Location"]).query)

        callback_response = client.get(
            "/oidc/callback/",
            {"error": "access_denied", "state": query["state"][0]},
            follow=True,
        )

        expect(callback_response.status_code) == 200
        expect(callback_response.content.decode()).contains("Authentik denied access.")

    @pytest.mark.django_db
    def it_explains_expired_authentik_sessions():
        authorization_response = client.get("/oidc/authenticate/")
        query = parse_qs(urlparse(authorization_response["Location"]).query)

        callback_response = client.get(
            "/oidc/callback/",
            {"error": "login_required", "state": query["state"][0]},
            follow=True,
        )

        expect(callback_response.status_code) == 200
        expect(callback_response.content.decode()).contains(
            "Your Authentik session expired."
        )

    @pytest.mark.django_db
    def it_distinguishes_provider_errors_from_access_denials():
        authorization_response = client.get("/oidc/authenticate/")
        query = parse_qs(urlparse(authorization_response["Location"]).query)

        callback_response = client.get(
            "/oidc/callback/",
            {"error": "server_error", "state": query["state"][0]},
            follow=True,
        )

        expect(callback_response.status_code) == 200
        html = callback_response.content.decode()
        expect(html).contains("Unable to complete sign-in with Authentik.")
        expect(html).does_not_contain("Authentik denied access.")
        expect(html).does_not_contain("Your Authentik session expired.")

    @pytest.mark.django_db
    def it_sends_unknown_callback_state_to_login():
        client.get("/oidc/authenticate/")

        callback_response = client.get(
            "/oidc/callback/",
            {"code": "valid-code", "state": "tampered"},
            follow=True,
        )

        expect(callback_response.status_code) == 200
        expect(callback_response.content.decode()).contains(
            "Your Authentik session expired."
        )
        expect(client.session.get("_auth_user_id")) is None

    @pytest.mark.django_db
    def it_ignores_a_stale_callback_when_the_oidc_session_is_still_valid():
        user = User.objects.create_user(username="person", email="person@example.com")
        client.force_login(user, backend="tab.core.auth.AuthentikOIDCBackend")
        session = client.session
        session["oidc_id_token_expiration"] = time.time() + 60
        session["oidc_login_next"] = "/projects/"
        session.save()

        callback_response = client.get(
            "/oidc/callback/",
            {"code": "valid-code", "state": "stale"},
        )

        expect(callback_response.status_code) == 302
        expect(callback_response["Location"]) == "/projects/"
        expect(client.session["_auth_user_id"]) == str(user.id)

    @pytest.mark.django_db
    def it_sends_an_expired_oidc_session_to_login_when_callback_state_is_missing():
        user = User.objects.create_user(username="person", email="person@example.com")
        client.force_login(user, backend="tab.core.auth.AuthentikOIDCBackend")
        session = client.session
        session["oidc_id_token_expiration"] = 0
        session.save()

        callback_response = client.get(
            "/oidc/callback/",
            {"code": "valid-code", "state": "stale"},
            follow=True,
        )

        expect(callback_response.status_code) == 200
        expect(callback_response.content.decode()).contains(
            "Your Authentik session expired."
        )
        expect(client.session.get("_auth_user_id")) is None

    @pytest.mark.django_db
    def it_rejects_a_callback_without_a_result_and_preserves_the_session():
        user = User.objects.create_user(username="person", email="person@example.com")
        client.force_login(user, backend="tab.core.auth.AuthentikOIDCBackend")

        callback_response = client.get("/oidc/callback/")

        expect(callback_response.status_code) == 400
        expect(client.session["_auth_user_id"]) == str(user.id)

    @pytest.mark.parametrize(
        "callback_params",
        [
            {"error": "access_denied"},
            {"error": "access_denied", "state": "tampered"},
            {"code": "valid-code", "error": "server_error", "state": "tampered"},
        ],
    )
    @pytest.mark.django_db
    def it_rejects_provider_errors_without_a_valid_state(callback_params):
        user = User.objects.create_user(username="person", email="person@example.com")
        client.force_login(
            user,
            backend="django.contrib.auth.backends.ModelBackend",
        )
        client.get("/oidc/authenticate/")

        callback_response = client.get("/oidc/callback/", callback_params)

        expect(callback_response.status_code) == 302
        expect(callback_response["Location"]) == "/accounts/login/"
        expect(client.session["_auth_user_id"]) == str(user.id)

    @pytest.mark.django_db
    def it_rejects_a_second_subject_for_an_existing_identity(
        organization, oidc_provider: LocalOIDCProvider
    ):
        user = User.objects.create_user(
            username="person", email=oidc_provider.email, is_staff=True
        )
        OIDCIdentity.objects.create(
            user=user,
            issuer=oidc_provider.issuer,
            subject="original-subject",
        )
        oidc_provider.id_token_subject = "replacement-subject"
        oidc_provider.userinfo_subject = "replacement-subject"

        with override_settings(**oidc_provider.django_settings):
            authorization_response = client.get("/oidc/authenticate/")
            query = oidc_provider.capture_authorization(
                authorization_response["Location"]
            )
            callback_response = client.get(
                "/oidc/callback/",
                {"code": "valid-code", "state": query["state"][0]},
                follow=True,
            )

        expect(callback_response.status_code) == 200
        expect(callback_response.content.decode()).contains(
            "This Authentik identity conflicts with an existing account."
        )
        expect(client.session.get("_auth_user_id")) is None
        expect(list(OIDCIdentity.objects.values_list("subject", flat=True))) == [
            "original-subject"
        ]

    @pytest.mark.django_db(transaction=True)
    def it_serializes_concurrent_email_bootstrap(organization):
        barrier = Barrier(2)

        def create_user(subject: str) -> int | None:
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                user = AuthentikOIDCBackend().create_user(
                    {
                        "sub": subject,
                        "email": "person@example.com",
                        "email_verified": True,
                    }
                )
                return user.id
            except SuspiciousOperation:
                return None
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            user_ids = list(
                executor.map(create_user, ["first-subject", "second-subject"])
            )

        expect(sum(user_id is not None for user_id in user_ids)) == 1
        expect(User.objects.filter(email__iexact="person@example.com").count()) == 1
        expect(OIDCIdentity.objects.count()) == 1

    @pytest.mark.django_db
    def it_silently_refreshes_an_existing_session(
        organization, oidc_provider: LocalOIDCProvider
    ):
        user = User.objects.create_user(
            username="person", email=oidc_provider.email, is_staff=True
        )
        identity = OIDCIdentity.objects.create(
            user=user,
            issuer=oidc_provider.issuer,
            subject=oidc_provider.id_token_subject,
        )

        with override_settings(**oidc_provider.django_settings):
            client.force_login(user, backend="tab.core.auth.AuthentikOIDCBackend")
            session = client.session
            session["oidc_id_token_expiration"] = 0
            session.save()

            refresh_response = client.get("/projects/")
            query = oidc_provider.capture_authorization(refresh_response["Location"])
            callback_response = client.get(
                "/oidc/callback/",
                {"code": "valid-code", "state": query["state"][0]},
            )

        expect(query["prompt"]) == ["none"]
        expect(callback_response.status_code) == 302
        expect(callback_response["Location"]) == "/projects/"
        expect(client.session["_auth_user_id"]) == str(user.id)
        expect(client.session["oidc_id_token_expiration"]) > time.time()
        expect(User.objects.get(pk=user.id).is_staff) is True
        expect(OIDCIdentity.objects.get(pk=identity.id).user_id) == user.id
        expect(OIDCIdentity.objects.count()) == 1

    @pytest.mark.django_db
    def it_logs_out_after_a_failed_silent_refresh(
        organization, oidc_provider: LocalOIDCProvider
    ):
        user = User.objects.create_user(
            username="person", email=oidc_provider.email, is_staff=True
        )
        OIDCIdentity.objects.create(
            user=user,
            issuer=oidc_provider.issuer,
            subject=oidc_provider.id_token_subject,
        )
        oidc_provider.token_error = "login_required"

        with override_settings(**oidc_provider.django_settings):
            client.force_login(user, backend="tab.core.auth.AuthentikOIDCBackend")
            session = client.session
            session["oidc_id_token_expiration"] = 0
            session.save()

            refresh_response = client.get("/projects/")
            query = oidc_provider.capture_authorization(refresh_response["Location"])
            callback_response = client.get(
                "/oidc/callback/",
                {"code": "valid-code", "state": query["state"][0]},
                follow=True,
            )

        expect(query["prompt"]) == ["none"]
        expect(callback_response.status_code) == 200
        expect(callback_response.content.decode()).contains(
            "Your Authentik session expired."
        )
        expect(client.session.get("_auth_user_id")) is None

    @pytest.mark.django_db
    def it_accepts_claims_for_an_organization(organization):
        backend = AuthentikOIDCBackend()
        expect(
            backend.verify_claims(
                {
                    "sub": "authentik-user",
                    "email": "person@EXAMPLE.com",
                    "email_verified": True,
                }
            )
        ) is True

    @pytest.mark.django_db
    def it_rejects_claims_without_an_organization():
        backend = AuthentikOIDCBackend()
        expect(
            backend.verify_claims(
                {
                    "sub": "authentik-user",
                    "email": "person@unknown.example",
                    "email_verified": True,
                }
            )
        ) is False
        expect(backend.verify_claims({"email": "invalid"})) is False
        expect(backend.verify_claims({})) is False

    @pytest.mark.django_db
    def it_rejects_unverified_email_claims(organization):
        backend = AuthentikOIDCBackend()
        expect(
            backend.verify_claims(
                {
                    "sub": "authentik-user",
                    "email": "person@example.com",
                    "email_verified": False,
                }
            )
        ) is False
        expect(
            backend.verify_claims(
                {"sub": "authentik-user", "email": "person@example.com"}
            )
        ) is False

    @pytest.mark.django_db
    def it_creates_users_using_email_login_rules(organization):
        backend = AuthentikOIDCBackend()
        user = backend.create_user(
            {
                "sub": "authentik-user",
                "email": "Person@Example.com",
                "email_verified": True,
            }
        )

        expect(user.email) == "person@example.com"
        expect(user.username) == "person"
        identity = OIDCIdentity.objects.get(user=user)
        expect(identity.issuer) == settings.OIDC_OP_ISSUER
        expect(identity.subject) == "authentik-user"

    @pytest.mark.django_db
    def it_resolves_existing_identities_by_issuer_and_subject(organization):
        backend = AuthentikOIDCBackend()
        user = backend.create_user(
            {
                "sub": "stable-subject",
                "email": "person@example.com",
                "email_verified": True,
            }
        )

        users = backend.filter_users_by_claims(
            {
                "sub": "stable-subject",
                "email": "renamed@example.com",
                "email_verified": True,
            }
        )

        expect(list(users)) == [user]

    def it_rejects_mismatched_userinfo_subjects():
        with pytest.raises(SuspiciousOperation):
            AuthentikOIDCBackend.verify_userinfo_subject(
                {"sub": "id-token-user"}, {"sub": "userinfo-user"}
            )
        with pytest.raises(SuspiciousOperation):
            AuthentikOIDCBackend.verify_userinfo_subject({}, {"sub": "userinfo-user"})

    def it_accepts_matching_userinfo_subjects():
        AuthentikOIDCBackend.verify_userinfo_subject(
            {"sub": "same-user"}, {"sub": "same-user"}
        )

    def it_accepts_tokens_issued_for_this_client():
        AuthentikOIDCBackend.verify_identity_token_claims(
            {
                "iss": settings.OIDC_OP_ISSUER,
                "aud": settings.OIDC_RP_CLIENT_ID,
            }
        )
        AuthentikOIDCBackend.verify_identity_token_claims(
            {
                "iss": settings.OIDC_OP_ISSUER,
                "aud": [settings.OIDC_RP_CLIENT_ID, "another-client"],
                "azp": settings.OIDC_RP_CLIENT_ID,
            }
        )

    @pytest.mark.parametrize(
        "claims",
        [
            {"iss": "https://issuer.example/", "aud": settings.OIDC_RP_CLIENT_ID},
            {"iss": settings.OIDC_OP_ISSUER, "aud": "another-client"},
            {
                "iss": settings.OIDC_OP_ISSUER,
                "aud": [settings.OIDC_RP_CLIENT_ID, "another-client"],
                "azp": "another-client",
            },
            {
                "iss": settings.OIDC_OP_ISSUER,
                "aud": settings.OIDC_RP_CLIENT_ID,
                "azp": "another-client",
            },
        ],
    )
    def it_rejects_tokens_not_issued_for_this_client(claims):
        with pytest.raises(SuspiciousOperation):
            AuthentikOIDCBackend.verify_identity_token_claims(claims)
