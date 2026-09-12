from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

import pytest

from tab.api.constants import TESTS_CACHE_KEY
from tab.core.management.commands.refreshdata import Command
from tab.projects.enums import Status, Target
from tab.projects.models import Project, Result


@pytest.mark.django_db
def describe_update_bulk_tests(expect):
    def it_finalizes_recent_bulk_created_duplicates():
        project = Project.objects.create(repository="https://github.com/foo/bar")
        test = project.tests.create(name="my-test")
        now = timezone.now()
        Result.objects.bulk_create(
            [
                Result(
                    test=test,
                    status=Status.PASSED,
                    branch="main",
                    commit="a1",
                    final=True,
                    created_at=now,
                ),
                Result(
                    test=test,
                    status=Status.PASSED,
                    branch="main",
                    commit="a1",
                    final=True,
                    created_at=now,
                ),
            ]
        )
        cache.set(TESTS_CACHE_KEY, {test.id})

        Command().update_bulk_tests()

        expect(test.results.filter(final=True).count()) == 1

    def it_finalizes_each_target_separately():
        project = Project.objects.create(repository="https://github.com/foo/bar")
        test = project.tests.create(name="my-test")
        now = timezone.now()
        Result.objects.bulk_create(
            [
                Result(
                    test=test,
                    status=Status.PASSED,
                    branch="main",
                    commit="a1",
                    target=Target.WEB.value,
                    final=True,
                    created_at=now,
                ),
                Result(
                    test=test,
                    status=Status.PASSED,
                    branch="main",
                    commit="a1",
                    target=Target.WEB.value,
                    final=True,
                    created_at=now,
                ),
                Result(
                    test=test,
                    status=Status.PASSED,
                    branch="main",
                    commit="a1",
                    target=Target.DESKTOP.value,
                    final=True,
                    created_at=now,
                ),
                Result(
                    test=test,
                    status=Status.PASSED,
                    branch="main",
                    commit="a1",
                    target=Target.DESKTOP.value,
                    final=True,
                    created_at=now,
                ),
            ]
        )
        cache.set(TESTS_CACHE_KEY, {test.id})

        Command().update_bulk_tests()

        expect(test.results.filter(final=True).count()) == 2

    def it_ignores_non_final_results_when_selecting():
        project = Project.objects.create(repository="https://github.com/foo/bar")
        test = project.tests.create(name="my-test")
        now = timezone.now()
        Result.objects.bulk_create(
            [
                Result(
                    test=test,
                    status=Status.FAILED,
                    branch="main",
                    commit="a1",
                    final=False,
                    created_at=now,
                ),
                Result(
                    test=test,
                    status=Status.PASSED,
                    branch="main",
                    commit="a1",
                    final=True,
                    created_at=now - timedelta(seconds=1),
                ),
                Result(
                    test=test,
                    status=Status.PASSED,
                    branch="main",
                    commit="a1",
                    final=True,
                    created_at=now - timedelta(seconds=1),
                ),
            ]
        )
        cache.set(TESTS_CACHE_KEY, {test.id})

        Command().update_bulk_tests()

        expect(test.results.filter(final=True).count()) == 1
