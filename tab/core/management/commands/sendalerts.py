from django.core.management.base import BaseCommand
from django.utils import timezone

import log

from tab.metrics.constants import DISABLED_REMINDER_THRESHOLD
from tab.metrics.models import Alert
from tab.projects.models import Project


class Command(BaseCommand):
    help = "Alert primary teams about tests disabled for more than a week"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show alerts that would be sent without sending Slack messages",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        start = timezone.now()
        log.info(f"Started job at {start.strftime('%Y-%m-%d %H:%M:%S')}")
        for project in Project.objects.all():
            self.send_disabled_reminders(project, dry_run)
        delta = timezone.now() - start
        log.info(f"Finished job after {delta.seconds // 60}:{delta.seconds % 60:02d}")

    def send_disabled_reminders(self, project: Project, dry_run: bool):
        cutoff = timezone.now() - DISABLED_REMINDER_THRESHOLD
        test = (
            project.tests.filter(disabled_at__isnull=False, disabled_at__lte=cutoff)
            .order_by("disabled_at")
            .first()
        )
        if not test:
            return

        alert = Alert(test=test)
        if dry_run:
            log.warning(f"Would send: {alert.build()}")
            return

        alert.save()
        alert.send(forward=False)
