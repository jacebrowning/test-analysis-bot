from datetime import timedelta

from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from django.utils.timesince import timesince

import pytest

from tab.core.management.commands.sendalerts import Command
from tab.core.models import Organization
from tab.metrics.models import Alert, Subscription, Team
from tab.projects.models import Project, Suite


@pytest.fixture
def organization():
    return Organization.objects.create(
        name="my-org",
        repository_index="https://github.com/foo",
        slack_bot_token="xoxb-test",
    )


@pytest.fixture
def project():
    return Project.objects.create(repository="https://github.com/foo/bar")


@pytest.fixture
def primary_team(organization, project):
    team = Team.objects.create(organization=organization, slack_channel_name="#primary")
    Subscription.objects.create(team=team, project=project, primary=True)
    return team


@pytest.fixture
def secondary_team(organization, project):
    team = Team.objects.create(
        organization=organization, slack_channel_name="#secondary"
    )
    Subscription.objects.create(team=team, project=project, primary=False)
    return team


def _disable(
    project: Project, *, weeks: float, name: str = "my-test", suite: Suite | None = None
):
    test = project.tests.create(name=name, suite=suite)
    test.disabled_at = timezone.now() - timedelta(weeks=weeks)
    test.save()
    return test


def _patch_send(mocker):
    return mocker.patch(
        "tab.metrics.models.send_slack_message",
        return_value="https://slack.test/msg",
    )


@pytest.mark.django_db
def describe_send_disabled_reminders(expect):
    def it_skips_projects_without_long_disabled_tests(
        project: Project, primary_team, mocker
    ):
        _disable(project, weeks=0.5)
        send = _patch_send(mocker)

        Command().send_disabled_reminders(project, dry_run=False)

        expect(send.called) is False
        expect(Alert.objects.count()) == 0

    def it_skips_when_there_is_no_primary_team(
        project: Project, secondary_team, mocker
    ):
        _disable(project, weeks=2)
        send = _patch_send(mocker)

        Command().send_disabled_reminders(project, dry_run=False)

        expect(send.called) is False

    def it_sends_to_the_primary_team_only(
        project: Project, primary_team, secondary_team, mocker
    ):
        oldest = _disable(project, weeks=3, name="oldest")
        _disable(project, weeks=2, name="newer")
        send = _patch_send(mocker)

        Command().send_disabled_reminders(project, dry_run=False)

        expect(send.call_count) == 1
        expect(Alert.objects.count()) == 1

        organization, channel, channel_id, message, unfurl = send.call_args.args
        expect(organization) == primary_team.organization
        expect(channel) == "#primary"
        expect(channel_id) == primary_team.slack_channel_id
        expect(unfurl) is False

        url = (
            settings.BASE_URL
            + reverse("projects:disabled-tests", args=[project.path])
            + "?sort=disabled_at"
        )
        age = timesince(oldest.disabled_at, depth=1)
        expect(message.text) == (
            f"Some tests have been disabled for more than {age}. "
            "Prioritize fixes to restore them"
        )
        expect(message.url) == url
        expect(message.label) == url

    def it_alerts_the_primary_team_for_the_matching_suite(
        organization, project: Project, mocker
    ):
        unit = Suite.objects.create(project=project, name="unit")
        e2e = Suite.objects.create(project=project, name="e2e")
        unit_team = Team.objects.create(
            organization=organization, slack_channel_name="#unit"
        )
        e2e_team = Team.objects.create(
            organization=organization, slack_channel_name="#e2e"
        )
        Subscription.objects.create(
            team=unit_team, project=project, suite=unit, primary=True
        )
        Subscription.objects.create(
            team=e2e_team, project=project, suite=e2e, primary=True
        )
        oldest = _disable(project, weeks=3, name="old-unit", suite=unit)
        _disable(project, weeks=2, name="old-e2e", suite=e2e)
        send = _patch_send(mocker)

        Command().send_disabled_reminders(project, dry_run=False)

        expect(Alert.objects.count()) == 1
        expect(Alert.objects.get().test) == oldest
        expect(send.call_args.args[1]) == "#unit"

    def it_skips_suite_subscriptions_without_matching_disabled_tests(
        organization, project: Project, mocker
    ):
        unit = Suite.objects.create(project=project, name="unit")
        e2e = Suite.objects.create(project=project, name="e2e")
        unit_team = Team.objects.create(
            organization=organization, slack_channel_name="#unit"
        )
        Subscription.objects.create(
            team=unit_team, project=project, suite=unit, primary=True
        )
        _disable(project, weeks=2, name="old-e2e", suite=e2e)
        send = _patch_send(mocker)

        Command().send_disabled_reminders(project, dry_run=False)

        expect(send.called) is False

    def it_does_not_send_slack_messages_on_dry_run(
        project: Project, primary_team, mocker
    ):
        _disable(project, weeks=2)
        send = _patch_send(mocker)

        Command().send_disabled_reminders(project, dry_run=True)

        expect(send.called) is False
        expect(Alert.objects.count()) == 0
