from django.conf import settings
from django.http import HttpRequest

import log
from ninja import ModelSchema, Schema
from ninja.parser import Parser
from ninja.security import APIKeyHeader
from ninja.types import DictStrAny

from tab.core.models import Organization
from tab.projects.constants import DEFAULT_SUITE
from tab.projects.models import Result


class JSONOrFormParser(Parser):
    def parse_body(self, request: HttpRequest) -> DictStrAny:
        if "json" in (request.content_type or ""):
            return super().parse_body(request)
        return {key: request.POST[key] for key in request.POST}


class ApiKey(APIKeyHeader):
    param_name = "X-API-Key"

    def authenticate(self, request, key):
        if hasattr(settings, "TEST"):
            log.warning("Bypassing authentication for tests")
            return True
        return Organization.objects.filter(key=key).exists()


class ResultRequest(ModelSchema):
    project: str
    suite: str = DEFAULT_SUITE
    test: str
    url: str | None = None

    class Meta:
        model = Result
        fields = [
            "branch",
            "commit",
            "status",
            "duration",
            "message",
            "target",
            "platform",
            "browser",
        ]

    @classmethod
    def get_metadata(cls, request_body: dict) -> dict:
        return {k: v for k, v in request_body.items() if k not in cls.Meta.fields}

    def get_model_fields(self) -> dict:
        return {
            field: getattr(self, field)
            for field in self.__class__.Meta.fields
            if field not in ["project", "suite", "test"]
        }


class ResultResponse(Schema):
    suite: str
    test: str
    status: str
    block: bool


class BulkResultRequest(Schema):
    project: str
    suite: str = DEFAULT_SUITE
    branch: str
    commit: str
    url: str | None = None

    class Config:
        model_fields = [
            "project",
            "branch",
            "commit",
            "tests",
        ]

    @classmethod
    def get_metadata(cls, request_body: dict) -> dict:
        return {
            k: v for k, v in request_body.items() if k not in cls.Config.model_fields
        }

    def get_model_fields(self) -> dict:
        return {
            field: getattr(self, field)
            for field in self.__class__.Config.model_fields
            if field not in ["project", "suite", "tests"]
        }


class BulkResultResponse(Schema):
    suite: str
    branch: str
    commit: str
    tests: int
    block: bool


class ShareRequest(Schema):
    project: str
    branch: str
    commit: str


class ShareResponse(Schema):
    project: str
    branch: str
    commit: str
    tests: int


class ErrorResponse(Schema):
    detail: str


class TrackRequest(Schema):
    project: str
    suite: str
    branch: str = ""
    commit: str = ""
    step: str

    @classmethod
    def get_metadata(cls, request_body: dict) -> dict:
        known_fields = ["project", "suite", "branch", "commit", "step"]
        return {k: v for k, v in request_body.items() if k not in known_fields}


class TrackResponse(Schema):
    project: str
    suite: str
    branch: str
    commit: str
    step: str
