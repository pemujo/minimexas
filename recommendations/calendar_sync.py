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
            elif any(k in summary.lower() for k in ["mami", "mamis", "mama", "mamas", "madre", "madres"]):
                category = "Salidas solo mamis"
            elif any(k in summary.lower() for k in ["papi", "papis", "papa", "papas", "padre", "padres"]):
                category = "Salidas solo papis"

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
                "source": "google_calendar",
                "is_portal_event": False,
                "portal_event_pk": None,
            })

        return events
    except Exception as e:
        logger.error(f"Error fetching Google Calendar API events: {e}", exc_info=True)
        return []


def _fetch_from_database():
    """
    Fetches active community events created directly in the portal database.
    """
    try:
        from .models import CommunityEvent
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        events = []
        db_events = list(CommunityEvent.objects.filter(is_active=True).order_by('start_datetime'))

        for evt in db_events:
            start_dt = evt.start_datetime
            end_dt = evt.end_datetime or (start_dt + datetime.timedelta(hours=2))

            is_past = False
            if hasattr(end_dt, 'tzinfo') and end_dt.tzinfo is not None:
                is_past = end_dt < now_utc
            elif isinstance(end_dt, datetime.datetime):
                is_past = end_dt < datetime.datetime.now()

            # Time formatted
            if evt.end_datetime:
                time_fmt = f"{start_dt.strftime('%I:%M %p').lstrip('0')} - {end_dt.strftime('%I:%M %p').lstrip('0')}"
            else:
                time_fmt = start_dt.strftime('%I:%M %p').lstrip('0')

            loc_url = evt.location_url
            if not loc_url and evt.location:
                loc_url = f"https://www.google.com/maps/search/?api=1&query={quote_plus(evt.location)}"

            events.append({
                "id": f"portal_{evt.id}",
                "title": evt.title,
                "start_datetime": start_dt,
                "end_datetime": end_dt,
                "date_formatted": start_dt.strftime("%A, %B %d, %Y"),
                "time_formatted": time_fmt,
                "location": evt.location or "San Francisco Bay Area",
                "location_url": loc_url,
                "category": evt.category or "Community Gathering",
                "description": evt.description or "",
                "organizer": evt.created_by_name or "MiniMexitas Organizer",
                "google_calendar_link": get_google_calendar_add_url(evt.title, start_dt, end_dt, evt.description, evt.location),
                "year": start_dt.year,
                "month": start_dt.month,
                "day": start_dt.day,
                "is_past": is_past,
                "source": "portal",
                "is_portal_event": True,
                "portal_event_pk": evt.id,
            })

        return events
    except Exception as e:
        logger.error(f"Error fetching portal database events: {e}", exc_info=True)
        return []


def _normalize_for_sort(dt):
    if dt is None:
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    if hasattr(dt, 'tzinfo') and dt.tzinfo is not None:
        return dt.astimezone(datetime.timezone.utc)
    return dt.replace(tzinfo=datetime.timezone.utc)


def fetch_community_events(force_refresh=False):
    """
    Dual-source community events loader:
    1. Fetches upcoming & past events directly from Google Calendar API.
    2. Fetches portal-created community events directly from the local database.
    3. Merges, sorts, and caches results for fast, responsive page loads.
    """
    if not force_refresh:
        cached = cache.get(CACHE_KEY_EVENTS)
        if cached is not None:
            return cached

    gcal_events = []
    cal_id = os.environ.get("GOOGLE_CALENDAR_ID")
    sa_info = getattr(settings, "GOOGLE_SERVICE_ACCOUNT_INFO", None)

    if cal_id and sa_info:
        logger.info(f"Syncing events from Google Calendar: {cal_id}")
        gcal_events = _fetch_from_google_calendar_api(cal_id, sa_info) or []

    portal_events = _fetch_from_database() or []

    # Merge events from both sources
    all_events = portal_events + gcal_events
    all_events.sort(key=lambda x: _normalize_for_sort(x.get('start_datetime')))

    cache.set(CACHE_KEY_EVENTS, all_events, CACHE_TIMEOUT_EVENTS)
    return all_events
