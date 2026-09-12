from datetime import timedelta

from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.utils import timezone

import log

from tab.api.constants import TESTS_CACHE_KEY
from tab.projects.models import Project, Result, Test
from tab.releases.constants import RESULTS_TIMEOUT
from tab.releases.models import Release


class Command(BaseCommand):
    help = "Finalize releases and update recently reported tests"

    def handle(self, *args, **options):
        start = timezone.now()
        log.info(f"Started job at {start.strftime('%Y-%m-%d %H:%M:%S')}")
        self.finalize_releases()
        self.fetch_active_branches()
        self.update_bulk_tests()
        delta = timezone.now() - start
        log.info(f"Finished job after {delta.seconds // 60}:{delta.seconds % 60:02d}")

    def finalize_releases(self):
        cutoff = timezone.now() - RESULTS_TIMEOUT
        releases = Release.objects.filter(
            created_at__lt=cutoff, finalized_at__isnull=True
        )
        for release in releases:
            release.finalize()

    def fetch_active_branches(self):
        for project in Project.objects.all():
            Result.objects.get_active_branches(project)

    def update_bulk_tests(self):
        if test_ids := cache.get(TESTS_CACHE_KEY):
            cache.delete(TESTS_CACHE_KEY)
            tests = Test.objects.filter(id__in=test_ids)
            log.info(f"Updating tests: {tests.count()} (multiple projects)")
            for test in tests:
                if test.update():
                    log.info(f"Updated test: {test}")
                test.save()
                for result in (
                    test.results.filter(
                        created_at__gte=timezone.now() - timedelta(hours=1),
                        final=True,
                    )
                    .order_by(
                        "commit", "target", "platform", "browser", "branch", "-id"
                    )
                    .distinct("commit", "target", "platform", "browser", "branch")
                ):
                    result.finalize()
