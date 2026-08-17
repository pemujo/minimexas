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
    organizer_dashboard_view
)

urlpatterns = [
    path('', home_view, name='home'),
    path('events/', events_view, name='events'),
    path('directory/', member_directory_view, name='member_directory'),
    path('profile/', profile_view, name='profile'),
    path('organizers/', organizer_dashboard_view, name='organizer_dashboard'),
    path('login/', login_page_view, name='login_page'),
    path('auth/google/', gmail_login_view, name='gmail_login'),
    path('auth/callback/', google_callback_view, name='google_callback'),
    path('logout/', logout_view, name='logout'),
    path('recommendations/', include('recommendations.urls', namespace='recommendations')),
]
