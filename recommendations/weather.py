"""
Weather Forecast & Outfit Recommendation Service
Supports Dual Providers:
1. Google AI (Gemini Flash 3.7 with Google Search Grounding)
2. Standard Weather API (Nominatim Geocoding + Open-Meteo Global Forecast Models)

Features:
- 6-hour caching to minimize API reprompts and network load.
- 30-day past-event guard to prevent requerying completed events.
- Algorithmic adult & kids clothing recommendation engine.
"""

import hashlib
import logging
import os
import re
from datetime import datetime, timezone, date
import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

DEFAULT_CACHE_TIMEOUT = 60 * 60 * 6  # 6 hours in seconds
PAST_EVENT_CACHE_TIMEOUT = 60 * 60 * 24 * 30  # 30 days in seconds

WMO_WEATHER_CODES = {
    0: ("Clear Sky", "bi-sun-fill", "text-warning", "☀️"),
    1: ("Mainly Clear", "bi-cloud-sun-fill", "text-warning", "🌤️"),
    2: ("Partly Cloudy", "bi-cloud-sun", "text-primary", "⛅"),
    3: ("Overcast", "bi-clouds-fill", "text-secondary", "☁️"),
    45: ("Foggy", "bi-cloud-fog-fill", "text-secondary", "🌫️"),
    48: ("Depositing Rime Fog", "bi-cloud-fog2", "text-secondary", "🌫️"),
    51: ("Light Drizzle", "bi-cloud-drizzle", "text-info", "🌦️"),
    53: ("Moderate Drizzle", "bi-cloud-drizzle-fill", "text-info", "🌦️"),
    55: ("Dense Drizzle", "bi-cloud-rain", "text-info", "🌧️"),
    61: ("Slight Rain", "bi-cloud-rain", "text-primary", "🌧️"),
    63: ("Moderate Rain", "bi-cloud-rain-fill", "text-primary", "🌧️"),
    65: ("Heavy Rain", "bi-cloud-heavy-rain", "text-primary", "🌧️"),
    71: ("Light Snow", "bi-cloud-snow", "text-info", "❄️"),
    73: ("Moderate Snow", "bi-cloud-snow-fill", "text-info", "❄️"),
    75: ("Heavy Snow", "bi-snow", "text-info", "❄️"),
    80: ("Light Rain Showers", "bi-cloud-rain", "text-primary", "🌦️"),
    81: ("Moderate Rain Showers", "bi-cloud-rain-fill", "text-primary", "🌧️"),
    82: ("Violent Rain Showers", "bi-cloud-lightning-rain-fill", "text-primary", "⛈️"),
    95: ("Thunderstorm", "bi-cloud-lightning-fill", "text-danger", "⛈️"),
    96: ("Thunderstorm with Slight Hail", "bi-cloud-lightning-rain-fill", "text-danger", "⛈️"),
    99: ("Thunderstorm with Heavy Hail", "bi-cloud-lightning-rain-fill", "text-danger", "⛈️"),
}


def _generate_cache_key(prefix: str, location: str, datetime_str: str) -> str:
    norm = f"{str(location or '').strip().lower()}__{str(datetime_str or '').strip().lower()}"
    digest = hashlib.md5(norm.encode('utf-8')).hexdigest()
    return f"{prefix}_{digest}"


def is_date_in_past(datetime_str: str) -> bool:
    """
    Checks if the specified datetime or date string is in the past.
    Supports formats like 'YYYY-MM-DD', 'YYYY-MM-DD HH:MM', 'Sat, Aug 22, 2026', 'August 22, 2026', etc.
    """
    if not datetime_str:
        return False
    dt_str = datetime_str.strip()
    now_utc = datetime.now(timezone.utc)
    today = now_utc.date()

    # 1. Look for ISO date YYYY-MM-DD in the string
    iso_match = re.search(r'\b(20\d{2})-(\d{1,2})-(\d{1,2})\b', dt_str)
    if iso_match:
        try:
            d = date(int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3)))
            return d < today
        except ValueError:
            pass

    # 2. Try common datetime string formats
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%a, %b %d, %Y %I:%M %p",
        "%a, %b %d, %Y %H:%M",
        "%a, %b %d, %Y",
        "%A, %B %d, %Y %I:%M %p",
        "%A, %B %d, %Y",
        "%B %d, %Y %I:%M %p",
        "%B %d, %Y %H:%M",
        "%B %d, %Y",
        "%b %d, %Y %I:%M %p",
        "%b %d, %Y",
    ]

    for fmt in formats:
        try:
            parsed = datetime.strptime(dt_str, fmt)
            return parsed.date() < today
        except ValueError:
            continue

    # 3. Regex for Month Day, Year (e.g., "August 15, 2026" or "Aug 15, 2026")
    month_match = re.search(
        r'\b(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2}),?\s+(20\d{2})\b',
        dt_str,
        re.IGNORECASE
    )
    if month_match:
        try:
            mon_str = month_match.group(1)[:3].title()
            day_val = int(month_match.group(2))
            year_val = int(month_match.group(3))
            parsed = datetime.strptime(f"{mon_str} {day_val} {year_val}", "%b %d %Y")
            return parsed.date() < today
        except Exception:
            pass

    return False


def extract_target_date(datetime_str: str) -> date | None:
    """Extracts date object from string or returns None."""
    if not datetime_str:
        return None
    dt_str = datetime_str.strip()
    iso_match = re.search(r'\b(20\d{2})-(\d{1,2})-(\d{1,2})\b', dt_str)
    if iso_match:
        try:
            return date(int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3)))
        except ValueError:
            pass
    month_match = re.search(
        r'\b(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2}),?\s+(20\d{2})\b',
        dt_str,
        re.IGNORECASE
    )
    if month_match:
        try:
            mon_str = month_match.group(1)[:3].title()
            day_val = int(month_match.group(2))
            year_val = int(month_match.group(3))
            return datetime.strptime(f"{mon_str} {day_val} {year_val}", "%b %d %Y").date()
        except Exception:
            pass
    return None


def generate_rule_based_outfit(temp_max: float, rain_prob: int, wind_speed: float) -> tuple[list[str], list[str]]:
    """Generates practical clothing recommendations for adults and kids based on forecast parameters."""
    adult_tips = []
    kids_tips = []

    # Temperature based rules
    if temp_max < 55:
        adult_tips.append("Warm layers: thermal base, fleece or knit sweater, and an insulated winter coat.")
        kids_tips.append("Warm base layers, insulated jacket, warm pants, and gloves or beanie.")
    elif temp_max < 65:
        adult_tips.append("Comfortable cool-weather layers: long-sleeve shirt with a light jacket, cardigan, or hoodie.")
        kids_tips.append("Long sleeves, comfortable jeans/pants, and a warm fleece hoodie or light jacket.")
    elif temp_max < 75:
        adult_tips.append("Mild weather attire: short-sleeve t-shirt or light button-down, with a light sweater for breezy shade.")
        kids_tips.append("T-shirt, lightweight pants or shorts, and keep a light hoodie handy for coastal winds.")
    elif temp_max < 85:
        adult_tips.append("Warm and sunny: breathable cotton or linen clothing, sunglasses, and sun hat.")
        kids_tips.append("Lightweight breathable clothes, sun hat, sunglasses, and apply child-safe sunscreen.")
    else:
        adult_tips.append("Hot weather: lightweight, loose-fitting light-colored clothes, sunglasses, wide-brim hat, and plenty of water.")
        kids_tips.append("Light summer clothes, wide-brim sun hat, UV sunglasses, sunscreen, and bring extra water.")

    # Rain rules
    if rain_prob >= 50:
        adult_tips.append(f"High chance of rain ({rain_prob}%): pack a compact umbrella, waterproof jacket, and water-resistant shoes.")
        kids_tips.append("Waterproof rain boots, hooded raincoat or poncho, and spare dry socks.")
    elif rain_prob >= 25:
        adult_tips.append(f"Possibility of light showers ({rain_prob}%): keep a travel umbrella or water-resistant layer nearby.")
        kids_tips.append("Bring a light water-resistant hooded jacket just in case.")

    # Wind rules
    if wind_speed >= 15:
        adult_tips.append(f"Breezy conditions ({wind_speed:.0f} mph): a windbreaker jacket will keep you comfortable.")
        kids_tips.append("Windbreaker and secure fitting hats/caps.")

    return adult_tips, kids_tips


def get_openmeteo_weather_forecast(location: str, datetime_str: str, force_refresh: bool = False) -> dict:
    """
    Standard Weather API Implementation using:
    - OpenStreetMap Nominatim for geocoding.
    - Open-Meteo for hourly & daily meteorological forecast models.
    - Algorithmic family & kids clothing recommendation engine.
    """
    loc_clean = str(location or '').strip()
    dt_clean = str(datetime_str or '').strip()

    if not loc_clean:
        return {
            "available": False,
            "provider": "standard",
            "status": "missing_location",
            "message": "Location is required for weather forecast."
        }

    cache_key = _generate_cache_key("openmeteo_weather_v1", loc_clean, dt_clean)

    if not force_refresh:
        cached_data = cache.get(cache_key)
        if cached_data and isinstance(cached_data, dict):
            cached_data["cached"] = True
            return cached_data

    # Guard: past events
    if is_date_in_past(dt_clean):
        cached_data = cache.get(cache_key)
        if cached_data and isinstance(cached_data, dict) and cached_data.get("available"):
            cached_data["cached"] = True
            return cached_data

        past_result = {
            "available": False,
            "provider": "standard",
            "status": "past_event",
            "message": "Weather forecast is not available for past events.",
            "location": loc_clean,
            "datetime_str": dt_clean,
            "cached": False,
        }
        cache.set(cache_key, past_result, PAST_EVENT_CACHE_TIMEOUT)
        return past_result

    try:
        # Step 1: Geocoding via Nominatim with Open-Meteo fallback
        lat, lon, display_name = None, None, loc_clean
        nom_url = "https://nominatim.openstreetmap.org/search"
        headers = {"User-Agent": "MiniMexitasApp/1.0 (info@minimexitas.org)"}
        nom_resp = requests.get(
            nom_url,
            params={"q": loc_clean, "format": "json", "limit": 1},
            headers=headers,
            timeout=6
        )
        nom_data = nom_resp.json()
        if nom_data and len(nom_data) > 0:
            lat = float(nom_data[0]["lat"])
            lon = float(nom_data[0]["lon"])
            display_name = nom_data[0].get("display_name", loc_clean)
        else:
            # Fallback geocoder: Open-Meteo Geocoding
            om_geo_url = "https://geocoding-api.open-meteo.com/v1/search"
            # Try full name or first part before comma
            for q in [loc_clean, loc_clean.split(',')[0].strip()]:
                if not q:
                    continue
                om_geo_resp = requests.get(
                    om_geo_url,
                    params={"name": q, "count": 1, "language": "en", "format": "json"},
                    timeout=5
                )
                om_geo_data = om_geo_resp.json()
                if om_geo_data.get("results"):
                    first = om_geo_data["results"][0]
                    lat = float(first["latitude"])
                    lon = float(first["longitude"])
                    display_name = f"{first.get('name', loc_clean)}, {first.get('admin1', '')}".strip(', ')
                    break

        if lat is None or lon is None:
            return {
                "available": False,
                "provider": "standard",
                "status": "geocode_failed",
                "message": f"Could not find coordinates for location '{loc_clean}'."
            }

        # Step 2: Query Open-Meteo Forecast
        fc_url = "https://api.open-meteo.com/v1/forecast"
        fc_params = {
            "latitude": lat,
            "longitude": lon,
            "daily": "weathercode,temperature_2m_max,temperature_2m_min,precipitation_probability_max,windspeed_10m_max",
            "current_weather": "true",
            "temperature_unit": "fahrenheit",
            "windspeed_unit": "mph",
            "precipitation_unit": "inch",
            "timezone": "auto"
        }
        fc_resp = requests.get(fc_url, params=fc_params, timeout=6)
        fc_data = fc_resp.json()

        daily = fc_data.get("daily", {})
        current = fc_data.get("current_weather", {})
        daily_times = daily.get("time", [])

        # Match specific target date if possible
        target_date = extract_target_date(dt_clean)
        day_idx = 0
        if target_date and daily_times:
            target_iso = target_date.isoformat()
            if target_iso in daily_times:
                day_idx = daily_times.index(target_iso)

        temp_max = daily.get("temperature_2m_max", [70])[day_idx]
        temp_min = daily.get("temperature_2m_min", [55])[day_idx]
        weather_code = daily.get("weathercode", [current.get("weathercode", 0)])[day_idx]
        rain_prob = daily.get("precipitation_probability_max", [0])[day_idx] or 0
        wind_speed = daily.get("windspeed_10m_max", [current.get("windspeed", 8)])[day_idx]
        temp_current = current.get("temperature")

        # Interpretation
        condition_tuple = WMO_WEATHER_CODES.get(weather_code, ("Clear / Sunny", "bi-sun-fill", "text-warning", "☀️"))
        condition_name, condition_icon, condition_color, condition_emoji = condition_tuple

        # Recommendations
        outfit_adults, outfit_kids = generate_rule_based_outfit(temp_max, rain_prob, wind_speed)

        # Markdown formatted text
        short_loc = display_name.split(',')[0]
        md_lines = [
            f"### {condition_emoji} Weather for {short_loc}",
            f"**Expected Conditions**: {condition_name} with temperatures between **{round(temp_min)}°F** and **{round(temp_max)}°F**" + (f" (currently **{round(temp_current)}°F**)" if temp_current is not None else "") + ".",
            f"- 🌧️ **Precipitation**: {rain_prob}% chance of rain",
            f"- 💨 **Wind**: up to {round(wind_speed)} mph",
            "",
            "### 👔 Recommended Attire & Family Tips",
            "**For Adults**:",
        ]
        for tip in outfit_adults:
            md_lines.append(f"- {tip}")
        md_lines.append("")
        md_lines.append("**For Kids**:")
        for tip in outfit_kids:
            md_lines.append(f"- {tip}")

        result = {
            "available": True,
            "provider": "standard",
            "status": "success",
            "location": loc_clean,
            "display_location": display_name,
            "datetime_str": dt_clean,
            "temp_current": round(temp_current, 1) if temp_current is not None else round(temp_max, 1),
            "temp_max": round(temp_max, 1),
            "temp_min": round(temp_min, 1),
            "condition": condition_name,
            "icon": condition_icon,
            "color_class": condition_color,
            "emoji": condition_emoji,
            "rain_chance": int(rain_prob),
            "wind_speed": round(wind_speed, 1),
            "outfit_adults": outfit_adults,
            "outfit_kids": outfit_kids,
            "recommendation": "\n".join(md_lines),
            "cached": False,
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "sources": [{"title": "Open-Meteo & NOAA National Forecasts", "url": "https://open-meteo.com"}],
        }

        cache.set(cache_key, result, DEFAULT_CACHE_TIMEOUT)
        return result

    except Exception as e:
        logger.error(f"Error querying Open-Meteo standard weather for {loc_clean} {dt_clean}: {e}", exc_info=True)
        return {
            "available": False,
            "provider": "standard",
            "status": "error",
            "message": f"Standard weather lookup failed: {str(e)}"
        }


def get_gemini_weather_forecast(location: str, datetime_str: str, force_refresh: bool = False) -> dict:
    """
    Queries Gemini Flash with Google Search Grounding for real-time weather and outfit recommendations.
    Results are cached for 6 hours by default.
    Automatically prevents querying Gemini for past events.
    """
    loc_clean = str(location or '').strip()
    dt_clean = str(datetime_str or '').strip()

    if not loc_clean:
        return {
            "available": False,
            "provider": "gemini",
            "status": "missing_location",
            "message": "Location is required for weather forecast."
        }

    cache_key = _generate_cache_key("gemini_weather_v1", loc_clean, dt_clean)

    # Check cache first
    if not force_refresh:
        cached_data = cache.get(cache_key)
        if cached_data and isinstance(cached_data, dict):
            cached_data["cached"] = True
            return cached_data

    # Guard: If event is in the past, do not query Gemini
    if is_date_in_past(dt_clean):
        cached_data = cache.get(cache_key)
        if cached_data and isinstance(cached_data, dict) and cached_data.get("available"):
            cached_data["cached"] = True
            return cached_data

        past_result = {
            "available": False,
            "provider": "gemini",
            "status": "past_event",
            "message": "Weather forecast is not available for past events.",
            "location": loc_clean,
            "datetime_str": dt_clean,
            "cached": False,
        }
        cache.set(cache_key, past_result, PAST_EVENT_CACHE_TIMEOUT)
        return past_result

    model_name = getattr(settings, "GEMINI_MODEL", "gemini-3.7-flash").strip() or "gemini-3.7-flash"

    try:
        from google import genai
        from google.genai import types

        client = genai.Client()

        prompt = (
            f"Get the weather forecast for {loc_clean} and {dt_clean} and give me a recommendation "
            f"of what to wear for me and kids. "
            f"Provide the expected temperature range, general conditions (sunny, rainy, foggy, windy), "
            f"and practical clothing/accessory tips for adults and kids in friendly, concise markdown bullet points."
        )

        config = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.3,
        )

        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=config,
        )

        text_content = ""
        if hasattr(response, "text") and response.text:
            text_content = response.text
        elif hasattr(response, "candidates") and response.candidates:
            cand = response.candidates[0]
            if hasattr(cand, "content") and cand.content and cand.content.parts:
                text_content = "".join([p.text for p in cand.content.parts if hasattr(p, "text") and p.text])

        if not text_content:
            return {
                "available": False,
                "provider": "gemini",
                "status": "empty_response",
                "message": "Unable to retrieve weather forecast at this time."
            }

        # Extract search grounding sources if available
        search_queries = []
        sources = []
        try:
            if hasattr(response, "candidates") and response.candidates:
                cand = response.candidates[0]
                metadata = getattr(cand, "grounding_metadata", None)
                if metadata:
                    if getattr(metadata, "web_search_queries", None):
                        search_queries = list(metadata.web_search_queries)
                    grounding_chunks = getattr(metadata, "grounding_chunks", None)
                    if grounding_chunks:
                        for chunk in grounding_chunks:
                            web = getattr(chunk, "web", None)
                            if web:
                                title = getattr(web, "title", "") or "Source"
                                uri = getattr(web, "uri", "")
                                if uri:
                                    sources.append({"title": title, "url": uri})
        except Exception as meta_err:
            logger.debug(f"Error parsing grounding metadata: {meta_err}")

        result = {
            "available": True,
            "provider": "gemini",
            "status": "success",
            "location": loc_clean,
            "datetime_str": dt_clean,
            "recommendation": text_content,
            "model": model_name,
            "cached": False,
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "search_queries": search_queries,
            "sources": sources[:4],  # Top 4 sources
        }

        # Store in cache
        cache.set(cache_key, result, DEFAULT_CACHE_TIMEOUT)
        return result

    except Exception as e:
        logger.error(f"Error querying Gemini Weather API for {loc_clean} {dt_clean}: {e}", exc_info=True)
        return {
            "available": False,
            "provider": "gemini",
            "status": "error",
            "message": f"Weather lookup failed: {str(e)}"
        }


def get_event_weather_forecast(
    location: str,
    datetime_str: str,
    force_refresh: bool = False,
    provider: str = "both"
) -> dict:
    """
    Unified entry point for event weather forecast.
    Supports provider="gemini", provider="standard", or provider="both" (default).
    """
    loc_clean = str(location or '').strip()
    dt_clean = str(datetime_str or '').strip()

    if not loc_clean:
        return {
            "available": False,
            "status": "missing_location",
            "message": "Location is required for weather forecast."
        }

    prov = str(provider or "both").strip().lower()

    if prov == "gemini":
        return get_gemini_weather_forecast(loc_clean, dt_clean, force_refresh=force_refresh)
    elif prov in ["standard", "openmeteo"]:
        return get_openmeteo_weather_forecast(loc_clean, dt_clean, force_refresh=force_refresh)
    else:
        # provider == "both"
        gemini_res = get_gemini_weather_forecast(loc_clean, dt_clean, force_refresh=force_refresh)
        standard_res = get_openmeteo_weather_forecast(loc_clean, dt_clean, force_refresh=force_refresh)

        available = gemini_res.get("available", False) or standard_res.get("available", False)
        status = "success" if available else (gemini_res.get("status") or standard_res.get("status") or "unavailable")
        message = gemini_res.get("message") or standard_res.get("message") or ""

        return {
            "available": available,
            "status": status,
            "message": message,
            "location": loc_clean,
            "datetime_str": dt_clean,
            "gemini": gemini_res,
            "standard": standard_res,
            # Fallback top-level properties for existing consumers
            "recommendation": gemini_res.get("recommendation") if gemini_res.get("available") else standard_res.get("recommendation"),
            "sources": gemini_res.get("sources") if gemini_res.get("available") else standard_res.get("sources"),
            "cached": gemini_res.get("cached", False) and standard_res.get("cached", False),
            "updated_at": gemini_res.get("updated_at") or standard_res.get("updated_at"),
        }
