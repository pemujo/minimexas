from django.urls import path
from .views import notes_list_view, events_view, profile_view, member_directory_view

app_name = 'recommendations'

urlpatterns = [
    path('', notes_list_view, name='notes_list'),
    path('events/', events_view, name='events_list'),
    path('directory/', member_directory_view, name='member_directory'),
    path('profile/', profile_view, name='profile'),
]

