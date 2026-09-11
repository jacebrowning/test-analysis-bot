from django.contrib import admin, messages
from django.utils.safestring import mark_safe

import log

from .models import OIDCIdentity, Organization, generate_key


@admin.register(OIDCIdentity)
class OIDCIdentityAdmin(admin.ModelAdmin):
    list_display = ("issuer", "subject", "user", "created_at")
    search_fields = ("issuer", "subject", "user__username", "user__email")
    autocomplete_fields = ("user",)
    readonly_fields = ("issuer", "subject", "user", "created_at")

    def has_add_permission(self, request):
        return False


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    search_fields = ("name", "email_domain", "repository_index")
    actions = ["regenerate_key", "test_exception"]
    list_display = (
        "name",
        "email_domain",
        "_repository_index",
        "created_at",
        "updated_at",
    )
    ordering = ("-updated_at",)
    list_filter = ("created_at", "updated_at")

    @admin.display(description="Repository Index", ordering="repository_index")
    def _repository_index(self, organization: Organization):
        url = organization.repository_index
        return mark_safe(f'<a href="{url}" target="_blank">{url}</a>')

    @admin.action(description="Regenerate key for selected organizations")
    def regenerate_key(self, request, queryset):
        for organization in queryset:
            organization.key = generate_key()
            organization.save()
        count = queryset.count()
        s = "" if count == 1 else "s"
        self.message_user(
            request,
            f"Successfully regenerated keys for {count} organization{s}.",
            messages.SUCCESS,
        )

    @admin.action(description="Trigger sample exception")
    def test_exception(self, request, queryset):
        log.error(f"Sample exception triggered by admin: {request.user}")

        raise RuntimeError("This is a sample exception.")

    readonly_fields = ("created_at", "updated_at")
