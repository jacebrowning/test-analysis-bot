import json
import tomllib

from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.shortcuts import redirect

import log
from ninja import Form, NinjaAPI
from unidecode import unidecode

from tab.core.models import Organization
from tab.projects.enums import Status
from tab.projects.models import Project, Result, Run, Suite, Test
from tab.releases.models import Environment, Release

from .helpers import parse_junit_xml, update_status
from .schemas import (
    ApiKey,
    BulkResultRequest,
    BulkResultResponse,
    ErrorResponse,
    JSONOrFormParser,
    ResultRequest,
    ResultResponse,
    ShareRequest,
    ShareResponse,
    TrackRequest,
    TrackResponse,
)

project = tomllib.load(open("pyproject.toml", "rb"))["project"]
readme_url = "https://github.com/KittyCAD/test-analysis-bot/blob/main/README.md"
api = NinjaAPI(
    title="Test Analysis Bot",
    version=project["version"],
    description=f"{project['description']} See the [README]({readme_url}) for examples.",
    parser=JSONOrFormParser(),
)
api_key = ApiKey()


@api.exception_handler(RequestDataTooBig)
def request_data_too_big(request, exc):
    max_mb = settings.DATA_UPLOAD_MAX_MEMORY_SIZE / (1024 * 1024)
    body_size = int(request.META.get("CONTENT_LENGTH") or 0)
    if body_size:
        body_mb = body_size / (1024 * 1024)
        detail = f"Request body of {body_mb:g} MB exceeded the {max_mb:g} MB limit."
    else:
        detail = f"Request body exceeded the {max_mb:g} MB limit."
    log.warning(f"Exceeded request body size: {body_size:g=} B, {max_mb:g=} MB")
    return api.create_response(request, {"detail": detail}, status=413)


@api.get("/", include_in_schema=False)
def index(request):
    return redirect("/api/docs")


@api.post(
    "/results",
    auth=api_key,
    response={
        200: ResultResponse,
        201: ResultResponse,
        413: ErrorResponse,
        422: ErrorResponse,
    },
    tags=["Tests"],
)
def results(request, payload: ResultRequest):
    assert hasattr(payload, "branch") and hasattr(payload, "commit")  # type hint

    try:
        if hasattr(settings, "TEST"):
            log.warning("Skipping ownership check for tests")
            project = Project.objects.from_repository(payload.project)
        else:
            key = request.headers.get(ApiKey.param_name)
            organization = Organization.objects.get(key=key)
            project = Project.objects.from_repository(
                payload.project, organization.repository_index
            )
    except ValueError as e:
        return 422, {"detail": str(e)}

    suite, created = Suite.objects.get_or_create(project=project, name=payload.suite)
    if created:
        log.info(f"Created suite: {suite}")
    else:
        log.info(f"Found suite: {suite}")

    metadata = ResultRequest.get_metadata(json.loads(request.body))
    Run.objects.track_step(
        suite=suite,
        branch=payload.branch,
        commit=payload.commit,
        step="start",
        metadata=metadata,
    )
    test, created = Test.objects.get_or_create(
        project=project,
        name=payload.test,
        defaults=dict(
            suite=suite,
            original_branch=payload.branch,
            original_commit=payload.commit,
            original_metadata=metadata,
        ),
    )
    if created:
        status = 201
        log.info(f"Created test: {test}")
    elif not all([test.suite, test.original_branch, test.original_commit]):
        status = 200
        test.suite = test.suite or suite
        test.original_branch = test.original_branch or payload.branch
        test.original_commit = test.original_commit or payload.commit
        test.original_metadata = test.original_metadata or metadata
        test.save()
        log.info(f"Updated test: {test}")
    else:
        status = 200
        log.info(f"Found test: {test}")

    result = Result.objects.create(
        test=test,
        suite=suite,
        **payload.get_model_fields(),
        metadata=metadata,
    )
    log.info(f"Created result: {result}")
    Environment.objects.process(project, payload.url, [result])

    Run.objects.track_step(
        suite=suite,
        branch=payload.branch,
        commit=payload.commit,
        step="finish",
        metadata=metadata,
    )

    return status, ResultResponse(
        suite=unidecode(str(suite)),
        test=unidecode(str(test)),
        status=result.status,
        block=result.status in Status.merge_blocked(),
    )


@api.post(
    "/results/bulk",
    auth=api_key,
    response={
        200: BulkResultResponse,
        422: ErrorResponse,
    },
    tags=["Tests"],
)
def bulk_results(request, payload: Form[BulkResultRequest]):
    try:
        if hasattr(settings, "TEST"):
            log.warning("Skipping ownership check for tests")
            project = Project.objects.from_repository(payload.project)
        else:
            key = request.headers.get(ApiKey.param_name)
            organization = Organization.objects.get(key=key)
            project = Project.objects.from_repository(
                payload.project, organization.repository_index
            )
    except ValueError as e:
        return 422, {"detail": str(e)}

    suite, created = Suite.objects.get_or_create(project=project, name=payload.suite)
    if created:
        log.info(f"Created suite: {suite}")
    else:
        log.info(f"Found suite: {suite}")

    if tests := request.FILES.get("tests"):
        content = tests.read().decode("utf-8")
        metadata = BulkResultRequest.get_metadata(request.POST.dict())
        Run.objects.track_step(
            suite=suite,
            branch=payload.branch,
            commit=payload.commit,
            step="start",
            metadata=metadata,
        )
        results = parse_junit_xml(
            content,
            project,
            suite,
            payload.branch,
            payload.commit,
            metadata,
        )
        Environment.objects.process(project, payload.url, results)
        Run.objects.track_step(
            suite=suite,
            branch=payload.branch,
            commit=payload.commit,
            step="finish",
            metadata=metadata,
        )
        return 200, BulkResultResponse(
            suite=unidecode(str(suite)),
            branch=payload.branch,
            commit=payload.commit,
            tests=len(results),
            block=any(result.status in Status.merge_blocked() for result in results),
        )

    return 422, {"detail": "Include 'tests' as a JUnit XML file upload."}


@api.post(
    "/share",
    auth=api_key,
    response={
        200: ShareResponse,
        404: ErrorResponse,
        422: ErrorResponse,
    },
    tags=["Tests"],
)
def share(request, payload: ShareRequest):
    try:
        key = request.headers.get(ApiKey.param_name)
        organization = Organization.objects.get(key=key)
        project = Project.objects.from_repository(
            payload.project, organization.repository_index
        )
    except (Organization.DoesNotExist, ValueError) as e:
        return 422, {"detail": str(e)}

    health = Result.objects.get_health(project, payload.commit)
    update_status(organization, project, payload.commit, payload.branch, health)

    return 200, ShareResponse(
        project=unidecode(str(project)),
        branch=payload.branch,
        commit=payload.commit,
        tests=health.total,
    )


@api.post(
    "/track",
    auth=api_key,
    response={
        200: TrackResponse,
        404: ErrorResponse,
        422: ErrorResponse,
    },
    tags=["Tests"],
)
def track(request, payload: Form[TrackRequest]):
    try:
        if hasattr(settings, "TEST"):
            log.warning("Skipping ownership check for tests")
            project = Project.objects.from_repository(payload.project)
        else:
            key = request.headers.get(ApiKey.param_name)
            organization = Organization.objects.get(key=key)
            project = Project.objects.from_repository(
                payload.project, organization.repository_index
            )
    except ValueError as e:
        return 422, {"detail": str(e)}

    suite = Suite.objects.filter(project=project, name=payload.suite).first()
    if suite:
        log.info(f"Found suite: {suite}")
    else:
        log.warning(f"Skipped tracking for unknown suite: {payload.suite}")
        return 404, {"detail": f"Unknown suite: {payload.suite}"}

    metadata = TrackRequest.get_metadata(request.POST.dict())
    Run.objects.track_step(
        suite=suite,
        branch=payload.branch,
        commit=payload.commit,
        step=payload.step,
        metadata=metadata,
    )

    return 200, TrackResponse(
        project=unidecode(str(project)),
        suite=payload.suite,
        branch=payload.branch,
        commit=payload.commit,
        step=payload.step,
    )
