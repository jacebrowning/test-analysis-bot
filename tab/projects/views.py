import re
import threading
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.views import View
from django.views.generic import FormView, ListView, TemplateView

import log
from django_tables2 import SingleTableMixin

from tab.core.helpers import get_or_create_user, organization_for_email
from tab.metrics.constants import DELTA_THRESHOLD
from tab.metrics.models import Alert

from .constants import ALL_BRANCHES, FAILURE_RATE_EPSILON
from .enums import Platform
from .forms import BulkUpdateTestForm, UpdateTestForm
from .helpers import build_metrics_json, get_disabled_test_metrics
from .models import Project, Result, Run, Status, Test
from .tables import DisabledTestTable, ResultTable, TestResultTable, TestTable


class IndexView(LoginRequiredMixin, TemplateView):
    template_name = "projects/index.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        assert self.request.user.is_authenticated
        organization = organization_for_email(self.request.user.email)
        if organization is None:
            projects = Project.objects.none()
        else:
            projects = (
                Project.objects.filter(
                    repository__startswith=organization.repository_index
                )
                .annotate(
                    suites_count=Count("suites", distinct=True),
                    tests_count=Count("tests", distinct=True),
                )
                .order_by("repository")
            )

        context["projects"] = projects
        if self.request.user.is_staff:
            context["admin_url"] = reverse("admin:index")

        return context


class SearchLabelMixin:
    search_labels: list[str] = []

    def dispatch(self, request, *args, **kwargs):
        if search := request.GET.get("search", "").strip():
            params = request.GET.copy()
            updated = False
            # Convert certain search terms to dedicated query params
            for label in self.search_labels:
                if match := re.search(rf"{label}:(\S+)", search, re.IGNORECASE):
                    value = match.group(1).lower().strip("@")
                    log.info(f"Converting '{label}:{value}' search to query param")
                    search = (
                        re.sub(rf"{label}:\S+", "", search, flags=re.IGNORECASE)
                        .replace("  ", " ")
                        .strip()
                    )
                    params[label] = value
                    updated = True
            # Redirect to new URL with dedicated query params
            if updated:
                if search:
                    params["search"] = search
                else:
                    params.pop("search", None)
                log.info(f"Redirecting with '?{params.urlencode()}'")
                return redirect(f"{request.path}?{params.urlencode()}")
        return super().dispatch(request, *args, **kwargs)  # type: ignore[misc]


class TestsView(LoginRequiredMixin, SingleTableMixin, SearchLabelMixin, ListView):
    table_class = TestTable
    template_name = "projects/tests.html"
    search_labels = ["tag"]

    def get_queryset(self):
        project = get_object_or_404(
            Project, repository__iendswith=self.kwargs["path"].strip("/")
        )

        suite_id = self.kwargs.get("suite_id")
        search = self.request.GET.get("search", "").strip()
        tag = self.request.GET.get("tag")
        enabled = self.request.GET.get("enabled", "true")

        queryset = project.tests.select_related(
            "suite", "last_result"
        ).prefetch_related("project__suites")
        if suite_id:
            queryset = queryset.filter(suite_id=suite_id)
        if search:
            queryset = queryset.filter(
                Q(suite__name__icontains=search) | Q(name__icontains=search)
            )
        if tag == "disabled":
            queryset = queryset.filter(disabled_at__isnull=False)
        elif tag:
            queryset = queryset.filter(
                Q(last_result__metadata__tags__icontains=tag)
                | Q(last_result__metadata__tags__icontains=f"@{tag}")
            )
        if enabled == "true":
            queryset = queryset.filter(enabled=True)
        elif enabled == "false":
            queryset = queryset.filter(enabled=False)
        if project.test_inactive_threshold:
            queryset = queryset.filter(
                updated_at__gte=timezone.now() - project.test_inactive_threshold
            )

        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        context["project"] = project = get_object_or_404(
            Project, repository__iendswith=self.kwargs["path"].strip("/")
        )
        context["suites"] = project.suites.active()
        context["suite_id"] = self.kwargs.get("suite_id")
        context["search"] = self.request.GET.get("search", "").strip()
        context["tag"] = self.request.GET.get("tag", "").strip()
        context["enabled"] = self.request.GET.get("enabled", "true")
        if self.request.user.is_staff:
            context["admin_url"] = reverse(
                "admin:projects_project_change", args=[project.pk]
            )

        return context


class DisabledTestsView(LoginRequiredMixin, SingleTableMixin, FormView):
    table_class = DisabledTestTable
    table_pagination = False
    template_name = "projects/tests-disabled.html"
    form_class = BulkUpdateTestForm

    def get_queryset(self):
        project = get_object_or_404(
            Project, repository__iendswith=self.kwargs["path"].strip("/")
        )
        if ids := self._parse_preselect_ids():
            return project.tests.filter(id__in=ids).select_related(
                "suite", "last_result", "disabled_user"
            )

        search = self.request.GET.get("search", "").strip()
        tracker = self.request.GET.get("tracker", "").strip()

        queryset = project.tests.filter(
            enabled=False, last_result__isnull=False
        ).select_related("suite", "last_result", "disabled_user")
        if search:
            queryset = queryset.filter(
                Q(suite__name__icontains=search)
                | Q(name__icontains=search)
                | Q(disabled_reason__icontains=search)
                | Q(disabled_tracker__icontains=search)
                | Q(disabled_user__email__icontains=search)
            )
        if tracker == "true":
            queryset = queryset.exclude(disabled_tracker__isnull=True)
        elif tracker == "false":
            queryset = queryset.filter(disabled_tracker__isnull=True)
        if project.test_inactive_threshold:
            queryset = queryset.filter(
                updated_at__gte=timezone.now() - project.test_inactive_threshold
            )

        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        context["project"] = project = get_object_or_404(
            Project, repository__iendswith=self.kwargs["path"].strip("/")
        )
        context["disabled_only"] = True
        context["search"] = self.request.GET.get("search", "").strip()
        context["preselect"] = self.request.GET.get("preselect", "").strip()
        context["query_string"] = self.request.GET.urlencode(safe=",")
        context["base_url"] = settings.BASE_URL
        if self.request.user.is_staff:
            context["admin_url"] = reverse(
                "admin:projects_project_change", args=[project.pk]
            )

        return context

    def get_table_kwargs(self):
        return {"preselect": bool(self._parse_preselect_ids())}

    def get_initial(self):
        tests = self.get_queryset()
        initial = {
            "disabled_user": (
                self.request.user.email if self.request.user.is_authenticated else ""
            ),
        }
        emails = set(t.disabled_user.email for t in tests if t.disabled_user)
        if len(emails) == 1:
            initial["disabled_user"] = emails.pop()
        reasons = set(t.disabled_reason for t in tests)
        if len(reasons) == 1:
            initial["disabled_reason"] = reasons.pop()
        trackers = set(t.disabled_tracker for t in tests)
        if len(trackers) == 1:
            initial["disabled_tracker"] = trackers.pop()
        return initial

    def form_valid(self, form):
        test_ids = form.cleaned_data["test_ids"].split(",")
        disabled = form.cleaned_data["disabled"]
        disabled_reason = form.cleaned_data["disabled_reason"]
        disabled_tracker = form.cleaned_data["disabled_tracker"]
        disabled_user = get_or_create_user(form.cleaned_data["disabled_user"])

        tests = self.get_queryset().filter(id__in=test_ids)
        for test in tests:
            test.disabled_at = timezone.now() if disabled else None
            test.disabled_reason = disabled_reason
            test.disabled_tracker = disabled_tracker
            test.disabled_user = disabled_user
            if (
                not disabled
                and test.last_result
                and test.last_result.status in Status.test_disabled()
            ):
                # Change the status to hide it from this view
                test.last_result.status = Status.INTERRUPTED
                test.last_result.save()
            test.save()

        log.info(f"{self.request.user} updated {len(tests)} tests")
        s = "" if len(tests) == 1 else "s"
        messages.success(self.request, f"Successfully updated {len(tests)} test{s}.")

        query_string = self.request.GET.urlencode(safe=",")
        redirect_url = self.request.path
        if query_string:
            redirect_url += f"?{query_string}"
        return redirect(redirect_url)

    def _parse_preselect_ids(self) -> list[int]:
        if not (raw := self.request.GET.get("preselect", "").strip()):
            return []
        return [int(part) for part in raw.split(",") if part.strip().isdigit()]


class DisabledTestsRegexView(DisabledTestsView):
    def dispatch(self, request, *args, **kwargs):
        """Disable authentication for this view to work with local test runners."""
        return super(FormView, self).dispatch(request, *args, **kwargs)

    def render_to_response(self, context, **response_kwargs):
        regex = "|".join(row.record.regex for row in context["table"].rows)
        return HttpResponse(f"'{regex}'", content_type="text/plain")


class ResultsView(LoginRequiredMixin, SingleTableMixin, SearchLabelMixin, ListView):
    table_class = ResultTable
    template_name = "projects/results.html"
    search_labels = ["platform", "tag"]

    def dispatch(self, request, *args, **kwargs):
        if request.GET.get("branch") == ALL_BRANCHES:
            params = request.GET.copy()
            params.pop("branch", None)
            path = f"{request.path}?{params.urlencode()}".strip("?")
            return redirect(path)
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        project = get_object_or_404(
            Project, repository__iendswith=self.kwargs["path"].strip("/")
        )

        branch = self.request.GET.get("branch", project.default_branch)
        suite_id = self.kwargs.get("suite_id")
        search = self.request.GET.get("search")
        platform = self.request.GET.get("platform")
        tag = self.request.GET.get("tag")
        show = self.request.GET.get("show", "all")

        queryset = (
            Result.objects.filter(test__project=project, branch=branch)
            .select_related("suite", "test", "test__project", "test__suite")
            .prefetch_related("test__project__suites")
        )
        latest_commit = queryset.values_list("commit", flat=True).first()
        queryset = queryset.filter(commit=latest_commit)

        if suite_id:
            queryset = queryset.filter(suite_id=suite_id)
        if search:
            queryset = queryset.filter(
                Q(suite__name__icontains=search) | Q(test__name__icontains=search)
            )
        if platform:
            queryset = queryset.filter(platform=platform)
        if tag == "disabled":
            queryset = queryset.filter(test__disabled_at__isnull=False)
        elif tag:
            queryset = queryset.filter(
                Q(metadata__tags__icontains=tag)
                | Q(metadata__tags__icontains=f"@{tag}")
            )
        if show == "fails":
            queryset = queryset.exclude(status__in=Status.merge_allowed()).filter(
                final=True
            )

        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        project = get_object_or_404(
            Project, repository__iendswith=self.kwargs["path"].strip("/")
        )
        branch = self.request.GET.get("branch", project.default_branch)
        if not Result.objects.filter(test__project=project, branch=branch).exists():
            messages.warning(
                self.request,
                "No results found. Stale data is pruned automatically. Try rerunning tests on this branch.",
            )

        context["project"] = project
        context["branch"] = branch
        context["merge_url"] = self._get_merge_url(project, branch)
        context["branches"] = Result.objects.get_active_branches(project)
        context["suites"] = project.suites.active()
        context["suite_id"] = self.kwargs.get("suite_id")
        context["search"] = self.request.GET.get("search", "").strip()
        context["platform"] = self.request.GET.get("platform", "").strip()
        context["tag"] = self.request.GET.get("tag", "").strip()
        context["show"] = self.request.GET.get("show", "all")
        context["base_url"] = settings.BASE_URL
        context["preselect"] = ",".join(self._get_test_ids())
        if context["suite_id"] and (
            suite := project.suites.filter(id=context["suite_id"]).first()
        ):
            context["setup_duration"] = Run.objects.get_setup_duration(suite, branch)
            context["tests_duration"] = Run.objects.get_tests_duration(suite, branch)
            context["teardown_duration"] = Run.objects.get_teardown_duration(
                suite, branch
            )
        if self.request.user.is_staff:
            if branch != project.default_branch:
                context["admin_url"] = (
                    reverse("admin:releases_release_changelist")
                    + f"?environment__project__repository={project.repository}&branch={branch}"
                )
            else:
                context["admin_url"] = (
                    reverse("admin:projects_result_changelist")
                    + f"?test__project__repository={project.repository}"
                )

        return context

    def get_table_kwargs(self):
        return {"request": self.request}

    def post(self, request, *args, **kwargs):
        project = get_object_or_404(
            Project, repository__iendswith=self.kwargs["path"].strip("/")
        )

        if result_id := request.POST.get("rerun"):
            result = get_object_or_404(Result, pk=result_id, test__project=project)
            if url := result.rerun():
                messages.success(
                    request,
                    format_html(
                        'Tests are rerunning <a href="{}" target="_blank" rel="noopener">here</a>. '
                        "Check back in a few minutes for new results.",
                        url,
                    ),
                )
                return redirect(request.get_full_path())

        messages.error(
            request,
            "Unable to rerun tests. Wait for any existing jobs to complete.",
        )
        return redirect(request.get_full_path())

    def _get_merge_url(self, project: Project, branch: str) -> str:
        result = (
            Result.objects.filter(
                test__project=project,
                branch=branch,
            )
            .select_related("test__project")
            .order_by("-created_at")
            .first()
        )
        return result.merge_url if result else ""

    def _get_test_ids(self) -> list[str]:
        return sorted({str(row.record.test_id) for row in self.get_table().rows})


class ResultsRegexView(ResultsView):
    def dispatch(self, request, *args, **kwargs):
        """Disable authentication for this view to work with local test runners."""
        return super(ListView, self).dispatch(request, *args, **kwargs)

    def render_to_response(self, context, **response_kwargs):
        regex = "|".join(row.record.test.regex for row in context["table"].rows)
        return HttpResponse(f"'{regex}'", content_type="text/plain")


class TestResultsView(LoginRequiredMixin, SingleTableMixin, FormView):
    table_class = TestResultTable
    template_name = "projects/test-results.html"
    form_class = UpdateTestForm

    def get_queryset(self):
        project = get_object_or_404(
            Project, repository__iendswith=self.kwargs["path"].strip("/")
        )
        test = get_object_or_404(Test, project=project, id=self.kwargs["test_id"])

        branch = self.request.GET.get("branch")
        platform = self.request.GET.get("platform")

        queryset = Result.objects.filter_with_default_branches(test, branch)
        if platform:
            queryset = queryset.filter(platform=platform)

        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        project = get_object_or_404(
            Project, repository__iendswith=self.kwargs["path"].strip("/")
        )
        test = get_object_or_404(
            Test.objects.select_related(
                "suite", "suite__parent", "suite__parent__project"
            ).prefetch_related("suite__children", "suite__parent__children"),
            project=project,
            id=self.kwargs["test_id"],
        )

        if "weeks" in self.request.GET:
            weeks = float(self.request.GET["weeks"])
            expand = True
        else:
            weeks = 1.5
            if "expand" in self.request.GET:
                expand = self.request.GET["expand"] == "true"
            elif test.last_result:
                expand = test.failure_rate >= DELTA_THRESHOLD
            elif result := test.results.first():
                expand = result.status in Status.merge_blocked()
            else:
                expand = False

        context["project"] = project
        context["test"] = test
        context["parent_suite"] = test.suite.parent if test.suite else None
        context["parent_test"], context["child_tests"] = (
            Test.objects.get_parent_and_child_tests(test)
        )
        context["expand"] = expand
        context["branch"] = self.request.GET.get("branch")
        context["platform"] = self.request.GET.get("platform", "").strip()
        context["history_data"] = test.history.get_data(test, weeks)

        for field in test._meta.get_fields():
            if hasattr(field, "help_text") and field.help_text:
                context[f"{field.name}_help"] = field.help_text
        context["setup_duration"] = Run.objects.get_setup_duration(test.suite)
        if self.request.user.is_staff:
            context["admin_url"] = reverse("admin:projects_test_change", args=[test.pk])
            if test.suite:
                context["suite_admin_url"] = reverse(
                    "admin:projects_suite_change", args=[test.suite.pk]
                )

        test_results_base = reverse(
            "projects:test-results",
            kwargs={
                "path": self.kwargs["path"],
                "test_id": test.id,
            },
        )
        params_all = self.request.GET.copy()
        params_all["branch"] = "all"
        context["view_all_branches_url"] = (
            test_results_base + "?" + params_all.urlencode()
        )

        clear_branch_q = self.request.GET.copy()
        clear_branch_q.pop("branch", None)
        context["clear_branch_filter_url"] = (
            f"{test_results_base}?{clear_branch_q.urlencode()}"
            if clear_branch_q
            else test_results_base
        )
        clear_platform_q = self.request.GET.copy()
        clear_platform_q.pop("platform", None)
        context["clear_platform_filter_url"] = (
            f"{test_results_base}?{clear_platform_q.urlencode()}"
            if clear_platform_q
            else test_results_base
        )

        platform_filter_links: list[tuple[str, str, str]] = []
        for plat, label in Platform.choices:
            p = self.request.GET.copy()
            p["platform"] = plat
            platform_filter_links.append(
                (label, f"{test_results_base}?{p.urlencode()}", plat)
            )
        context["platform_filter_links"] = platform_filter_links
        context["show_filter_results_menu"] = (
            context["branch"] != ALL_BRANCHES
            or bool(platform_filter_links)
            or bool(context["platform"])
        )

        return context

    def get_initial(self):
        test = get_object_or_404(
            Test,
            project__repository__iendswith=self.kwargs["path"].strip("/"),
            id=self.kwargs["test_id"],
        )
        original_email = test.disabled_user.email if test.disabled_user else ""
        current_email = (
            self.request.user.email if self.request.user.is_authenticated else ""
        )
        return {
            "test_id": test.id,
            "disabled": bool(test.disabled_at),
            "disabled_reason": test.disabled_reason,
            "disabled_tracker": test.disabled_tracker,
            "disabled_user": original_email or current_email,
        }

    def form_valid(self, form):
        test = get_object_or_404(
            Test,
            project__repository__iendswith=self.kwargs["path"].strip("/"),
            id=self.kwargs["test_id"],
        )
        disabled = form.cleaned_data["disabled"]
        disabled_reason = form.cleaned_data["disabled_reason"] or ""
        disabled_tracker = form.cleaned_data["disabled_tracker"] or ""

        previously_disabled = self._update_override_behavior(
            test,
            disabled=disabled,
            disabled_reason=disabled_reason,
            disabled_tracker=disabled_tracker,
            sibling=False,
        )

        parent_test, child_tests = Test.objects.get_parent_and_child_tests(test)
        related = [t for t in (parent_test, *child_tests) if t is not None]
        for other in related:
            self._update_override_behavior(
                other,
                disabled=disabled,
                disabled_reason=disabled_reason,
                disabled_tracker=disabled_tracker,
                sibling=True,
            )

        log.info(f"{self.request.user} updated test {test.name}")
        blocking = "disabled from blocking" if test.disabled_at else "allowed to block"
        if bool(test.disabled_at) != previously_disabled:
            blocking = "now " + blocking
        messages.success(self.request, f"Test is {blocking} merges.")

        redirect_url = self.request.path
        if branch := self.request.GET.get("branch"):
            self._rerun(test, branch)
            redirect_url += f"?branch={branch}"

        return redirect(redirect_url)

    def _update_override_behavior(
        self,
        test: Test,
        *,
        disabled: bool,
        disabled_reason: str,
        disabled_tracker: str,
        sibling: bool = False,
    ):
        previously_disabled = bool(test.disabled_at)
        if sibling:
            test.disabled_reason = test.disabled_reason or disabled_reason
            test.disabled_tracker = test.disabled_tracker or disabled_tracker
            if disabled and not previously_disabled:
                test.disabled_at = timezone.now()
        else:
            test.disabled_reason = disabled_reason
            test.disabled_tracker = disabled_tracker
            test.disabled_at = timezone.now() if disabled else None
        test.disabled_user = self.request.user  # type: ignore[assignment]
        test.failure_rate = min(  # prevent from being restored on save
            max(test.failure_rate + FAILURE_RATE_EPSILON, FAILURE_RATE_EPSILON),
            1.0,
        )
        test.save()
        newly_disabled = disabled and not previously_disabled
        if newly_disabled:
            log.info(f"Sending alert for disabled test: {test.name}")
            alert = Alert.objects.create(test=test)
            thread = threading.Thread(target=alert.send, kwargs={"forward": False})
            thread.start()
        return previously_disabled

    def _rerun(self, test: Test, branch: str):
        if result := Result.objects.filter(
            test=test, branch=branch, final=True
        ).first():
            log.info(f"Rerunning test with updated behavior: {test.name}")
            thread = threading.Thread(target=result.rerun)
            thread.start()


class TestResultView(LoginRequiredMixin, TemplateView):
    template_name = "projects/test-result.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        project = get_object_or_404(
            Project, repository__iendswith=self.kwargs["path"].strip("/")
        )
        test = get_object_or_404(Test, project=project, id=self.kwargs["test_id"])
        result = get_object_or_404(Result, test=test, id=self.kwargs["result_id"])

        context["project"] = project
        context["test"] = test
        context["result"] = result
        context["status"] = Status(result.status)

        if self.request.user.is_staff:
            context["admin_url"] = reverse(
                "admin:projects_result_change", args=[result.pk]
            )

        return context


class ResultDetailsView(LoginRequiredMixin, TemplateView):
    """API view to lazily load result details modal content."""

    template_name = "projects/_result_details.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        result = get_object_or_404(Result, pk=self.kwargs["result_id"])
        context["result"] = result
        return context


class DataExportMixin:
    def _attachment(
        self, project: Project, body: str, suffix: str = ""
    ) -> HttpResponse:
        stem = project.path.replace("/", "-").lower()
        filename = f"tab-ai-data-{stem}{suffix}.json"
        response = HttpResponse(body, content_type="application/json; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


class LeastReliableTestsMixin:
    def _least_reliable_tests(self, project: Project) -> list[Test]:
        week_ago = timezone.now() - timedelta(days=7)
        least_reliable = (
            Q(failure_rate__gt=0.40, block_rate__gt=0)
            | Q(block_rate__gt=0.05, failure_rate__gt=0.20)
            | Q(failure_rate__gt=0.50)
            | Q(average_duration__gt=90)
        )
        queryset = (
            project.tests.filter(
                least_reliable,
                created_at__lte=week_ago,
                last_result__isnull=False,
                last_result__created_at__gte=week_ago,
                disabled_at__isnull=True,
            )
            .exclude(last_result__status=Status.DISABLED)
            .select_related("suite")
            .order_by("-failure_rate", "-average_duration")[:10]
        )
        return sorted(queryset, key=lambda t: str(t).lower())


class MetricsView(LeastReliableTestsMixin, LoginRequiredMixin, TemplateView):
    template_name = "projects/metrics.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        project = get_object_or_404(
            Project, repository__iendswith=self.kwargs["path"].strip("/")
        )

        context["project"] = project
        tests_sorted = self._least_reliable_tests(project)
        context["least_reliable_tests"] = tests_sorted
        context["disabled_test_metrics"] = get_disabled_test_metrics(project)
        if self.request.user.is_staff:
            context["admin_url"] = (
                reverse(
                    "admin:projects_test_changelist",
                )
                + f"?project__repository={project.repository}"
            )

        return context


class MetricsDownloadView(
    DataExportMixin, LeastReliableTestsMixin, LoginRequiredMixin, View
):

    def get(self, request, *args, **kwargs):
        project = get_object_or_404(
            Project, repository__iendswith=self.kwargs["path"].strip("/")
        )
        tests = self._least_reliable_tests(project)
        return self._attachment(project, build_metrics_json(project, tests))


class MetricsRawView(LeastReliableTestsMixin, LoginRequiredMixin, TemplateView):

    template_name = "projects/export.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        project = get_object_or_404(
            Project, repository__iendswith=self.kwargs["path"].strip("/")
        )
        context["project"] = project
        tests = self._least_reliable_tests(project)
        context["export_json"] = build_metrics_json(project, tests)
        context["back_url"] = reverse("projects:metrics", args=[project.path])
        context["back_label"] = "Back to Metrics"
        return context


class SingleTestMixin:
    kwargs: dict

    def _project_and_test(self) -> tuple[Project, Test]:
        project = get_object_or_404(
            Project, repository__iendswith=self.kwargs["path"].strip("/")
        )
        test = get_object_or_404(
            Test.objects.select_related("suite"),
            project=project,
            id=self.kwargs["test_id"],
        )
        return project, test


class TestDownloadView(DataExportMixin, SingleTestMixin, LoginRequiredMixin, View):

    def get(self, request, *args, **kwargs):
        project, test = self._project_and_test()
        body = build_metrics_json(project, [test], limit=25)
        return self._attachment(project, body, suffix=f"-test-{test.pk}")


class TestRawView(SingleTestMixin, LoginRequiredMixin, TemplateView):

    template_name = "projects/export.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        project, test = self._project_and_test()
        context["project"] = project
        context["test"] = test
        context["export_json"] = build_metrics_json(project, [test], limit=25)
        context["back_url"] = reverse(
            "projects:test-results", args=[project.path, test.id]
        )
        context["back_label"] = "Back to Test"
        return context
