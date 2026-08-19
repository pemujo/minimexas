import os
import re
import urllib.parse
import urllib.request
import functools
import logging
from django.conf import settings
import gspread

logger = logging.getLogger(__name__)


# Known Bay Area locations & landmarks for instant coordinate mapping
BAY_AREA_GEOLOCATIONS = {
    "san francisco": (37.7749, -122.4194),
    "sf": (37.7749, -122.4194),
    "mission": (37.7599, -122.4148),
    "mission district": (37.7599, -122.4148),
    "mission dolores": (37.7596, -122.4269),
    "dolores park": (37.7596, -122.4269),
    "castro": (37.7609, -122.4350),
    "soma": (37.7785, -122.4056),
    "financial district": (37.7946, -122.4000),
    "richmond district": (37.7797, -122.4842),
    "sunset district": (37.7535, -122.4842),
    "north beach": (37.8000, -122.4100),
    "marina": (37.8037, -122.4368),
    "haight": (37.7692, -122.4481),
    "oakland": (37.8044, -122.2712),
    "berkeley": (37.8715, -122.2730),
    "san jose": (37.3382, -121.8863),
    "palo alto": (37.4419, -122.1430),
    "mountain view": (37.3861, -122.0839),
    "sunnyvale": (37.3688, -122.0363),
    "santa clara": (37.3541, -121.9552),
    "redwood city": (37.4852, -122.2364),
    "san mateo": (37.5630, -122.3255),
    "fremont": (37.5483, -121.9886),
    "hayward": (37.6688, -122.0808),
    "san rafael": (37.9735, -122.5311),
    "marin": (37.9838, -122.5449),
    "walnut creek": (37.9101, -122.0652),
    "concord": (37.9780, -122.0311),
    "pleasanton": (37.6604, -121.8758),
    "dublin": (37.7022, -121.9358),
    "livermore": (37.6819, -121.7680),
    "richmond": (37.9358, -122.3477),
    "daly city": (37.7058, -122.4619),
    "south san francisco": (37.6547, -122.4077),
    "burlingame": (37.5841, -122.3661),
    "cupertino": (37.3230, -122.0322),
    "milpitas": (37.4323, -121.8996),
    "san leandro": (37.7249, -122.1561),
    "alameda": (37.7652, -122.2416),
    "foster city": (37.5585, -122.2711),
    "belmont": (37.5202, -122.2758),
    "san carlos": (37.5072, -122.2605),
    "los altos": (37.3852, -122.1141),
    "santa cruz": (36.9741, -122.0308),
    "vallejo": (38.1041, -122.2566),
    "napa": (38.2975, -122.2869),
    "sonoma": (38.2919, -122.4580),
    "petaluma": (38.2324, -122.6367),
    "union city": (37.5934, -122.0438),
    "campbell": (37.2872, -121.9499),
    "los gatos": (37.2358, -121.9624),
    "morgan hill": (37.1305, -121.6544),
    "gilroy": (37.0058, -121.5683),
    "menlo park": (37.4530, -122.1817),
    "millbrae": (37.5985, -122.3872),
    "pacifica": (37.6138, -122.4869),
    "half moon bay": (37.4636, -122.4286),
    "east bay": (37.8044, -122.2712),
    "south bay": (37.3382, -121.8863),
    "north bay": (37.9735, -122.5311),
    "peninsula": (37.5630, -122.3255),
}


@functools.lru_cache(maxsize=512)
def resolve_google_maps_url(url):
    """
    Follows HTTP redirects for Google Maps short links (e.g. maps.app.goo.gl or goo.gl/maps)
    with a short timeout and caches the resulting full URL.
    """
    if not url or not isinstance(url, str):
        return url
    url_clean = url.strip()
    if not any(domain in url_clean for domain in ('maps.app.goo.gl', 'goo.gl/maps', 'bit.ly', 'tinyurl.com')):
        return url_clean
    try:
        req = urllib.request.Request(
            url_clean,
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        )
        with urllib.request.urlopen(req, timeout=2.5) as response:
            final_url = response.geturl()
            if final_url and final_url != url_clean:
                return final_url
    except Exception as e:
        logger.debug(f"Could not resolve short maps URL {url_clean}: {e}")
    return url_clean


def _extract_coords_from_string(text):
    """
    Extracts latitude and longitude from URLs, query parameters, or raw coordinate strings.
    """
    if not text:
        return None, None

    match = (
        re.search(r'@(-?\d+\.\d+),(-?\d+\.\d+)', text)
        or re.search(r'[?&/](?:q|ll|loc:|destination|query|center|daddr)=(-?\d+\.\d+),(-?\d+\.\d+)', text)
        or re.search(r'/search/(-?\d+\.\d+),(-?\d+\.\d+)', text)
        or re.search(r'!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)', text)
        or re.search(r'!8m2!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)', text)
        or re.search(r'!1d(-?\d+\.\d+)!2d(-?\d+\.\d+)', text)
        or re.search(r'^(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)$', text)
        or re.search(r'(?<!\d)(-?\d{1,2}\.\d{3,7})\s*,\s*(-?\d{1,3}\.\d{3,7})(?!\d)', text)
    )
    if match:
        try:
            return float(match.group(1)), float(match.group(2))
        except (ValueError, TypeError):
            pass
    return None, None


def parse_location_and_coords(location_val, recommendation_text=""):
    """
    Parses a raw location string (or extracts a Google Maps link from recommendation text).
    Resolves short URLs, extracts place titles, and extracts exact or fallback Bay Area coordinates.
    Returns a dict with location, location_url, location_display, lat, lng, and has_location.
    """
    raw_loc = str(location_val or '').strip()
    rec_text = str(recommendation_text or '').strip()

    # If no explicit location, search for a Google Maps URL embedded inside recommendation text
    if not raw_loc and rec_text:
        match_url = re.search(r'https?://(?:maps\.google\.[a-z.]+|goo\.gl/maps|maps\.app\.goo\.gl|www\.google\.[a-z.]+/maps)[^\s<>"\']*', rec_text, re.IGNORECASE)
        if match_url:
            raw_loc = match_url.group(0).rstrip('.,;:)')

    if not raw_loc:
        return {
            'location': '',
            'location_url': '',
            'location_display': '',
            'lat': None,
            'lng': None,
            'has_location': False,
        }

    # Resolve short URLs if needed
    expanded_url = raw_loc
    if raw_loc.startswith(('http://', 'https://')):
        expanded_url = resolve_google_maps_url(raw_loc)

    is_url = raw_loc.startswith(('http://', 'https://')) or expanded_url.startswith(('http://', 'https://'))
    location_url = raw_loc if is_url else f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote_plus(raw_loc)}"

    # Clean display title
    location_display = ""
    if is_url:
        # Check place path in expanded or raw URL
        for u in (expanded_url, raw_loc):
            place_match = re.search(r'/place/([^/@?]+)', u)
            if place_match:
                extracted = urllib.parse.unquote_plus(place_match.group(1)).replace('+', ' ').strip()
                if extracted and not extracted.startswith('data='):
                    location_display = extracted
                    break
            q_match = re.search(r'[?&](?:query|q|destination|daddr)=([^&]+)', u)
            if q_match:
                extracted = urllib.parse.unquote_plus(q_match.group(1)).replace('+', ' ').strip()
                if extracted:
                    location_display = extracted
                    break
            s_match = re.search(r'/search/([^/@?]+)', u)
            if s_match:
                extracted = urllib.parse.unquote_plus(s_match.group(1)).replace('+', ' ').strip()
                if extracted:
                    location_display = extracted
                    break

        if not location_display or location_display.startswith('data='):
            location_display = "Google Maps Location"
    else:
        location_display = raw_loc

    # Extract coordinates
    lat, lng = _extract_coords_from_string(expanded_url)
    if lat is None or lng is None:
        lat, lng = _extract_coords_from_string(raw_loc)

    # Lookup known Bay Area cities/landmarks if coordinates not explicitly found
    if lat is None or lng is None:
        search_blob = f"{location_display} {raw_loc} {expanded_url} {rec_text}".lower()
        for place_name, coords in BAY_AREA_GEOLOCATIONS.items():
            pattern = r'\b' + re.escape(place_name) + r'\b'
            if re.search(pattern, search_blob):
                lat, lng = coords
                break

    # Fallback to Bay Area center coordinates if location exists but couldn't be geocoded
    if lat is None or lng is None:
        lat, lng = (37.7749, -122.4194)

    return {
        'location': raw_loc,
        'location_url': location_url,
        'location_display': location_display,
        'lat': lat,
        'lng': lng,
        'has_location': True,
    }


def _normalize_record_keys(record):
    """
    Normalizes dictionary keys from Google Sheets so Django templates
    can access fields reliably using dot notation (e.g. row.Shared_By).
    Also parses and enriches location information and map coordinates.
    """
    normalized = {}
    location_raw = ''

    for key, value in record.items():
        clean_key = str(key).strip().replace(" ", "_")
        normalized[clean_key] = value
        
        # Also ensure canonical field names match expected template properties
        lower_key = clean_key.lower()
        if lower_key in ('shared_by', 'sharedby', 'author', 'member', 'compartido_por', 'compartidopor'):
            normalized['Shared_By'] = value
        elif lower_key in ('date', 'timestamp', 'time', 'fecha'):
            normalized['Date'] = value
        elif lower_key in ('subject', 'topic', 'category', 'title', 'tema', 'categoria', 'categoría'):
            normalized['Subject'] = value
        elif lower_key in ('recommendation', 'notes', 'message', 'content', 'recommendations', 'recomendacion', 'recomendación', 'notas'):
            normalized['Recommendation'] = value
        elif lower_key in ('location', 'ubicacion', 'ubicación', 'map', 'maps', 'google_maps', 'maps_url', 'location_url', 'address', 'direccion', 'dirección', 'lugar', 'place'):
            normalized['Location'] = value
            location_raw = value

    # Parse and enrich location metadata & coordinates
    loc_info = parse_location_and_coords(
        location_raw or normalized.get('Location', ''),
        recommendation_text=normalized.get('Recommendation', '')
    )
    normalized['Location'] = loc_info['location']
    normalized['location_url'] = loc_info['location_url']
    normalized['location_display'] = loc_info['location_display']
    normalized['lat'] = loc_info['lat']
    normalized['lng'] = loc_info['lng']
    normalized['has_location'] = loc_info['has_location']

    return normalized


def get_gspread_client():
    """
    Initializes a gspread client from environment-loaded service account credentials dictionary,
    falling back to a file path if dictionary is not present.
    """
    sa_info = getattr(settings, "GOOGLE_SERVICE_ACCOUNT_INFO", None)
    if sa_info:
        return gspread.service_account_from_dict(sa_info)
    
    creds_path = getattr(settings, "GOOGLE_CREDENTIALS_PATH", None)
    if creds_path:
        return gspread.service_account(filename=creds_path)

    raise ValueError("Google Service Account credentials are not configured in .env or environment.")


def open_google_spreadsheet(gc):
    """
    Opens the target Google Spreadsheet using the GOOGLE_SHEET_KEY (or GOOGLE_SHEET_ID)
    environment variable.
    """
    sheet_key = getattr(settings, 'GOOGLE_SHEET_KEY', '') or os.environ.get('GOOGLE_SHEET_KEY', os.environ.get('GOOGLE_SHEET_ID', ''))
    if sheet_key and str(sheet_key).strip():
        return gc.open_by_key(str(sheet_key).strip())

    raise ValueError("GOOGLE_SHEET_KEY is not configured in .env or environment variables.")


def _get_worksheet_case_insensitive(sh, target_name):
    """
    Safely retrieves a worksheet by case-insensitive and whitespace-stripped name,
    supporting common bilingual aliases.
    """
    if not sh or not target_name:
        return None

    try:
        ws = sh.worksheet(target_name)
        if ws:
            return ws
    except Exception:
        pass

    target_clean = str(target_name).strip().lower().replace("_", "").replace(" ", "")
    try:
        worksheets = sh.worksheets()
    except Exception:
        return None

    for ws in worksheets:
        title = getattr(ws, 'title', '')
        title_clean = str(title).strip().lower().replace("_", "").replace(" ", "")
        if title_clean == target_clean:
            return ws
        # Semantic aliases
        if target_clean in ('pendingrequests', 'requests') and title_clean in ('solicitudes', 'pendientes', 'solicitudespendientes'):
            return ws
        if target_clean in ('members',) and title_clean in ('miembros', 'socios', 'directorio'):
            return ws
        if target_clean in ('auditlog', 'auditlogs') and title_clean in ('auditlog', 'auditlogs', 'bitacora', 'auditoria', 'registro', 'audit'):
            return ws
        if target_clean in ('surveys', 'survey', 'polls', 'poll') and title_clean in ('surveys', 'survey', 'polls', 'poll', 'encuestas', 'votaciones', 'votacion'):
            return ws
        if target_clean in ('eventrsvps', 'rsvps', 'eventrsvp', 'rsvp') and title_clean in ('eventrsvps', 'rsvps', 'eventrsvp', 'rsvp', 'asistencia', 'asistencias', 'confirmaciones'):
            return ws
        if target_clean in ('events', 'communityevents', 'eventos') and title_clean in ('events', 'communityevents', 'eventos', 'eventoscomunidad'):
            return ws
        if target_clean in ('recomendaciones', 'recommendations', 'notes') and title_clean in ('recomendaciones', 'recommendations', 'notes', 'recs', 'directoriorecs'):
            return ws
    return None


def fetch_recommendations():
    """
    Fetches rows from the Google Sheet and returns normalized dictionaries.
    Prioritizes 'Recomendaciones', 'Recommendations', or 'Notes' tab before fallback.
    Resilient to missing headers, varied column orders, and missing Location column definitions.
    """
    gc = get_gspread_client()
    sh = open_google_spreadsheet(gc)
    worksheet = (
        _get_worksheet_case_insensitive(sh, "Recomendaciones")
        or _get_worksheet_case_insensitive(sh, "Recommendations")
        or _get_worksheet_case_insensitive(sh, "Notes")
        or getattr(sh, 'sheet1', None)
    )
    if not worksheet:
        return []

    # First attempt to read via get_all_values() for full column resilience
    try:
        all_values = worksheet.get_all_values()
        if all_values and isinstance(all_values, list) and len(all_values) > 0 and isinstance(all_values[0], (list, tuple)):
            headers = [str(h).strip() for h in all_values[0]]
            canonical_headers = ['Date', 'Subject', 'Shared By', 'Recommendation', 'Location']
            if not headers or all(not h for h in headers):
                headers = canonical_headers

            records = []
            for row in all_values[1:]:
                if not isinstance(row, (list, tuple)) or all(not str(c).strip() for c in row):
                    continue
                record = {}
                for col_idx, cell in enumerate(row):
                    if col_idx < len(headers) and headers[col_idx]:
                        record[headers[col_idx]] = cell
                    elif col_idx == 4:
                        record['Location'] = cell
                    else:
                        record[f'col_{col_idx}'] = cell

                has_loc_in_rec = any(str(k).strip().lower() in ('location', 'ubicacion', 'ubicación', 'map', 'maps', 'address', 'direccion') for k in record)
                if not has_loc_in_rec and len(row) >= 5:
                    record['Location'] = row[4]

                records.append(_normalize_record_keys(record))
            if records:
                return records
    except Exception as e:
        logger.debug(f"get_all_values() not usable or failed: {e}")

    # Fallback / standard get_all_records()
    try:
        raw_records = worksheet.get_all_records() if worksheet else []
        if raw_records and isinstance(raw_records, list):
            return [_normalize_record_keys(row) for row in raw_records if isinstance(row, dict)]
    except Exception as e:
        logger.warning(f"Failed to fetch recommendations: {e}")

    return []


def sync_recommendation_to_google_sheet(shared_by, subject, recommendation, date_str=None, location=None):
    """
    Appends a new community recommendation to the 'Recomendaciones' (or 'Recommendations') tab.
    Matches column headers dynamically, initializes default headers if missing, and appends the row.
    """
    if not shared_by or not recommendation:
        return False

    try:
        from datetime import datetime
        gc = get_gspread_client()
        sh = open_google_spreadsheet(gc)

        worksheet = (
            _get_worksheet_case_insensitive(sh, "Recomendaciones")
            or _get_worksheet_case_insensitive(sh, "Recommendations")
            or _get_worksheet_case_insensitive(sh, "Notes")
        )
        if not worksheet:
            try:
                worksheet = getattr(sh, 'sheet1', None)
            except Exception:
                worksheet = None

        if not worksheet:
            worksheet = sh.add_worksheet(title="Recomendaciones", rows=100, cols=5)

        all_values = worksheet.get_all_values()
        expected_headers = ['Date', 'Subject', 'Shared By', 'Recommendation', 'Location']

        if not all_values:
            worksheet.update(values=[expected_headers], range_name='A1:E1')
            all_values = [expected_headers]

        headers = all_values[0]
        if not headers or all(not str(h).strip() for h in headers):
            headers = expected_headers
            worksheet.update(values=[headers], range_name='A1:E1')

        if not date_str:
            date_str = datetime.now().strftime("%Y-%m-%d")

        field_map = {
            'date': str(date_str).strip(),
            'fecha': str(date_str).strip(),
            'timestamp': str(date_str).strip(),
            'time': str(date_str).strip(),
            'subject': str(subject).strip(),
            'tema': str(subject).strip(),
            'categoria': str(subject).strip(),
            'categoría': str(subject).strip(),
            'topic': str(subject).strip(),
            'shared by': str(shared_by).strip(),
            'shared_by': str(shared_by).strip(),
            'sharedby': str(shared_by).strip(),
            'compartido por': str(shared_by).strip(),
            'compartidopor': str(shared_by).strip(),
            'autor': str(shared_by).strip(),
            'author': str(shared_by).strip(),
            'member': str(shared_by).strip(),
            'recommendation': str(recommendation).strip(),
            'recomendacion': str(recommendation).strip(),
            'recomendación': str(recommendation).strip(),
            'notes': str(recommendation).strip(),
            'notas': str(recommendation).strip(),
            'message': str(recommendation).strip(),
            'content': str(recommendation).strip(),
            'location': str(location or '').strip(),
            'ubicacion': str(location or '').strip(),
            'ubicación': str(location or '').strip(),
            'map': str(location or '').strip(),
            'maps': str(location or '').strip(),
            'google_maps': str(location or '').strip(),
            'maps_url': str(location or '').strip(),
            'location_url': str(location or '').strip(),
            'address': str(location or '').strip(),
            'direccion': str(location or '').strip(),
            'dirección': str(location or '').strip(),
            'place': str(location or '').strip(),
            'lugar': str(location or '').strip(),
        }

        # Check if headers include a location column; if not and location provided, add header
        has_location_header = any(str(h).strip().lower() in ('location', 'ubicacion', 'ubicación', 'map', 'maps', 'address', 'direccion') for h in headers)
        if not has_location_header and location and str(location).strip():
            headers.append('Location')
            try:
                worksheet.update(values=[headers], range_name=f'A1:{chr(ord("A") + len(headers) - 1)}1')
            except Exception:
                pass

        if headers:
            row_data = [field_map.get(str(h).strip().lower(), '') for h in headers]
            if all(not str(val).strip() for val in row_data):
                row_data = [date_str, subject, shared_by, recommendation, str(location or '').strip()]
        else:
            row_data = [date_str, subject, shared_by, recommendation, str(location or '').strip()]

        worksheet.append_row(row_data)
        logger.info(f"Successfully synced new recommendation in '{subject}' shared by '{shared_by}' with location '{location or ''}' to Google Sheet.")
        return True
    except Exception as e:
        logger.warning(f"Failed to sync recommendation to Google Sheet: {e}")
        return False


def update_recommendation_in_google_sheet(
    row_index,
    shared_by,
    subject,
    recommendation,
    date_str=None,
    location=None,
    original_subject=None,
    original_shared_by=None,
    original_recommendation=None
):
    """
    Updates an existing community recommendation row in the 'Recomendaciones' (or 'Recommendations') tab.
    Identifies target row using row_index (1-indexed, header is row 1) with fallback value matching.
    """
    if not shared_by or not recommendation:
        return False

    try:
        from datetime import datetime
        gc = get_gspread_client()
        sh = open_google_spreadsheet(gc)

        worksheet = (
            _get_worksheet_case_insensitive(sh, "Recomendaciones")
            or _get_worksheet_case_insensitive(sh, "Recommendations")
            or _get_worksheet_case_insensitive(sh, "Notes")
        )
        if not worksheet:
            try:
                worksheet = getattr(sh, 'sheet1', None)
            except Exception:
                worksheet = None

        if not worksheet:
            return False

        all_values = worksheet.get_all_values()
        expected_headers = ['Date', 'Subject', 'Shared By', 'Recommendation', 'Location']

        if not all_values:
            worksheet.update(values=[expected_headers], range_name='A1:E1')
            all_values = [expected_headers]

        headers = [str(h).strip() for h in all_values[0]]
        if not headers or all(not h for h in headers):
            headers = expected_headers
            worksheet.update(values=[headers], range_name='A1:E1')

        # Check if headers include a location column; if not and location provided, add header
        has_location_header = any(str(h).strip().lower() in ('location', 'ubicacion', 'ubicación', 'map', 'maps', 'address', 'direccion') for h in headers)
        if not has_location_header and location and str(location).strip():
            headers.append('Location')
            try:
                worksheet.update(values=[headers], range_name=f'A1:{chr(ord("A") + len(headers) - 1)}1')
            except Exception:
                pass

        if not date_str:
            date_str = datetime.now().strftime("%Y-%m-%d")

        field_map = {
            'date': str(date_str).strip(),
            'fecha': str(date_str).strip(),
            'timestamp': str(date_str).strip(),
            'time': str(date_str).strip(),
            'subject': str(subject).strip(),
            'tema': str(subject).strip(),
            'categoria': str(subject).strip(),
            'categoría': str(subject).strip(),
            'topic': str(subject).strip(),
            'shared by': str(shared_by).strip(),
            'shared_by': str(shared_by).strip(),
            'sharedby': str(shared_by).strip(),
            'compartido por': str(shared_by).strip(),
            'compartidopor': str(shared_by).strip(),
            'autor': str(shared_by).strip(),
            'author': str(shared_by).strip(),
            'member': str(shared_by).strip(),
            'recommendation': str(recommendation).strip(),
            'recommendations': str(recommendation).strip(),
            'recomendacion': str(recommendation).strip(),
            'recomendación': str(recommendation).strip(),
            'notas': str(recommendation).strip(),
            'notes': str(recommendation).strip(),
            'message': str(recommendation).strip(),
            'content': str(recommendation).strip(),
            'location': str(location or '').strip(),
            'ubicacion': str(location or '').strip(),
            'ubicación': str(location or '').strip(),
            'map': str(location or '').strip(),
            'maps': str(location or '').strip(),
            'google_maps': str(location or '').strip(),
            'maps_url': str(location or '').strip(),
            'location_url': str(location or '').strip(),
            'address': str(location or '').strip(),
            'direccion': str(location or '').strip(),
            'dirección': str(location or '').strip(),
            'place': str(location or '').strip(),
            'lugar': str(location or '').strip(),
        }

        row_data = [field_map.get(str(h).strip().lower(), '') for h in headers]
        if all(not str(val).strip() for val in row_data):
            row_data = [date_str, subject, shared_by, recommendation, str(location or '').strip()]

        target_row_idx = None
        try:
            r_idx = int(row_index)
            if 1 < r_idx <= len(all_values):
                target_row_idx = r_idx
        except (ValueError, TypeError):
            target_row_idx = None

        # Fallback search if row_index was not exact or rows shifted
        if not target_row_idx and (original_recommendation or original_subject):
            orig_rec_clean = str(original_recommendation or '').strip().lower()
            orig_sub_clean = str(original_subject or '').strip().lower()
            orig_author_clean = str(original_shared_by or '').strip().lower()

            for idx, existing_row in enumerate(all_values[1:], start=2):
                row_str = " ".join([str(c).lower() for c in existing_row])
                if orig_rec_clean and orig_rec_clean in row_str:
                    target_row_idx = idx
                    break
                if orig_sub_clean and orig_author_clean and orig_sub_clean in row_str and orig_author_clean in row_str:
                    target_row_idx = idx
                    break

        if target_row_idx:
            while len(row_data) < len(headers):
                row_data.append('')
            end_col = chr(ord('A') + len(row_data) - 1) if len(row_data) <= 26 else 'Z'
            range_name = f"A{target_row_idx}:{end_col}{target_row_idx}"
            worksheet.update(values=[row_data], range_name=range_name)
            logger.info(f"Successfully updated recommendation at row {target_row_idx} in Google Sheet.")
            return True
        else:
            worksheet.append_row(row_data)
            logger.info("Target row not found for edit; appended as new row in Google Sheet.")
            return True
    except Exception as e:
        logger.warning(f"Failed to update recommendation in Google Sheet: {e}")
        return False


def sync_profile_to_google_sheet(profile):
    """
    Securely synchronizes a MemberProfile record to the 'Members' tab in the Google Spreadsheet.
    Finds the row corresponding to the member's email (searching across all columns for existing entries),
    updates that row in-place, or appends a new row if not present.
    Executes safely with error handling to avoid disrupting web requests.
    """
    if not profile or not profile.email:
        return False

    try:
        from datetime import datetime
        gc = get_gspread_client()
        sh = open_google_spreadsheet(gc)

        worksheet = _get_worksheet_case_insensitive(sh, "Members")
        if not worksheet:
            worksheet = sh.add_worksheet(title="Members", rows=100, cols=10)

        # Get all current values in the sheet
        all_values = worksheet.get_all_values()
        expected_headers = ['Name', 'Gmail', 'Region', 'City', 'Phone', 'Family Info', 'Interests', 'Role', 'Last Updated']

        if not all_values:
            worksheet.update(values=[expected_headers], range_name='A1:I1')
            all_values = [expected_headers]

        headers = all_values[0]

        # Ensure missing expected columns are appended to header row
        missing_cols = [col for col in expected_headers if col.lower() not in [h.strip().lower() for h in headers]]
        if missing_cols:
            new_headers = headers + missing_cols
            end_letter = chr(64 + len(new_headers)) if len(new_headers) <= 26 else 'Z'
            worksheet.update(values=[new_headers], range_name=f"A1:{end_letter}1")
            headers = new_headers

        # Build column index mapping (lowercase header -> 0-based column index)
        col_map = {h.strip().lower(): idx for idx, h in enumerate(headers)}

        clean_target_email = profile.email.strip().lower()

        # Find target row index (1-based for gspread):
        # Scan through all rows and check every cell in each row for matching email
        target_row_idx = None
        for r_idx, row in enumerate(all_values[1:], start=2):
            for cell in row:
                if str(cell).strip().lower() == clean_target_email:
                    target_row_idx = r_idx
                    break
            if target_row_idx:
                break

        updated_time = datetime.now().strftime("%Y-%m-%d %H:%M")
        region_display = profile.get_region_display() if profile.region else ''
        role_display = 'Admin' if profile.is_admin else 'Member'

        field_values = {
            'gmail': profile.email,
            'email': profile.email,
            'name': profile.full_name,
            'region': region_display,
            'city': profile.city,
            'phone': profile.phone_number,
            'family info': profile.family_info,
            'interests': profile.interests,
            'email notifications': 'Yes' if profile.email_notifications else 'No',
            'notifications': 'Yes' if profile.email_notifications else 'No',
            'role': role_display,
            'last updated': updated_time,
        }

        if target_row_idx:
            # Update existing row
            current_row = all_values[target_row_idx - 1] if target_row_idx - 1 < len(all_values) else []
            # Pad current_row to match headers length
            while len(current_row) < len(headers):
                current_row.append('')

            # Preserve existing role in Google Sheet if already defined
            role_col_idx = col_map.get('role', None)
            existing_role = current_row[role_col_idx] if role_col_idx is not None and role_col_idx < len(current_row) else ''

            for header_name, val in field_values.items():
                if header_name == 'role':
                    # The website NEVER alters the Role column in Google Sheets
                    continue
                if header_name in col_map:
                    idx = col_map[header_name]
                    if idx < len(current_row):
                        current_row[idx] = val

            end_letter = chr(64 + len(headers)) if len(headers) <= 26 else 'Z'
            worksheet.update(values=[current_row], range_name=f"A{target_row_idx}:{end_letter}{target_row_idx}")
            logger.info(f"Successfully updated profile for {profile.email} at Google Sheet row {target_row_idx}.")
        else:
            # Append new row matching header order (new members default to 'Member' role)
            new_row = []
            for h in headers:
                key = h.strip().lower()
                if key == 'role':
                    new_row.append('Member')
                else:
                    new_row.append(field_values.get(key, ''))
            worksheet.append_row(new_row)
            logger.info(f"Successfully appended new profile row for {profile.email} to Google Sheet.")

        return True
    except Exception as e:
        logger.warning(f"Failed to sync profile {profile.email} to Google Sheet: {e}")
        return False


def sync_pending_request_to_google_sheet(request_obj):
    """
    Dual-syncs a MembershipRequest record to the 'Pending_Requests' tab in Google Sheets.
    Creates the tab with standard headers if it does not already exist.
    Updates in-place if the email already exists in the sheet, or appends a new row.
    Safely handles exceptions to prevent breaking the web application.
    """
    if not request_obj or not request_obj.email:
        return False

    try:
        from datetime import datetime
        gc = get_gspread_client()
        sh = open_google_spreadsheet(gc)

        worksheet = _get_worksheet_case_insensitive(sh, "Pending_Requests")
        if not worksheet:
            worksheet = sh.add_worksheet(title="Pending_Requests", rows=100, cols=11)

        all_values = worksheet.get_all_values()
        expected_headers = ['Name', 'Gmail', 'Phone', 'Region', 'City', 'Referral Source', 'Status', 'Submitted At', 'Reviewed By', 'Reviewed At', 'Review Notes']

        if not all_values:
            worksheet.update(values=[expected_headers], range_name='A1:K1')
            all_values = [expected_headers]

        headers = all_values[0]

        # Ensure missing headers are added
        missing_cols = [col for col in expected_headers if col.lower() not in [h.strip().lower() for h in headers]]
        if missing_cols:
            new_headers = headers + missing_cols
            end_letter = chr(64 + len(new_headers)) if len(new_headers) <= 26 else 'Z'
            worksheet.update(values=[new_headers], range_name=f"A1:{end_letter}1")
            headers = new_headers

        col_map = {h.strip().lower(): idx for idx, h in enumerate(headers)}
        clean_target_email = request_obj.email.strip().lower()

        target_row_idx = None
        for r_idx, row in enumerate(all_values[1:], start=2):
            for cell in row:
                if str(cell).strip().lower() == clean_target_email:
                    target_row_idx = r_idx
                    break
            if target_row_idx:
                break

        submitted_time = request_obj.created_at.strftime("%Y-%m-%d %H:%M") if request_obj.created_at else datetime.now().strftime("%Y-%m-%d %H:%M")
        reviewed_time = request_obj.reviewed_at.strftime("%Y-%m-%d %H:%M") if request_obj.reviewed_at else ""
        region_display = request_obj.get_region_display() if request_obj.region else ''
        status_display = request_obj.get_status_display() if hasattr(request_obj, 'get_status_display') else str(request_obj.status).title()

        field_values = {
            'name': request_obj.full_name,
            'gmail': request_obj.email,
            'email': request_obj.email,
            'phone': request_obj.phone_number,
            'region': region_display,
            'city': request_obj.city,
            'referral source': request_obj.referral_source,
            'status': status_display,
            'submitted at': submitted_time,
            'reviewed by': request_obj.reviewed_by or '',
            'reviewed at': reviewed_time,
            'review notes': request_obj.review_notes or '',
        }

        if target_row_idx:
            current_row = all_values[target_row_idx - 1] if target_row_idx - 1 < len(all_values) else []
            while len(current_row) < len(headers):
                current_row.append('')

            for header_name, val in field_values.items():
                if header_name in col_map:
                    idx = col_map[header_name]
                    if idx < len(current_row):
                        current_row[idx] = val

            end_letter = chr(64 + len(headers)) if len(headers) <= 26 else 'Z'
            worksheet.update(values=[current_row], range_name=f"A{target_row_idx}:{end_letter}{target_row_idx}")
            logger.info(f"Successfully updated pending request for {request_obj.email} in Google Sheet.")
        else:
            new_row = []
            for h in headers:
                key = h.strip().lower()
                new_row.append(field_values.get(key, ''))
            worksheet.append_row(new_row)
            logger.info(f"Successfully appended pending request for {request_obj.email} to Pending_Requests tab.")

        return True
    except Exception as e:
        logger.warning(f"Failed to dual-sync pending request for {request_obj.email} to Google Sheet: {e}")
        return False


def update_pending_request_status_in_google_sheet(email, status_display, reviewed_by="", review_notes=""):
    """
    Updates the status, reviewer, and optional review notes in the 'Pending_Requests' tab when an organizer approves or declines.
    If a database record exists, syncs the full model. Otherwise, performs in-place worksheet update/append.
    """
    if not email:
        return False
    try:
        from datetime import datetime
        from recommendations.models import MembershipRequest
        
        req_obj = MembershipRequest.objects.filter(email__iexact=email.strip().lower()).first()
        if req_obj:
            if status_display.lower() in ('approved', 'aprobado'):
                req_obj.status = MembershipRequest.STATUS_APPROVED
            elif status_display.lower() in ('declined', 'rejected', 'rechazado'):
                req_obj.status = MembershipRequest.STATUS_REJECTED
            if reviewed_by:
                req_obj.reviewed_by = reviewed_by
            if review_notes:
                req_obj.review_notes = review_notes
            return sync_pending_request_to_google_sheet(req_obj)

        gc = get_gspread_client()
        sh = open_google_spreadsheet(gc)
        worksheet = _get_worksheet_case_insensitive(sh, "Pending_Requests")
        if not worksheet:
            worksheet = sh.add_worksheet(title="Pending_Requests", rows=100, cols=11)

        all_values = worksheet.get_all_values()
        expected_headers = ['Name', 'Gmail', 'Phone', 'Region', 'City', 'Referral Source', 'Status', 'Submitted At', 'Reviewed By', 'Reviewed At', 'Review Notes']

        if not all_values:
            worksheet.update(values=[expected_headers], range_name='A1:K1')
            all_values = [expected_headers]

        headers = all_values[0]
        missing_cols = [col for col in expected_headers if col.lower() not in [h.strip().lower() for h in headers]]
        if missing_cols:
            new_headers = headers + missing_cols
            end_letter = chr(64 + len(new_headers)) if len(new_headers) <= 26 else 'Z'
            worksheet.update(values=[new_headers], range_name=f"A1:{end_letter}1")
            headers = new_headers

        col_map = {h.strip().lower(): idx for idx, h in enumerate(headers)}
        status_col_idx = col_map.get('status')
        reviewed_by_col_idx = col_map.get('reviewed by')
        reviewed_at_col_idx = col_map.get('reviewed at')
        review_notes_col_idx = col_map.get('review notes')

        clean_target_email = email.strip().lower()
        target_row_idx = None
        for r_idx, row in enumerate(all_values[1:], start=2):
            for cell in row:
                if str(cell).strip().lower() == clean_target_email:
                    target_row_idx = r_idx
                    break
            if target_row_idx:
                break

        if target_row_idx:
            current_row = all_values[target_row_idx - 1]
            while len(current_row) < len(headers):
                current_row.append('')

            if status_col_idx is not None:
                current_row[status_col_idx] = status_display
            if reviewed_by_col_idx is not None:
                current_row[reviewed_by_col_idx] = reviewed_by
            if reviewed_at_col_idx is not None:
                current_row[reviewed_at_col_idx] = datetime.now().strftime("%Y-%m-%d %H:%M")
            if review_notes_col_idx is not None and review_notes:
                current_row[review_notes_col_idx] = review_notes

            end_letter = chr(64 + len(headers)) if len(headers) <= 26 else 'Z'
            worksheet.update(values=[current_row], range_name=f"A{target_row_idx}:{end_letter}{target_row_idx}")
            logger.info(f"Updated pending request status for {email} to {status_display} in Google Sheet.")
        else:
            new_row = []
            for h in headers:
                key = h.strip().lower()
                if key in ('gmail', 'email'):
                    new_row.append(email)
                elif key == 'status':
                    new_row.append(status_display)
                elif key == 'reviewed by':
                    new_row.append(reviewed_by)
                elif key == 'reviewed at':
                    new_row.append(datetime.now().strftime("%Y-%m-%d %H:%M"))
                elif key == 'review notes':
                    new_row.append(review_notes)
                else:
                    new_row.append('')
            worksheet.append_row(new_row)
            logger.info(f"Appended request status for {email} to {status_display} in Google Sheet.")

        return True
    except Exception as e:
        logger.warning(f"Failed to update pending request status in Google Sheet: {e}")
        return False


def sync_audit_log_to_google_sheet(log_entry):
    """
    Appends an immutable audit log record to the 'Audit_Log' tab in Google Sheets.
    Creates the tab with standard headers if it does not already exist.
    """
    if not log_entry:
        return False

    try:
        from datetime import datetime
        gc = get_gspread_client()
        sh = open_google_spreadsheet(gc)

        worksheet = _get_worksheet_case_insensitive(sh, "Audit_Log")
        if not worksheet:
            worksheet = sh.add_worksheet(title="Audit_Log", rows=100, cols=7)

        all_values = worksheet.get_all_values()
        expected_headers = ['Timestamp', 'Action', 'Target Member', 'Target Email', 'Performed By', 'Admin Email', 'Notes']

        if not all_values:
            worksheet.update(values=[expected_headers], range_name='A1:G1')
            all_values = [expected_headers]

        headers = all_values[0]
        missing_cols = [col for col in expected_headers if col.lower() not in [h.strip().lower() for h in headers]]
        if missing_cols:
            new_headers = headers + missing_cols
            end_letter = chr(64 + len(new_headers)) if len(new_headers) <= 26 else 'Z'
            worksheet.update(values=[new_headers], range_name=f"A1:{end_letter}1")
            headers = new_headers

        timestamp_str = log_entry.created_at.strftime("%Y-%m-%d %H:%M") if log_entry.created_at else datetime.now().strftime("%Y-%m-%d %H:%M")
        action_display = log_entry.get_action_display() if hasattr(log_entry, 'get_action_display') else str(log_entry.action).title()

        field_values = {
            'timestamp': timestamp_str,
            'action': action_display,
            'target member': log_entry.target_name,
            'target email': log_entry.target_email,
            'performed by': log_entry.actor_name,
            'admin email': log_entry.actor_email,
            'notes': log_entry.notes or '',
        }

        new_row = []
        for h in headers:
            key = h.strip().lower()
            new_row.append(field_values.get(key, ''))

        worksheet.append_row(new_row)
        logger.info(f"Successfully appended audit log entry for {log_entry.target_email} ({action_display}) to Google Sheet.")
        return True
    except Exception as e:
        logger.warning(f"Failed to sync audit log entry to Google Sheet: {e}")
        return False


def backfill_audit_logs_to_google_sheet():
    """
    Backfills all existing SQLite MembershipAuditLog entries into the Google Sheets 'Audit_Log' tab.
    """
    from recommendations.models import MembershipAuditLog
    logs = MembershipAuditLog.objects.all().order_by('created_at')
    count = 0
    for log_entry in logs:
        if sync_audit_log_to_google_sheet(log_entry):
            count += 1
    return count


def sync_survey_to_google_sheet(survey):
    """
    Synchronizes a CommunitySurvey model and its options/votes to the 'Surveys' tab in Google Sheets.
    Creates or updates rows corresponding to each option with current vote counts, percentages, and transparent voter names.
    """
    if not survey:
        return False

    try:
        from datetime import datetime
        gc = get_gspread_client()
        sh = open_google_spreadsheet(gc)

        worksheet = _get_worksheet_case_insensitive(sh, "Surveys")
        if not worksheet:
            worksheet = sh.add_worksheet(title="Surveys", rows=100, cols=11)

        all_values = worksheet.get_all_values()
        expected_headers = [
            'Survey ID', 'Survey Question', 'Category', 'Mode', 'Status', 
            'Option Text', 'Votes Count', 'Percentage', 'Voter Names', 'Created By', 'Created At'
        ]

        if not all_values:
            worksheet.update(values=[expected_headers], range_name='A1:K1')
            all_values = [expected_headers]

        headers = all_values[0]
        # Auto-heal missing headers
        missing_cols = [col for col in expected_headers if col.lower() not in [h.strip().lower() for h in headers]]
        if missing_cols:
            new_headers = headers + missing_cols
            end_letter = chr(64 + len(new_headers)) if len(new_headers) <= 26 else 'Z'
            worksheet.update(values=[new_headers], range_name=f"A1:{end_letter}1")
            headers = new_headers

        survey_id_str = str(survey.id)
        category_display = survey.get_category_display() if hasattr(survey, 'get_category_display') else survey.category
        mode_display = "Multiple Choice" if survey.is_multiple_choice else "Single Choice"
        status_display = "Active" if survey.is_active else "Closed"
        created_by_str = survey.created_by or survey.created_by_email or "Admin"
        created_at_str = survey.created_at.strftime("%Y-%m-%d %H:%M") if survey.created_at else datetime.now().strftime("%Y-%m-%d %H:%M")
        total_votes = survey.total_votes

        options = list(survey.options.all().order_by('order', 'id'))
        if not options:
            return True

        # Build new rows for all options of this survey
        fresh_survey_rows = []
        for opt in options:
            vote_count = opt.vote_count
            pct_str = f"{opt.percentage(total_votes)}%"
            voters_str = ", ".join(opt.voter_names)

            field_map = {
                'survey id': survey_id_str,
                'survey question': survey.title,
                'category': category_display,
                'mode': mode_display,
                'status': status_display,
                'option text': opt.text,
                'votes count': str(vote_count),
                'percentage': pct_str,
                'voter names': voters_str,
                'created by': created_by_str,
                'created at': created_at_str,
            }

            row_data = [field_map.get(h.strip().lower(), '') for h in headers]
            fresh_survey_rows.append(row_data)

        # Retain existing non-survey rows and replace this survey's rows
        kept_rows = [headers]
        id_col_idx = 0
        for idx, h in enumerate(headers):
            if h.strip().lower() == 'survey id':
                id_col_idx = idx
                break

        for row in all_values[1:]:
            if len(row) > id_col_idx and str(row[id_col_idx]).strip() == survey_id_str:
                continue
            kept_rows.append(row)

        full_sheet_matrix = kept_rows + fresh_survey_rows

        # Clear worksheet content safely and write all rows
        worksheet.clear()
        end_letter = chr(64 + len(headers)) if len(headers) <= 26 else 'K'
        worksheet.update(values=full_sheet_matrix, range_name=f"A1:{end_letter}{len(full_sheet_matrix)}")
        logger.info(f"Successfully synced survey #{survey.id} '{survey.title}' to Google Sheet Surveys tab.")
        return True
    except Exception as e:
        logger.warning(f"Failed to sync survey #{getattr(survey, 'id', '')} to Google Sheet: {e}")
        return False


def delete_survey_from_google_sheet(survey_id):
    """
    Deletes all option rows corresponding to `survey_id` from the 'Surveys' tab in Google Sheets.
    """
    if not survey_id:
        return False

    try:
        gc = get_gspread_client()
        sh = open_google_spreadsheet(gc)

        worksheet = _get_worksheet_case_insensitive(sh, "Surveys")
        if not worksheet:
            return True

        all_values = worksheet.get_all_values()
        if not all_values or len(all_values) <= 1:
            return True

        headers = all_values[0]
        id_col_idx = 0
        for idx, h in enumerate(headers):
            if h.strip().lower() == 'survey id':
                id_col_idx = idx
                break

        target_id_str = str(survey_id).strip()
        remaining_rows = [headers]
        removed_count = 0

        for row in all_values[1:]:
            if len(row) > id_col_idx and str(row[id_col_idx]).strip() == target_id_str:
                removed_count += 1
                continue
            remaining_rows.append(row)

        if removed_count > 0:
            worksheet.clear()
            end_letter = chr(64 + len(headers)) if len(headers) <= 26 else 'K'
            worksheet.update(values=remaining_rows, range_name=f"A1:{end_letter}{len(remaining_rows)}")
            logger.info(f"Removed survey #{survey_id} ({removed_count} rows) from Google Sheet Surveys tab.")

        return True
    except Exception as e:
        logger.warning(f"Failed to delete survey #{survey_id} from Google Sheet: {e}")
        return False


def sync_event_rsvp_to_google_sheet(rsvp):
    """
    Synchronizes an event RSVP response to the 'Event_RSVPs' tab in Google Sheets.
    Finds the row corresponding to (event_id, member_email) and updates it in-place,
    or appends a new row if not present.
    """
    if not rsvp or not rsvp.event_id or not rsvp.member_email:
        return False

    try:
        from datetime import datetime
        gc = get_gspread_client()
        sh = open_google_spreadsheet(gc)

        worksheet = _get_worksheet_case_insensitive(sh, "Event_RSVPs")
        if not worksheet:
            logger.info("Worksheet 'Event_RSVPs' not found. Creating it automatically...")
            worksheet = sh.add_worksheet(title="Event_RSVPs", rows="100", cols="10")
            headers = ['Event ID', 'Event Title', 'Member Name', 'Member Email', 'RSVP Status', 'Notes', 'Updated At']
            worksheet.update(values=[headers], range_name="A1:G1")
            headers = [h.strip() for h in headers]
            all_values = [headers]
        else:
            all_values = worksheet.get_all_values()
            if not all_values:
                headers = ['Event ID', 'Event Title', 'Member Name', 'Member Email', 'RSVP Status', 'Notes', 'Updated At']
                worksheet.update(values=[headers], range_name="A1:G1")
                all_values = [headers]
            else:
                headers = [h.strip() for h in all_values[0]]

        status_display = rsvp.get_status_display() if hasattr(rsvp, 'get_status_display') else rsvp.status
        updated_at_str = rsvp.updated_at.strftime("%Y-%m-%d %H:%M") if rsvp.updated_at else datetime.now().strftime("%Y-%m-%d %H:%M")

        field_map = {
            'event id': str(rsvp.event_id),
            'event title': rsvp.event_title or '',
            'member name': rsvp.member_name or '',
            'member email': rsvp.member_email or '',
            'rsvp status': status_display,
            'status': status_display,
            'notes': rsvp.notes or '',
            'updated at': updated_at_str,
        }

        row_data = [field_map.get(h.lower(), '') for h in headers]

        # Find existing row by matching Event ID and Member Email
        event_id_idx = None
        email_idx = None
        for idx, h in enumerate(headers):
            h_clean = h.lower()
            if 'event' in h_clean and 'id' in h_clean:
                event_id_idx = idx
            elif 'email' in h_clean:
                email_idx = idx

        target_event_id = str(rsvp.event_id).strip().lower()
        target_email = str(rsvp.member_email).strip().lower()

        match_row_num = None
        if event_id_idx is not None and email_idx is not None:
            for row_idx, row in enumerate(all_values[1:], start=2):
                if len(row) > max(event_id_idx, email_idx):
                    r_ev_id = str(row[event_id_idx]).strip().lower()
                    r_email = str(row[email_idx]).strip().lower()
                    if r_ev_id == target_event_id and r_email == target_email:
                        match_row_num = row_idx
                        break

        end_letter = chr(64 + len(headers)) if len(headers) <= 26 else 'G'

        if match_row_num:
            range_name = f"A{match_row_num}:{end_letter}{match_row_num}"
            worksheet.update(values=[row_data], range_name=range_name)
            logger.info(f"Updated RSVP for {rsvp.member_email} (event {rsvp.event_id}) at row {match_row_num}")
        else:
            worksheet.append_row(row_data)
            logger.info(f"Appended new RSVP for {rsvp.member_email} (event {rsvp.event_id}) to Google Sheet")

        return True
    except Exception as e:
        logger.warning(f"Failed to sync RSVP for {getattr(rsvp, 'member_email', '')} to Google Sheet: {e}")
        return False


def sync_community_event_to_google_sheet(event):
    """
    Synchronizes a portal-created CommunityEvent to the 'Events' tab in Google Sheets.
    Finds the row corresponding to Event ID and updates it in-place, or appends a new row if not present.
    """
    if not event or not event.id:
        return False

    try:
        from datetime import datetime
        gc = get_gspread_client()
        sh = open_google_spreadsheet(gc)

        worksheet = _get_worksheet_case_insensitive(sh, "Events")
        if not worksheet:
            logger.info("Worksheet 'Events' not found. Creating it automatically...")
            worksheet = sh.add_worksheet(title="Events", rows="100", cols="11")
            headers = ['Event ID', 'Event Title', 'Category', 'Start Date & Time', 'End Date & Time', 'Location', 'Location URL', 'Description', 'Created By', 'Created At', 'Status']
            worksheet.update(values=[headers], range_name="A1:K1")
            headers = [h.strip() for h in headers]
            all_values = [headers]
        else:
            all_values = worksheet.get_all_values()
            if not all_values:
                headers = ['Event ID', 'Event Title', 'Category', 'Start Date & Time', 'End Date & Time', 'Location', 'Location URL', 'Description', 'Created By', 'Created At', 'Status']
                worksheet.update(values=[headers], range_name="A1:K1")
                all_values = [headers]
            else:
                headers = [h.strip() for h in all_values[0]]

        event_id_str = f"portal_{event.id}"
        start_str = event.start_datetime.strftime("%Y-%m-%d %H:%M") if event.start_datetime else ""
        end_str = event.end_datetime.strftime("%Y-%m-%d %H:%M") if event.end_datetime else ""
        created_str = event.created_at.strftime("%Y-%m-%d %H:%M") if event.created_at else datetime.now().strftime("%Y-%m-%d %H:%M")
        status_str = "Active" if event.is_active else "Inactive / Cancelled"

        field_map = {
            'event id': event_id_str,
            'id': event_id_str,
            'event title': event.title or '',
            'title': event.title or '',
            'category': event.category or '',
            'start date & time': start_str,
            'start date': start_str,
            'end date & time': end_str,
            'end date': end_str,
            'location': event.location or '',
            'location url': event.location_url or '',
            'description': event.description or '',
            'created by': f"{event.created_by_name} ({event.created_by_email})" if event.created_by_email else (event.created_by_name or ''),
            'created at': created_str,
            'status': status_str,
        }

        row_data = [field_map.get(h.lower(), '') for h in headers]

        # Find existing row by Event ID
        event_id_idx = None
        for idx, h in enumerate(headers):
            h_clean = h.lower()
            if ('event' in h_clean and 'id' in h_clean) or h_clean == 'id':
                event_id_idx = idx
                break

        match_row_num = None
        if event_id_idx is not None:
            for row_idx, row in enumerate(all_values[1:], start=2):
                if len(row) > event_id_idx:
                    r_ev_id = str(row[event_id_idx]).strip().lower()
                    if r_ev_id in (event_id_str.lower(), str(event.id).lower()):
                        match_row_num = row_idx
                        break

        end_letter = chr(64 + len(headers)) if len(headers) <= 26 else 'K'

        if match_row_num:
            range_name = f"A{match_row_num}:{end_letter}{match_row_num}"
            worksheet.update(values=[row_data], range_name=range_name)
            logger.info(f"Updated CommunityEvent {event_id_str} at row {match_row_num} in Google Sheet")
        else:
            worksheet.append_row(row_data)
            logger.info(f"Appended new CommunityEvent {event_id_str} to Google Sheet")

        return True
    except Exception as e:
        logger.warning(f"Failed to sync CommunityEvent {getattr(event, 'id', '')} to Google Sheet: {e}")
        return False


def delete_community_event_from_google_sheet(event_id):
    """
    Marks a CommunityEvent as 'Deleted (Inactive)' or removes row in the 'Events' tab of Google Sheets.
    """
    if not event_id:
        return False

    try:
        gc = get_gspread_client()
        sh = open_google_spreadsheet(gc)

        worksheet = _get_worksheet_case_insensitive(sh, "Events")
        if not worksheet:
            return True

        all_values = worksheet.get_all_values()
        if not all_values:
            return True

        headers = [h.strip() for h in all_values[0]]
        event_id_idx = None
        status_idx = None
        for idx, h in enumerate(headers):
            h_clean = h.lower()
            if ('event' in h_clean and 'id' in h_clean) or h_clean == 'id':
                event_id_idx = idx
            elif h_clean == 'status':
                status_idx = idx

        target_ids = {f"portal_{event_id}".lower(), str(event_id).lower()}
        if event_id_idx is not None:
            for row_idx, row in enumerate(all_values[1:], start=2):
                if len(row) > event_id_idx:
                    r_ev_id = str(row[event_id_idx]).strip().lower()
                    if r_ev_id in target_ids:
                        if status_idx is not None:
                            col_letter = chr(65 + status_idx)
                            worksheet.update_cell(row_idx, status_idx + 1, "Deleted (Inactive)")
                            logger.info(f"Marked event {event_id} as deleted at row {row_idx} in Google Sheet")
                        else:
                            worksheet.delete_rows(row_idx)
                            logger.info(f"Deleted row {row_idx} for event {event_id} in Google Sheet")
                        return True
        return True
    except Exception as e:
        logger.warning(f"Failed to delete event #{event_id} from Google Sheet: {e}")
        return False


def delete_profile_from_google_sheet(email):
    """
    Deletes the member row matching the email from the 'Members' tab of the Google Sheet.
    Safely handles missing worksheets or non-existent rows.
    Returns: bool (True if successfully removed or not found, False on API error).
    """
    if not email:
        return False

    clean_target_email = email.strip().lower()

    try:
        gc = get_gspread_client()
        sh = open_google_spreadsheet(gc)

        worksheet = _get_worksheet_case_insensitive(sh, "Members")
        if not worksheet:
            logger.warning("Members worksheet not found when attempting to delete profile.")
            return True

        all_values = worksheet.get_all_values()
        if not all_values or len(all_values) <= 1:
            return True

        target_row_idx = None
        for r_idx, row in enumerate(all_values[1:], start=2):
            for cell in row:
                if str(cell).strip().lower() == clean_target_email:
                    target_row_idx = r_idx
                    break
            if target_row_idx:
                break

        if target_row_idx:
            worksheet.delete_rows(target_row_idx)
            logger.info(f"Successfully deleted profile row for {clean_target_email} at Google Sheet row {target_row_idx}.")
        else:
            logger.info(f"Profile for {clean_target_email} not found in Google Sheet (already absent).")

        return True
    except Exception as e:
        logger.warning(f"Failed to delete profile for {email} from Google Sheet: {e}")
        return False


def reconcile_members_with_google_sheet():
    """
    Automatic two-way reconciliation between the Google Sheets 'Members' tab and local SQLite MemberProfile.

    1. Fetches all records from the Google Sheet 'Members' tab.
    2. Identifies all valid member rows and their emails.
    3. Purges local SQLite MemberProfiles whose emails are no longer present in Google Sheets.
    4. Imports and creates SQLite MemberProfiles for any entries found in Google Sheets that don't exist locally.
    5. Synchronizes roles (Organizer / Member) and profile info.
    6. Returns a summary dictionary:
       {
           'success': True,
           'purged_count': int,
           'created_count': int,
           'updated_count': int,
           'total_sheet_members': int,
           'purged_emails': list,
       }
    """
    from recommendations.models import MemberProfile, MembershipRequest

    try:
        gc = get_gspread_client()
        sh = open_google_spreadsheet(gc)

        worksheet = _get_worksheet_case_insensitive(sh, "Members")
        if not worksheet:
            logger.warning("Members worksheet not found during reconciliation.")
            return {
                'success': False,
                'error': "Members worksheet not found in Google Sheet.",
                'purged_count': 0,
                'created_count': 0,
                'updated_count': 0,
                'total_sheet_members': 0,
                'purged_emails': [],
            }

        all_values = worksheet.get_all_values()
        if not all_values or len(all_values) <= 1:
            logger.warning("Members worksheet has no data rows during reconciliation.")
            return {
                'success': True,
                'purged_count': 0,
                'created_count': 0,
                'updated_count': 0,
                'total_sheet_members': 0,
                'purged_emails': [],
            }

        headers = [h.strip().lower() for h in all_values[0]]
        col_map = {h: idx for idx, h in enumerate(headers)}

        def get_val(row, col_keys, default=''):
            for k in col_keys:
                if k in col_map:
                    idx = col_map[k]
                    if idx < len(row):
                        v = str(row[idx]).strip()
                        if v:
                            return v
            return default

        # Extract all valid members from sheet
        sheet_members = {}
        for row in all_values[1:]:
            email = get_val(row, ['gmail', 'email']).lower()
            if not email or '@' not in email:
                for cell in row:
                    c_str = str(cell).strip().lower()
                    if '@' in c_str and '.' in c_str and ' ' not in c_str:
                        email = c_str
                        break
            if not email or '@' not in email:
                continue

            name = get_val(row, ['name', 'full_name', 'member_name']) or 'Member'
            phone = get_val(row, ['phone', 'phone_number', 'whatsapp', 'phone number'])
            region_raw = get_val(row, ['region', 'bay_area_region', 'bay area region'])
            city = get_val(row, ['city', 'neighborhood', 'city / neighborhood'])
            family_info = get_val(row, ['family info', 'family_info', 'family & kids'])
            interests = get_val(row, ['interests', 'event interests'])
            bio = get_val(row, ['bio', 'about me', 'about'])
            role_val = get_val(row, ['role', 'admin', 'is_admin', 'isadmin']).lower()
            is_admin = role_val in ('admin', 'organizer', 'true', 'yes', '1')

            # Map region string to model region key
            region_key = ''
            if region_raw:
                r_lower = region_raw.lower()
                if 'san francisco' in r_lower or r_lower == 'sf':
                    region_key = 'san_francisco'
                elif 'peninsula' in r_lower:
                    region_key = 'peninsula'
                elif 'south bay' in r_lower or 'silicon valley' in r_lower or 'san jose' in r_lower:
                    region_key = 'south_bay'
                elif 'east bay' in r_lower or 'oakland' in r_lower or 'berkeley' in r_lower:
                    region_key = 'east_bay'
                elif 'north bay' in r_lower or 'marin' in r_lower or 'sonoma' in r_lower or 'napa' in r_lower:
                    region_key = 'north_bay'
                elif 'santa cruz' in r_lower:
                    region_key = 'santa_cruz'
                elif 'other' in r_lower:
                    region_key = 'other'
                elif region_raw in dict(MemberProfile.REGION_CHOICES):
                    region_key = region_raw

            sheet_members[email] = {
                'name': name,
                'phone': phone,
                'region_key': region_key,
                'city': city,
                'family_info': family_info,
                'interests': interests,
                'bio': bio,
                'is_admin': is_admin,
            }

        purged_count = 0
        purged_emails = []
        updated_count = 0
        created_count = 0

        # Step 1: Purge local SQLite profiles that are NOT in Google Sheets
        existing_profiles = list(MemberProfile.objects.all())
        for profile in existing_profiles:
            p_email = profile.email.strip().lower()
            if p_email not in sheet_members:
                logger.info(f"Reconciliation: Purging orphan profile {p_email} (deleted from Google Sheet).")
                profile.delete()
                MembershipRequest.objects.filter(email__iexact=p_email).delete()
                purged_count += 1
                purged_emails.append(p_email)
            else:
                # Step 2: Update existing profile if sheet role or basic details differ
                sheet_data = sheet_members[p_email]
                changed = False
                if profile.is_admin != sheet_data['is_admin']:
                    profile.is_admin = sheet_data['is_admin']
                    changed = True
                if not profile.full_name and sheet_data['name']:
                    profile.full_name = sheet_data['name']
                    changed = True
                if not profile.phone_number and sheet_data['phone']:
                    profile.phone_number = sheet_data['phone']
                    changed = True
                if not profile.region and sheet_data['region_key']:
                    profile.region = sheet_data['region_key']
                    changed = True
                if not profile.city and sheet_data['city']:
                    profile.city = sheet_data['city']
                    changed = True
                if not profile.family_info and sheet_data['family_info']:
                    profile.family_info = sheet_data['family_info']
                    changed = True
                if not profile.interests and sheet_data['interests']:
                    profile.interests = sheet_data['interests']
                    changed = True
                if not profile.bio and sheet_data['bio']:
                    profile.bio = sheet_data['bio']
                    changed = True

                if changed:
                    profile.save()
                    updated_count += 1

        # Step 3: Create local profiles for sheet members missing from SQLite
        existing_local_emails = set(MemberProfile.objects.values_list('email', flat=True))
        existing_local_emails_lower = {e.strip().lower() for e in existing_local_emails if e}

        for sheet_email, sheet_data in sheet_members.items():
            if sheet_email not in existing_local_emails_lower:
                logger.info(f"Reconciliation: Creating local profile for {sheet_email} from Google Sheet.")
                MemberProfile.objects.create(
                    email=sheet_email,
                    full_name=sheet_data['name'],
                    phone_number=sheet_data['phone'],
                    region=sheet_data['region_key'],
                    city=sheet_data['city'],
                    family_info=sheet_data['family_info'],
                    interests=sheet_data['interests'],
                    bio=sheet_data['bio'],
                    is_admin=sheet_data['is_admin']
                )
                created_count += 1

        logger.info(
            f"Reconciliation Complete: {purged_count} purged, {created_count} created, "
            f"{updated_count} updated, {len(sheet_members)} total in Google Sheet."
        )

        return {
            'success': True,
            'purged_count': purged_count,
            'created_count': created_count,
            'updated_count': updated_count,
            'total_sheet_members': len(sheet_members),
            'purged_emails': purged_emails,
        }
    except Exception as e:
        logger.error(f"Error during Google Sheet reconciliation: {e}", exc_info=True)
        return {
            'success': False,
            'error': str(e),
            'purged_count': 0,
            'created_count': 0,
            'updated_count': 0,
            'total_sheet_members': 0,
            'purged_emails': [],
        }



