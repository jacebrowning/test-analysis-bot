import json
from pathlib import Path

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile

import log
import pytest

from tab.api.constants import TESTS_CACHE_KEY
from tab.core.models import Organization
from tab.projects.models import Result, Test


def post_json(client, url: str, data: dict):
    log.info(f"POST {url}: {data}")
    response = client.post(
        url,
        data=json.dumps(data),
        content_type="application/json",
        headers={"X-API-Key": "fake-api-key"},
    )
    try:
        log.info(f"{response.status_code} response: {response.json()}")
    except ValueError:
        log.info(f"{response.status_code} response: {response.text}")
    return response


def post_form(client, url: str, data: dict):
    log.info(f"POST {url}: {data}")
    response = client.post(
        url,
        data=data,
        headers={"X-API-Key": "fake-api-key"},
    )
    try:
        log.info(f"{response.status_code} response: {response.json()}")
    except ValueError:
        log.info(f"{response.status_code} response: {response.text}")
    return response


def describe_results(expect, client):

    url = "/api/results"

    @pytest.fixture
    def payload():
        return {
            "project": "https://github.com/my-user/my-project",
            "suite": "e2e",
            "test": "my feature › my test",
            "branch": "main",
            "commit": "abc123",
            "status": "passed",
        }

    @pytest.mark.django_db
    def it_creates_projects_and_tests_automatically(payload):
        response = post_json(client, url, payload)

        expect(response.status_code) == 201
        expect(response.json()) == {
            "suite": "my-user > my-project > e2e",
            "test": "e2e > my feature > my test",
            "status": "passed",
            "block": False,
        }

    @pytest.mark.parametrize(
        "project",
        [
            "https://github.com/my-user",
            "github.com/my-user/my-project",
            "my-user/my-project",
        ],
    )
    def it_rejects_invalid_repositories(payload, project):
        payload["project"] = project
        response = post_json(client, url, payload)

        expect(response.status_code) == 422
        expect(response.json()) == {
            "detail": f"Invalid repository URL: {project}",
        }

    def it_rejects_oversized_request_body(payload, settings):
        settings.DATA_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024
        payload["message"] = "x" * (1024 * 1024)
        body_mb = len(json.dumps(payload).encode()) / (1024 * 1024)
        response = post_json(client, url, payload)

        expect(response.status_code) == 413
        expect(response.json()) == {
            "detail": f"Request body of {body_mb:g} MB exceeded the 1 MB limit.",
        }

    @pytest.mark.django_db
    def it_updates_existing_test(payload):
        payload2 = payload.copy()
        payload2["branch"] = ""
        payload2["commit"] = ""
        response = post_json(client, url, payload2)
        test = Test.objects.get()

        expect(response.status_code) == 201
        expect(response.json()) == {
            "suite": "my-user > my-project > e2e",
            "test": "e2e > my feature > my test",
            "status": "passed",
            "block": False,
        }
        expect(test.original_branch) == ""
        expect(test.original_commit) == ""

        response = post_json(client, url, payload)
        test.refresh_from_db()

        expect(response.status_code) == 200
        expect(test.original_branch) == "main"
        expect(test.original_commit) == "abc123"


def describe_bulk_results(expect, client):
    url = "/api/results/bulk"

    @pytest.fixture
    def payload():
        junit_xml = Path(__file__).parent / "files" / "junit-nextest.xml"
        return {
            "project": "https://github.com/my-user/my-project",
            "suite": "unit",
            "branch": "main",
            "commit": "abc123",
            "tests": junit_xml.open("rb"),
            "EXTRA": "foobar",
        }

    @pytest.mark.django_db
    def it_creates_tests_from_junit_xml(payload):
        response = post_form(client, url, payload)
        expect(response.json()) == {
            "suite": "my-user > my-project > unit",
            "branch": "main",
            "commit": "abc123",
            "tests": 25,
            "block": False,
        }
        test: Test = Test.objects.first()  # type: ignore[assignment]
        expect(test.original_branch) == "main"
        expect(test.original_commit) == "abc123"
        expect(test.original_metadata) == {"EXTRA": "foobar", "suite": "unit"}
        result: Result = Result.objects.first()  # type: ignore[assignment]
        expect(result.branch) == "main"
        expect(result.commit) == "abc123"
        expect(result.metadata) == {"EXTRA": "foobar", "suite": "unit"}

    @pytest.mark.django_db
    def it_defers_large_reports_with_self_closing_testcases(payload):
        cache.delete(TESTS_CACHE_KEY)
        testcases = "".join(f'<testcase name="test-{index}" />' for index in range(301))
        content = f"<testsuites><testsuite>{testcases}</testsuite></testsuites>"
        payload["tests"] = SimpleUploadedFile("junit.xml", content.encode())

        response = post_form(client, url, payload)

        expect(response.status_code) == 200
        expect(response.json()["tests"]) == 301
        expect(cache.get(TESTS_CACHE_KEY)) == set(
            Test.objects.values_list("id", flat=True)
        )

    @pytest.mark.django_db
    def it_requires_tests_as_file_upload(payload):
        del payload["tests"]
        response = post_form(client, url, payload)
        expect(response.status_code) == 422
        expect(response.json()) == {
            "detail": "Include 'tests' as a JUnit XML file upload.",
        }


def describe_share(expect, client):

    url = "/api/share"

    @pytest.fixture
    def payload():
        return {
            "project": "https://github.com/my-user/my-project",
            "branch": "my-branch",
            "commit": "abc123",
        }

    @pytest.mark.parametrize("post", [post_json, post_form], ids=["json", "form"])
    @pytest.mark.django_db
    def it_updates_status(payload, mocker, post):
        mock_github = mocker.patch("tab.core.models.Github")
        mock_repo = mock_github.return_value.get_repo.return_value
        mock_commit = mock_repo.get_commit.return_value
        mock_create_status = mock_commit.create_status

        Organization.objects.create(
            name="MyOrganization",
            key="fake-api-key",
            repository_index="https://github.com/my-user",
            repository_token="fake-token",
        )

        response = post(client, url, payload)

        expect(response.status_code) == 200
        expect(response.json()) == {
            "project": "my-user > my-project",
            "branch": "my-branch",
            "commit": "abc123",
            "tests": 0,
        }
        expect(mock_create_status.call_args) == mocker.call(
            state="success",
            target_url="http://testserver.com/projects/my-user/my-project/results?branch=my-branch",
            description="0 of 0 passing",
            context="Test Analysis Bot",
        )

    @pytest.mark.parametrize(
        "project",
        [
            "https://github.com/my-user",
            "github.com/my-user/my-project",
            "my-user/my-project",
        ],
    )
    @pytest.mark.parametrize("post", [post_json, post_form], ids=["json", "form"])
    @pytest.mark.django_db
    def it_rejects_invalid_repositories(payload, project, post):
        Organization.objects.create(name="MyOrganization", key="fake-api-key")

        payload["project"] = project
        response = post(client, url, payload)

        expect(response.status_code) == 422
        expect(response.json()) == {
            "detail": f"Invalid repository URL: {project}",
        }


def describe_track(expect):
    url = "/api/track"

    @pytest.fixture
    def payload():
        return {
            "project": "https://github.com/my-user/my-project",
            "suite": "e2e",
            "branch": "main",
            "commit": "abc123",
            "step": "setup",
        }

    @pytest.mark.django_db
    def it_requires_suite_to_exist(client, payload):
        response = post_form(client, url, payload)
        expect(response.status_code) == 404
        expect(response.json()) == {
            "detail": "Unknown suite: e2e",
        }
