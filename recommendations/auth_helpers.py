import logging
from django.conf import settings
from .sheets import get_gspread_client

logger = logging.getLogger(__name__)


def is_gmail_allowed(user_email):
    """
    Checks if the signed-in Gmail address is in the 'Members' tab of your Google Sheet.
    """
    if not user_email or not isinstance(user_email, str):
        return False, None

    try:
        gc = get_gspread_client()
        sh = gc.open("WhatsApp Recommendations")
        
        worksheet = sh.worksheet("Members")
        records = worksheet.get_all_records()
        
        clean_user_email = user_email.strip().lower()
        
        for row in records:
            member_email = str(row.get('Gmail', row.get('Email', ''))).strip().lower()
            if member_email == clean_user_email:
                name = row.get('Name', 'Member') or 'Member'
                return True, str(name).strip()
                
        return False, None
    except Exception as e:
        logger.error(f"Error checking allowed Gmail list: {e}", exc_info=True)
        return False, None
