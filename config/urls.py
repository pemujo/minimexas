from django.urls import path, include
from recommendations.views import (
    home_view, 
    login_page_view, 
    gmail_login_view, 
    google_callback_view, 
    logout_view,
    events_view,
    profile_view,
    member_directory_view,
    organizer_dashboard_view,
    join_request_view,
    organizer_approve_request_view,
    organizer_reject_request_view,
    organizer_direct_add_member_view
)

urlpatterns = [
    path('', home_view, name='home'),
    path('join/', join_request_view, name='join_request'),
    path('events/', events_view, name='events'),
    path('directory/', member_directory_view, name='member_directory'),
    path('profile/', profile_view, name='profile'),
    path('organizers/', organizer_dashboard_view, name='organizer_dashboard'),
    path('organizers/requests/<int:request_id>/approve/', organizer_approve_request_view, name='organizer_approve_request'),
    path('organizers/requests/<int:request_id>/reject/', organizer_reject_request_view, name='organizer_reject_request'),
    path('organizers/members/add/', organizer_direct_add_member_view, name='organizer_direct_add_member'),
    path('login/', login_page_view, name='login_page'),
    path('auth/google/', gmail_login_view, name='gmail_login'),
    path('auth/callback/', google_callback_view, name='google_callback'),
    path('logout/', logout_view, name='logout'),
    path('recommendations/', include('recommendations.urls', namespace='recommendations')),
]
