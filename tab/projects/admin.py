from django import forms
from django.contrib import admin
from django.utils import timezone
from django.utils.safestring import mark_safe
from django.utils.timesince import timesince

from .models import Project, Result, Run, Suite, Test


class SuiteAdminForm(forms.ModelForm):
    local_command = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "placeholder": (
                    'pytest -k "{test.substring}"'
                    "\n\n"
                    "# or"
                    "\n\n"
                    'playwright test --grep="{test.regex}"'
                ),
                "cols": 80,
            }
        ),
        required=False,
        help_text=Suite._meta.get_field("local_command").help_text,
    )


class SuiteInline(admin.TabularInline):
    model = Suite
    max_num = 0
    fields = ("name", "local_command", "supports_override")
    show_change_link = True


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    search_fields = ("repository", "error_indicators")
    list_display = (
        "id",
        "repository",
        "default_branch",
        "branch_inactive_threshold_humanized",
        "test_inactive_threshold_humanized",
        "test_stale_threshold_humanized",
        "result_stale_threshold_humanized",
        "suites_count",
        "tests_count",
        "cleaned_at",
        "created_at",
        "updated_at",
    )
    ordering = ("repository",)
    list_filter = (
        "cleaned_at",
        "created_at",
        "updated_at",
    )

    @admin.display(description="Branches Inactive")
    def branch_inactive_threshold_humanized(self, project: Project) -> str:
        if not project.branch_inactive_threshold:
            return "Never"
        return timesince(timezone.now() - project.branch_inactive_threshold)

    @admin.display(description="Tests Inactive")
    def test_inactive_threshold_humanized(self, project: Project) -> str:
        if not project.test_inactive_threshold:
            return "Never"
        return timesince(timezone.now() - project.test_inactive_threshold)

    @admin.display(description="Tests Stale")
    def test_stale_threshold_humanized(self, project: Project) -> str:
        if not project.test_stale_threshold:
            return "Never"
        return timesince(timezone.now() - project.test_stale_threshold)

    @admin.display(description="Results Stale")
    def result_stale_threshold_humanized(self, project: Project) -> str:
        if not project.result_stale_threshold:
            return "Never"
        return timesince(timezone.now() - project.result_stale_threshold)

    @admin.display(description="Suites Count")
    def suites_count(self, project: Project):
        return project.suites.count()

    @admin.display(description="Tests Count")
    def tests_count(self, project: Project):
        return project.tests.count()

    readonly_fields = (
        "suites_count",
        "tests_count",
        "cleaned_at",
        "created_at",
        "updated_at",
    )
    inlines = [SuiteInline]


@admin.register(Suite)
class SuiteAdmin(admin.ModelAdmin):
    def get_queryset(self, request):
        return super().get_queryset(request).select_related("project")

    form = SuiteAdminForm
    search_fields = ("project__repository", "name", "local_command")
    list_display = (
        "id",
        "project",
        "name",
        "parent",
        "supports_override",
        "tests_count",
        "_local_command",
        "created_at",
        "updated_at",
    )
    ordering = ("-updated_at",)
    list_filter = ("name", "supports_override", "project__repository")

    @admin.display(description="Tests Count")
    def tests_count(self, suite: Suite):
        return suite.tests.count()

    @admin.display(description="Local Command")
    def _local_command(self, suite: Suite):
        return mark_safe(f"<pre>{suite.local_command}</pre>")

    @admin.action(description="Reset test origins for selected suites")
    def reset_test_origins(self, request, queryset):
        count = 0
        for suite in queryset:
            suite.tests.update(
                original_branch="", original_commit="", original_metadata={}
            )
            count += 1
        s = "" if count == 1 else "s"
        self.message_user(
            request,
            f"Successfully reset test origins for {count} suite{s}.",
        )

    actions = [reset_test_origins]

    raw_id_fields = (
        "project",
        "parent",
    )
    readonly_fields = (
        "tests_count",
        "average_setup_duration",
        "average_tests_duration",
        "average_teardown_duration",
        "created_at",
        "updated_at",
    )


@admin.register(Run)
class RunAdmin(admin.ModelAdmin):
    search_fields = (
        "project__repository",
        "suite__name",
        "branch",
        "commit",
    )
    list_display = (
        "id",
        "suite",
        "_branch",
        "_commit",
        "setup_started_at",
        "tests_started_at",
        "tests_finished_at",
        "teardown_finished_at",
    )
    ordering = ("-setup_started_at",)
    list_filter = (
        "project__repository",
        "suite__name",
    )

    @admin.display(description="Branch")
    def _branch(self, run: Run):
        return mark_safe(f'<a href="{run.branch_url}" target="_blank">{run.branch}</a>')

    @admin.display(description="Commit")
    def _commit(self, run: Run):
        return mark_safe(
            f'<a href="{run.commit_url}" target="_blank">{run.commit_humanized}</a>'
        )

    readonly_fields = ("_setup_duration", "tests_duration", "teardown_duration")

    @admin.display(description="Setup Duration")
    def _setup_duration(self, run: Run):
        if not run.setup_duration:
            return "—"
        return f"{run.setup_duration:.2f} seconds"

    @admin.display(description="Tests Duration")
    def tests_duration(self, run: Run):
        if not run.tests_duration:
            return "—"
        return f"{run.tests_duration:.2f} seconds"

    @admin.display(description="Teardown Duration")
    def teardown_duration(self, run: Run):
        if not run.teardown_duration:
            return "—"
        return f"{run.teardown_duration:.2f} seconds"


@admin.register(Test)
class TestAdmin(admin.ModelAdmin):

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("project", "suite", "last_result", "maintainer")
        )

    search_fields = (
        "project__repository",
        "original_branch",
        "original_commit",
        "suite__name",
        "name",
        "disabled_user__email",
        "maintainer__email",
    )
    list_display = (
        "id",
        "project",
        "name",
        "markers",
        "enabled",
        "failure_rate",
        "block_rate",
        "average_duration",
        "_original_branch",
        "_original_commit",
        "created_at",
        "updated_at",
    )
    ordering = ("-updated_at",)
    list_filter = (
        "enabled",
        "created_at",
        "updated_at",
        "project__repository",
    )

    @admin.display(description="Original Branch")
    def _original_branch(self, test: Test):
        return mark_safe(
            f'<a href="{test.original_branch_url}" target="_blank">{test.original_branch}</a>'
        )

    @admin.display(description="Original Commit")
    def _original_commit(self, test: Test):
        return mark_safe(
            f'<a href="{test.original_commit_url}" target="_blank">{test.original_commit}</a>'
        )

    @admin.action(description="Disable selected tests")
    def disable(self, request, queryset):
        count = 0
        test: Test
        for test in queryset.filter(disabled_at__isnull=True):
            test.disabled_at = timezone.now()
            test.disabled_user = test.disabled_user or request.user
            test.save()
            count += 1
        s = "" if count == 1 else "s"
        self.message_user(
            request, f"Successfully marked {count} test{s} as non-blocking."
        )

    @admin.action(description="Enable selected tests")
    def enable(self, request, queryset):
        count = 0
        test: Test
        for test in queryset.filter(disabled_at__isnull=False):
            test.disabled_at = None
            test.disabled_user = test.disabled_user or request.user
            test.save()
            count += 1
        s = "" if count == 1 else "s"
        self.message_user(
            request, f"Successfully enabled {count} test{s} to block merges."
        )

    @admin.action(description="Update selected tests")
    def update(self, request, queryset):
        count = 0
        test: Test
        for test in queryset:
            if test.update():
                test.save()
                count += 1
        s = "" if count == 1 else "s"
        self.message_user(request, f"Successfully updated {count} test{s}.")

    actions = [disable, enable, update]

    raw_id_fields = ("project", "suite", "maintainer", "disabled_user")
    readonly_fields = (
        "enabled",
        "significant_branches",
        "failure_rate",
        "failure_rate_delta",
        "block_rate",
        "block_rate_delta",
        "average_duration",
        "last_result",
        "markers",
        "created_at",
        "updated_at",
    )

    def save_model(self, request, obj, form, change):
        if change and any(
            field in form.changed_data
            for field in [
                "disabled_at",
                "disabled_reason",
                "disabled_tracker",
            ]
        ):
            obj.disabled_user = obj.disabled_user or request.user
        super().save_model(request, obj, form, change)


@admin.register(Result)
class ResultAdmin(admin.ModelAdmin):

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("test__project")

    search_fields = (
        "test__project__repository",
        "test__name",
        "branch",
        "commit",
        "message",
    )
    list_display = (
        "id",
        "test__project",
        "test__name",
        "_branch",
        "_commit",
        "target",
        "platform",
        "browser",
        "final",
        "status",
        "duration",
        "markers",
        "created_at",
    )
    list_filter = (
        "status",
        "target",
        "platform",
        "browser",
        "final",
        "created_at",
        "test__project__repository",
    )

    @admin.display(description="Branch")
    def _branch(self, result: Result):
        return mark_safe(
            f'<a href="{result.branch_url}" target="_blank">{result.branch}</a>'
        )

    @admin.display(description="Commit")
    def _commit(self, result: Result):
        return mark_safe(
            f'<a href="{result.commit_url}" target="_blank">{result.commit_humanized}</a>'
        )

    raw_id_fields = ("test", "suite")
    readonly_fields = ("markers", "created_at", "logs")
