from django.urls import path
from .views import (
    notes_list_view,
    create_recommendation_view,
    edit_recommendation_view,
    delete_recommendation_view,
    events_view,
    profile_view,
    member_directory_view,
)

app_name = 'recommendations'

urlpatterns = [
    path('', notes_list_view, name='notes_list'),
    path('<int:rec_id>/', notes_list_view, name='recommendation_detail'),
    path('create/', create_recommendation_view, name='recommendation_create'),
    path('edit/', edit_recommendation_view, name='recommendation_edit'),
    path('delete/', delete_recommendation_view, name='recommendation_delete'),
    path('events/', events_view, name='events_list'),
    path('directory/', member_directory_view, name='member_directory'),
    path('profile/', profile_view, name='profile'),
]

