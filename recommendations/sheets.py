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
        sh = gc.open("WhatsApp Recommendations")

        try:
            worksheet = sh.worksheet("Members")
        except Exception:
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

