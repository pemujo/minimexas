from django.urls import path
from .views import notes_list_view

app_name = 'recommendations'  # <--- THIS NAMESPACE MUST BE HERE

urlpatterns = [
    path('', notes_list_view, name='notes_list'),
]
