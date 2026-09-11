import random
import string
from contextlib import suppress

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.urls import reverse

import log

from .constants import TEST_OTP
from .models import Organization


def organization_for_email(email: str) -> Organization | None:
    """Match a full address first, then a shared domain."""
    local_part, separator, domain = email.strip().rpartition("@")
    if not separator or not local_part or not domain:
        return None
    exact = (
        Organization.objects.filter(email_domain__iexact=email.strip())
        .order_by("pk")
        .first()
    )
    if exact:
        return exact
    return (
        Organization.objects.filter(email_domain__iexact=domain).order_by("pk").first()
    )


def has_organization_email_domain(email: str) -> bool:
    try:
        validate_email(email)
    except ValidationError:
        return False

    return organization_for_email(email) is not None


def get_or_create_user(email: str) -> User:
    try:
        return User.objects.get(email__iexact=email)
    except User.DoesNotExist:
        log.warning(f"User {email} does not exist, creating it automatically")
        username = base_username = email.lower().split("@")[0]
        for count in range(1, 10):
            if count > 1:
                username = f"{base_username}{count}"
            with suppress(IntegrityError):
                with transaction.atomic():
                    return User.objects.create_user(
                        username=username, email=email.lower()
                    )
    raise ValueError(f"Unable to generate username for {email}")


def generate_otp() -> str:
    if hasattr(settings, "TEST"):
        log.warning("Bypassing OTP generation for tests")
        return TEST_OTP
    return "".join(random.choices(string.digits, k=6))


def send_otp_email(email: str, otp: str):
    subject = "Your login code"
    html = render_to_string(
        "core/emails/otp.html",
        {
            "otp": otp,
            "login_url": f"{settings.BASE_URL}{reverse('login')}?otp={otp}",
        },
    )
    message = EmailMessage(subject, html, settings.DEFAULT_FROM_EMAIL, [email])
    message.content_subtype = "html"
    message.send()
