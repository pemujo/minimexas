import logging
import secrets
from functools import wraps
import requests
from django.conf import settings
from django.core.cache import cache
from django.shortcuts import render, redirect
from django.urls import reverse
from .sheets import fetch_recommendations
from .auth_helpers import is_gmail_allowed

logger = logging.getLogger(__name__)


# --- Access Check & Decorators ---
def is_authenticated_member(request):
    return request.session.get('is_verified_member', False)


def member_required(view_func):
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not is_authenticated_member(request):
            return redirect('login_page')
        return view_func(request, *args, **kwargs)
    return _wrapped_view


def get_google_redirect_uri(request):
    """
    Returns the absolute OAuth callback URL.
    Uses settings.GOOGLE_OAUTH_REDIRECT_URI if explicitly set (e.g. for reverse proxies),
    otherwise builds the absolute URI from the current request.
    """
    explicit_uri = getattr(settings, 'GOOGLE_OAUTH_REDIRECT_URI', None)
    if explicit_uri:
        return explicit_uri
    return request.build_absolute_uri(reverse('google_callback'))


# --- Google OAuth Views ---
def gmail_login_view(request):
    """Redirects the user to Google's sign-in page with CSRF state protection."""
    state = secrets.token_urlsafe(32)
    request.session['oauth_state'] = state
    
    redirect_uri = get_google_redirect_uri(request)
    google_auth_url = (
        f"https://accounts.google.com/o/oauth2/v2/auth?"
        f"client_id={settings.GOOGLE_CLIENT_ID}&"
        f"response_type=code&"
        f"scope=openid%20email%20profile&"
        f"redirect_uri={redirect_uri}&"
        f"state={state}"
    )
    return redirect(google_auth_url)


def google_callback_view(request):
    """Handles the redirect back from Google."""
    code = request.GET.get('code')
    if not code:
        return redirect('login_page')

    # Verify CSRF state token
    expected_state = request.session.pop('oauth_state', None)
    received_state = request.GET.get('state')
    if not expected_state or expected_state != received_state:
        return render(request, 'recommendations/login.html', {
            'error': 'Authentication failed: Invalid state parameter (CSRF protection).'
        })

    redirect_uri = get_google_redirect_uri(request)

    # 1. Exchange authorization code for access token
    try:
        token_response = requests.post(
            'https://oauth2.googleapis.com/token',
            data={
                'code': code,
                'client_id': settings.GOOGLE_CLIENT_ID,
                'client_secret': settings.GOOGLE_CLIENT_SECRET,
                'redirect_uri': redirect_uri,
                'grant_type': 'authorization_code',
            },
            timeout=10
        ).json()
    except Exception as e:
        logger.error(f"Error exchanging OAuth code: {e}")
        return render(request, 'recommendations/login.html', {
            'error': 'Failed to communicate with Google authentication servers.'
        })

    access_token = token_response.get('access_token')
    if not access_token:
        error_msg = token_response.get('error_description', 'Failed to authenticate with Google.')
        return render(request, 'recommendations/login.html', {'error': error_msg})

    # 2. Get user info (email) from Google
    try:
        user_info = requests.get(
            'https://www.googleapis.com/oauth2/v2/userinfo',
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=10
        ).json()
    except Exception as e:
        logger.error(f"Error fetching Google user info: {e}")
        return render(request, 'recommendations/login.html', {
            'error': 'Failed to retrieve user profile information from Google.'
        })

    user_email = user_info.get('email')
    email_verified = user_info.get('verified_email', user_info.get('email_verified', False))

    if not user_email:
        return render(request, 'recommendations/login.html', {
            'error': 'Could not obtain email address from Google profile.'
        })

    if not email_verified:
        return render(request, 'recommendations/login.html', {
            'error': 'The Google account email is not verified by Google.'
        })

    # 3. Check if Gmail is in allowed Google Sheet list
    allowed, member_name = is_gmail_allowed(user_email)

    if allowed:
        request.session['is_verified_member'] = True
        request.session['member_name'] = member_name or 'Member'
        request.session['member_email'] = user_email
        request.session.set_expiry(60 * 60 * 24 * 30)  # Logged in for 30 days
        return redirect('home')
    else:
        error_msg = f"The Gmail account ({user_email}) is not listed as an active group member."
        return render(request, 'recommendations/login.html', {'error': error_msg})


def login_page_view(request):
    if is_authenticated_member(request):
        return redirect('home')
    return render(request, 'recommendations/login.html', {
        'is_dev_mode': settings.DEBUG
    })


def dev_login_view(request):
    """
    Developer bypass login available ONLY when DEBUG = True.
    Allows instant local development and testing without requiring Google Cloud Console OAuth setup.
    """
    if not settings.DEBUG:
        return redirect('login_page')
    
    request.session['is_verified_member'] = True
    request.session['member_name'] = request.GET.get('name', 'Developer Member')
    request.session['member_email'] = request.GET.get('email', 'dev@minimexas.local')
    request.session.set_expiry(60 * 60 * 24 * 30)
    return redirect('home')


def logout_view(request):
    request.session.flush()
    return redirect('login_page')


# --- Protected Views ---
@member_required
def home_view(request):
    return render(request, 'recommendations/home.html', {
        'member_name': request.session.get('member_name', 'Member')
    })


@member_required
def notes_list_view(request):
    notes = cache.get('whatsapp_recommendations_cache')
    error = None

    if notes is None:
        try:
            notes = fetch_recommendations()
            cache.set('whatsapp_recommendations_cache', notes, 900)
        except Exception as e:
            logger.error(f"Error loading Google Sheet: {e}", exc_info=True)
            notes = []
            error = f"Unable to load Google Sheet: {str(e)}"

    return render(request, 'recommendations/notes.html', {
        'notes': notes,
        'error': error,
        'member_name': request.session.get('member_name', 'Member')
    })


from .calendar_sync import fetch_community_events
import json


@member_required
def events_view(request):
    force_refresh = request.GET.get('refresh') == '1'
    events = fetch_community_events(force_refresh=force_refresh)

    # Prepare JSON serializable events for client-side interactive calendar navigation
    events_payload = []
    for e in events:
        events_payload.append({
            "id": e["id"],
            "title": e["title"],
            "year": e["year"],
            "month": e["month"],
            "day": e["day"],
            "date_formatted": e["date_formatted"],
            "time_formatted": e["time_formatted"],
            "location": e["location"],
            "location_url": e["location_url"],
            "category": e["category"],
            "description": e["description"],
            "organizer": e["organizer"],
            "google_calendar_link": e["google_calendar_link"],
        })

    return render(request, 'recommendations/events.html', {
        'events': events,
        'events_json': json.dumps(events_payload),
        'member_name': request.session.get('member_name', 'Member')
    })

