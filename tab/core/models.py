import secrets

from django.conf import settings
from django.db import models

import log
from github import Auth, Github, GithubIntegration


def generate_key() -> str:
    return "".join(secrets.choice("0123456789abcdef") for _ in range(32))


class OIDCIdentity(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="oidc_identities",
    )
    issuer = models.URLField()
    subject = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("issuer", "subject"), name="unique_oidc_identity"
            ),
            models.UniqueConstraint(fields=("issuer", "user"), name="unique_oidc_user"),
        ]

    def __str__(self):
        return f"{self.issuer} › {self.subject}"


class Organization(models.Model):
    name = models.CharField(max_length=100)
    email_domain = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="Email domain or address",
        help_text="Company domain (example.com) or a single address (you@gmail.com)",
    )
    repository_index = models.URLField(unique=True)
    repository_token = models.CharField(max_length=100, blank=True)
    github_app_id = models.IntegerField(
        null=True, blank=True, verbose_name="GitHub App ID"
    )
    github_app_private_key = models.TextField(
        null=True, blank=True, verbose_name="GitHub App private key"
    )
    slack_bot_token = models.CharField(
        max_length=100, blank=True, verbose_name="Slack bot token"
    )

    key = models.CharField(
        max_length=32, unique=True, default=generate_key, verbose_name="API key"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

    def get_github_client(self) -> Github | None:
        if self.github_app_id and self.github_app_private_key:
            log.debug("Authenticating with GitHub App")
            auth = Auth.AppAuth(self.github_app_id, self.github_app_private_key)
            integration = GithubIntegration(auth=auth)
            installation = integration.get_org_installation(
                self.repository_index.removeprefix("https://github.com/")
            )
            return installation.get_github_for_installation()

        if self.repository_token:
            log.debug("Authenticating with repository token")
            return Github(self.repository_token)

        log.warning(f"{self} has no repository token")
        return None
