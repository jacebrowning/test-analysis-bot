from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand
from django.db.models import Q, Subquery
from django.utils import timezone

import log

from tab.projects.models import Project, Result, Run, Test
from tab.releases.enums import Type
from tab.releases.models import Environment, Release

CHUNK_SIZE = 1000
TIME_BUDGET = timedelta(minutes=45)
CORE_TZ = ZoneInfo("America/Los_Angeles")


def should_run(moment: datetime | None = None) -> bool:
    now = (moment or timezone.now()).astimezone(CORE_TZ)
    is_weekday = now.weekday() < 5
    is_off_peak = now.hour in {2, 3}
    return not is_weekday or is_off_peak


class Command(BaseCommand):
    help = "Delete inactive and stale test results"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be deleted without actually deleting anything",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Run stale data cleanup even during peak working hours",
        )

    def handle(self, *args, **options):
        if not options["force"] and not should_run():
            log.info("Skipping stale data cleanup during peak working hours")
            return

        dry_run = options["dry_run"]
        start = timezone.now()
        self._time_budget_warned = False
        self._time_budget_deadline = start + TIME_BUDGET
        log.info(f"Started job at {start.strftime('%Y-%m-%d %H:%M:%S')}")
        self.delete_stale_environments()
        self.delete_stale_releases()
        for project in Project.objects.all():
            count = 0
            if project.test_stale_threshold:
                count += self.delete_stale_tests(project, dry_run)
            if project.result_stale_threshold:
                count += self.delete_stale_runs(project, dry_run)
                count += self.delete_stale_results(project, dry_run)
            if count:
                project.cleaned_at = timezone.now()
                project.save()
        delta = timezone.now() - start
        log.info(f"Finished job after {delta.seconds // 60}:{delta.seconds % 60:02d}")

    @property
    def time_budget_remaining(self) -> bool:
        if timezone.now() < self._time_budget_deadline:
            return True
        if not self._time_budget_warned:
            log.warning("Time budget exhausted, deferring remaining cleanup")
            self._time_budget_warned = True
        return False

    def delete_stale_environments(self):
        cutoff = timezone.now() - timedelta(weeks=13)
        environments = Environment.objects.filter(
            name=Type.REVIEW, created_at__lt=cutoff
        ).exclude(
            # Preserve example environments with placeholders for identifiers
            placeholder=True,
        )
        for environment in environments:
            environment.delete()

    def delete_stale_releases(self):
        cutoff = timezone.now() - timedelta(weeks=26)
        releases = Release.objects.filter(created_at__lt=cutoff)
        for release in releases:
            release.delete()

    def delete_stale_tests(self, project: Project, dry_run: bool) -> int:
        log.info(f"Cleaning up stale tests: {project}")

        cutoff = timezone.now() - project.test_stale_threshold
        tests = Test.objects.filter(
            project=project,
            updated_at__lt=cutoff,
        ).order_by()  # remove default ordering for performance

        if dry_run:
            count = tests.count()
            log.warning(f"Would delete {count} tests: {project}")
            return 0

        deleted = 0
        while self.time_budget_remaining:
            chunk = tests.values("pk")[:CHUNK_SIZE]
            chunk_count, _ = (
                Test.objects.filter(pk__in=Subquery(chunk)).order_by().delete()
            )
            if not chunk_count:
                break
            deleted += chunk_count
            log.info(f"Deleted {chunk_count} chunk tests: {project}")

        log.info(f"Deleted {deleted} total tests: {project}")
        return deleted

    def delete_stale_runs(self, project: Project, dry_run: bool) -> int:
        log.info(f"Cleaning up stale runs: {project}")
        cutoff = timezone.now() - (project.result_stale_threshold * 5)
        runs = (
            Run.objects.filter(project=project)
            .filter(setup_started_at__lt=cutoff)
            .order_by()
        )

        if dry_run:
            count = runs.count()
            log.warning(f"Would delete {count} runs: {project}")
            return 0

        deleted = 0
        while self.time_budget_remaining:
            chunk = runs.values("pk")[:CHUNK_SIZE]
            chunk_count, _ = (
                Run.objects.filter(pk__in=Subquery(chunk)).order_by().delete()
            )
            if not chunk_count:
                break
            deleted += chunk_count
            log.info(f"Deleted {chunk_count} chunk runs: {project}")

        log.info(f"Deleted {deleted} total runs: {project}")
        return deleted

    def delete_stale_results(self, project: Project, dry_run: bool) -> int:
        log.info(f"Cleaning up stale results: {project}")

        short_cutoff = timezone.now() - project.result_stale_threshold
        long_cutoff = timezone.now() - (project.result_stale_threshold * 5)
        results = (
            Result.objects.filter(test__project=project)
            .filter(
                # Delete non-default branch results older than the threshold
                (
                    Q(created_at__lt=short_cutoff)
                    & ~Q(branch__in=project.default_branches)
                )
                # Delete default branch results older than the threshold
                | (
                    Q(branch__in=project.default_branches)
                    & Q(created_at__lt=long_cutoff)
                )
            )
            .order_by()  # remove default ordering for performance
        )

        if dry_run:
            count = results.count()
            log.warning(f"Would delete {count} total results: {project}")
            return 0

        deleted = 0
        while self.time_budget_remaining:
            chunk = results.values("pk")[:CHUNK_SIZE]
            chunk_count, _ = (
                Result.objects.filter(pk__in=Subquery(chunk)).order_by().delete()
            )
            if not chunk_count:
                break
            deleted += chunk_count
            log.info(f"Deleted {chunk_count} chunk results: {project}")

        log.info(f"Deleted {deleted} total results: {project}")
        return deleted
