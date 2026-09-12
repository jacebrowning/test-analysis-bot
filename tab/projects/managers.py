from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist
from django.db import models
from django.db.models import Count, Q
from django.utils import timezone

import log

from .constants import (
    ACTIVE_BRANCHES_CACHE_KEY,
    ACTIVE_BRANCHES_CACHE_TIMEOUT,
    ALL_BRANCHES,
    DURATION_CACHE_KEY,
    DURATION_CACHE_TIMEOUT,
    EXPIRED_THRESHOLD,
    PENDING_THRESHOLD,
)
from .enums import Status
from .types import Health

if TYPE_CHECKING:
    from .models import Project, Run, Suite, Test


def safe_get(key, *, fallback=None):
    try:
        return cache.get(key)
    except Exception as e:
        log.error(f"Cache lookup failed: {e}")
        return fallback


def safe_set(key, value, *, timeout=None):
    try:
        cache.set(key, value, timeout=timeout)
    except Exception as e:
        log.error(f"Cache write failed: {e}")


class ProjectManager(models.Manager):

    @staticmethod
    def clean_repository(value: str):
        return value.removesuffix(".git").strip("/")

    def from_repository(self, project_url: str, organization_url: str = ""):
        project_url = self.clean_repository(project_url)

        if "://" not in project_url or project_url.count("/") < 4:
            raise ValueError(f"Invalid repository URL: {project_url}")

        if organization_url and not project_url.startswith(organization_url + "/"):
            raise ValueError(f"Repository not part of your organization: {project_url}")

        try:
            project = self.get(repository__iexact=project_url)
            log.info(f"Found project: {project}")
        except ObjectDoesNotExist:
            project = self.create(repository=project_url)
            log.info(f"Created project: {project}")

        return project


class SuiteManager(models.Manager):
    def active(self):
        return (
            self.get_queryset()
            .annotate(num_tests=Count("tests"))
            .filter(num_tests__gt=0)
            .order_by("name")
        )


class TestManager(models.Manager):
    def get_parent_and_child_tests(self, test: Test) -> tuple[Test | None, list[Test]]:
        parent_suite = test.suite.parent if test.suite else None
        parent_test: Test | None = (
            self.filter(  # type: ignore[assignment]
                project=parent_suite.project,
                suite=parent_suite,
                name=test.name,
            )
            .select_related("project")
            .first()
            if parent_suite
            else None
        )
        if test.suite and test.suite.children.exists():
            child_suites = test.suite.children.all()
        elif parent_suite:
            child_suites = parent_suite.children.all()
        else:
            child_suites = []  # type: ignore[assignment]
        child_tests: list[Test] = (
            list(
                self.filter(suite__in=child_suites, name=test.name)  # type: ignore[arg-type]
                .exclude(pk=test.pk)
                .select_related("project")
                .order_by("project__repository")
            )
            if child_suites
            else []
        )
        return parent_test, child_tests


class ResultManager(models.Manager):
    def filter_with_default_branches(self, test: Test, branch: str | None):
        if branch == ALL_BRANCHES:
            results = self.filter(test=test)
        elif branch:
            branches = [branch] + test.significant_branches
            results = self.filter(test=test, branch__in=branches)
        else:
            results = self.filter(test=test, branch__in=test.significant_branches)
        return results.select_related("suite", "test__project", "test__suite")

    def get_active_branches(self, project: Project) -> list[str]:
        """Get a cached list of active branches for a project."""
        cache_key = f"{ACTIVE_BRANCHES_CACHE_KEY}:{project.pk}"
        cached = safe_get(cache_key)
        if cached is not None:
            return list(cached)

        results = self.filter(test__project=project).distinct().order_by("branch")
        if project.branch_inactive_threshold:
            results = results.filter(
                created_at__gte=timezone.now() - project.branch_inactive_threshold
            )
        branches = list(results.values_list("branch", flat=True))
        if len(branches) > 1:
            safe_set(cache_key, branches, timeout=ACTIVE_BRANCHES_CACHE_TIMEOUT)
        return branches

    def get_latest_commit(self, project: Project, branch: str) -> str | None:
        queryset = self.filter(test__project=project, branch=branch).order_by(
            "-created_at"
        )
        return queryset.values_list("commit", flat=True).first()

    def get_health(
        self, project: Project, commit: str | None, *, final: bool = False
    ) -> Health:
        assert "github.com" in project.repository, "Only GitHub is supported for now"
        if not commit:
            return Health(total=0, state="pending", description="no results")

        latest_commit = self.get_latest_commit(project, project.default_branch)
        latest_aggregate = self.filter(
            test__project=project,
            commit=latest_commit,
            branch=project.default_branch,
            final=True,
        ).aggregate(
            total=Count("id"),
            passed=Count("id", filter=Q(status__in=Status.merge_allowed())),
        )
        expected_total = latest_aggregate["total"]
        expected_passed = latest_aggregate["passed"]

        results = self.filter(test__project=project, commit=commit, final=True)
        aggregate = results.aggregate(
            total=Count("id"),
            passed=Count("id", filter=Q(status__in=Status.merge_allowed())),
            failed=Count("id", filter=Q(status__in=Status.merge_blocked())),
        )
        total = aggregate["total"]
        passed = aggregate["passed"]
        failed = aggregate["failed"]
        pending = max(0, expected_passed - passed)

        if (
            release_created_at := project.environments.filter(releases__commit=commit)
            .values_list("releases__created_at", flat=True)
            .order_by("releases__created_at")
            .first()
        ):
            age = timezone.now() - release_created_at
        else:
            age = timedelta()

        log.info(
            f"Processed expected results for {project.path} @ {commit[:7]}: "
            f"{passed}/{expected_passed} passing, {total}/{expected_total} total"
            f", started {round(age.total_seconds() / 60, 1)} minutes ago"
        )
        if pending and age < PENDING_THRESHOLD and not final:
            state = "pending"
        elif failed:
            state = "failure"
        else:
            state = "success"

        description = f"{passed} of {total} passing"
        if state == "pending":
            if pending >= 1000:
                description += f", {pending/1000:.1f}k more results expected"
            else:
                s = "" if pending == 1 else "s"
                description += f", {pending} more result{s} expected"
        elif failed:
            failed_results = results.filter(
                status__in=Status.merge_blocked()
            ).select_related("test", "test__project", "suite")
            new_failed = len([r for r in failed_results if r.new_failure])  # type: ignore[attr-defined]
            if new_failed:
                s = "" if new_failed == 1 else "s"
                description += f", {new_failed} new failure{s}"

        return Health(total=total, state=state, description=description)


class RunManager(models.Manager):
    def track_step(
        self,
        suite: Suite,
        branch: str,
        commit: str,
        step: str,
        metadata: dict,
    ) -> tuple[Run | None, bool]:
        assert step in ("setup", "start", "finish", "teardown"), f"Invalid step: {step}"

        run: Run
        if step == "setup":
            run, created = self.get_or_create(  # type: ignore[assignment]
                project=suite.project,
                suite=suite,
                branch=branch,
                commit=commit,
                defaults={"metadata": metadata},
            )
            if created:
                log.info(f"Created run: {run}")
            else:
                log.info(f"Found run: {run}")
        else:
            try:
                run = self.get(  # type: ignore[assignment]
                    project=suite.project,
                    suite=suite,
                    branch=branch,
                    commit=commit,
                )
            except ObjectDoesNotExist:
                return None, False
            else:
                created = False
                log.info(f"Found run: {run}")

        now = timezone.now()
        expired = (
            run.tests_started_at and now - run.tests_started_at > EXPIRED_THRESHOLD
        )
        if step == "setup" and not run.setup_started_at:
            run.setup_started_at = now
        elif step == "start" and not run.tests_started_at:
            run.tests_started_at = now
        elif step == "finish" and (not run.tests_finished_at or not expired):
            if run.teardown_finished_at and run.teardown_finished_at < now:
                assert run.tests_finished_at, f"Invalid state: {run}"
                delta = run.teardown_finished_at - run.tests_finished_at
                run.teardown_finished_at = now + delta
            run.tests_finished_at = now
        elif step == "teardown" and (not run.teardown_finished_at or not expired):
            run.tests_finished_at = run.tests_finished_at or now
            run.teardown_finished_at = now
        run.save()

        if (
            (step == "start" and run.setup_duration > 0)
            or (step == "finish" and run.tests_duration > 0)
            or (step == "teardown" and run.teardown_duration > 0)
        ):
            suite = run.suite
            if suite.update(run):
                suite.save(
                    update_fields=[
                        "average_setup_duration",
                        "average_tests_duration",
                        "average_teardown_duration",
                        "updated_at",
                    ]
                )

        return run, created

    def get_setup_duration(
        self, suite: Suite | None, branch: str | None = None, commit: str | None = None
    ) -> float:
        """Get a cached value for the setup duration of a suite."""
        if suite is None:
            return 0.0
        if branch is None and suite.average_setup_duration >= 0:
            return suite.average_setup_duration
        cache_key = f"{DURATION_CACHE_KEY}:setup:{suite.id}:{branch}:{commit}"
        duration = safe_get(cache_key, fallback=0.0)
        if duration is None:
            query = self.filter(suite=suite, branch=branch)
            if commit:
                query = query.filter(commit=commit)
            run: Run = query.first()  # type: ignore[assignment]
            duration = run.setup_duration if run else 0.0
            safe_set(cache_key, duration, timeout=DURATION_CACHE_TIMEOUT)
        return duration

    def get_tests_duration(
        self, suite: Suite | None, branch: str, commit: str | None = None
    ) -> float:
        """Get a cached value for the tests duration of a suite."""
        if suite is None:
            return 0.0
        cache_key = f"{DURATION_CACHE_KEY}:tests:{suite.id}:{branch}:{commit}"
        duration = safe_get(cache_key, fallback=0.0)
        if duration is None:
            query = self.filter(suite=suite, branch=branch)
            if commit:
                query = query.filter(commit=commit)
            run: Run = query.first()  # type: ignore[assignment]
            duration = run.tests_duration if run else 0.0
            safe_set(cache_key, duration, timeout=DURATION_CACHE_TIMEOUT)
        return duration

    def get_teardown_duration(
        self, suite: Suite | None, branch: str, commit: str | None = None
    ) -> float:
        """Get a cached value for the teardown duration of a suite."""
        if suite is None:
            return 0.0
        cache_key = f"{DURATION_CACHE_KEY}:teardown:{suite.id}:{branch}:{commit}"
        duration = safe_get(cache_key, fallback=0.0)
        if duration is None:
            query = self.filter(suite=suite, branch=branch)
            if commit:
                query = query.filter(commit=commit)
            run: Run = query.first()  # type: ignore[assignment]
            duration = run.teardown_duration if run else 0.0
            safe_set(cache_key, duration, timeout=DURATION_CACHE_TIMEOUT)
        return duration
