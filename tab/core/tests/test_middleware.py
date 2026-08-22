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
    if match := re.search(r'<meta property="og:image" content="([^"]+)"', html):
        tags["og:image"] = match.group(1).strip()
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

        def build_absolute_uri(location=None):
            if location is None:
                location = req.path
            if location.startswith("http://") or location.startswith("https://"):
                return location
            if not location.startswith("/"):
                location = f"/{location}"
            return f"http://testserver.com{location}"

        mocker.patch.object(req, "build_absolute_uri", side_effect=build_absolute_uri)
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
        from tab.projects.models import Suite

        suite = Suite.objects.create(project=project, name="unit")

        return {
            "project": project,
            "nested_project": nested_project,
            "single_segment_project": single_segment_project,
            "test": test,
            "nested_test": nested_test,
            "result": result,
            "suite": suite,
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
                    "og:image": "http://testserver.com/projects/test-org.png",
                },
            ),
            (
                "projects/test-org/test-repo",
                {
                    "og:title": "Test Analysis Bot | test-org › test-repo",
                    "og:description": "Keep tabs on unreliable tests by exploring all tests reported for this project.",
                    "og:image": "http://testserver.com/projects/test-org/test-repo.png",
                },
            ),
            (
                "projects/test-org/test-repo/metrics",
                {
                    "og:title": "Test Analysis Bot | test-org › test-repo",
                    "og:description": "Keep tabs on unreliable tests by viewing metrics for this project.",
                    "og:image": "http://testserver.com/projects/test-org/test-repo/metrics.png",
                },
            ),
            (
                "projects/test-org/test-repo/results",
                {
                    "og:title": "Test Analysis Bot | test-org › test-repo",
                    "og:description": "Keep tabs on unreliable tests by browsing the latest results for this project.",
                    "og:image": "http://testserver.com/projects/test-org/test-repo/results.png",
                },
            ),
            (
                "projects/test-org/test-repo/tests/disabled",
                {
                    "og:title": "Test Analysis Bot | test-org › test-repo",
                    "og:description": "Keep tabs on unreliable tests by viewing disabled tests for this project.",
                    "og:image": "http://testserver.com/projects/test-org/test-repo/tests/disabled.png",
                },
            ),
            (
                "projects/test-org/test-repo/tests/{test_id}",
                {
                    "og:title": "Test Analysis Bot | test-org › test-repo",
                    "og:description": "Test result history for: my-test",
                    "og:image": "http://testserver.com/projects/test-org/test-repo/tests/{test_id}.png",
                },
            ),
            (
                "projects/test-org/test-repo/tests/{test_id}/results/{result_id}",
                {
                    "og:title": "Test Analysis Bot | test-org › test-repo",
                    "og:description": "Failed test result for: my-test",
                    "og:image": "http://testserver.com/projects/test-org/test-repo/tests/{test_id}/results/{result_id}.png",
                },
            ),
            (
                "projects/test-org/test-repo/suite/{suite_id}",
                {
                    "og:title": "Test Analysis Bot | test-org › test-repo",
                    "og:description": "Suite tests for: unit",
                    "og:image": "http://testserver.com/projects/test-org/test-repo/suite/{suite_id}.png",
                },
            ),
            (
                "projects/test-org/test-repo/results/suite/{suite_id}",
                {
                    "og:title": "Test Analysis Bot | test-org › test-repo",
                    "og:description": "Latest results for suite: unit",
                    "og:image": "http://testserver.com/projects/test-org/test-repo/results/suite/{suite_id}.png",
                },
            ),
            (
                "projects",
                {
                    "og:title": "Test Analysis Bot",
                    "og:description": "Keep tabs on unreliable tests by exploring all projects.",
                    "og:image": "http://testserver.com/projects.png",
                },
            ),
            (
                "releases",
                {
                    "og:title": "Test Analysis Bot | Releases",
                    "og:description": "Keep tabs on unreliable tests by exploring release history.",
                    "og:image": "http://testserver.com/releases.png",
                },
            ),
            (
                "projects/test-org/test-repo/tests/{test_id}/export",
                {
                    "og:title": "Test Analysis Bot",
                    "og:description": "Keep tabs on unreliable tests.",
                    "og:image": "http://testserver.com/projects/test-org/test-repo/tests/{test_id}/export.png",
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
            "suite_id": test_data["suite"].id,
            "result_id": test_data["result"].id,
        }
        path = path_template.format(**format_kwargs)

        # Generate HTML
        html = middleware._get_html(http_request, path)

        expect(html).is_not(None)
        tags = _extract_og_tags(html)

        # Check expected tags, handling placeholders
        for key, expected_value in expected_tags.items():
            # Replace placeholders with actual values
            if any(
                token in expected_value
                for token in ("{result_id}", "{test_id}", "{suite_id}")
            ):
                expected_value = expected_value.format(
                    test_id=test_data["test"].id,
                    result_id=test_data["result"].id,
                    suite_id=test_data["suite"].id,
                )
            elif "{default_branch}" in expected_value:
                expected_value = expected_value.format(
                    default_branch=test_data["project"].default_branch
                )
            elif "{nested_test_id}" in expected_value:
                expected_value = expected_value.format(
                    nested_test_id=test_data["nested_test"].id
                )

            expect(tags[key]) == expected_value

    @pytest.mark.django_db
    @pytest.mark.parametrize(
        "path_template",
        [
            "projects",
            "releases",
            "projects/test-org",
            "projects/test-org/test-repo",
            "projects/test-org/test-repo/metrics",
            "projects/test-org/test-repo/results",
            "projects/test-org/test-repo/tests/disabled",
            "projects/test-org/test-repo/tests/{test_id}",
            "projects/test-org/test-repo/tests/{test_id}/results/{result_id}",
            "projects/test-org/test-repo/suite/{suite_id}",
            "projects/test-org/test-repo/results/suite/{suite_id}",
            "projects/test-org/test-repo/tests/{test_id}/export",
        ],
    )
    def it_serves_preview_png_for_crawler_paths(test_data, client, path_template):
        from io import BytesIO

        from PIL import Image

        test = test_data["test"]
        test.failure_rate = 0.42
        test.save(update_fields=["failure_rate"])
        path = path_template.format(
            test_id=test.id,
            result_id=test_data["result"].id,
            suite_id=test_data["suite"].id,
        )

        response = client.get(f"/{path}.png")

        expect(response.status_code) == 200
        expect(response["Content-Type"]) == "image/png"
        image = Image.open(BytesIO(response.content))
        expect(image.size) == (512, 512)
        expect(image.format) == "PNG"
