import re

from django.core.cache import cache
from django.db import models
from django.utils import timezone

import log

from tab.core.models import Organization
from tab.projects.models import Project, Result, Suite, Test

from .constants import (
    ALERT_CACHE_KEY,
    ALERT_CACHE_TIMEOUT,
    ALERT_LIMIT,
    DELTA_THRESHOLD,
)
from .helpers import send_slack_message
from .managers import SuiteHistoryManager, TestHistoryManager
from .types import Message


class TestHistory(models.Model):
    test = models.ForeignKey(Test, on_delete=models.CASCADE, related_name="history")
    result = models.ForeignKey(
        Result, null=True, blank=True, on_delete=models.SET_NULL, related_name="history"
    )

    failure_rate = models.FloatField()
    block_rate = models.FloatField()
    average_duration = models.FloatField()
    timestamp = models.DateTimeField(auto_now_add=True)

    objects: TestHistoryManager = TestHistoryManager()

    class Meta:
        ordering = ["-timestamp"]
        indexes = [models.Index(fields=["test", "timestamp"])]
        verbose_name_plural = "Test histories"

    def __str__(self):
        return f"{self.test.project.name} @ {self.timestamp.date()}"

    def evaluate(self) -> bool:
        if not self.test.enabled:
            log.debug(f"Skipped alert for disabled test: {self.test}")
            return False

        if self.test.block_rate < 0.05:
            log.debug(f"Skipped alert for non-blocking test: {self.test}")
            return False

        # TODO: Consider using the past history record rather the the property
        if self.test.failure_rate_delta < DELTA_THRESHOLD:
            log.debug(
                f"Failure rate {self.test.failure_rate_delta} below threshold: {self.test}"
            )
            return False

        previous = self.test.history.filter(timestamp__lt=self.timestamp).first()
        if previous and previous.failure_rate >= self.test.failure_rate:
            log.debug(f"Failure rate is trending downward: {self.test}")
            return False

        key = f"{ALERT_CACHE_KEY}:{self.test.id}"
        if cache.get(key):
            log.debug(f"Skipped redundant alert for test: {self.test}")
            return False

        alert = Alert.objects.create(test=self.test, history=self)
        log.warning(f"Alert triggered: {alert.message}")
        cache.set(key, True, timeout=ALERT_CACHE_TIMEOUT)
        alert.send()
        return True


class SuiteHistory(models.Model):
    suite = models.ForeignKey(Suite, on_delete=models.CASCADE, related_name="history")
    run = models.ForeignKey(
        "projects.Run",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="history",
    )

    average_setup_duration = models.FloatField()
    average_tests_duration = models.FloatField()
    average_teardown_duration = models.FloatField()
    timestamp = models.DateTimeField(auto_now_add=True)

    objects: SuiteHistoryManager = SuiteHistoryManager()

    class Meta:
        ordering = ["-timestamp"]
        indexes = [models.Index(fields=["suite", "timestamp"])]
        verbose_name_plural = "Suite histories"

    def __str__(self):
        return f"{self.suite} @ {self.timestamp.date()}"


class Team(models.Model):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="teams"
    )
    slack_channel_name = models.CharField(max_length=100)
    slack_channel_id = models.CharField(
        max_length=100, blank=True, verbose_name="Slack Channel ID"
    )

    alerted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["organization", "slack_channel_name"]
        unique_together = ["organization", "slack_channel_name"]

    def __str__(self):
        return f"{self.organization} › {self.slack_channel_name}"

    @property
    def recently_alerted(self):
        threshold = timezone.now() - ALERT_LIMIT
        return self.alerted_at and self.alerted_at > threshold

    def save(self, *args, **kwargs):
        if label := self.slack_channel_name.strip("# "):
            self.slack_channel_name = "#" + label
        super().save(*args, **kwargs)


class Subscription(models.Model):
    team = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name="subscriptions"
    )

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="subscriptions"
    )
    suite = models.ForeignKey(
        Suite,
        on_delete=models.CASCADE,
        related_name="subscriptions",
        null=True,
        blank=True,
    )
    test = models.CharField(max_length=500, null=True, blank=True)

    primary = models.BooleanField(default=True)

    class Meta:
        ordering = ["team", "project"]
        unique_together = ["team", "project", "suite", "test"]

    def __str__(self):
        value = f"{self.team} | {self.project}"
        if self.suite:
            value += f" › {self.suite}"
        if self.test:
            value += f" › {self.test}"
        if not self.primary:
            value += f" [SECONDARY]"
        return value


class Alert(models.Model):
    test = models.ForeignKey(Test, on_delete=models.CASCADE, related_name="alerts")
    history = models.OneToOneField(
        TestHistory,
        on_delete=models.CASCADE,
        related_name="alert",
        null=True,
        blank=True,
    )

    url = models.URLField(null=True, blank=True, verbose_name="URL")

    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.test.project.name}  @ {self.created_at.date()}"

    @property
    def message(self):
        return self.build()

    @property
    def subscriptions(self) -> list[Subscription]:
        matches: list[Subscription] = []

        subscriptions = Subscription.objects.filter(
            project=self.test.project
        ).select_related("project", "team__organization")

        for primary in (True, False):
            for suite in (self.test.suite, None):
                for subscription in subscriptions:
                    if subscription.primary != primary:
                        continue

                    if subscription.suite != suite:
                        continue

                    if subscription.test and not re.search(
                        subscription.test, self.test.name
                    ):
                        continue

                    if subscription not in matches:
                        matches.append(subscription)

        return matches

    def build(self, *, url: str | None = None, debug: bool = False) -> Message:
        if url:
            return Message(
                text=f"Relevant alert",
                label="original thread",
                url=url,
                debug=debug,
            )

        label = self.test.project.name + " › " + self.test.name
        if _user := self.test.disabled_user:
            user = _user.get_full_name() or _user.email
        else:
            user = "unknown user"
        url = self.test.url + "?expand=true"

        if self.history:
            text = f"Failure rate increased by {self.history.test.failure_rate_delta:.0%} today"
            extra = self.history.result.message if self.history.result else None
        elif self.test.disabled_at:
            text = "Manually disabled from blocking merges"
            reason = self.test.disabled_reason or "(no reason provided)"
            extra = f"{user}: {reason}"
        else:
            text = "Automatically restored to block merges again"
            extra = f"Originally disabled by {user}, fixes have made this test pass reliably again."

        return Message(text, label, url, extra=extra or "", debug=debug)

    def send(
        self, *, forward: bool = True, debug: bool = False, force: bool = False
    ) -> int:
        count = 0

        for subscription in self.subscriptions:

            if subscription.primary:
                message = self.build(debug=debug, url=None if force else self.url)
                unfurl = bool(self.url) and not force
            elif not forward:
                log.info(f"Skipped secondary alert for test: {self.test}")
                continue
            elif self.url:
                message = self.build(debug=debug, url=self.url)
                unfurl = True
            else:
                log.warning(f"No existing primary alert for test: {self.test}")
                continue

            team: Team = subscription.team
            team.refresh_from_db()
            if team.recently_alerted and not force:
                log.info(f"Skipped redundant alert for team: {team}")
                continue

            if url := send_slack_message(
                team.organization,
                team.slack_channel_name,
                team.slack_channel_id,
                message,
                unfurl,
            ):
                count += 1
                team.alerted_at = timezone.now()
                team.save()
                if not self.url:
                    self.url = url
                    self.sent_at = team.alerted_at
                    self.save()

        if not count:
            log.warning(f"No teams alertable for test: {self.test}")

        return count
