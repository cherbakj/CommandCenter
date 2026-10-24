"""Current weather by zip code. zippopotam.us turns the zip into coordinates and Open-Meteo
supplies the forecast. Both are free and need no key."""
import json
import re
import time
import urllib.request
from datetime import datetime

from .. import config, db

CACHE_SECONDS = 600
_cache: dict = {}

# WMO weather codes -> (description, icon)
WMO = {
    0: ("Clear", "☀️"), 1: ("Mostly clear", "🌤️"), 2: ("Partly cloudy", "⛅"), 3: ("Overcast", "☁️"),
    45: ("Fog", "🌫️"), 48: ("Freezing fog", "🌫️"),
    51: ("Light drizzle", "🌦️"), 53: ("Drizzle", "🌦️"), 55: ("Heavy drizzle", "🌧️"),
    56: ("Freezing drizzle", "🌧️"), 57: ("Freezing drizzle", "🌧️"),
    61: ("Light rain", "🌦️"), 63: ("Rain", "🌧️"), 65: ("Heavy rain", "🌧️"),
    66: ("Freezing rain", "🌧️"), 67: ("Freezing rain", "🌧️"),
    71: ("Light snow", "🌨️"), 73: ("Snow", "🌨️"), 75: ("Heavy snow", "❄️"), 77: ("Snow grains", "🌨️"),
    80: ("Light showers", "🌦️"), 81: ("Showers", "🌧️"), 82: ("Heavy showers", "⛈️"),
    85: ("Snow showers", "🌨️"), 86: ("Snow showers", "❄️"),
    95: ("Thunderstorm", "⛈️"), 96: ("Thunderstorm, hail", "⛈️"), 99: ("Thunderstorm, hail", "⛈️"),
}


def _get(url) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "CommandCenter/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def _describe(code, is_day=1):
    text, icon = WMO.get(code, ("Unknown", "🌡️"))
    if not is_day and code in (0, 1):
        icon = "🌙"
    return text, icon


def _location(zip_code, country) -> dict:
    key = f"weather:loc:{country}:{zip_code}"
    loc = db.kv_get(key)
    if not loc:
        data = _get(f"https://api.zippopotam.us/{country}/{zip_code}")
        place = data["places"][0]
        loc = {"lat": float(place["latitude"]), "lon": float(place["longitude"]),
               "name": f"{place['place name']}, {place.get('state abbreviation', '')}".rstrip(", ")}
        db.kv_set(key, loc)
    return loc


def locations() -> list[dict]:
    """The places the weather panel can switch between: today.zip first, then today.weather_locations."""
    cfg = config.section("today", {})
    home = str(cfg.get("zip", "")).strip()
    known = db.kv_get(f"weather:loc:{cfg.get('country', 'us')}:{home}") or {}
    places = [{"name": known.get("name", "Home").split(",")[0], "zip": home}] if home else []
    for p in cfg.get("weather_locations") or []:
        z = str(p.get("zip", "")).strip()
        if re.fullmatch(r"\d{5}", z) and z not in [x["zip"] for x in places]:
            places.append({"name": str(p.get("name") or z)[:30], "zip": z})
    return places


def selected_zip() -> str:
    zips = [p["zip"] for p in locations()]
    chosen = db.kv_get("weather:selected")
    return chosen if chosen in zips else (zips[0] if zips else "")


def select(zip_code: str):
    if zip_code not in [p["zip"] for p in locations()]:
        raise ValueError("Pick one of the configured weather locations")
    db.kv_set("weather:selected", zip_code)


def current(zip_code: str | None = None) -> dict:
    cfg = config.section("today", {})
    zip_code = zip_code or selected_zip()
    if not zip_code:
        raise ValueError("Set today.zip in config.yaml")
    country = cfg.get("country", "us")
    units = cfg.get("units", "fahrenheit")
    cache_key = (zip_code, country, units)
    cached = _cache.get(cache_key)
    if cached and time.time() - cached["at"] < CACHE_SECONDS:
        return cached["data"]

    loc = _location(zip_code, country)
    wind = "mph" if units == "fahrenheit" else "kmh"
    f = _get(
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={loc['lat']}&longitude={loc['lon']}"
        "&current=temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,is_day"
        "&hourly=temperature_2m,weather_code,precipitation_probability,is_day"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset"
        f"&temperature_unit={units}&wind_speed_unit={wind}&timezone=auto&forecast_days=2"
    )
    cur, daily, hourly = f["current"], f["daily"], f["hourly"]
    text, icon = _describe(cur["weather_code"], cur["is_day"])
    this_hour = datetime.now().strftime("%Y-%m-%dT%H:00")
    start = hourly["time"].index(this_hour) if this_hour in hourly["time"] else 0
    hours = []
    for i in range(start + 1, min(start + 7, len(hourly["time"]))):
        h_text, h_icon = _describe(hourly["weather_code"][i], hourly["is_day"][i])
        hours.append({"time": hourly["time"][i], "temp": round(hourly["temperature_2m"][i]),
                      "icon": h_icon, "text": h_text, "rain": hourly["precipitation_probability"][i]})
    data = {
        "location": loc["name"], "zip": zip_code,
        "unit": "°F" if units == "fahrenheit" else "°C", "wind_unit": wind,
        "temp": round(cur["temperature_2m"]), "feels_like": round(cur["apparent_temperature"]),
        "humidity": cur["relative_humidity_2m"], "wind": round(cur["wind_speed_10m"]),
        "text": text, "icon": icon,
        "high": round(daily["temperature_2m_max"][0]), "low": round(daily["temperature_2m_min"][0]),
        "rain_chance": daily["precipitation_probability_max"][0],
        "sunrise": daily["sunrise"][0], "sunset": daily["sunset"][0],
        "hours": hours,
        "locations": locations(), "selected": zip_code,
    }
    _cache[cache_key] = {"at": time.time(), "data": data}
    return data
