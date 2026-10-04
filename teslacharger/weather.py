"""Previsione del sole e della produzione dei pannelli, con i dati gratuiti di Open-Meteo.

Open-Meteo fornisce la radiazione solare prevista ora per ora, che è il dato da cui
dipende la produzione. La previsione viene tarata sull'impianto confrontando, per i
giorni passati, la radiazione con quello che i pannelli hanno prodotto davvero.
"""

from datetime import date, datetime

from .config import HOME_PLAN
from .http import request_json

URL = "https://api.open-meteo.com/v1/forecast"
PAST_DAYS = 14
FORECAST_DAYS = 3
MJ_TO_KWH = 1 / 3.6

# Codici meteo WMO raggruppati: descrizione e icona
CONDITIONS = (
    ((0,), "Sereno", "sunny"),
    ((1, 2), "Poco nuvoloso", "partly_cloudy_day"),
    ((3,), "Coperto", "cloud"),
    ((45, 48), "Nebbia", "foggy"),
    ((51, 53, 55, 56, 57), "Pioviggine", "rainy"),
    ((61, 63, 65, 66, 67, 80, 81, 82), "Pioggia", "rainy"),
    ((71, 73, 75, 77, 85, 86), "Neve", "weather_snowy"),
    ((95, 96, 99), "Temporale", "thunderstorm"),
)


def condition(code) -> tuple[str, str]:
    for codes, text, icon in CONDITIONS:
        if code in codes:
            return text, icon
    return "Variabile", "partly_cloudy_day"


def fetch(latitude: float, longitude: float) -> dict:
    return request_json(
        URL,
        params={
            "latitude": latitude,
            "longitude": longitude,
            "hourly": "shortwave_radiation",
            "daily": "shortwave_radiation_sum,sunshine_duration,weather_code,temperature_2m_max,temperature_2m_min",
            "past_days": PAST_DAYS,
            "forecast_days": FORECAST_DAYS,
            "timezone": "Europe/Rome",
        },
    )


def yield_factor(data: dict, produced: dict[str, float]) -> float | None:
    """kWh prodotti dall'impianto per ogni kWh/m² di radiazione, dai giorni passati."""
    daily = data["daily"]
    radiation = pv = 0.0
    for day, mj in zip(daily["time"], daily["shortwave_radiation_sum"]):
        if day in produced and mj and day < date.today().isoformat():
            radiation += mj * MJ_TO_KWH
            pv += produced[day]
    return pv / radiation if radiation > 5 else None


def forecast(data: dict, factor: float, settings, now: datetime | None = None) -> list[dict]:
    """Per oggi e i giorni seguenti: meteo, produzione prevista ed energia utile per l'auto."""
    now = now or datetime.now()
    today = now.date().isoformat()
    hourly = {}
    for stamp, watts in zip(data["hourly"]["time"], data["hourly"]["shortwave_radiation"]):
        hourly.setdefault(stamp[:10], []).append((int(stamp[11:13]), watts or 0))
    minimum_w = settings.min_amps * 230
    maximum_w = settings.max_amps * 230
    days = []
    daily = data["daily"]
    for i, day in enumerate(daily["time"]):
        if day < today:
            continue
        car_kwh, home_kwh, first, last = 0.0, 0.0, None, None
        home_hours = HOME_PLAN.get(date.fromisoformat(day).weekday(), ())
        for hour, watts in hourly.get(day, []):
            # Potenza media dei pannelli nell'ora, e quota destinata all'auto
            share = factor * watts * settings.pv_share / 100
            in_window = settings.day_start.hour <= hour < settings.day_end.hour
            if in_window and share >= minimum_w:
                car_kwh += min(share, maximum_w) / 1000
                if any(start <= hour < end for start, end in home_hours):
                    home_kwh += min(share, maximum_w) / 1000
                first = hour if first is None else first
                last = hour
        text, icon = condition(daily["weather_code"][i])
        days.append({
            "day": day,
            "text": text,
            "icon": icon,
            "temp_min": daily["temperature_2m_min"][i],
            "temp_max": daily["temperature_2m_max"][i],
            "sun_hours": round((daily["sunshine_duration"][i] or 0) / 3600, 1),
            "pv_kwh": round(factor * (daily["shortwave_radiation_sum"][i] or 0) * MJ_TO_KWH, 1),
            "car_kwh": round(car_kwh, 1),
            # Energia per l'auto nelle sole ore in cui di solito è a casa
            "home_kwh": round(home_kwh, 1),
            "home_day": bool(home_hours),
            "car_from": f"{first:02d}:00" if first is not None else None,
            "car_to": f"{last + 1:02d}:00" if last is not None else None,
        })
    return days
