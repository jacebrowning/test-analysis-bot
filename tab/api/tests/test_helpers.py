from pathlib import Path

from django.core.cache import cache

import pytest

from tab.projects.models import Project, Suite, Test

from ..constants import TESTS_CACHE_KEY
from ..helpers import parse_junit_xml


def describe_parse_junit_xml(expect):

    @pytest.fixture
    def content():
        path = Path(__file__).parent / "files" / "junit-nextest.xml"
        return path.read_text()

    @pytest.mark.django_db
    def it_recomputes_metrics_from_each_result(content):
        cache.delete(TESTS_CACHE_KEY)
        project = Project.objects.create(repository="https://github.com/foo/bar")
        suite = Suite.objects.create(project=project)

        results = parse_junit_xml(
            content, project, suite, branch="main", commit="abc123", metadata={}
        )

        expect(len(results)) == 25
        expect(results[0].test.failure_rate) >= 0.0
        expect(cache.get(TESTS_CACHE_KEY)) == None

    @pytest.mark.django_db
    def it_caches_test_ids_for_post_processing_when_deferred(content):
        cache.delete(TESTS_CACHE_KEY)
        project = Project.objects.create(repository="https://github.com/foo/bar")
        suite = Suite.objects.create(project=project)

        results = parse_junit_xml(
            content,
            project,
            suite,
            branch="main",
            commit="abc123",
            metadata={},
            deferred=True,
        )

        expect(len(results)) == 25
        expect(results[0].test.failure_rate) == -1  # deferred post-processing
        expect(cache.get(TESTS_CACHE_KEY)) == {result.test.id for result in results}

    @pytest.mark.django_db
    @pytest.mark.parametrize("deferred", [False, True])
    def it_updates_test_suite_when_changed(content, deferred):
        project = Project.objects.create(repository="https://github.com/foo/bar")
        suite1 = Suite.objects.create(project=project, name="suite1")
        suite2 = Suite.objects.create(project=project, name="suite2")
        test = Test.objects.create(
            project=project,
            name="nextest-run › kcl-derive-docs › tests::test_get_inner_array_type",
        )

        parse_junit_xml(
            content,
            project,
            suite1,
            branch="other",
            commit="abc123",
            metadata={},
            deferred=deferred,
        )
        test.refresh_from_db()
        expect(test.suite) == suite1
        expect(test.original_branch) == "other"
        expect(test.original_commit) == "abc123"
        expect(test.original_metadata) == {}

        parse_junit_xml(
            content,
            project,
            suite2,
            branch="main",
            commit="def456",
            metadata={},
            deferred=deferred,
        )
        test.refresh_from_db()
        expect(test.suite) == suite2
        expect(test.original_branch) == "other"
        expect(test.original_commit) == "abc123"
        expect(test.original_metadata) == {}

    @pytest.mark.django_db
    def it_fixes_pytest_indentation():
        path = Path(__file__).parent / "files" / "junit-pytest.xml"
        content = path.read_text()

        project = Project.objects.create(repository="https://github.com/foo/bar")
        suite = Suite.objects.create(project=project)

        results = parse_junit_xml(
            content, project, suite, branch="main", commit="abc123", metadata={}
        )

        result = next(r for r in results if r.message)
        expect(result.message).contains("    @requires_engine\n")
        expect(result.message).contains("    @pytest.mark.asyncio\n")
        expect(result.message).contains("    async def test_")

    @pytest.mark.django_db
    @pytest.mark.parametrize("deferred", [False, True])
    def it_parses_tags_from_testcase_properties(deferred):
        content = """
        <testsuites>
          <testsuite name="suite">
            <testcase name="tagged" classname="mod">
              <properties>
                <property name="tag" value="@slow"/>
                <property name="tag" value="@slow"/>
                <property name="tag" value="@flaky"/>
                <property name="tag" value=""/>
                <property name="tags" value="foo,bar"/>
              </properties>
            </testcase>
            <testcase name="untagged" classname="mod"/>
          </testsuite>
        </testsuites>
        """
        project = Project.objects.create(repository="https://github.com/foo/bar")
        suite = Suite.objects.create(project=project)
        shared = {"EXTRA": "foobar", "tags": ["existing"]}

        results = parse_junit_xml(
            content,
            project,
            suite,
            branch="main",
            commit="abc123",
            metadata=shared,
            deferred=deferred,
        )

        by_name = {r.test.name: r for r in results}
        tagged = by_name["suite › mod › tagged"]
        untagged = by_name["suite › mod › untagged"]
        expect(tagged.metadata["tags"]) == [
            "existing",
            "@slow",
            "@flaky",
            "foo",
            "bar",
        ]
        expect(tagged.metadata["EXTRA"]) == "foobar"
        expect(untagged.metadata) == {"EXTRA": "foobar", "tags": ["existing"]}
        expect(shared) == {"EXTRA": "foobar", "tags": ["existing"]}
