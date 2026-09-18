from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

import pytest

from tab.api.constants import TESTS_CACHE_KEY
from tab.core.management.commands.refreshdata import Command
from tab.core.models import Organization
from tab.projects.enums import Status, Target
from tab.projects.models import Project, Result, Suite
from tab.releases.enums import Type
from tab.releases.models import Environment, Release


@pytest.mark.django_db
def describe_handle(expect):
    def it_finalizes_retry_results_before_publishing_release_status(mocker):
        mock_update_status = mocker.patch("tab.releases.models.update_status")
        Organization.objects.create(repository_index="https://github.com/foo")
        project = Project.objects.create(repository="https://github.com/foo/bar")
        suite = Suite.objects.create(project=project, name="unit")
        test = project.tests.create(name="my-test", suite=suite)
        Result.objects.create(
            test=test,
            suite=suite,
            status=Status.PASSED,
            branch="main",
            commit="main1",
        )
        Result.objects.bulk_create(
            [
                Result(
                    test=test,
                    suite=suite,
                    status=Status.FAILED,
                    branch="feature",
                    commit="feature1",
                ),
                Result(
                    test=test,
                    suite=suite,
                    status=Status.PASSED,
                    branch="feature",
                    commit="feature1",
                ),
            ]
        )
        cache.set(TESTS_CACHE_KEY, {test.id})
        environment = Environment.objects.create(
            project=project,
            name=Type.REVIEW,
            url="https://example.com",
        )
        release = Release.objects.create(
            environment=environment,
            branch="feature",
            commit="feature1",
        )
        Release.objects.filter(pk=release.pk).update(
            created_at=timezone.now() - timedelta(hours=1)
        )

        Command().handle()

        final_result = test.results.get(branch="feature", final=True)
        expect(final_result.status) == Status.PASSED
        health = mock_update_status.call_args.args[-1]
        expect(health.state) == "success"
        expect(health.description) == "1 of 1 passing"
        release.refresh_from_db()
        expect(release.finalized_at).is_not(None)


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
