from django.urls import include, path
from django.http import JsonResponse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.generic import TemplateView
from operations.version import APP_VERSION

urlpatterns = [
    path('health/', lambda request: JsonResponse({'application': 'roundtable-internal', 'version': APP_VERSION})),
    path('', ensure_csrf_cookie(TemplateView.as_view(template_name='app.html')), name='home'),
    path('api/auth/', include('accounts.urls')),
    path('api/', include('workspace.urls')),
    path('api/', include('delivery.urls')),
]
