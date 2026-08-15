import logging
from django.conf import settings
import gspread

logger = logging.getLogger(__name__)


def _normalize_record_keys(record):
    """
    Normalizes dictionary keys from Google Sheets so Django templates
    can access fields reliably using dot notation (e.g. row.Shared_By).
    """
    normalized = {}
    for key, value in record.items():
        clean_key = str(key).strip().replace(" ", "_")
        normalized[clean_key] = value
        
        # Also ensure canonical field names match expected template properties
        lower_key = clean_key.lower()
        if lower_key in ('shared_by', 'sharedby', 'author', 'member'):
            normalized['Shared_By'] = value
        elif lower_key in ('date', 'timestamp', 'time'):
            normalized['Date'] = value
        elif lower_key in ('subject', 'topic', 'category', 'title'):
            normalized['Subject'] = value
        elif lower_key in ('recommendation', 'notes', 'message', 'content', 'recommendations'):
            normalized['Recommendation'] = value

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


def fetch_recommendations():
    """
    Fetches rows from the Google Sheet and returns normalized dictionaries.
    """
    gc = get_gspread_client()
    sh = gc.open("WhatsApp Recommendations")
    worksheet = sh.sheet1
    
    raw_records = worksheet.get_all_records()
    return [_normalize_record_keys(row) for row in raw_records]
