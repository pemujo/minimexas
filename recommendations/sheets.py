import os
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
    Finds a worksheet in spreadsheet `sh` matching target_name.
    Tries exact title first, then searches all worksheets case-insensitively.
    """
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
    return None


def fetch_recommendations():
    """
    Fetches rows from the Google Sheet and returns normalized dictionaries.
    """
    gc = get_gspread_client()
    sh = open_google_spreadsheet(gc)
    worksheet = getattr(sh, 'sheet1', None) or _get_worksheet_case_insensitive(sh, "Recomendaciones")
    
    raw_records = worksheet.get_all_records() if worksheet else []
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



