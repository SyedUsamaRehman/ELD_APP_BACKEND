from django.urls import path
from . import views

urlpatterns = [
    path('calculate-trip/', views.calculate_trip_view, name='calculate_trip'),
    path('health/', views.health_check, name='health_check'),
]
