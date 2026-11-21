from django.http import JsonResponse
from django.urls import include, path


def private(request):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "请先登录。"}, status=401)
    return JsonResponse({"ok": True})


urlpatterns = [path("api/auth/", include("accounts.urls")), path("api/private", private)]
