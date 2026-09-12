from datetime import UTC, datetime, timedelta

from django.contrib.auth.models import User
from django.utils import timezone

import log
import pytest

from ..constants import DEFAULT_SUITE, FAILURE_RATE_EPSILON, RESTORATION_THRESHOLD
from ..enums import Platform, Status, Target
from ..models import Project, Result, Run, Suite, Test
from . import EXAMPLE_TESTS, ExampleTest
from .constants import TEST_PROMPT


def describe_project(expect):
    def it_formats_name():
        project = Project(repository="https://github.com/MyUser/my_repo")
        expect(project.name) == "MyUser › my_repo"

    def it_extracts_repository_index():
        project = Project(repository="https://github.com/MyUser/my_repo")
        expect(project.repository_index) == "https://github.com/MyUser"


def describe_suite(expect):
    @pytest.fixture
    def project():
        return Project.objects.create(repository="https://github.com/foo/bar")

    def describe_str(expect):
        def it_formats_name():
            suite = Suite(
                project=Project(repository="https://github.com/MyUser/my_repo"),
                name="my_suite",
            )
            expect(str(suite)) == "MyUser › my_repo › my_suite"

        def it_formats_name_with_default():
            suite = Suite(
                project=Project(repository="https://github.com/MyUser/my_repo"),
                name=DEFAULT_SUITE,
            )
            expect(str(suite)) == "MyUser › my_repo"

    def describe_update_average_setup_duration(expect, project: Project):
        @pytest.mark.django_db
        def it_returns_false_if_no_runs():
            project.save()
            suite: Suite = project.suites.create(name="my-suite")
            expect(suite.update_average_setup_duration()) == False

        @pytest.mark.django_db
        def it_computes_average_setup_duration():
            project.save()
            suite: Suite = project.suites.create(name="my-suite")
            now = timezone.now()
            for seconds in (2, 4, 6):
                started = now - timedelta(seconds=seconds + 10)
                tests_started = now - timedelta(seconds=10)
                suite.runs.create(
                    project=project,
                    branch="main",
                    commit=f"commit{seconds}",
                    setup_started_at=started,
                    tests_started_at=tests_started,
                )

            suite.average_setup_duration = -1
            expect(suite.update_average_setup_duration()) == True
            expect(suite.average_setup_duration) == 4.0

    def describe_update_average_tests_duration(expect, project: Project):
        @pytest.mark.django_db
        def it_computes_average_tests_duration():
            project.save()
            suite: Suite = project.suites.create(name="my-suite")
            now = timezone.now()
            for seconds in (10, 20, 30):
                suite.runs.create(
                    project=project,
                    branch="main",
                    commit=f"commit{seconds}",
                    tests_started_at=now - timedelta(seconds=seconds),
                    tests_finished_at=now,
                )

            suite.average_tests_duration = -1
            expect(suite.update_average_tests_duration()) == True
            expect(suite.average_tests_duration) == 20.0

    def describe_update_average_teardown_duration(expect, project: Project):
        @pytest.mark.django_db
        def it_computes_average_teardown_duration():
            project.save()
            suite: Suite = project.suites.create(name="my-suite")
            now = timezone.now()
            for seconds in (1, 2, 3):
                suite.runs.create(
                    project=project,
                    branch="main",
                    commit=f"commit{seconds}",
                    tests_finished_at=now - timedelta(seconds=seconds),
                    teardown_finished_at=now,
                )

            suite.average_teardown_duration = -1
            expect(suite.update_average_teardown_duration()) == True
            expect(suite.average_teardown_duration) == 2.0


def describe_test(expect):
    @pytest.fixture
    def project():
        return Project(repository="https://github.com/foo/bar")

    @pytest.fixture
    def suite(project):
        return Suite(project=project, name="my-suite")

    def describe_str(expect, project: Project):
        def it_formats_name():
            test = Test(project=project, name="my-test")
            expect(str(test)) == "my-test"

        def it_formats_name_with_suite(suite):
            test = Test(project=project, suite=suite, name="my-test")
            expect(str(test)) == "my-suite › my-test"

        def it_formats_name_with_default_suite(suite):
            suite.name = DEFAULT_SUITE
            test = Test(project=project, suite=suite, name="my-test")
            expect(str(test)) == "my-test"

    def describe_label(expect, project: Project, suite: Suite):

        @pytest.mark.django_db
        def it_handles_solo_suite():
            project.save()
            suite.save()
            test = Test.objects.create(project=project, suite=suite, name="my-test")
            expect(test.label) == "my-test"

        @pytest.mark.django_db
        def it_handles_multiple_suites():
            project.save()
            suite.save()
            Suite.objects.create(project=project, name="my-suite2")
            test = Test.objects.create(project=project, suite=suite, name="my-test")
            expect(test.label) == "my-suite › my-test"

    def describe_regex(expect):
        @pytest.mark.parametrize(("example_test"), EXAMPLE_TESTS)
        def it_escapes_special_characters(example_test: ExampleTest):
            log.debug(f"{example_test.case} case: {example_test.name!r}")
            log.debug(f"Expected regex: {example_test.regex!r}")
            if not example_test.regex:
                pytest.skip("No example regex provided for this case")
            test = Test(name=example_test.name)
            log.debug(f"Actual regex: {test.regex!r}")
            expect(test.regex) == example_test.regex

    def describe_substring(expect):
        @pytest.mark.parametrize(("example_test"), EXAMPLE_TESTS)
        def it_returns_plain_words(example_test: ExampleTest):
            log.debug(f"{example_test.case} case: {example_test.name!r}")
            log.debug(f"Expected substring: {example_test.substring!r}")
            if not example_test.substring:
                pytest.skip("No example substring provided for this case")
            test = Test(name=example_test.name)
            log.debug(f"Actual substring: {test.substring!r}")
            expect(test.substring) == example_test.substring

    def describe_disabled(expect, admin_user, project: Project):
        @pytest.mark.django_db
        def it_is_cleared_after_zero_failures():
            project.save()
            test = Test.objects.create(
                project=project,
                name="my-test",
                disabled_at=timezone.now() - RESTORATION_THRESHOLD,
                disabled_user=admin_user,
                disabled_reason="Waiting on infra.",
                disabled_tracker="https://example.com/ticket/1",
                failure_rate=0.25,
            )
            expect(bool(test.disabled_at)) == True

            test.failure_rate = 0
            test.save()
            expect(bool(test.disabled_at)) == False
            expect(test.disabled_reason) == ""
            expect(test.disabled_tracker) == None
            expect(test.disabled_user) == admin_user

        @pytest.mark.django_db
        def it_is_not_cleared_if_disabled_recently():
            project.save()
            test = Test.objects.create(
                project=project,
                name="my-test",
                disabled_at=timezone.now() - timedelta(days=1),
                disabled_user=admin_user,
                failure_rate=0.25,
            )
            expect(bool(test.disabled_at)) == True

            test.failure_rate = 0
            test.save()
            expect(bool(test.disabled_at)) == True

    def describe_enabled(expect, project: Project):
        @pytest.mark.django_db
        def it_is_true_if_last_result():
            project.save()
            test = Test.objects.create(project=project, name="my-test")
            test.results.create(test=test, branch="main", status=Status.PASSED)
            expect(test.enabled) == True

        @pytest.mark.django_db
        def it_is_false_if_last_result_is_skipped():
            project.save()
            test = Test.objects.create(project=project, name="my-test")
            test.results.create(test=test, branch="main", status=Status.SKIPPED)
            expect(test.enabled) == False

        @pytest.mark.django_db
        def it_is_true_if_any_results_for_latest_commit():
            project.save()
            test = Test.objects.create(project=project, name="my-test")
            test.results.create(
                test=test, branch="main", commit="abc123", status=Status.PASSED
            )
            test.results.create(
                test=test, branch="main", commit="abc123", status=Status.SKIPPED
            )
            expect(test.enabled) == True

    def describe_significant_branches(expect):
        def it_includes_default_and_original_branches():
            project = Project(
                repository="https://github.com/foo/bar",
                default_branches=["staging", "production"],
            )
            expect(project.default_branches) == ["staging", "production"]
            test = Test(project=project, name="my-test", original_branch="my-branch")
            expect(test.significant_branches) == ["my-branch", "staging", "production"]
            expect(project.default_branch) == "staging"

    def describe_update_failure_rate(expect, project: Project):
        @pytest.mark.django_db
        def it_returns_false_if_no_results():
            project.save()
            test = project.tests.create(name="my-test")
            expect(test.update_failure_rate()) == False
            expect(test.update_block_rate()) == False

        @pytest.mark.django_db
        def it_computes_failure_rate():
            project.save()
            test: Test = project.tests.create(
                name="my-test", original_branch="my-branch"
            )
            test.results.create(
                test=test, status=Status.PASSED, branch="my-branch", commit="a1"
            )
            test.results.create(
                test=test, status=Status.SKIPPED, branch="main", commit="b2"
            )
            test.results.create(
                test=test, status=Status.FAILED, branch="main", commit="b2"
            )
            test.results.create(
                test=test, status=Status.FAILED, branch="other", commit="c3"
            )

            test.failure_rate = -1
            expect(test.update_failure_rate()) == True
            expect(test.failure_rate) == 0.333333

            test.block_rate = -1
            expect(test.update_block_rate()) == True
            expect(test.block_rate) == 0.5

        @pytest.mark.django_db
        def it_stays_above_zero_if_disabled_and_recent_branch_failures():
            project.save()
            test: Test = project.tests.create(name="my-test")
            test.results.create(test=test, status=Status.PASSED, branch="main")
            test.failure_rate = -1
            test.disabled_at = timezone.now()
            test.save()

            test.results.create(test=test, status=Status.FAILED, branch="other")
            expect(test.failure_rate) == FAILURE_RATE_EPSILON

    def describe_update_average_duration(expect, project: Project):
        @pytest.mark.django_db
        def it_returns_false_if_no_results():
            project.save()
            test: Test = project.tests.create(name="my-test")
            expect(test.update_average_duration()) == False

        @pytest.mark.django_db
        def it_computes_average_duration():
            project.save()
            test: Test = project.tests.create(
                name="my-test", original_branch="my-branch"
            )
            test.results.create(
                test=test, status=Status.PASSED, branch="my-branch", duration=2
            )
            test.results.create(
                test=test, status=Status.FAILED, branch="main", duration=3
            )
            test.results.create(
                test=test, status=Status.FAILED, branch="other", duration=4
            )

            test.average_duration = -1
            expect(test.update_average_duration()) == True
            expect(test.average_duration) == 2.5


def describe_result(expect):
    def describe_markers(expect):

        def it_adds_disabled_marker_if_test_is_disabled():
            test = Test(name="test", disabled_at=timezone.now() - timedelta(days=1))
            result = Result(test=test, status=Status.FAILED, created_at=timezone.now())
            expect(result.markers) == ["disabled"]

    def describe_command(expect):
        def it_includes_checkout_command():
            test = Test(name="my-test")
            suite = Suite(name="my-suite", local_command="pytest {test.name}")
            result = Result(test=test, branch="my-branch", suite=suite)
            expect(result.command) == [
                (
                    "git fetch origin && git checkout my-branch && git reset --hard origin/my-branch",
                    True,
                ),
                ("\n", False),
                ("# then", False),
                ("\n", False),
                ("pytest my-test", True),
            ]

    def describe_originated_from_branch(expect):
        def it_detects_if_branch_is_original_and_not_default():
            project = Project()

            test = Test(project=project, name="test1", original_branch="my-branch")
            result = Result(test=test, branch="my-branch")
            expect(result.originated_from_branch) == True

            test = Test(project=project, name="test2", original_branch="my-branch")
            result = Result(test=test, branch="main")
            expect(result.originated_from_branch) == False

            test = Test(project=project, name="test3", original_branch="main")
            result = Result(test=test, branch="main")
            expect(result.originated_from_branch) == False

    def describe_prompt(expect):
        def it_matches_the_full_copy_text():
            baseline_at = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
            disabled_at = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)
            project = Project(repository="https://github.com/my-org/my-repo")
            suite = Suite(
                project=project,
                name="my-suite",
                local_command='make test-e2e-desktop E2E_GREP="my-suite.*{test.name}"',
            )
            disabled_user = User(username="tab_user", email="user@example.com")
            maintainer = User(username="maintainer", email="maintainer@example.com")
            test = Test(
                project=project,
                suite=suite,
                name="my-test",
                original_branch="my-branch",
                original_commit="abc123",
                maintainer=maintainer,
                failure_rate=0.099,
                block_rate=0.088,
                average_duration=4.2,
                disabled_at=disabled_at,
                disabled_reason="Waiting on infra.",
                disabled_tracker="https://example.com/ticket/1",
                disabled_user=disabled_user,
            )
            test.created_at = baseline_at
            logs = [{"step": "build", "rc": 1}]
            result = Result(
                test=test,
                suite=suite,
                status=Status.FAILED,
                message="AssertionError: expected 1 == 2",
                branch="my-branch",
                commit="abc123",
                duration=12.3,
                target=Target.DESKTOP.value,
                platform=Platform.MACOS.value,
                browser="Chromium",
                metadata={
                    "logs": logs,
                    "GITHUB_RUN_ID": "99",
                    "retry_count": 1,
                    "tags": ["disabled"],
                },
            )
            result.created_at = baseline_at
            expect(result.prompt) == TEST_PROMPT

    def describe_run_url(expect):
        def it_includes_run_id_and_pr_number():
            result = Result(
                test=Test(project=Project(repository="https://github.com/foo/bar")),
                branch="main",
                metadata={"GITHUB_RUN_ID": "123", "CI_PR_NUMBER": "456"},
            )
            expect(
                result.run_url
            ) == "https://github.com/foo/bar/actions/runs/123?pr=456"

    def describe_new_failure(expect):
        def it_is_true_for_failure_on_rarely_blocking_test():
            test = Test(
                project=Project(),
                name="test",
                block_rate=0.005,
            )
            result = Result(test=test, status=Status.FAILED, branch="my-branch")
            expect(result.new_failure) == True

        def it_is_false_when_failure_rate_is_high():
            test = Test(
                project=Project(),
                name="test",
                block_rate=0.005,
                failure_rate=0.10,
            )
            result = Result(test=test, status=Status.FAILED, branch="my-branch")
            expect(result.new_failure) == False

        def it_is_true_for_failure_new_to_branch():
            project = Project(default_branches=["main"])
            test = Test(
                project=project,
                name="test",
                original_branch="my-branch",
                block_rate=0.5,  # high block rate is ignored
            )
            result = Result(test=test, status=Status.FAILED, branch="my-branch")
            expect(result.new_failure) == True

    def describe_new_fix(expect):
        def is_true_for_pass_on_often_blocking_test():
            test = Test(project=Project(), name="test", failure_rate=0.51)
            result = Result(test=test, status=Status.PASSED)
            expect(result.new_fix) == True

        def is_always_false_for_ignored_failures():
            test = Test(project=Project(), name="test", failure_rate=0.51)
            result = Result(test=test, status=Status.DISABLED)
            expect(result.new_fix) == False

    def describe_rerunnable(expect):
        def it_is_false_for_new_results():
            test = Test(project=Project(), name="test")
            result = Result(
                test=test,
                status=Status.FAILED,
                branch="my-branch",
                final=True,
                metadata={"GITHUB_RUN_ID": "123"},
                created_at=timezone.now() - timedelta(minutes=5),
            )
            expect(result.rerunnable) == False

            result.created_at = timezone.now() - timedelta(minutes=15)
            expect(result.rerunnable) == True

    def describe_finalize(expect):
        @pytest.mark.django_db
        def it_demotes_results_for_same_commit():
            project = Project.objects.create(repository="https://github.com/foo/bar")
            test = project.tests.create(name="my-test")

            # Simulate a test that was rerun
            test.results.create(
                test=test, status=Status.FAILED, branch="my-branch", commit="a1"
            )
            test.results.create(
                test=test, status=Status.PASSED, branch="my-branch", commit="a1"
            )

            # Expect only one final result
            expect(test.results.filter(final=True).count()) == 1

        @pytest.mark.django_db
        def it_demotes_bulk_created_duplicates_with_identical_timestamps():
            project = Project.objects.create(repository="https://github.com/foo/bar")
            test = project.tests.create(name="my-test")
            now = timezone.now()
            results = Result.objects.bulk_create(
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

            results[-1].finalize()

            expect(test.results.filter(final=True).count()) == 1

        @pytest.mark.django_db
        def it_keeps_separate_results_per_target():
            project = Project.objects.create(repository="https://github.com/foo/bar")
            test = project.tests.create(name="my-test")
            Result.objects.create(
                test=test,
                status=Status.PASSED,
                branch="main",
                commit="a1",
                target=Target.WEB.value,
                final=True,
            )
            Result.objects.create(
                test=test,
                status=Status.PASSED,
                branch="main",
                commit="a1",
                target=Target.DESKTOP.value,
                final=True,
            )

            expect(test.results.filter(final=True).count()) == 2

        @pytest.mark.django_db
        def it_keeps_separate_results_per_browser():
            project = Project.objects.create(repository="https://github.com/foo/bar")
            test = project.tests.create(name="my-test")
            Result.objects.create(
                test=test,
                status=Status.PASSED,
                branch="main",
                commit="a1",
                browser="chromium",
                final=True,
            )
            Result.objects.create(
                test=test,
                status=Status.PASSED,
                branch="main",
                commit="a1",
                browser="firefox",
                final=True,
            )

            expect(test.results.filter(final=True).count()) == 2

    def describe_save(expect):
        @pytest.mark.django_db
        def it_cleans_message():
            test = Project.objects.create(
                repository="https://github.com/foo/bar"
            ).tests.create(name="test")
            result = Result.objects.create(
                test=test,
                status="failed",
                branch="main",
                commit="abc123",
                message="Error: [31mTimed out 5000ms waiting for [39m[2mexpect([22m[31mlocator[39m[2m).[22mnot[2m.[22mtoBeDisabled[2m()[22m",
            )
            expect(
                result.message
            ) == "Error: Timed out 5000ms waiting for expect(locator).not.toBeDisabled()"
