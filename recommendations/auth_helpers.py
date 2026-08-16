import logging
from django.conf import settings
from .sheets import get_gspread_client

logger = logging.getLogger(__name__)


def is_gmail_allowed(user_email):
    """
    Checks if the signed-in Gmail address is in the 'Members' tab of the Google Sheet,
    and checks whether this member is designated as an Organizer / Admin strictly in the Sheet.
    Returns: (is_allowed: bool, member_name: str, is_admin: bool)
    """
    if not user_email or not isinstance(user_email, str):
        return False, None, False

    clean_user_email = user_email.strip().lower()

    try:
        gc = get_gspread_client()
        sh = gc.open("WhatsApp Recommendations")
        
        worksheet = sh.worksheet("Members")
        records = worksheet.get_all_records()
        
        for row in records:
            member_email = str(row.get('Gmail', row.get('Email', ''))).strip().lower()
            if member_email == clean_user_email:
                name = str(row.get('Name', 'Member') or 'Member').strip()
                
                # Check sheet role / admin columns (Role, Admin, IsAdmin, Type) strictly from Google Sheet
                role_val = str(row.get('Role', row.get('Admin', row.get('IsAdmin', row.get('Type', ''))))).strip().lower()
                is_sheet_admin = role_val in ('admin', 'organizer', 'true', 'yes', '1')
                
                return True, name, is_sheet_admin

        return False, None, False
    except Exception as e:
        logger.error(f"Error checking allowed Gmail list: {e}", exc_info=True)
        return False, None, False
