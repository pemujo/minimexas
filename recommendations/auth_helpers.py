import logging
from django.conf import settings
from .sheets import get_gspread_client

logger = logging.getLogger(__name__)


def is_gmail_allowed(user_email):
    """
    Checks if the signed-in Gmail address is in the 'Members' tab of the Google Sheet,
    and checks whether this member is designated as an Organizer / Admin.
    Returns: (is_allowed: bool, member_name: str, is_admin: bool)
    """
    if not user_email or not isinstance(user_email, str):
        return False, None, False

    clean_user_email = user_email.strip().lower()
    is_env_admin = clean_user_email in getattr(settings, 'ADMIN_EMAILS', [])

    try:
        gc = get_gspread_client()
        sh = gc.open("WhatsApp Recommendations")
        
        worksheet = sh.worksheet("Members")
        records = worksheet.get_all_records()
        
        for row in records:
            member_email = str(row.get('Gmail', row.get('Email', ''))).strip().lower()
            if member_email == clean_user_email:
                name = str(row.get('Name', 'Member') or 'Member').strip()
                
                # Check sheet role / admin columns (Role, Admin, IsAdmin, Type)
                role_val = str(row.get('Role', row.get('Admin', row.get('IsAdmin', row.get('Type', ''))))).strip().lower()
                is_sheet_admin = role_val in ('admin', 'organizer', 'true', 'yes', '1')
                
                is_admin = is_env_admin or is_sheet_admin
                return True, name, is_admin
                
        # If email is explicitly in ADMIN_EMAILS env setting, allow access as admin
        if is_env_admin:
            return True, "Organizer", True

        return False, None, False
    except Exception as e:
        logger.error(f"Error checking allowed Gmail list: {e}", exc_info=True)
        # Fallback to ADMIN_EMAILS in case Google Sheets is temporarily unreachable
        if is_env_admin:
            return True, "Organizer", True
        return False, None, False
