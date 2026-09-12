from django.contrib import admin
from django.utils.safestring import mark_safe

from markdown import markdown

from tab.projects.models import Suite

from .models import Alert, Subscription, SuiteHistory, Team, TestHistory


@admin.register(TestHistory)
class TestHistoryAdmin(admin.ModelAdmin):
    def get_queryset(self, request):
        return super().get_queryset(request).select_related("test__project")

    list_display = (
        "id",
        "test__project",
        "test__name",
        "result",
        "failure_rate",
        "block_rate",
        "average_duration",
        "timestamp",
    )
    search_fields = (
        "test__project__repository",
        "test__name",
    )
    list_filter = (
        "test__project__repository",
        "test__suite__name",
    )

    raw_id_fields = ("test", "result")
    readonly_fields = (
        "failure_rate",
        "block_rate",
        "average_duration",
        "timestamp",
    )


@admin.register(SuiteHistory)
class SuiteHistoryAdmin(admin.ModelAdmin):
    def get_queryset(self, request):
        return super().get_queryset(request).select_related("suite__project")

    list_display = (
        "id",
        "suite__project",
        "suite__name",
        "run",
        "average_setup_duration",
        "average_tests_duration",
        "average_teardown_duration",
        "timestamp",
    )
    search_fields = (
        "suite__project__repository",
        "suite__name",
    )
    list_filter = (
        "suite__project__repository",
        "suite__name",
    )

    raw_id_fields = ("suite", "run")
    readonly_fields = (
        "average_setup_duration",
        "average_tests_duration",
        "average_teardown_duration",
        "timestamp",
    )


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "organization",
        "slack_channel_name",
        "slack_channel_id",
        "alerted_at",
    )
    list_filter = ("alerted_at",)
    search_fields = (
        "organization__name",
        "slack_channel_name",
        "slack_channel_id",
    )


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "suite":
            kwargs["queryset"] = Suite.objects.select_related("project").order_by(
                "project__repository", "name"
            )
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    search_fields = (
        "team__organization__name",
        "team__slack_channel_name",
        "project__repository",
        "suite__name",
        "test",
    )
    list_display = (
        "id",
        "team",
        "project",
        "suite",
        "test",
        "primary",
    )
    list_filter = (
        "primary",
        "project",
        "suite",
    )


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related(
                "history__test__project", "history__test__suite", "history__result"
            )
        )

    list_display = (
        "id",
        "test__name",
        "_message",
        "_subscriptions",
        "created_at",
        "sent_at",
    )
    list_filter = (
        "created_at",
        "sent_at",
        "test__project__repository",
    )
    search_fields = (
        "test__project__repository",
        "test__name",
    )

    @admin.display(description="Message")
    def _message(self, alert: Alert):
        if alert.url:
            label = alert.url.split("/archives/")[0]
            return mark_safe(f"<a href='{alert.url}' target='_blank'>{label}</a>")
        return self._message_text(alert)

    @admin.display(description="Message | Text")
    def _message_text(self, alert: Alert):
        return alert.build()

    @admin.display(description="Message | HTML")
    def _message_html(self, alert: Alert):
        message = alert.build(debug=True)
        return mark_safe(message.html)

    @admin.display(description="Message | Markdown")
    def _message_markdown(self, alert: Alert):
        message = alert.build(debug=True)
        html = markdown(message.markdown, extensions=["fenced_code"])
        return mark_safe(html)

    @admin.display(description="Message | Slack")
    def _message_mrkdwn(self, alert: Alert):
        return alert.build(debug=True).mrkdwn

    @admin.display(description="Subscriptions")
    def _subscriptions(self, alert: Alert):
        return mark_safe("<br><br>".join([str(s) for s in alert.subscriptions]))

    @admin.action(description="Send selected alerts")
    def send(self, request, queryset):
        count = 0
        alert: Alert
        for alert in queryset:
            count += alert.send()
        s = "" if count == 1 else "s"
        self.message_user(request, f"Successfully sent {count} alert{s}.")

    @admin.action(description="Send selected alerts (debug)")
    def send_debug(self, request, queryset):
        count = 0
        alert: Alert
        for alert in queryset:
            count += alert.send(debug=True)
        s = "" if count == 1 else "s"
        self.message_user(request, f"Successfully sent {count} debug alert{s}.")

    actions = [send, send_debug]

    raw_id_fields = ("test", "history")
    readonly_fields = (
        "_message_text",
        "_message_html",
        "_message_markdown",
        "_message_mrkdwn",
        "_subscriptions",
        "created_at",
        "sent_at",
        "url",
    )
