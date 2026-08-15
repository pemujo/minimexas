from django.conf import settings
from django.contrib import admin
from django.urls import path, include
from recommendations.views import (
    home_view, 
    login_page_view, 
    gmail_login_view, 
    google_callback_view, 
    dev_login_view,
    logout_view,
    events_view
)

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', home_view, name='home'),
    path('events/', events_view, name='events'),
    path('login/', login_page_view, name='login_page'),
    path('auth/google/', gmail_login_view, name='gmail_login'),
    path('auth/callback/', google_callback_view, name='google_callback'),
    path('logout/', logout_view, name='logout'),
    path('recommendations/', include('recommendations.urls', namespace='recommendations')),
]

# Only enable the developer bypass route during local debugging
if settings.DEBUG:
    urlpatterns.append(path('auth/dev-login/', dev_login_view, name='dev_login'))
