import secrets

from django.db import models


def generate_device_token():
    return secrets.token_urlsafe(24)


class Session(models.Model):
    name = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Station(models.Model):
    session = models.ForeignKey(Session, related_name="stations", on_delete=models.CASCADE)
    label = models.CharField(max_length=100)
    device_token = models.CharField(max_length=64, unique=True, default=generate_device_token)
    distance_ft = models.FloatField()
    bearing_degrees = models.FloatField()
    ready = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.label} ({self.session.name})"


class Launch(models.Model):
    STATUS_PENDING = "pending"
    STATUS_LAUNCHED = "launched"
    STATUS_LANDED = "landed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_LAUNCHED, "Launched"),
        (STATUS_LANDED, "Landed"),
    ]

    session = models.ForeignKey(Session, related_name="launches", on_delete=models.CASCADE)
    number = models.PositiveIntegerField(blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    launched_at = models.DateTimeField(null=True, blank=True)
    landed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("session", "number")
        ordering = ["session", "number"]

    def save(self, *args, **kwargs):
        if self.number is None:
            self.number = self.session.launches.count() + 1
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.session.name} — Launch {self.number}"


class Sample(models.Model):
    launch = models.ForeignKey(Launch, related_name="samples", on_delete=models.CASCADE)
    station = models.ForeignKey(Station, related_name="samples", on_delete=models.CASCADE)
    data = models.JSONField(help_text="List of {t, elevation, azimuth} readings")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("launch", "station")

    def __str__(self):
        return f"Sample: {self.station.label} / {self.launch}"


class Result(models.Model):
    METHOD_SINGLE = "single_station"
    METHOD_MULTI = "multi_station"
    METHOD_CHOICES = [
        (METHOD_SINGLE, "Single station"),
        (METHOD_MULTI, "Multi station"),
    ]

    launch = models.OneToOneField(Launch, related_name="result", on_delete=models.CASCADE)
    best_altitude_ft = models.FloatField(null=True, blank=True)
    method = models.CharField(max_length=20, choices=METHOD_CHOICES, blank=True)
    station_breakdown = models.JSONField(
        null=True, blank=True, help_text="Per-station individual altitude estimates"
    )
    computed_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Result: {self.launch} = {self.best_altitude_ft} ft"
