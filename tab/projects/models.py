from __future__ import annotations

import json
import re
import threading
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.db import models
from django.db.models import QuerySet
from django.urls import reverse
from django.utils import timezone

import log

from tab.core.models import Organization

from . import managers
from .constants import (
    ANSI_ESCAPE,
    CHECKOUT_COMMAND,
    DEFAULT_SUITE,
    FAILURE_RATE_EPSILON,
    PENDING_THRESHOLD,
    RESTORATION_THRESHOLD,
    get_default_branches,
)
from .enums import Browser, Platform, Status, Target
from .helpers import build_result_prompt, humanize_duration, rerun_failed_jobs


class Project(models.Model):
    repository = models.URLField(unique=True, db_index=True)
    default_branches = models.JSONField(
        default=get_default_branches,
        help_text="Results from these branches will be considered in computed metrics",
    )
    error_indicators = models.JSONField(
        default=list,
        blank=True,
        help_text="Message fragments that indicate there was a setup error rather than failure",
    )
    skipped_indicators = models.JSONField(
        default=list,
        blank=True,
        help_text="Message fragments that indicate a passed test was not actually able to run",
    )

    branch_inactive_threshold = models.DurationField(
        default=timedelta(days=7),
        help_text="Branches older than this will be hidden by default",
    )
    test_inactive_threshold = models.DurationField(
        default=timedelta(days=7),
        help_text="Tests with no results this recent will be hidden by default",
    )
    test_stale_threshold = models.DurationField(
        default=timedelta(days=30),
        help_text="Tests with no results this recent will be pruned automatically",
    )
    result_stale_threshold = models.DurationField(
        default=timedelta(days=7),
        help_text="Branch results older than this will be pruned automatically",
    )

    cleaned_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    objects: managers.ProjectManager = managers.ProjectManager()

    def __str__(self):
        return self.name

    @property
    def repository_index(self) -> str:
        parts = self.repository.split("/")
        return "/".join(parts[:-1])

    @property
    def path(self) -> str:
        self._update_repository()
        parts = self.repository.split("/")
        return "/".join(parts[3:])

    @property
    def name(self) -> str:
        return self.path.replace("/", " › ")

    @property
    def default_branch(self) -> str:
        return self.default_branches[0]

    @property
    def has_disabled_tests(self) -> bool:
        cutoff = timezone.now() - timedelta(days=7)
        return self.tests.filter(
            disabled_at__isnull=False, updated_at__gte=cutoff
        ).exists()

    def save(self, *args, **kwargs):
        self._update_repository()
        super().save(*args, **kwargs)

    def _update_repository(self):
        self.repository = managers.ProjectManager.clean_repository(self.repository)


class Suite(models.Model):
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="suites"
    )
    name = models.CharField(max_length=100, db_index=True)
    parent = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="children",
    )

    supports_override = models.BooleanField(
        default=True,
        help_text="Indicates the suite is configured to allow disabling tests",
    )
    local_command = models.TextField(
        default="",
        blank=True,
        help_text="Pattern to run individual tests locally",
    )
    error_indicators = models.JSONField(
        default=list,
        blank=True,
        help_text="Message fragments that indicate there was a setup error rather than failure",
    )
    skipped_indicators = models.JSONField(
        default=list,
        blank=True,
        help_text="Message fragments that indicate a passed test was not actually able to run",
    )

    failure_rate_upper_threshold = models.FloatField(
        default=0.50, help_text="Upper threshold to consider unacceptable"
    )
    failure_rate_lower_threshold = models.FloatField(
        default=0.10, help_text="Lower threshold to consider acceptable"
    )
    block_rate_upper_threshold = models.FloatField(
        default=0.05, help_text="Upper threshold to consider unacceptable"
    )
    block_rate_lower_threshold = models.FloatField(
        default=0.01, help_text="Lower threshold to consider acceptable"
    )
    average_duration_upper_threshold = models.FloatField(
        default=60, help_text="Upper threshold to consider unacceptable"
    )
    average_duration_lower_threshold = models.FloatField(
        default=30, help_text="Lower threshold to consider acceptable"
    )

    average_setup_duration = models.FloatField(
        default=-1,
        editable=False,
        help_text="Seconds of setup duration from recent runs on default branches",
    )
    average_tests_duration = models.FloatField(
        default=-1,
        editable=False,
        help_text="Seconds of tests duration from recent runs on default branches",
    )
    average_teardown_duration = models.FloatField(
        default=-1,
        editable=False,
        help_text="Seconds of teardown duration from recent runs on default branches",
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    objects: managers.SuiteManager = managers.SuiteManager()

    class Meta:
        ordering = ["name"]
        unique_together = ["project", "name"]

    def __str__(self):
        if self.name == DEFAULT_SUITE:
            return str(self.project)
        return f"{self.project} › {self.name}"

    def update_average_setup_duration(self) -> bool:
        return self._update_average_duration(
            "average_setup_duration",
            "setup_duration",
            filters={
                "setup_started_at__isnull": False,
                "tests_started_at__isnull": False,
            },
            order_by="-tests_started_at",
        )

    def update_average_tests_duration(self) -> bool:
        return self._update_average_duration(
            "average_tests_duration",
            "tests_duration",
            filters={
                "tests_started_at__isnull": False,
                "tests_finished_at__isnull": False,
            },
            order_by="-tests_finished_at",
        )

    def update_average_teardown_duration(self) -> bool:
        return self._update_average_duration(
            "average_teardown_duration",
            "teardown_duration",
            filters={
                "tests_finished_at__isnull": False,
                "teardown_finished_at__isnull": False,
            },
            order_by="-teardown_finished_at",
        )

    def _update_average_duration(
        self,
        field: str,
        attr: str,
        *,
        filters: dict,
        order_by: str,
    ) -> bool:
        old = getattr(self, field)
        queryset = self.runs.filter(
            branch__in=self.project.default_branches,
            **filters,
        ).order_by(order_by)
        runs = list(queryset[:150])
        durations = [getattr(run, attr) for run in runs if getattr(run, attr) > 0]
        if not durations:
            return False

        new = round(sum(durations) / len(durations), 3)
        if old == new:
            return False

        log.debug(f"Suite has new {field.replace('_', ' ')}: {old} => {new} seconds")
        setattr(self, field, new)
        return True

    def update(self, run: Run | None = None) -> bool:
        if not any(
            [
                self.update_average_setup_duration(),
                self.update_average_tests_duration(),
                self.update_average_teardown_duration(),
            ]
        ):
            return False
        self.history.create_from_suite(self, run)
        return True


class Run(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="runs")
    suite = models.ForeignKey(Suite, on_delete=models.CASCADE, related_name="runs")
    branch = models.CharField(max_length=500, default="", db_index=True)
    commit = models.CharField(max_length=100, default="", db_index=True)

    metadata = models.JSONField(default=dict, blank=True)

    setup_started_at = models.DateTimeField(null=True, blank=True, db_index=True)
    tests_started_at = models.DateTimeField(null=True, blank=True, db_index=True)
    tests_finished_at = models.DateTimeField(null=True, blank=True, db_index=True)
    teardown_finished_at = models.DateTimeField(null=True, blank=True, db_index=True)

    objects: managers.RunManager = managers.RunManager()

    class Meta:
        ordering = ["-setup_started_at"]
        unique_together = ["project", "suite", "branch", "commit"]

    def __str__(self):
        branch = self.branch or "???"
        commit = self.commit_humanized if self.commit else "???"
        return f"{self.suite} on {branch!r} at {commit}"

    @property
    def branch_url(self) -> str:
        if not self.branch:
            return ""
        return f"{self.project.repository}/tree/{self.branch}"

    @property
    def commit_humanized(self) -> str:
        if not self.commit:
            return ""
        return self.commit[:7]

    @property
    def commit_url(self) -> str:
        if not self.commit:
            return ""
        return f"{self.project.repository}/commit/{self.commit}"

    @property
    def setup_duration(self) -> float:
        if not self.setup_started_at or not self.tests_started_at:
            return 0.0
        return (self.tests_started_at - self.setup_started_at).total_seconds()

    @property
    def tests_duration(self) -> float:
        if not self.tests_started_at or not self.tests_finished_at:
            return 0.0
        return (self.tests_finished_at - self.tests_started_at).total_seconds()

    @property
    def teardown_duration(self) -> float:
        if not self.tests_finished_at or not self.teardown_finished_at:
            return 0.0
        return (self.teardown_finished_at - self.tests_finished_at).total_seconds()


class Test(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="tests")
    suite = models.ForeignKey(
        Suite, null=True, blank=True, on_delete=models.SET_NULL, related_name="tests"
    )

    name = models.CharField(max_length=4000, db_index=True)
    original_branch = models.CharField(
        max_length=500,
        default="",
        help_text="Name of the branch that originally added this test",
    )
    original_commit = models.CharField(
        max_length=100,
        default="",
        help_text="Hash of the commit that originally added this test",
    )
    original_metadata = models.JSONField(default=dict, blank=True)
    maintainer = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintained_tests",
        help_text="User responsible for maintaining this test",
    )

    disabled_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Timestamp of when the test was disabled",
    )
    disabled_reason = models.TextField(
        default="",
        blank=True,
        help_text="Explanation of why the test is temporarily disabled",
        db_index=True,
    )
    disabled_tracker = models.URLField(
        null=True,
        blank=True,
        help_text="URL of the ticket tracking the work to restore the test",
        db_index=True,
    )
    disabled_user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="User who disabled the test",
    )

    enabled = models.BooleanField(
        default=False,
        editable=False,
        help_text="Test is allowed to block merges and releases",
        db_index=True,
    )
    failure_rate = models.FloatField(
        default=-1,
        editable=False,
        help_text="Total failure rate on significant branches including reruns",
    )
    block_rate = models.FloatField(
        default=-1,
        editable=False,
        help_text="Effective failure rate with reruns and ignored failures excluded",
    )
    average_duration = models.FloatField(
        default=-1,
        editable=False,
        help_text="Seconds duration from recent runs on significant branches",
    )
    last_result = models.ForeignKey(
        "Result", on_delete=models.SET_NULL, null=True, related_name="+"
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    objects: managers.TestManager = managers.TestManager()

    class Meta:
        indexes = [
            models.Index(fields=["project", "name"]),
            models.Index(fields=["project", "enabled"]),
        ]
        unique_together = [("project", "name")]

    def __str__(self):
        if not self.suite:
            return self.name
        if self.suite.name == DEFAULT_SUITE:
            return self.name
        return f"{self.suite.name} › {self.name}"

    @property
    def label(self) -> str:
        if self.pk and self.project.suites.count() <= 1:
            return self.name
        return str(self)

    @property
    def url(self) -> str:
        return settings.BASE_URL + reverse(
            "projects:test-results",
            args=[self.project.path, self.id],
        )

    @property
    def regex(self) -> str:
        parts = self.name.split(" › ")

        # Remove test runner prefix
        if "jest" in parts[0] or "vitest" in parts[0]:
            parts = parts[1:]

        # Remove file path prefix
        if "." in parts[0]:
            parts = parts[1:]

        # Remove prefix for fully-namespaced tests
        if len(parts) > 0 and "::" in parts[-1]:
            return parts[-1]
        if len(parts) > 1 and "::" in parts[-2]:
            return parts[-1]

        # Remove duplicate prefixes
        if len(parts) >= 2:
            parts[1] = parts[1].removeprefix(parts[0]).strip()

        # Join nested description blocks
        escaped_parts = [re.escape(part) for part in parts]
        label = ".*".join(escaped_parts)

        return (
            label.replace(r"\ ", " ")  # remove excessive escaping of spaces
            .replace("'", r"'\''")
            .replace(" > ", " ")  # fix for vitest pattern matching
            .replace('"', ".")  # replace quote marks for better shell support
            .strip()
        )

    @property
    def substring(self) -> str:
        parts = self.name.split(" › ")[-2:]
        if "." in parts[0]:  # remove module namespaces
            parts[0] = parts[0].split(".")[-1]
        if " [" in parts[-1]:  # remove parameterized variant
            parts[-1] = parts[-1].split(" [")[0]
        return " and ".join(parts).strip()

    @property
    def significant_branches(self) -> list[str]:
        branches = list(self.project.default_branches)
        if self.original_branch and self.original_branch not in branches:
            branches.insert(0, self.original_branch)
        if settings.DEBUG:
            branches.insert(0, "")  # unknown local branch
        return branches

    @property
    def original_branch_url(self) -> str:
        if not self.original_branch:
            return ""
        return f"{self.project.repository}/tree/{self.original_branch}"

    @property
    def original_commit_url(self) -> str:
        if not self.original_commit:
            return ""
        return f"{self.project.repository}/commit/{self.original_commit}"

    @property
    def markers(self) -> list[str]:
        metadata = self.last_result.metadata if self.last_result else {}
        # TODO: Consider making 'annotations' and/or 'tags' a proper field
        values = metadata.get("annotations", []) + metadata.get("tags", [])
        if self.disabled_at:
            values.append("disabled")
        return values

    @property
    def command(self) -> list[tuple[str, bool]]:
        result = self.last_result or self.results.first()
        return result.command if result else []

    @property
    def error_indicators(self) -> list[str]:
        if self.suite and self.suite.error_indicators:
            return self.suite.error_indicators
        return self.project.error_indicators

    @property
    def skipped_indicators(self) -> list[str]:
        if self.suite and self.suite.skipped_indicators:
            return self.suite.skipped_indicators
        return self.project.skipped_indicators

    @property
    def failure_rate_humanized(self) -> str:
        if self.failure_rate < 0:
            return "—"
        return f"{self.failure_rate:.1%}"

    @property
    def block_rate_humanized(self) -> str:
        if self.block_rate < 0:
            return "—"
        return f"{self.block_rate:.1%}"

    @property
    def average_duration_humanized(self) -> str:
        if self.average_duration < 0:
            return "—"
        return humanize_duration(self.average_duration)

    @property
    def failure_rate_delta(self) -> float:
        previous = timezone.now() - timedelta(days=1)
        record = self.history.filter(timestamp__lte=previous).first()
        if not record or record.failure_rate < 0:
            return 0.0
        return self.failure_rate - record.failure_rate

    @property
    def block_rate_delta(self) -> float:
        previous = timezone.now() - timedelta(days=1)
        record = self.history.filter(timestamp__lte=previous).first()
        if not record or record.block_rate < 0:
            return 0.0
        return self.block_rate - record.block_rate

    def update_failure_rate(self) -> bool:
        old = self.failure_rate
        queryset = self.results.filter(
            branch__in=self.significant_branches,
        ).order_by("-created_at")
        results = self._bias_to_recent(queryset)
        if not results:
            return False

        failed = sum(result.status in Status.test_failed() for result in results)
        new = round(failed / len(results), 6)

        if (
            self.disabled_at
            and new < FAILURE_RATE_EPSILON
            and self.results.exclude(branch__in=self.significant_branches)
            .filter(
                status__in=Status.test_failed(),
                created_at__gte=timezone.now() - timedelta(days=3),
            )
            .exists()
        ):
            log.info(f"Passing test still has recent branch failures: {self.name}")
            # Prevent disabled test being restored with recent branch failures
            new = FAILURE_RATE_EPSILON

        if old == new:
            return False

        log.debug(f"Test has new failure rate: {old:.3%} => {new:.3%}")
        self.failure_rate = new
        return True

    def update_block_rate(self) -> bool:
        old = self.block_rate
        queryset = self.results.filter(
            branch__in=self.significant_branches,
            final=True,
        ).order_by("-created_at")
        results = self._bias_to_recent(queryset, min_samples=15, max_samples=40)
        if not results:
            return False

        failed = sum(result.status in Status.merge_blocked() for result in results)
        new = round(failed / len(results), 6)

        if old == new:
            return False

        log.debug(f"Test has new block rate: {old:.3%} => {new:.3%}")
        self.block_rate = new
        return True

    def update_average_duration(self) -> bool:
        old = self.average_duration
        queryset = self.results.filter(
            branch__in=self.significant_branches,
            status__in=Status.measurable(),
            duration__gt=0,
        ).order_by("-created_at")
        results = self._bias_to_recent(queryset, max_samples=150)
        durations = [result.duration for result in results if result.duration]
        if not durations:
            return False

        new = round(sum(durations) / len(durations), 3)
        if old == new:
            return False

        log.debug(f"Test has new average duration: {old} => {new} seconds")
        self.average_duration = new
        return True

    def _bias_to_recent(
        self, results: QuerySet[Result], *, min_samples: int = 20, max_samples: int = 60
    ) -> list[Result]:
        """Limit results to the past day, backfilling with older results if needed."""
        candidates = list(results[:max_samples])
        if not candidates:
            return []

        lookback_window = timezone.now() - timedelta(days=1)
        recent = [r for r in candidates if r.created_at >= lookback_window]
        if len(recent) >= min_samples:
            return recent
        return candidates[:min_samples]

    def update(self, result=None) -> bool:
        if failure_rated_updated := self.update_failure_rate():
            self.history.create_from_test(self, result)
        return any(
            [
                failure_rated_updated,
                self.update_block_rate(),
                self.update_average_duration(),
            ]
        )

    def save(self, *args, **kwargs):
        if self.pk:
            self._update_last_result()
        if (
            0 <= self.failure_rate < FAILURE_RATE_EPSILON
            and self.disabled_at
            and self.disabled_at <= timezone.now() - RESTORATION_THRESHOLD
        ):
            log.info(f"Restoring test based on failure rate: {self}")
            self.disabled_at = None
            self.disabled_reason = ""
            self.disabled_tracker = None
            # TODO: Find a way to avoid this circular import
            from tab.metrics.models import Alert

            alert = Alert.objects.create(test=self)
            thread = threading.Thread(target=alert.send, kwargs={"forward": False})
            thread.start()
        self.enabled = bool(
            not self.disabled_at
            and self.last_result
            and self.last_result.status not in Status.test_disabled()
        )
        super().save(*args, **kwargs)

    def _update_last_result(self):
        self.last_result = self.results.filter(
            branch=self.project.default_branch
        ).first()
        if self.last_result and self.last_result.status == Status.SKIPPED:
            if result := (
                self.results.filter(
                    branch=self.last_result.branch, commit=self.last_result.commit
                )
                .exclude(status=Status.SKIPPED)
                .first()
            ):
                self.last_result = result


class Result(models.Model):
    suite = models.ForeignKey(
        Suite, null=True, blank=True, on_delete=models.SET_NULL, related_name="results"
    )
    test = models.ForeignKey(Test, on_delete=models.CASCADE, related_name="results")

    branch = models.CharField(max_length=500, default="", db_index=True)
    commit = models.CharField(max_length=100, default="", db_index=True)
    target = models.CharField(
        max_length=100, null=True, blank=True, choices=Target.choices, db_index=True
    )
    platform = models.CharField(
        max_length=100, null=True, blank=True, choices=Platform.choices, db_index=True
    )
    browser = models.CharField(max_length=100, null=True, blank=True, db_index=True)
    final = models.BooleanField(
        default=True, help_text="Indicates this was the final retry", db_index=True
    )

    status = models.CharField(max_length=20, choices=Status.choices, db_index=True)
    duration = models.FloatField(null=True)
    message = models.TextField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects: managers.ResultManager = managers.ResultManager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # Optimizes Test._update_last_result()
            models.Index(fields=["test", "branch", "commit"]),
            # Optimizes Test.update_*() and ResultManager.get_latest_commit()
            models.Index(fields=["test", "branch", "created_at"]),
            # Optimizes ResultManager.get_health()
            models.Index(fields=["test", "commit", "branch", "final"]),
        ]

    def __str__(self):
        status = Status(self.status).label
        duration = self.duration_humanized if self.duration else "???s"
        branch = self.branch or "???"
        commit = self.commit_humanized if self.commit else "???"
        return f"{status} after {duration} on {branch!r} at {commit}"

    @property
    def test_label(self) -> str:
        suite = self.suite or self.test.suite
        if not suite:
            return self.test.name
        if suite.name == DEFAULT_SUITE:
            return self.test.name
        if self.pk and self.test.project.suites.count() <= 1:
            return self.test.name
        return f"{suite.name} › {self.test.name}"

    @property
    def url(self) -> str:
        return settings.BASE_URL + reverse(
            "projects:test-result",
            args=[self.test.project.path, self.test.id, self.id],
        )

    @property
    def markers(self) -> list[str]:
        metadata = self.metadata
        # TODO: Consider making 'annotations' and/or 'tags' a proper field
        values = metadata.get("annotations", []) + metadata.get("tags", [])
        if self.created_at is None and self.test.disabled_at:
            values.append("disabled")
        elif (
            self.created_at
            and self.test.disabled_at
            and self.created_at > self.test.disabled_at
        ):
            values.append("disabled")
        return values

    @property
    def command(self) -> list[tuple[str, bool]]:
        """Returns a list of (line, copyable) tuples."""

        def copyable(line: str) -> bool:
            line = line.strip()
            return bool(line) and not line.startswith("#")

        if suite := self.suite or self.test.suite:
            if pattern := suite.local_command:
                try:
                    command = pattern.format(test=self.test)
                    lines = command.split("\n")
                    return [
                        (CHECKOUT_COMMAND.format(branch=self.branch), True),
                        ("\n", False),
                        ("# then", False),
                        ("\n", False),
                    ] + [(line, copyable(line)) for line in lines]

                except (KeyError, AttributeError) as e:
                    error = repr(e)
                    log.error(f"Invalid local command for {suite}: {error}")
                    return [(pattern, False), (f"# invalid pattern: {error}", False)]
        return []

    @property
    def external(self) -> bool:
        if path := self.metadata.get("GITHUB_REPOSITORY"):
            if path != self.test.project.path:
                return True
        return False

    @property
    def branch_url(self) -> str:
        if not self.branch or self.external:
            return ""
        return f"{self.test.project.repository}/tree/{self.branch}"

    @property
    def merge_url(self) -> str:
        if self.external:
            return ""
        if number := self.metadata.get("CI_PR_NUMBER"):  # GitHub
            return f"{self.test.project.repository}/pull/{number}"
        if number := self.metadata.get("CI_MERGE_REQUEST_IID"):  # GitLab
            return f"{self.test.project.repository}/-/merge_requests/{number}"
        return ""

    @property
    def commit_humanized(self) -> str:
        if not self.commit:
            return ""
        return self.commit[:7]

    @property
    def commit_url(self) -> str:
        if not self.commit or self.external:
            return ""
        return f"{self.test.project.repository}/commit/{self.commit}"

    @property
    def originated_from_branch(self) -> bool:
        return (
            self.branch == self.test.original_branch
            and self.branch not in self.test.project.default_branches
        )

    @property
    def run_url(self) -> str:
        base_url = self.test.project.repository
        if path := self.metadata.get("GITHUB_REPOSITORY"):
            base_url = base_url.replace(self.test.project.path, path)
        if run_id := self.metadata.get("GITHUB_RUN_ID"):
            url = f"{base_url}/actions/runs/{run_id}"
            if number := self.metadata.get("CI_PR_NUMBER"):
                url += f"?pr={number}"
            return url
        return ""

    @property
    def environment_url(self) -> str:
        if url := self.metadata.get("url"):
            return url
        return ""

    @property
    def new_failure(self) -> bool:
        if self.status not in Status.merge_blocked():
            return False
        failure_threshold = (
            self.suite.failure_rate_lower_threshold
            if self.suite
            else Suite._meta.get_field("failure_rate_lower_threshold").default
        )
        if self.test.failure_rate >= failure_threshold:
            return False
        if self.originated_from_branch:
            return True
        block_threshold = (
            self.suite.block_rate_lower_threshold
            if self.suite
            else Suite._meta.get_field("block_rate_lower_threshold").default
        )
        return (
            self.test.block_rate < block_threshold
            and self.branch not in self.test.project.default_branches
        )

    @property
    def new_fix(self) -> bool:
        threshold = (
            self.suite.failure_rate_upper_threshold
            if self.suite
            else Suite._meta.get_field("failure_rate_upper_threshold").default
        )
        return (
            self.status not in Status.test_failed() | {Status.TIMEDOUT}
            and self.test.failure_rate > threshold
            and self.branch not in self.test.project.default_branches
        )

    @property
    def rerunnable(self) -> bool:
        age = timezone.now() - self.created_at
        return (
            self.final
            and bool(self.run_url)
            and self.status in Status.merge_blocked()
            and self.branch not in self.test.project.default_branches
            and age > PENDING_THRESHOLD
        )

    @property
    def duration_humanized(self) -> str:
        return humanize_duration(self.duration)

    @property
    def logs(self) -> list:
        return self.metadata.get("logs") or []

    @property
    def logs_json(self) -> str:
        if not self.logs:
            return "[]"
        return json.dumps(self.logs, indent=2)

    @property
    def logs_table_data(self):
        if not self.logs:
            return {"headers": [], "rows": []}

        first_entry = self.logs[0]
        if not isinstance(first_entry, dict):
            return {"headers": [], "rows": []}

        headers = sorted(list(first_entry.keys()))
        rows = []

        for log_entry in self.logs:
            if isinstance(log_entry, dict):
                row = []
                for key in headers:
                    value = log_entry.get(key, "—")
                    row.append(value)
                rows.append(row)

        return {"headers": headers, "rows": rows}

    @property
    def prompt(self) -> str:
        """Plain markdown blob for pasting into AI coding agents."""
        return build_result_prompt(self)

    def normalize(self):
        """Pre-save logic to normalize payload values."""
        if self.duration:
            self.duration = round(self.duration, 3)
        if self.message:
            self.message = ANSI_ESCAPE.sub("", self.message)
        if self.target:
            self.target = Target.normalize(self.target)
        if self.platform:
            self.platform = Platform.normalize(self.platform)
        if self.browser:
            self.browser = Browser.normalize(self.browser)
        self.status = Status.normalize(
            self.status,
            markers=self.markers,
            message=self.message,
            error_indicators=self.test.error_indicators,
            skipped_indicators=self.test.skipped_indicators,
        )

    def finalize(self):
        """Post-save logic to mark other results as reruns."""
        if self.final:
            Result.objects.filter(
                test=self.test,
                commit=self.commit,
                target=self.target,
                platform=self.platform,
                browser=self.browser,
                final=True,
                id__lt=self.id,
            ).update(final=False)

    def save(self, *args, **kwargs):
        self.normalize()
        super().save(*args, **kwargs)
        self.finalize()
        self.test.update(self)
        self.test.save()

    def rerun(self) -> str | None:
        if not self.run_url:
            return None

        organization = Organization.objects.get(
            repository_index=self.test.project.repository_index
        )
        run_id = self.metadata["GITHUB_RUN_ID"]
        if url := rerun_failed_jobs(organization, self.test.project, int(run_id)):
            count = Result.objects.filter(
                test__project=self.test.project,
                branch=self.branch,
                commit=self.commit,
                final=True,
                metadata__GITHUB_RUN_ID=run_id,
            ).update(final=False)
            log.info(f"Marked {count} results to be rerun")

        return url
