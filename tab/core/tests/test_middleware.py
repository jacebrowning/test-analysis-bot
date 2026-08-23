import re
from unittest.mock import Mock

from django.http import HttpRequest, QueryDict

import pytest

from tab.core.middleware import (
    CrawlerPreviewMiddleware,
    ExceptionLoggingMiddleware,
)


def describe_exception_logging_middleware(expect):
    @pytest.fixture
    def middleware():
        return ExceptionLoggingMiddleware(get_response=Mock())

    @pytest.fixture
    def http_request():
        req = HttpRequest()
        req.method = "GET"
        req.path = "/test-path/"
        return req

    def it_logs_exceptions(mocker, middleware, http_request):
        mock_logger = mocker.patch("tab.core.middleware.log")
        exception = ValueError("Test error")

        try:
            raise exception
        except ValueError:
            result = middleware.process_exception(http_request, exception)

        expect(mock_logger.critical.called).is_(True)

        call_args = mock_logger.critical.call_args
        log_message = call_args[0][0]

        expect(log_message).contains("GET")
        expect(log_message).contains("/test-path/")
        expect(log_message).contains("ValueError")
        expect(log_message).contains("Test error")
        expect(result).is_(None)

    def it_includes_stack_trace_in_log(mocker, middleware, http_request):
        mock_logger = mocker.patch("tab.core.middleware.log")
        exception = RuntimeError("Runtime error")

        try:
            raise exception
        except RuntimeError:
            middleware.process_exception(http_request, exception)

        call_args = mock_logger.critical.call_args
        log_message = call_args[0][0]

        expect(log_message).contains("Traceback")
        expect(log_message).contains("RuntimeError")


def _extract_og_tags(html: str) -> dict[str, str]:
    tags = {}
    if match := re.search(r'<meta property="og:title" content="([^"]+)"', html):
        tags["og:title"] = match.group(1).strip()
    if match := re.search(r'<meta property="og:description" content="([^"]+)"', html):
        tags["og:description"] = match.group(1).strip()
    if match := re.search(r'<meta property="og:type" content="([^"]+)"', html):
        tags["og:type"] = match.group(1).strip()
    if match := re.search(r'<meta property="og:url" content="([^"]+)"', html):
        tags["og:url"] = match.group(1).strip()
    if match := re.search(r"<title>([^<]+)</title>", html):
        tags["title"] = match.group(1).strip()
    return tags


def describe_crawler_preview_middleware(expect):
    @pytest.fixture
    def middleware():
        return CrawlerPreviewMiddleware(get_response=Mock())

    @pytest.fixture
    def http_request(mocker):
        req = HttpRequest()
        req.method = "GET"
        req.path = "/projects/test-org/test-repo"
        req.GET = QueryDict()
        mocker.patch.object(
            req,
            "build_absolute_uri",
            return_value="http://testserver.com/projects/test-org/test-repo",
        )
        return req

    @pytest.fixture
    def test_data():
        """Create test data for OG tag tests."""
        from tab.projects.enums import Status
        from tab.projects.models import Project, Result, Test

        project = Project.objects.create(
            repository="https://github.com/test-org/test-repo"
        )
        nested_project = Project.objects.create(
            repository="https://github.com/test-org/nested/path/repo"
        )
        single_segment_project = Project.objects.create(
            repository="https://github.com/test-org"
        )
        test = Test.objects.create(project=project, name="my-test")
        nested_test = Test.objects.create(project=nested_project, name="my-test")
        result = Result.objects.create(
            test=test,
            status=Status.FAILED,
            branch="main",
            commit="abc123",
        )

        return {
            "project": project,
            "nested_project": nested_project,
            "single_segment_project": single_segment_project,
            "test": test,
            "nested_test": nested_test,
            "result": result,
        }

    @pytest.mark.django_db
    @pytest.mark.parametrize(
        ("path_template", "expected_tags"),
        [
            (
                "projects/test-org",
                {
                    "og:title": "Test Analysis Bot | test-org",
                    "og:description": "Keep tabs on unreliable tests by exploring all projects for this organization.",
                },
            ),
            (
                "projects/test-org/test-repo",
                {
                    "og:title": "Test Analysis Bot | test-org › test-repo",
                    "og:description": "Keep tabs on unreliable tests by exploring all tests reported for this project.",
                },
            ),
            (
                "projects/test-org/test-repo/metrics",
                {
                    "og:title": "Test Analysis Bot | test-org › test-repo",
                    "og:description": "Keep tabs on unreliable tests by viewing metrics for this project.",
                },
            ),
            (
                "projects/test-org/test-repo/results",
                {
                    "og:title": "Test Analysis Bot | test-org › test-repo",
                    "og:description": "Keep tabs on unreliable tests by browsing the latest results for this project.",
                },
            ),
            (
                "projects/test-org/test-repo/tests/disabled",
                {
                    "og:title": "Test Analysis Bot | test-org › test-repo",
                    "og:description": "Keep tabs on unreliable tests by viewing disabled tests for this project.",
                },
            ),
            (
                "projects/test-org/test-repo/tests/{test_id}",
                {
                    "og:title": "Test Analysis Bot | test-org › test-repo",
                    "og:description": "Test result history for: my-test",
                },
            ),
            (
                "projects/test-org/test-repo/tests/{test_id}/results/{result_id}",
                {
                    "og:title": "Test Analysis Bot | test-org › test-repo",
                    "og:description": "Failed test result for: my-test",
                },
            ),
        ],
    )
    def it_generates_og_tags(
        middleware,
        http_request,
        test_data,
        path_template,
        expected_tags,
    ):
        """Test that OG tags are generated correctly for various URL patterns."""
        # Build path with actual IDs
        format_kwargs = {
            "test_id": test_data["test"].id,
            "nested_test_id": test_data["nested_test"].id,
        }
        if "{result_id}" in path_template:
            format_kwargs["result_id"] = test_data["result"].id

        path = path_template.format(**format_kwargs)

        # Generate HTML
        html = middleware._get_html(http_request, path)

        expect(html).is_not(None)
        tags = _extract_og_tags(html)

        # Check expected tags, handling placeholders
        for key, expected_value in expected_tags.items():
            # Replace placeholders with actual values
            if "{result_id}" in expected_value:
                expected_value = expected_value.format(result_id=test_data["result"].id)
            elif "{default_branch}" in expected_value:
                expected_value = expected_value.format(
                    default_branch=test_data["project"].default_branch
                )
            elif "{test_id}" in expected_value:
                expected_value = expected_value.format(test_id=test_data["test"].id)
            elif "{nested_test_id}" in expected_value:
                expected_value = expected_value.format(
                    nested_test_id=test_data["nested_test"].id
                )

            expect(tags[key]) == expected_value
