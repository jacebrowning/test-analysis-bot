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
from tab.projects.enums import Status
from tab.projects.models import Project, Result, Test


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
    """

    def process_request(self, request: HttpRequest) -> HttpResponse | None:
        if request.method != "GET" or request.user.is_authenticated:
            return None

        user_agent = request.user_agent  # type: ignore[attr-defined]
        if not user_agent or not user_agent.is_bot:
            return None

        if html := self._get_html(request, request.path):
            return HttpResponse(html, content_type="text/html")

        return None

    def _get_html(self, request: HttpRequest, path: str) -> str | None:

        path_parts = path.strip("/").split("/")
        projects_idx = 0

        # Pattern: projects/<path>/tests/<test_id>/results/<result_id>
        if (
            len(path_parts) >= projects_idx + 6
            and path_parts[projects_idx] == "projects"
        ):
            tests_idx = None
            results_idx = None
            for i, part in enumerate(path_parts):
                if part == "tests" and tests_idx is None:
                    tests_idx = i
                elif (
                    part == "results" and results_idx is None and tests_idx is not None
                ):
                    results_idx = i

            if (
                tests_idx is not None
                and results_idx is not None
                and results_idx == tests_idx + 2
                and len(path_parts) > results_idx + 1
            ):
                test_id = int(path_parts[tests_idx + 1])
                result_id = int(path_parts[results_idx + 1])
                project_path = "/".join(path_parts[projects_idx + 1 : tests_idx])
                project = get_object_or_404(Project, repository__iendswith=project_path)
                test = get_object_or_404(Test, project=project, id=test_id)
                result = get_object_or_404(Result, test=test, id=result_id)

                title = project.name
                status_label = Status(result.status).label
                description = f"{status_label} test result for: {test.name}"
                return self._render_html(title, description, request)

        # Pattern: projects/<path>/tests/disabled
        if (
            len(path_parts) >= projects_idx + 3
            and path_parts[projects_idx] == "projects"
            and len(path_parts) >= projects_idx + 3
            and path_parts[-2] == "tests"
            and path_parts[-1] == "disabled"
        ):
            project_path = "/".join(path_parts[projects_idx + 1 : -2])
            project = get_object_or_404(Project, repository__iendswith=project_path)

            title = project.name
            description = "Keep tabs on unreliable tests by viewing disabled tests for this project."
            return self._render_html(title, description, request)

        # Pattern: projects/<path>/tests/<test_id>
        if (
            len(path_parts) >= projects_idx + 4
            and path_parts[projects_idx] == "projects"
            and path_parts[-2] == "tests"
        ):
            test_id = int(path_parts[-1])
            project_path = "/".join(path_parts[projects_idx + 1 : -2])
            project = get_object_or_404(Project, repository__iendswith=project_path)
            test = get_object_or_404(Test, project=project, id=test_id)

            title = project.name
            description = f"Test result history for: {test.name}"
            return self._render_html(title, description, request)

        # Pattern: projects/<path>/results
        if (
            len(path_parts) >= projects_idx + 3
            and path_parts[projects_idx] == "projects"
            and path_parts[-1] == "results"
        ):
            project_path = "/".join(path_parts[projects_idx + 1 : -1])
            project = get_object_or_404(Project, repository__iendswith=project_path)

            title = project.name
            description = "Keep tabs on unreliable tests by browsing the latest results for this project."
            return self._render_html(title, description, request)

        # Pattern: projects/<path>/metrics
        if (
            len(path_parts) >= projects_idx + 3
            and path_parts[projects_idx] == "projects"
            and path_parts[-1] == "metrics"
        ):
            project_path = "/".join(path_parts[projects_idx + 1 : -1])
            project = get_object_or_404(Project, repository__iendswith=project_path)

            title = project.name
            description = (
                "Keep tabs on unreliable tests by viewing metrics for this project."
            )
            return self._render_html(title, description, request)

        # Pattern: projects/<path>
        if (
            len(path_parts) >= projects_idx + 2
            and path_parts[projects_idx] == "projects"
        ):
            project_path = "/".join(path_parts[projects_idx + 1 :])
            project = get_object_or_404(Project, repository__iendswith=project_path)

            title = project.name
            if len(path_parts) == projects_idx + 2:
                description = "Keep tabs on unreliable tests by exploring all projects for this organization."
            else:
                description = "Keep tabs on unreliable tests by exploring all tests reported for this project."
            return self._render_html(title, description, request)

        return None

    def _render_html(
        self, title: str | None, description: str, request: HttpRequest
    ) -> str:

        context = {
            "og_title": title,
            "og_description": description,
            "meta_description": description,
        }
        return render_to_string("crawler.html", context, request=request)
