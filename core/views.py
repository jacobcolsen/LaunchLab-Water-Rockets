import io

import qrcode
from django.db import connection, DatabaseError
from django.http import HttpResponse, JsonResponse
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


def station_qr_view(request, session_id):
    join_url = request.build_absolute_uri(f"/station?session={session_id}")
    img = qrcode.make(join_url)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return HttpResponse(buffer.getvalue(), content_type="image/png")
