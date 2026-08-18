from django.contrib import admin
from .models import MemberProfile


@admin.register(MemberProfile)
class MemberProfileAdmin(admin.ModelAdmin):
    list_display = ('full_name', 'email', 'region', 'city', 'phone_number', 'email_notifications', 'is_admin', 'updated_at')
    list_filter = ('email_notifications', 'is_admin', 'region', 'created_at', 'updated_at')
    search_fields = ('full_name', 'email', 'city', 'bio', 'interests')
    ordering = ('full_name', 'email')
    readonly_fields = ('created_at', 'updated_at')
