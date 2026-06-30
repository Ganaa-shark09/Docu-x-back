from django.urls import path

from apps.vector_search.views import VectorSearchAPIView

urlpatterns = [
    path("search/", VectorSearchAPIView.as_view(), name="vector-search"),
]
