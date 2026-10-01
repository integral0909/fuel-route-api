from django.db import models


class FuelStation(models.Model):
    # The OPIS file can list the same id several times with different prices;
    # we store the lowest one and how many rows there were.
    opis_id = models.PositiveIntegerField(primary_key=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=120)
    state = models.CharField(max_length=2, db_index=True)
    rack_id = models.PositiveIntegerField(null=True, blank=True)
    price = models.DecimalField(max_digits=8, decimal_places=5)  # $/gal
    price_samples = models.PositiveSmallIntegerField(default=1)
    latitude = models.FloatField()
    longitude = models.FloatField()
    geocode_source = models.CharField(max_length=20)  # census_place / census_cousub / nominatim

    class Meta:
        ordering = ["opis_id"]

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state}) ${self.price}"
