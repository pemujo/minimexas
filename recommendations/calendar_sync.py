import datetime
import logging
import os
from urllib.parse import quote_plus
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

CACHE_KEY_EVENTS = "community_events_cache"
CACHE_TIMEOUT_EVENTS = 120  # 2 minutes for timely sync with Google Calendar



def get_google_calendar_add_url(title, start_dt, end_dt, description="", location=""):
    """
    Builds a direct 'Add to Google Calendar' template link.
    Format: https://calendar.google.com/calendar/render?action=TEMPLATE&text=...&dates=...&details=...&location=...
    """
    def _format_dt(dt):
        if isinstance(dt, datetime.datetime):
            return dt.strftime("%Y%m%dT%H%M%SZ")
        elif isinstance(dt, datetime.date):
            return dt.strftime("%Y%m%d")
        return ""

    dates_str = f"{_format_dt(start_dt)}/{_format_dt(end_dt)}"
    base_url = "https://calendar.google.com/calendar/render?action=TEMPLATE"
    params = [
        f"text={quote_plus(str(title))}",
        f"dates={dates_str}",
        f"details={quote_plus(str(description))}",
        f"location={quote_plus(str(location))}",
    ]
    return f"{base_url}&{'&'.join(params)}"


def _parse_event_datetime(dt_str, default_date=None):
    """
    Parses various datetime and date formats into datetime object.
    """
    if not dt_str:
        return default_date or datetime.datetime.now()
    
    # ISO 8601 strings (e.g., 2026-09-15T14:00:00Z or 2026-09-15T14:00:00-07:00)
    try:
        clean_str = dt_str.replace("Z", "+00:00")
        return datetime.datetime.fromisoformat(clean_str)
    except Exception:
        pass

    # YYYY-MM-DD format
    try:
        d = datetime.date.fromisoformat(dt_str[:10])
        return datetime.datetime.combine(d, datetime.time(12, 0))
    except Exception:
        pass

    return default_date or datetime.datetime.now()


def _fetch_from_google_calendar_api(calendar_id, sa_info):
    """
    Fetches upcoming events via Google Calendar REST API v3 using Service Account Credentials.
    """
    try:
        import requests
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request

        scopes = [
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/calendar.events.readonly"
        ]
        credentials = service_account.Credentials.from_service_account_info(sa_info, scopes=scopes)
        credentials.refresh(Request())
        token = credentials.token

        # Fetch from up to 1 year in the past to allow viewing past events
        past_window = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=365)
        api_url = f"https://www.googleapis.com/calendar/v3/calendars/{quote_plus(calendar_id)}/events"
        headers = {"Authorization": f"Bearer {token}"}
        params = {
            "singleEvents": "true",
            "orderBy": "startTime",
            "timeMin": past_window.isoformat(),
            "maxResults": 250,
        }

        response = requests.get(api_url, headers=headers, params=params, timeout=10)
        if response.status_code != 200:
            logger.warning(f"Google Calendar API returned status {response.status_code}: {response.text}")
            return []

        data = response.json()
        items = data.get("items", [])
        events = []
        now_utc = datetime.datetime.now(datetime.timezone.utc)

        for idx, item in enumerate(items):
            start_raw = item.get("start", {}).get("dateTime") or item.get("start", {}).get("date")
            end_raw = item.get("end", {}).get("dateTime") or item.get("end", {}).get("date")
            start_dt = _parse_event_datetime(start_raw)
            end_dt = _parse_event_datetime(end_raw, default_date=start_dt + datetime.timedelta(hours=2))

            summary = item.get("summary", "Community Event")
            location = item.get("location", "San Francisco Bay Area")
            description = item.get("description", "")
            
            # Extract category or use default
            category = "Community Gathering"
            if any(k in summary.lower() for k in ["tech", "network", "mixer", "career"]):
                category = "Networking & Tech"
            elif any(k in summary.lower() for k in ["festival", "grito", "independencia", "muertos", "cultural"]):
                category = "Cultural & Heritage"
            elif any(k in summary.lower() for k in ["picnic", "soccer", "park", "family", "kids"]):
                category = "Family & Outdoors"
            elif any(k in summary.lower() for k in ["taco", "dinner", "food", "brunch", "drinks"]):
                category = "Culinary & Social"

            # Check if event has ended
            is_past = False
            if hasattr(end_dt, 'tzinfo') and end_dt.tzinfo is not None:
                is_past = end_dt < now_utc
            elif isinstance(end_dt, datetime.datetime):
                is_past = end_dt < datetime.datetime.now()

            events.append({
                "id": item.get("id", f"gcal_{idx}"),
                "title": summary,
                "start_datetime": start_dt,
                "end_datetime": end_dt,
                "date_formatted": start_dt.strftime("%A, %B %d, %Y"),
                "time_formatted": f"{start_dt.strftime('%I:%M %p').lstrip('0')} - {end_dt.strftime('%I:%M %p').lstrip('0')}",
                "location": location,
                "location_url": f"https://www.google.com/maps/search/?api=1&query={quote_plus(location)}",
                "category": category,
                "description": description,
                "organizer": item.get("organizer", {}).get("displayName") or "MiniMexitas Events",
                "google_calendar_link": get_google_calendar_add_url(summary, start_dt, end_dt, description, location),
                "year": start_dt.year,
                "month": start_dt.month,
                "day": start_dt.day,
                "is_past": is_past,
            })

        return events
    except Exception as e:
        logger.error(f"Error fetching Google Calendar API events: {e}", exc_info=True)
        return []


def fetch_community_events(force_refresh=False):
    """
    Fetches community events directly from Google Calendar API.
    Results are cached for 15 minutes.
    """
    if not force_refresh:
        cached = cache.get(CACHE_KEY_EVENTS)
        if cached is not None:
            return cached

    events = []
    cal_id = os.environ.get("GOOGLE_CALENDAR_ID")
    sa_info = getattr(settings, "GOOGLE_SERVICE_ACCOUNT_INFO", None)

    if cal_id and sa_info:
        logger.info(f"Syncing events directly from Google Calendar: {cal_id}")
        events = _fetch_from_google_calendar_api(cal_id, sa_info) or []
    else:
        logger.info("GOOGLE_CALENDAR_ID not set or Service Account not configured; returning empty event list.")
        events = []

    cache.set(CACHE_KEY_EVENTS, events, CACHE_TIMEOUT_EVENTS)
    return events
