import io

import qrcode
from django.http import HttpResponse
from django.shortcuts import render


def optical_control_view(request):
    session_id = request.GET.get("session")
    return render(request, "core/optical_control.html", {"session_id": session_id})


def optical_station_view(request):
    session_id = request.GET.get("session")
    return render(request, "core/optical_station.html", {"session_id": session_id})


def optical_station_qr_view(request, session_id):
    join_url = request.build_absolute_uri(f"/optical/station?session={session_id}")
    img = qrcode.make(join_url)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return HttpResponse(buffer.getvalue(), content_type="image/png")
