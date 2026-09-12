import random
from datetime import timedelta
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

import log

from tab.api.helpers import parse_junit_xml
from tab.core.models import Organization
from tab.metrics.models import Team, TestHistory
from tab.projects.enums import Platform, Status, Target
from tab.projects.models import Project, Result, Run, Suite, Test
from tab.releases.enums import Type
from tab.releases.models import Environment, Release

JUNIT_FIXTURE_PATH = (
    Path(__file__).resolve().parents[3] / "api" / "tests" / "files" / "junit.xml"
)


class Command(BaseCommand):
    help = "Create sample organization, project, and metrics"

    def add_arguments(self, parser):
        super().add_arguments(parser)
        for action in parser._actions:
            if "--verbosity" in getattr(action, "option_strings", []):
                action.default = 2  # INFO and above by default
                break

    def handle(self, *args, **options):
        log.reset()
        log.init(verbosity=options["verbosity"])
        self.load_sample_data()
        self.create_default_user()
        self.create_default_organization()
        self.create_default_team()
        self.generate_sample_data()
        self.generate_review_releases()

    def load_sample_data(self):
        project, _created = Project.objects.get_or_create(
            repository="https://github.com/KittyCAD/modeling-app"
        )
        content = JUNIT_FIXTURE_PATH.read_text(encoding="utf-8")
        suite, _created = Suite.objects.get_or_create(project=project, name="unit")
        metadata = {
            "GITHUB_RUN_ID": "999999",
            "GITHUB_HEAD_REF": "main",
            "CI_COMMIT_SHA": "junit-sample",
        }
        results = parse_junit_xml(
            content,
            project,
            suite,
            branch="main",
            commit="junit-sample",
            metadata=metadata,
        )
        self.stdout.write(self.style.SUCCESS(f"Loaded {len(results)} test results"))

    def create_default_user(self):
        User = get_user_model()
        if not User.objects.filter(username="admin").exists():
            User.objects.create_superuser(
                username="admin", email="admin@zoo.dev", password="password"
            )
            self.stdout.write(self.style.SUCCESS("Default superuser created"))
        else:
            self.stdout.write(self.style.WARNING("Default superuser already exists"))

    def create_default_organization(self):
        organization, created = Organization.objects.get_or_create(
            name="Zoo",
            email_domain="zoo.dev",
            repository_index="https://github.com/KittyCAD",
            key="localhost",
        )
        if created:
            self.stdout.write(
                self.style.SUCCESS("Default organization created: %s" % organization)
            )
        else:
            self.stdout.write(self.style.WARNING("Default organization already exists"))

    def create_default_team(self):
        organization = Organization.objects.get(name="Zoo")
        team, created = Team.objects.get_or_create(
            organization=organization,
            slack_channel_name="#test-analysis-bot",
        )
        if created:
            self.stdout.write(self.style.SUCCESS("Default team created: %s" % team))
        else:
            self.stdout.write(self.style.WARNING("Default team already exists"))

    def generate_sample_data(self):
        for test in Test.objects.filter(name="sample test"):
            test.save()  # ensure sample tests are enabled

        project, created = Project.objects.get_or_create(
            repository="https://github.com/KittyCAD/sample-project"
        )
        if created:
            self.stdout.write(self.style.SUCCESS("Created sample project"))
        else:
            self.stdout.write(self.style.WARNING("Sample project already exists"))

        test, created = Test.objects.get_or_create(
            project=project, name="sample test", original_branch="main"
        )
        if created:
            self.stdout.write(self.style.SUCCESS(f"Created sample test"))
        else:
            self.stdout.write(self.style.WARNING(f"Sample test already exists"))
            test.history.all().delete()

        test.results.filter(branch="main").delete()

        suite = test.suite
        if suite:
            suite.runs.filter(branch="main").delete()
            suite.history.all().delete()

        days = 8
        num_results = 500
        end = timezone.now()
        start = end - timedelta(days=days)

        self._generate_results(test, num_results, start, end)
        if suite:
            self._generate_runs(project, suite, num_results, start, end)
            if suite.update():
                suite.save(
                    update_fields=[
                        "average_setup_duration",
                        "average_tests_duration",
                        "average_teardown_duration",
                        "updated_at",
                    ]
                )
        test.save()  # refresh last_result
        self._generate_history(test, days)

    def _generate_history(self, test, days):
        failure_rate = 0.25
        for hour in range(24 * days):
            timestamp = timezone.now() - timedelta(hours=hour)
            delta = random.uniform(0.01, 0.05)
            if hour // 24 % 2 == 0:
                if random.random() < 0.10:
                    failure_rate = max(0.0, failure_rate - delta)
                else:
                    continue
            else:
                if random.random() < 0.20:
                    failure_rate = min(1.0, failure_rate + delta)
                else:
                    continue

            history = TestHistory.objects.create(
                test=test,
                failure_rate=failure_rate,
                block_rate=random.uniform(0.0, 0.1),
                average_duration=random.uniform(15.0, 60.0),
            )
            history.timestamp = timestamp
            history.save()
        self.stdout.write(self.style.SUCCESS("Generated sample history metrics"))

    def _generate_results(self, test, num_results, start, end):
        statuses = [Status.PASSED, Status.FAILED]
        targets = [None, Target.WEB, Target.DESKTOP]
        platforms = [None, Platform.MACOS, Platform.WINDOWS, Platform.LINUX]
        browsers = [None, "Chromium", "Firefox", "WebKit", "Edge"]
        sample_messages = [
            "",
            "AssertionError: expected 42",
            "Timeout 5000ms exceeded",
            "Connection refused to localhost:5432",
            "Element not found: .submit-btn",
            "ValueError: invalid literal for int()",
            "All assertions passed",
            "Test completed successfully",
        ]
        results = []
        for i in range(num_results):
            fraction = (i + 0.5) / num_results
            created_at = start + (end - start) * fraction
            results.append(
                Result(
                    test=test,
                    suite=test.suite,
                    branch="main",
                    commit=f"sample{i:05x}",
                    status=random.choice(statuses),
                    duration=round(random.uniform(15.0, 60.0), 2),
                    final=True,
                    message=random.choice(sample_messages),
                    target=random.choice(targets),
                    platform=random.choice(platforms),
                    browser=random.choice(browsers),
                    created_at=created_at,
                )
            )
        Result.objects.bulk_create(results)
        self.stdout.write(self.style.SUCCESS("Generated sample test results"))

    def _generate_runs(self, project, suite, num_results, start, end):
        runs = []
        for i in range(num_results):
            fraction = (i + 0.5) / num_results
            tests_started_at = start + (end - start) * fraction
            setup_duration = round(random.uniform(8.0, 15.0), 2)
            tests_duration = round(random.uniform(30.0, 90.0), 2)
            teardown_duration = round(random.uniform(2.0, 8.0), 2)
            tests_finished_at = tests_started_at + timedelta(seconds=tests_duration)
            runs.append(
                Run(
                    project=project,
                    suite=suite,
                    branch="main",
                    commit=f"sample{i:05x}",
                    setup_started_at=tests_started_at
                    - timedelta(seconds=setup_duration),
                    tests_started_at=tests_started_at,
                    tests_finished_at=tests_finished_at,
                    teardown_finished_at=tests_finished_at
                    + timedelta(seconds=teardown_duration),
                )
            )
        Run.objects.bulk_create(runs)
        self.stdout.write(self.style.SUCCESS("Generated sample suite runs"))

    def generate_review_releases(self):
        project, _ = Project.objects.get_or_create(
            repository="https://github.com/KittyCAD/modeling-app"
        )
        staging = Environment.objects.filter(project=project, name=Type.STAGING).first()
        if staging is None:
            staging = Environment.objects.create(
                project=project,
                name=Type.STAGING,
                url="https://app.dev.zoo.dev",
            )
        Environment.objects.get_or_create(
            project=project,
            name=Type.REVIEW,
            url="https://modeling-{slug}.vercel.dev.zoo.dev",
        )

        staging_release = (
            Release.objects.filter(environment=staging).order_by("-created_at").first()
        )
        if staging_release is None:
            now = timezone.now()
            staging_release = Release.objects.create(
                environment=staging,
                branch="main",
                commit="stagapp" + "0" * 25,
                results=50,
                tested_at=now - timedelta(hours=1),
            )

        count = 1000
        now = timezone.now()
        # Walk backward from "now" with growing gaps so timeline breaks are visible.
        age = timedelta(hours=1)
        for index in range(count):
            pr_number = 1000 + index
            url = f"https://modeling-pr-{pr_number}.vercel.dev.zoo.dev"
            environment, _env_created = Environment.objects.get_or_create(
                project=project,
                name=Type.REVIEW,
                url=url,
            )
            environment.dependencies.add(staging)

            branch = f"feat/sample-{pr_number}"
            commit = f"rev{pr_number:05d}{'a' * 27}"
            release, _release_created = Release.objects.get_or_create(
                environment=environment,
                commit=commit,
                defaults={
                    "branch": branch,
                    "results": random.randint(3, 80),
                    "tested_at": now - age - timedelta(hours=1),
                },
            )
            Release.objects.filter(pk=release.pk).update(
                created_at=now - age,
                tested_at=now - age - timedelta(minutes=30),
                branch=branch,
            )
            release.dependencies.add(staging_release)

            # Next older release: gap grows each step (6h, 12h, 18h, ... capped at 14d).
            gap_hours = min(24 * 14, 6 * (index + 1))
            age += timedelta(hours=gap_hours)

        self.stdout.write(self.style.SUCCESS(f"Generated sample review releases"))
