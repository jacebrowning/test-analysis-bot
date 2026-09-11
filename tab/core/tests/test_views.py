from django.conf import settings
from django.core import mail
from django.urls import reverse

import pytest

from ..constants import TEST_OTP
from ..helpers import send_otp_email
from ..models import Organization


def describe_login_and_verify(expect, client):
    login_url = "/accounts/login/"
    verify_url = "/accounts/verify/"

    @pytest.fixture
    def organization():
        return Organization.objects.create(email_domain="example.com")

    @pytest.mark.django_db
    def it_shows_email_login_when_enabled():
        response = client.get(login_url)
        expect(response.status_code) == 200
        html = response.content.decode("utf-8")
        expect(html).contains("We'll send you a generated, one-time password")
        expect(html).contains('name="email"')

    @pytest.mark.django_db
    def it_hides_email_login_when_disabled(settings):
        settings.POSTMARK_API_KEY = ""
        response = client.get(login_url)
        expect(response.status_code) == 200
        html = response.content.decode("utf-8")
        expect(html).excludes("We'll send you a generated, one-time password")
        expect(html).excludes('name="email"')
        expect(html).contains(
            "If you are an administrator, you can login with your password."
        )
        expect(html).contains("Admin")

    @pytest.mark.django_db
    def it_accepts_valid_otp(organization):
        # Submit email to login
        response = client.post(login_url, {"email": "test@example.com"}, follow=True)
        expect(response.status_code) == 200
        html = response.content.decode("utf-8")
        expect(html).contains("Verify One-Time Password")

        # Submit OTP to verify
        response = client.post(verify_url, {"otp": TEST_OTP}, follow=True)
        expect(response.status_code) == 200
        html = response.content.decode("utf-8")
        expect(html).contains("Logout")

    @pytest.mark.django_db
    def it_rejects_invalid_otp(organization):
        # Submit email to login
        response = client.post(login_url, {"email": "test@example.com"}, follow=True)
        expect(response.status_code) == 200
        html = response.content.decode("utf-8")
        expect(html).contains("Verify One-Time Password")

        # Submit invalid OTP to verify
        response = client.post(verify_url, {"otp": "invalid"}, follow=True)
        expect(response.status_code) == 200
        html = response.content.decode("utf-8")
        expect(html).contains("Invalid OTP. Please try again.")

    @pytest.mark.django_db
    def it_rejects_unknown_domains():
        response = client.post(login_url, {"email": "test@example.com"}, follow=True)
        expect(response.status_code) == 200
        html = response.content.decode("utf-8")
        expect(html).contains("No organization found for that email domain.")

    @pytest.mark.django_db
    def it_accepts_an_exact_email_address():
        Organization.objects.create(email_domain="jacebrowning@gmail.com")
        response = client.post(
            login_url, {"email": "jacebrowning@gmail.com"}, follow=True
        )
        expect(response.status_code) == 200
        html = response.content.decode("utf-8")
        expect(html).contains("Verify One-Time Password")

    @pytest.mark.django_db
    def it_rejects_other_addresses_on_the_same_consumer_domain():
        Organization.objects.create(email_domain="jacebrowning@gmail.com")
        response = client.post(login_url, {"email": "other@gmail.com"}, follow=True)
        expect(response.status_code) == 200
        html = response.content.decode("utf-8")
        expect(html).contains("No organization found for that email domain.")

    @pytest.mark.django_db
    @pytest.mark.parametrize(
        "email",
        [
            "invalid",
            "person@@example.com",
            "person@attacker.example@example.com",
            " person@example.com",
            "person@example.com ",
            "person\n@example.com",
        ],
    )
    def it_rejects_malformed_email_addresses(organization, email):
        response = client.post(login_url, {"email": email}, follow=True)
        expect(response.status_code) == 200
        html = response.content.decode("utf-8")
        expect(html).contains("No organization found for that email domain.")

    @pytest.mark.django_db
    def it_redirects_to_login_if_no_email_in_session():
        # Try to access verify page without going through login first
        response = client.get(verify_url, follow=True)
        expect(response.status_code) == 200
        html = response.content.decode("utf-8")
        expect(html).contains("Please enter your email address first.")

    @pytest.mark.django_db
    def it_preserves_next_parameter_through_login_flow(organization):
        # Submit email to login with next parameter
        response = client.post(
            f"{login_url}?next=/projects/", {"email": "test@example.com"}, follow=True
        )
        expect(response.status_code) == 200
        html = response.content.decode("utf-8")
        expect(html).contains("Verify One-Time Password")

        # Submit OTP to verify - next parameter should be in the URL
        response = client.post(
            f"{verify_url}?next=/projects/", {"otp": TEST_OTP}, follow=True
        )
        expect(response.status_code) == 200

        # Should redirect to the next URL after successful login
        expect(response.redirect_chain[-1][0]).contains("/projects/")

    @pytest.mark.django_db
    def it_rejects_external_next_urls(organization):
        response = client.post(
            f"{login_url}?next=https://attacker.example/",
            {"email": "test@example.com"},
        )
        expect(response.status_code) == 302
        expect(response["Location"]) == verify_url

        response = client.post(
            f"{verify_url}?next=https://attacker.example/",
            {"otp": TEST_OTP},
        )
        expect(response.status_code) == 302
        expect(response["Location"]) == "/"

    @pytest.mark.django_db
    def it_redirects_to_verify_when_otp_is_in_query(organization):
        client.post(login_url, {"email": "test@example.com"}, follow=True)

        response = client.get(f"{login_url}?otp={TEST_OTP}", follow=True)
        expect(response.status_code) == 200
        expect(response.redirect_chain[0][0]).contains(f"{verify_url}?otp={TEST_OTP}")

        html = response.content.decode("utf-8")
        expect(html).contains(f'value="{TEST_OTP}"')

    @pytest.mark.django_db
    def it_preserves_next_parameter_when_redirecting_with_otp(organization):
        client.post(
            f"{login_url}?next=/projects/", {"email": "test@example.com"}, follow=True
        )

        response = client.get(
            f"{login_url}?otp={TEST_OTP}&next=/projects/", follow=True
        )
        expect(response.status_code) == 200
        expect(response.redirect_chain[0][0]).contains(
            f"{verify_url}?otp={TEST_OTP}&next=%2Fprojects%2F"
        )


def describe_send_otp_email(expect):
    @pytest.mark.django_db
    def it_includes_login_link_in_message():
        mail.outbox.clear()

        send_otp_email("test@example.com", TEST_OTP)

        expect(len(mail.outbox)) == 1
        message = mail.outbox[0]

        login_url = f"{settings.BASE_URL}{reverse('login')}"
        expect(message.body).contains(login_url)
        expect(message.body).contains(f"otp={TEST_OTP}")
