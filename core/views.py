from django.db import connection, DatabaseError
from django.http import JsonResponse


def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except DatabaseError:
        return JsonResponse({"status": "error", "db": "unreachable"}, status=503)

    return JsonResponse({"status": "ok", "db": "ok"})
