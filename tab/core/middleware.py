import sys
import time
import traceback

from django.contrib.auth import logout as auth_logout
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from django.utils.deprecation import MiddlewareMixin

import log
from mozilla_django_oidc.middleware import SessionRefresh

from tab.core.oidc import uses_oidc_backend
from tab.core.previews import (
    render_disabled_preview_png,
    render_logo_preview_png,
    render_metrics_preview_png,
    render_project_preview_png,
    render_result_preview_png,
    render_results_preview_png,
    render_suite_preview_png,
    render_test_preview_png,
)
from tab.projects.enums import Status
from tab.projects.models import Project, Result, Suite, Test


class AuthentikSessionRefresh(SessionRefresh):
    """Refresh safe requests and reject stale OIDC sessions before unsafe ones."""

    def process_request(self, request: HttpRequest) -> HttpResponse | None:
        if self._has_invalid_oidc_session(request):
            auth_logout(request)
            return None

        if request.method == "GET":
            return super().process_request(request)

        if not self._has_expired_oidc_session(request):
            return None

        auth_logout(request)
        return HttpResponse(
            "OIDC session expired. Please sign in again.",
            status=401,
        )

    def _has_invalid_oidc_session(self, request: HttpRequest) -> bool:
        return uses_oidc_backend(request) and not request.user.is_authenticated

    def _has_expired_oidc_session(self, request: HttpRequest) -> bool:
        if not uses_oidc_backend(request) or not request.user.is_authenticated:
            return False
        if request.path in self.exempt_urls or any(
            pattern.match(request.path) for pattern in self.exempt_url_patterns
        ):
            return False

        expiration = request.session.get("oidc_id_token_expiration", 0)
        return not isinstance(expiration, (int, float)) or expiration <= time.time()


class ExceptionLoggingMiddleware(MiddlewareMixin):
    """
    Middleware to catch and log exceptions with single-line formatted stack traces.

    This ensures that when exceptions occur in the Django application, they are logged
    in a format that works well with log aggregation systems like Axiom by keeping
    the entire stack trace on a single log line.
    """

    def process_exception(
        self, request: HttpRequest, exception: Exception
    ) -> HttpResponse | None:
        # Get the full exception info
        exc_type, exc_value, exc_traceback = sys.exc_info()

        # Format the traceback as a string
        tb_lines = traceback.format_exception(exc_type, exc_value, exc_traceback)
        tb_text = "".join(tb_lines)

        # Log the exception (the custom formatter will escape newlines)
        log.critical(
            f"Unhandled exception for {request.method} {request.path}\n{tb_text}",
            exc_info=False,  # Don't include exc_info since we're formatting it ourselves
        )

        return None


class CrawlerPreviewMiddleware(MiddlewareMixin):
    """
    Middleware to return minimal OG tags for crawlers like Slack.

    Appending ``.png`` to a supported URL serves a public preview image
    (no auth), so Slack's image proxy can fetch ``og:image``.
    """

    def process_request(self, request: HttpRequest) -> HttpResponse | None:
        if request.method != "GET":
            return None

        path = request.path
        if path.endswith(".png"):
            if image := self._get_png(path[: -len(".png")]):
                return HttpResponse(image, content_type="image/png")
            return None

        if request.user.is_authenticated:
            return None

        user_agent = request.user_agent  # type: ignore[attr-defined]
        if not user_agent or not user_agent.is_bot:
            return None

        if html := self._get_html(request, path):
            return HttpResponse(html, content_type="text/html")

        return None

    def _og_image_url(self, request: HttpRequest, path: str) -> str:
        image_path = path if path.startswith("/") else f"/{path}"
        return request.build_absolute_uri(f"{image_path}.png")

    def _get_png(self, path: str) -> bytes | None:
        path_parts = [part for part in path.strip("/").split("/") if part]
        if not path_parts:
            return None

        # App indexes — no page-specific metric, use the logo
        if path_parts in (["projects"], ["releases"]):
            return render_logo_preview_png()

        if path_parts[0] != "projects":
            return None

        # Pattern: projects/<path>/tests/<test_id>/results/<result_id>
        if result_match := self._match_test_result(path_parts):
            _project, _test, result = result_match
            return render_result_preview_png(result)

        # Pattern: projects/<path>/tests/disabled
        if disabled := self._match_disabled_tests(path_parts):
            return render_disabled_preview_png(disabled)

        # Pattern: projects/<path>/tests/<test_id>
        if test_match := self._match_test(path_parts):
            _project, test = test_match
            return render_test_preview_png(test)

        # Pattern: projects/<path>/suite/<suite_id>
        if suite_match := self._match_suite(path_parts):
            _project, suite = suite_match
            return render_suite_preview_png(suite)

        # Pattern: projects/<path>/results/suite/<suite_id>
        if suite_results := self._match_suite_results(path_parts):
            project, suite = suite_results
            return render_results_preview_png(project, suite=suite)

        # Pattern: projects/<path>/results
        if results := self._match_project_suffix(path_parts, "results"):
            return render_results_preview_png(results)

        # Pattern: projects/<path>/metrics
        if metrics := self._match_project_suffix(path_parts, "metrics"):
            return render_metrics_preview_png(metrics)

        # Export / regex / maintainer — before project catch-all (which 404s)
        if self._is_project_utility_path(path_parts):
            return render_logo_preview_png()

        # Pattern: projects/<path>
        if project_match := self._match_project(path_parts):
            project, is_org = project_match
            return render_project_preview_png(project, is_org=is_org)

        return None

    def _get_html(self, request: HttpRequest, path: str) -> str | None:
        path_parts = [part for part in path.strip("/").split("/") if part]
        if not path_parts:
            return None

        og_image = self._og_image_url(request, path)

        if path_parts == ["projects"]:
            return self._render_html(
                None,
                "Keep tabs on unreliable tests by exploring all projects.",
                request,
                og_image,
            )

        if path_parts == ["releases"]:
            return self._render_html(
                "Releases",
                "Keep tabs on unreliable tests by exploring release history.",
                request,
                og_image,
            )

        if path_parts[0] != "projects":
            return None

        # Pattern: projects/<path>/tests/<test_id>/results/<result_id>
        if result_match := self._match_test_result(path_parts):
            project, test, result = result_match
            status_label = Status(result.status).label
            description = f"{status_label} test result for: {test.name}"
            return self._render_html(project.name, description, request, og_image)

        # Pattern: projects/<path>/tests/disabled
        if disabled := self._match_disabled_tests(path_parts):
            description = "Keep tabs on unreliable tests by viewing disabled tests for this project."
            return self._render_html(disabled.name, description, request, og_image)

        # Pattern: projects/<path>/tests/<test_id>
        if test_match := self._match_test(path_parts):
            project, test = test_match
            description = f"Test result history for: {test.name}"
            return self._render_html(project.name, description, request, og_image)

        # Pattern: projects/<path>/suite/<suite_id>
        if suite_match := self._match_suite(path_parts):
            project, suite = suite_match
            description = f"Suite tests for: {suite.name}"
            return self._render_html(project.name, description, request, og_image)

        # Pattern: projects/<path>/results/suite/<suite_id>
        if suite_results := self._match_suite_results(path_parts):
            project, suite = suite_results
            description = f"Latest results for suite: {suite.name}"
            return self._render_html(project.name, description, request, og_image)

        # Pattern: projects/<path>/results
        if results := self._match_project_suffix(path_parts, "results"):
            description = "Keep tabs on unreliable tests by browsing the latest results for this project."
            return self._render_html(results.name, description, request, og_image)

        # Pattern: projects/<path>/metrics
        if metrics := self._match_project_suffix(path_parts, "metrics"):
            description = (
                "Keep tabs on unreliable tests by viewing metrics for this project."
            )
            return self._render_html(metrics.name, description, request, og_image)

        if self._is_project_utility_path(path_parts):
            return self._render_html(
                None,
                "Keep tabs on unreliable tests.",
                request,
                og_image,
            )

        # Pattern: projects/<path>
        if project_match := self._match_project(path_parts):
            project, is_org = project_match
            if is_org:
                description = "Keep tabs on unreliable tests by exploring all projects for this organization."
            else:
                description = "Keep tabs on unreliable tests by exploring all tests reported for this project."
            return self._render_html(project.name, description, request, og_image)

        return None

    def _match_test_result(
        self, path_parts: list[str]
    ) -> tuple[Project, Test, Result] | None:
        if len(path_parts) < 6:
            return None
        tests_idx = None
        results_idx = None
        for i, part in enumerate(path_parts):
            if part == "tests" and tests_idx is None:
                tests_idx = i
            elif part == "results" and results_idx is None and tests_idx is not None:
                results_idx = i
        if (
            tests_idx is None
            or results_idx is None
            or results_idx != tests_idx + 2
            or len(path_parts) <= results_idx + 1
        ):
            return None
        try:
            test_id = int(path_parts[tests_idx + 1])
            result_id = int(path_parts[results_idx + 1])
        except ValueError:
            return None
        project_path = "/".join(path_parts[1:tests_idx])
        project = get_object_or_404(Project, repository__iendswith=project_path)
        test = get_object_or_404(Test, project=project, id=test_id)
        result = get_object_or_404(Result, test=test, id=result_id)
        return project, test, result

    def _match_disabled_tests(self, path_parts: list[str]) -> Project | None:
        if (
            len(path_parts) < 3
            or path_parts[-2] != "tests"
            or path_parts[-1] != "disabled"
        ):
            return None
        project_path = "/".join(path_parts[1:-2])
        return get_object_or_404(Project, repository__iendswith=project_path)

    def _match_test(self, path_parts: list[str]) -> tuple[Project, Test] | None:
        if len(path_parts) < 4 or path_parts[-2] != "tests":
            return None
        try:
            test_id = int(path_parts[-1])
        except ValueError:
            return None
        project_path = "/".join(path_parts[1:-2])
        project = get_object_or_404(Project, repository__iendswith=project_path)
        test = get_object_or_404(Test, project=project, id=test_id)
        return project, test

    def _match_suite(self, path_parts: list[str]) -> tuple[Project, Suite] | None:
        if len(path_parts) < 4 or path_parts[-2] != "suite":
            return None
        # Avoid matching .../results/suite/<id>
        if len(path_parts) >= 5 and path_parts[-3] == "results":
            return None
        try:
            suite_id = int(path_parts[-1])
        except ValueError:
            return None
        project_path = "/".join(path_parts[1:-2])
        project = get_object_or_404(Project, repository__iendswith=project_path)
        suite = get_object_or_404(Suite, project=project, id=suite_id)
        return project, suite

    def _match_suite_results(
        self, path_parts: list[str]
    ) -> tuple[Project, Suite] | None:
        if (
            len(path_parts) < 5
            or path_parts[-3] != "results"
            or path_parts[-2] != "suite"
        ):
            return None
        try:
            suite_id = int(path_parts[-1])
        except ValueError:
            return None
        project_path = "/".join(path_parts[1:-3])
        project = get_object_or_404(Project, repository__iendswith=project_path)
        suite = get_object_or_404(Suite, project=project, id=suite_id)
        return project, suite

    def _match_project_suffix(
        self, path_parts: list[str], suffix: str
    ) -> Project | None:
        if len(path_parts) < 3 or path_parts[-1] != suffix:
            return None
        project_path = "/".join(path_parts[1:-1])
        return get_object_or_404(Project, repository__iendswith=project_path)

    def _match_project(self, path_parts: list[str]) -> tuple[Project, bool] | None:
        if len(path_parts) < 2:
            return None
        project_path = "/".join(path_parts[1:])
        project = get_object_or_404(Project, repository__iendswith=project_path)
        is_org = len(path_parts) == 2
        return project, is_org

    def _is_project_utility_path(self, path_parts: list[str]) -> bool:
        """Export / regex / maintainer pages fall back to the app logo."""
        if len(path_parts) < 3 or path_parts[0] != "projects":
            return False
        tail = path_parts[-1]
        if tail in {"export", "export.json", "export.md", "regex", "maintainer"}:
            return True
        if len(path_parts) >= 2 and path_parts[-2] == "export":
            return True
        return False

    def _render_html(
        self,
        title: str | None,
        description: str,
        request: HttpRequest,
        og_image: str | None = None,
    ) -> str:
        context = {
            "og_title": title,
            "og_description": description,
            "meta_description": description,
            "og_image": og_image,
        }
        return render_to_string("crawler.html", context, request=request)
