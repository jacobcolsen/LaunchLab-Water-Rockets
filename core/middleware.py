from django.http import HttpResponse

from .basic_auth import check_basic_auth


class BasicAuthMiddleware:
    """Site-wide password gate - every view in this app is otherwise
    unauthenticated by design (see core/optical_api.py), so this is what
    stands between the open internet and flight data once deployed
    somewhere public. No-op when SITE_BASIC_AUTH_USER isn't set."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if check_basic_auth(request.META.get('HTTP_AUTHORIZATION')):
            return self.get_response(request)
        response = HttpResponse('Authentication required', status=401)
        response['WWW-Authenticate'] = 'Basic realm="LaunchLab"'
        return response
