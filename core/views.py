from django.db import connection, DatabaseError
from django.http import JsonResponse
from django.shortcuts import render


def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except DatabaseError:
        return JsonResponse({"status": "error", "db": "unreachable"}, status=503)

    return JsonResponse({"status": "ok", "db": "ok"})


def control_view(request):
    session_id = request.GET.get("session")
    return render(request, "core/control.html", {"session_id": session_id})


def station_view(request):
    session_id = request.GET.get("session")
    return render(request, "core/station.html", {"session_id": session_id})
