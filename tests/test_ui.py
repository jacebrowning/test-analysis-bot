import re
from pathlib import Path

from django.urls import reverse

import pytest
from playwright.sync_api import Page

from tab.core.models import Organization
from tab.projects.enums import Status
from tab.projects.models import Project, Test
from tab.releases.enums import Type
from tab.releases.models import Environment, Release

from .utils import force_login, take_snapshot


@pytest.fixture(autouse=True)
def organization():
    organization = Organization.objects.create(
        name="Test Org",
        email_domain="example.com",
        repository_index="https://github.com/foo",
    )

    project = Project.objects.create(repository="https://github.com/foo/bar")
    staging = Environment.objects.create(
        project=project, name=Type.STAGING, url="https://staging.example.com"
    )
    production = Environment.objects.create(
        project=project, name=Type.PRODUCTION, url="https://api.example.com"
    )
    staging.dependencies.add(production)

    app = Release.objects.create(environment=staging, branch="main", commit="aaa1111")
    api = Release.objects.create(
        environment=production, branch="main", commit="bbb2222"
    )
    app.dependencies.add(api)
    return organization


@pytest.mark.django_db
def test_releases(page: Page, live_server, admin_user):
    force_login(page, live_server, admin_user)
    page.goto(f"{live_server.url}{reverse('releases:index')}")

    assert page.get_by_text("Environment Dependencies").is_visible()
    environment_graph = page.locator("#environment-graph-data").evaluate(
        "el => el.textContent"
    )
    assert "https://staging.example.com" in environment_graph

    assert page.get_by_text("Change History").is_visible()
    release_graph = page.locator("#release-graph-data").evaluate("el => el.textContent")
    assert "aaa1111" in release_graph

    lines = page.locator("#show-dependency-lines")
    review = page.locator("#show-review-releases")
    assert lines.is_checked()
    assert not review.is_checked()
    take_snapshot(page, "releases/lines-on-review-off")

    lines.uncheck()
    assert re.search(r"[?&]lines=false", page.url)
    assert not review.is_checked()
    take_snapshot(page, "releases/lines-off-review-off")

    with page.expect_navigation():
        review.check()
    lines = page.locator("#show-dependency-lines")
    review = page.locator("#show-review-releases")
    assert not lines.is_checked()
    assert review.is_checked()
    assert re.search(r"[?&]lines=false", page.url)
    assert re.search(r"[?&]review=true", page.url)
    take_snapshot(page, "releases/lines-off-review-on")

    lines.check()
    assert not re.search(r"[?&]lines=false", page.url)
    assert review.is_checked()
    take_snapshot(page, "releases/lines-on-review-on")


@pytest.mark.django_db
def test_export_button(page: Page, live_server, admin_user):
    project = Project.objects.get(repository="https://github.com/foo/bar")
    test = Test.objects.create(project=project, name="flaky-test")
    test.results.create(
        branch="main",
        commit="abc123",
        status=Status.PASSED,
        duration=1.0,
    )

    force_login(page, live_server, admin_user)
    page.goto(
        f"{live_server.url}"
        f"{reverse('projects:test-results', args=[project.path, test.id])}"
    )

    button = page.locator("#export-button")
    assert button.is_visible()
    assert not button.evaluate("el => el.classList.contains('disabled')")

    with page.expect_download() as download_info:
        button.click()

    download = download_info.value
    assert download.suggested_filename == f"tab-export-foo-bar-test-{test.pk}.json"
    path = download.path()
    assert path is not None
    body = Path(path).read_text()
    assert '"name": "flaky-test"' in body

    button.locator("xpath=following-sibling::button").click()
    page.get_by_role("link", name="Preview Content").click()
    assert page.url.endswith(
        reverse("projects:test-export-page", args=[project.path, test.id])
    )
    assert '"name": "flaky-test"' in page.locator("pre").inner_text()
