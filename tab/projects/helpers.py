from __future__ import annotations

import json
import secrets
import string
from datetime import timedelta
from datetime import timezone as dt_timezone
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.http import HttpRequest
from django.urls import reverse
from django.utils import timezone as django_timezone

import log
from github import GithubException
from requests.exceptions import RequestException

from tab.core.models import Organization

from .enums import Platform, Status, Target

if TYPE_CHECKING:
    from .models import Project, Result, Test


METRICS_JSON_ROOT_META = "The following is exported from the Test Analysis Bot (TAB)."
METRICS_JSON_TEST_META = "These are the least-reliable and slowest tests in this project. Use this information to triage and suggest fixes. Durations are in seconds."
METRICS_JSON_RESULT_META = "These are recent results for this test including at least one pass and one fail, if available. Durations are in seconds."


def insert_breaks(text: str) -> str:
    """Insert line-break opportunities for long test names."""
    return text.replace("_", "_<wbr>").replace("::", "::<wbr>")


def humanize_duration(value: float | None) -> str:
    """Format duration in seconds as XmXs when over a minute."""
    if value is None or value < 0:
        return "—"
    if value >= 100:  # 3 or more digits
        minutes = int(value // 60)
        seconds = int(value % 60)
        return f"{minutes}m{seconds}s"
    return f"{value:.1f}s"


def get_disabled_test_metrics(project: Project) -> dict[User, dict[str, int]]:
    """Compute statistics on disabled tests grouped by user."""

    # Get all tests that were ever disabled
    tests = project.tests.filter(disabled_user__isnull=False).select_related(
        "disabled_user"
    )

    # Group by user and current status
    data = {}
    for test in tests:
        user: User = test.disabled_user  # type: ignore[assignment]
        if user not in data:
            data[user] = {
                "total": 0,
                "disabled": 0,
                "enabled": 0,
            }

        data[user]["total"] += 1
        if test.disabled_at:
            data[user]["disabled"] += 1
        else:
            data[user]["enabled"] += 1

    return data


def rerun_failed_jobs(
    organization: Organization, project: Project, run_id: int
) -> str | None:
    assert "github.com" in project.repository, "Only GitHub is supported for now"
    github = organization.get_github_client()
    if not github:
        return None

    try:
        repo = github.get_repo(project.path)
        run = repo.get_workflow_run(run_id)
        status, _headers, body = run._requester.requestJson(
            "POST", f"{run.url}/rerun-failed-jobs"
        )
    except (GithubException, RequestException, OSError) as e:
        log.error(f"Unable to rerun failed jobs for {project.path} @ {run_id}: {e}")
        return None

    if status != 201:
        log.error(f"Unable to rerun failed jobs for {project.path} @ {run_id}: {body}")
        return None

    # GitHub Actions reuses the same run ID for subsequent attempts
    url = f"{project.repository}/actions/runs/{run_id}"
    log.info(f"Rerun failed jobs for {project.path} @ {run_id}: {url}")
    return url


def build_result_prompt(result: Result) -> str:
    test = result.test
    lines: list[str] = []
    lines.append("The following is exported from the Test Analysis Bot (TAB).")
    lines.append("")
    lines.append("Use this information to:")
    lines.append("")
    lines.append("1. Classify the failure")
    lines.append("2. Assess whether it is a real regression, flaky, or infra-related")
    lines.append("3. Explain which fields most strongly support that conclusion")
    lines.append("4. Suggest the most likely causes")
    lines.append("5. Propose a minimal fix if appropriate")
    lines.append("6. Verify the fix or ask the user to do so")
    lines.append("")
    lines.append("## Test identity")
    lines.append("")
    lines.append(f"- Repository: {test.project.path}")
    lines.append(f"- Name: {result.test_label}")
    lines.append(f"- Markers: {json.dumps(result.markers)}")
    lines.append(
        "- Date created: " + (test.created_at.isoformat() if test.created_at else "—")
    )
    lines.append(f"- Added in branch: {test.original_branch or '—'}")
    lines.append(f"- Added in commit: {test.original_commit or '—'}")
    maintainer_email = test.maintainer.email if test.maintainer else None
    lines.append(f"- Maintainer: {maintainer_email or '—'}")
    lines.append("")
    lines.append("## Historical signals")
    lines.append("")
    lines.append(f"- Failure rate: {test.failure_rate_humanized}")
    lines.append(f"- Block rate: {test.block_rate_humanized}")
    lines.append(f"- Average duration: {test.average_duration_humanized}")
    lines.append("")
    for name in ("failure_rate", "block_rate", "average_duration"):
        field = test._meta.get_field(name)
        label = str(field.verbose_name).capitalize()  # type: ignore[union-attr]
        lines.append(f"_{label}: {field.help_text}_")  # type: ignore[union-attr]
    if test.disabled_at:
        lines.append("")
        lines.append("## Override behavior")
        lines.append("")
        lines.append(f"- Disabled: {str(bool(test.disabled_at)).lower()}")
        lines.append(f"- Disabled since: {test.disabled_at.isoformat()}")
        if test.disabled_reason.strip():
            lines.append(f"- Reason: {test.disabled_reason.strip()}")
        else:
            lines.append("- Reason: —")
        lines.append(f"- Tracker: {test.disabled_tracker or '—'}")
        disabled_by = test.disabled_user.email if test.disabled_user else None
        lines.append(f"- Last updated by: {disabled_by or '—'}")
        lines.append("")
        lines.append(
            "_TAB has a feature to suppress failures in known broken or flaky tests._"
        )
        lines.append(
            "_This turns blocking failures into a non-blocking status to let PRs merge._"
        )
    lines.append("")
    lines.append("## Result details")
    lines.append("")
    lines.append(f"- Status: {Status(result.status).value}")
    lines.append(
        "- Reported at: "
        + (result.created_at.isoformat() if result.created_at else "—")
    )
    lines.append(f"- Duration: {result.duration_humanized}")
    lines.append(f"- Branch: {result.branch or '—'}")
    lines.append(f"- Commit: {result.commit or '—'}")
    lines.append(f"- Target: {Target(result.target).label if result.target else '—'}")
    lines.append(
        f"- Platform: {Platform(result.platform).label if result.platform else '—'}"
    )
    lines.append(f"- Browser: {result.browser or '—'}")
    lines.append(f"- New failure: {str(result.new_failure).lower()}")
    lines.append("")
    lines.append(
        "_New failure: History data indicates the test is only blocking this branch._"
    )
    if result.message:
        lines.append("")
        lines.append("## Failure message")
        lines.append("")
        lines.append("```text")
        lines.append(result.message.rstrip())
        lines.append("```")
    lines.append("")
    lines.append("## Rerun locally")
    lines.append("")
    if result.command:
        lines.append("```shell")
        lines.append("\n".join(line for line, _ in result.command).strip())
        lines.append("```")
    else:
        lines.append("[TODO: add this]")
    lines.append("")
    lines.append("_Use this to reproduce the failure and validate fixes._")
    lines.append("_Do not discard uncommitted changes without user approval._")
    if result.logs:
        lines.append("")
        lines.append("## Additional logs")
        lines.append("")
        lines.append("```json")
        lines.append(result.logs_json)
        lines.append("```")
    return "\n".join(lines).strip() + "\n"


def build_metrics_json(project: Project, tests: list[Test], limit: int = 10) -> str:

    def interpolated_command(test: Test) -> str | None:
        if not test.suite:
            return None
        pattern = test.suite.local_command
        if not pattern:
            return ""
        try:
            return pattern.format(test=test)
        except (KeyError, AttributeError) as e:
            log.error(f"Invalid local command for {test.suite}: {repr(e)}")
            return pattern

    def dt_iso(dt):
        if not dt:
            return None
        if django_timezone.is_aware(dt):
            dt = dt.astimezone(dt_timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def setup_duration(r: Result) -> float | None:
        if suite := r.suite:
            try:
                run = suite.runs.get(branch=r.branch, commit=r.commit)
            except suite.runs.model.DoesNotExist:
                return None
            return run.setup_duration if run.setup_duration > 0 else None
        return None

    def result_row(r: Result) -> dict:
        return {
            "tab_id": r.pk,
            "tab_url": r.url,
            "created_at": dt_iso(r.created_at),
            "branch": r.branch or None,
            "commit": r.commit or None,
            "target": r.target or None,
            "platform": r.platform or None,
            "browser": r.browser or None,
            "status": r.status,
            "setup_duration": setup_duration(r),
            "test_duration": r.duration,
            "message": r.message or None,
            "logs": r.logs,
        }

    def recent_results(test: Test) -> list:

        pass_result = (
            test.results.filter(status=Status.PASSED).order_by("-created_at").first()
        )
        blocked_statuses = [s.value for s in Status.merge_blocked()]
        fail_result = (
            test.results.filter(status__in=blocked_statuses)
            .order_by("-created_at")
            .first()
        )

        chosen_ids: list[int] = []
        seen: set[int] = set()

        def add(r: Result | None) -> None:
            if r is not None and r.pk not in seen:
                seen.add(r.pk)
                chosen_ids.append(r.pk)

        add(pass_result)
        add(fail_result)

        if len(chosen_ids) < limit:
            for r in test.results.order_by("-created_at")[:100]:
                if len(chosen_ids) >= limit:
                    break
                add(r)

        if not chosen_ids:
            return []

        rows = list(test.results.filter(pk__in=chosen_ids))
        by_pk = {r.pk: r for r in rows}
        ordered = [by_pk[pk] for pk in chosen_ids if pk in by_pk]
        ordered.sort(key=lambda r: r.created_at, reverse=True)
        return ordered[:limit]

    now = django_timezone.now()
    if django_timezone.is_aware(now):
        now = now.astimezone(dt_timezone.utc)
    exported_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    test_rows: list = []
    if tests:
        test_rows.append(METRICS_JSON_TEST_META)
        for test in tests:
            selected = recent_results(test)
            result_rows = (
                [METRICS_JSON_RESULT_META] + [result_row(r) for r in selected]
                if selected
                else []
            )
            test_rows.append(
                {
                    "tab_id": test.pk,
                    "tab_url": test.url,
                    "created_at": dt_iso(test.created_at),
                    "name": test.name,
                    "suite": test.suite.name if test.suite else None,
                    "original_branch": test.original_branch or None,
                    "original_commit": test.original_commit or None,
                    "maintainer": test.maintainer.email if test.maintainer else None,
                    "markers": test.markers,
                    "command": interpolated_command(test),
                    "failure_rate": (
                        None if test.failure_rate < 0 else test.failure_rate
                    ),
                    "block_rate": None if test.block_rate < 0 else test.block_rate,
                    "average_duration": (
                        None if test.average_duration < 0 else test.average_duration
                    ),
                    "results": result_rows,
                }
            )

    payload = {
        "message": METRICS_JSON_ROOT_META,
        "exported_at": exported_at,
        "project": {
            "tab_id": project.pk,
            "tab_url": settings.BASE_URL
            + reverse("projects:tests", args=[project.path]),
            "created_at": dt_iso(project.created_at),
            "name": project.name,
            "repository_url": project.repository,
            "default_branch": project.default_branch,
        },
        "tests": test_rows,
    }

    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


EXPORT_TOKEN_PARAM = "token"
EXPORT_TOKEN_TIMEOUT = timedelta(days=7).total_seconds()
_EXPORT_TOKEN_CACHE_PREFIX = "export:"
_EXPORT_TOKEN_REQUEST_ATTR = "_export_token"


def valid_export_token(token: str) -> bool:
    return bool(token) and cache.get(_EXPORT_TOKEN_CACHE_PREFIX + token) is not None


def tokenize(request: HttpRequest, path: str) -> str:
    token = getattr(request, _EXPORT_TOKEN_REQUEST_ATTR, None) or request.GET.get(
        EXPORT_TOKEN_PARAM
    )
    if not isinstance(token, str) or not valid_export_token(token):
        token = _generate_export_token()
    setattr(request, _EXPORT_TOKEN_REQUEST_ATTR, token)
    return request.build_absolute_uri(f"{path}?{EXPORT_TOKEN_PARAM}={token}")


def _generate_export_token() -> str:
    token = "".join(secrets.choice(string.ascii_letters) for _ in range(8))
    cache.set(_EXPORT_TOKEN_CACHE_PREFIX + token, True, timeout=EXPORT_TOKEN_TIMEOUT)
    return token
