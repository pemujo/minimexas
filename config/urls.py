from django.conf import settings
from django.urls import path, include, re_path
from django.views.static import serve
from recommendations.views import (
    home_view, 
    login_page_view, 
    gmail_login_view, 
    google_callback_view, 
    logout_view,
    events_view,
    event_rsvp_view,
    organizer_event_broadcast_view,
    organizer_create_event_view,
    organizer_edit_event_view,
    organizer_delete_event_view,
    profile_view,
    profile_delete_self_view,
    member_directory_view,
    organizer_dashboard_view,
    join_request_view,
    organizer_approve_request_view,
    organizer_reject_request_view,
    organizer_direct_add_member_view,
    organizer_delete_member_view,
    organizer_sync_sheets_view,
    surveys_list_view,
    survey_vote_view,
    organizer_survey_create_view,
    organizer_survey_toggle_status_view,
    organizer_survey_delete_view,
)

urlpatterns = [
    path('', home_view, name='home'),
    path('join/', join_request_view, name='join_request'),
    path('events/', events_view, name='events'),
    path('events/create/', organizer_create_event_view, name='event_create'),
    path('events/<int:event_id>/edit/', organizer_edit_event_view, name='event_edit'),
    path('events/<int:event_id>/delete/', organizer_delete_event_view, name='event_delete'),
    path('events/rsvp/', event_rsvp_view, name='event_rsvp'),
    path('events/broadcast/', organizer_event_broadcast_view, name='event_broadcast'),
    path('directory/', member_directory_view, name='member_directory'),
    path('surveys/', surveys_list_view, name='surveys'),
    path('surveys/create/', organizer_survey_create_view, name='survey_create'),
    path('surveys/<int:survey_id>/vote/', survey_vote_view, name='survey_vote'),
    path('surveys/<int:survey_id>/toggle/', organizer_survey_toggle_status_view, name='survey_toggle_status'),
    path('surveys/<int:survey_id>/delete/', organizer_survey_delete_view, name='survey_delete'),
    path('profile/', profile_view, name='profile'),
    path('profile/delete/', profile_delete_self_view, name='profile_delete_self'),
    path('organizers/', organizer_dashboard_view, name='organizer_dashboard'),
    path('organizers/sync/', organizer_sync_sheets_view, name='organizer_sync_sheets'),
    path('organizers/requests/<int:request_id>/approve/', organizer_approve_request_view, name='organizer_approve_request'),
    path('organizers/requests/<int:request_id>/reject/', organizer_reject_request_view, name='organizer_reject_request'),
    path('organizers/members/add/', organizer_direct_add_member_view, name='organizer_direct_add_member'),
    path('organizers/members/<int:member_id>/delete/', organizer_delete_member_view, name='organizer_delete_member'),
    path('login/', login_page_view, name='login_page'),
    path('auth/google/', gmail_login_view, name='gmail_login'),
    path('auth/callback/', google_callback_view, name='google_callback'),
    path('logout/', logout_view, name='logout'),
    path('recommendations/', include('recommendations.urls', namespace='recommendations')),
]

# Ensure static and media files are properly routed in all environments
_static_dir = settings.STATICFILES_DIRS[0] if settings.STATICFILES_DIRS else settings.STATIC_ROOT
urlpatterns += [
    re_path(r'^static/(?P<path>.*)$', serve, {'document_root': _static_dir}),
    re_path(r'^media/(?P<path>.*)$', serve, {'document_root': settings.MEDIA_ROOT}),
]

