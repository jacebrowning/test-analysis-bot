from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import OuterRef, Q, Subquery
from django.urls import reverse
from django.views.generic import TemplateView

from tab.core.helpers import organization_for_email
from tab.core.models import Organization

from .constants import CHANGE_HISTORY_LIMIT
from .enums import Type
from .helpers import build_environment_graph, build_release_graph
from .models import Environment, Release


class IndexView(LoginRequiredMixin, TemplateView):
    template_name = "releases/list.html"

    def get_organization(self) -> Organization | None:
        assert self.request.user.is_authenticated
        return organization_for_email(self.request.user.email)

    def get_history_limit(self) -> int:
        raw = self.request.GET.get("limit")
        if raw is None:
            return CHANGE_HISTORY_LIMIT
        try:
            return max(1, min(int(raw), 500))
        except (TypeError, ValueError):
            return CHANGE_HISTORY_LIMIT

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        organization = self.get_organization()
        show_review = self.request.GET.get("review") == "true"
        show_lines = self.request.GET.get("lines", "true") == "true"
        limit = self.get_history_limit()
        truncated = False

        if organization is None:
            environments: list[Environment] = []
            releases: list[Release] = []
        else:
            environments = list(Environment.objects.filter_promotable(organization))
            releases_qs = Release.objects.filter(
                environment__project__repository__startswith=organization.repository_index
            ).exclude(environment__name=Type.LOCAL)
            if show_review:
                latest_review_id = (
                    Release.objects.filter(
                        environment__project_id=OuterRef("environment__project_id"),
                        branch=OuterRef("branch"),
                        environment__name=Type.REVIEW,
                    )
                    .order_by("-created_at", "-pk")
                    .values("pk")[:1]
                )
                releases_qs = releases_qs.filter(
                    ~Q(environment__name=Type.REVIEW) | Q(pk=Subquery(latest_review_id))
                )
            else:
                releases_qs = releases_qs.exclude(environment__name=Type.REVIEW)
            # Fetch one extra row to detect whether the chart is truncated.
            releases = list(
                releases_qs.select_related("environment__project").prefetch_related(
                    "dependencies"
                )[: limit + 1]
            )
            truncated = len(releases) > limit
            releases = releases[:limit]

        context["show_review"] = show_review
        context["show_lines"] = show_lines
        context["history_limit"] = limit
        context["release_graph_truncated"] = truncated
        context["environment_graph"] = build_environment_graph(environments)
        context["release_graph"] = build_release_graph(
            releases, truncated=truncated, include_review=show_review
        )
        if self.request.user.is_staff:
            context["admin_url"] = reverse("admin:releases_environment_changelist")
        return context
