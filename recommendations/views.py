import logging
import secrets
import csv
from functools import wraps
import requests
from django.conf import settings
from django.core.cache import cache
from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import render, redirect
from django.urls import reverse
from .sheets import fetch_recommendations, sync_profile_to_google_sheet
from .auth_helpers import is_gmail_allowed
from .models import MemberProfile

logger = logging.getLogger(__name__)


# --- Access Check & Decorators ---
def is_authenticated_member(request):
    return request.session.get('is_verified_member', False)


def is_admin_member(request):
    return request.session.get('is_verified_member', False) and request.session.get('is_admin', False)


def member_required(view_func):
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not is_authenticated_member(request):
            return redirect('login_page')
        return view_func(request, *args, **kwargs)
    return _wrapped_view


def admin_required(view_func):
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not is_authenticated_member(request):
            return redirect('login_page')
        if not is_admin_member(request):
            return render(request, 'recommendations/login.html', {
                'error': 'Access Denied: Organizer & Admin privileges are required for this section.'
            }, status=403)
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
    allowed, member_name, is_admin = is_gmail_allowed(user_email)

    if allowed:
        request.session['is_verified_member'] = True
        request.session['member_email'] = user_email
        request.session['is_admin'] = is_admin
        profile, _ = MemberProfile.objects.get_or_create(
            email=user_email,
            defaults={'full_name': member_name or 'Member', 'is_admin': is_admin}
        )
        if is_admin and not profile.is_admin:
            profile.is_admin = True
            profile.save()
        elif profile.is_admin and not is_admin:
            # If profile has is_admin flag stored in database, respect it
            request.session['is_admin'] = True

        display_name = profile.full_name or member_name or 'Member'
        request.session['member_name'] = display_name
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
    Pass ?admin=1 to simulate an organizer/admin account, or ?admin=0 for a regular member.
    """
    if not settings.DEBUG:
        return redirect('login_page')
    
    email = request.GET.get('email', 'dev@minimexitas.local')
    name = request.GET.get('name', 'Developer Member')
    is_admin = request.GET.get('admin', '1') == '1'

    request.session['is_verified_member'] = True
    request.session['member_email'] = email
    request.session['is_admin'] = is_admin

    profile, _ = MemberProfile.objects.get_or_create(
        email=email,
        defaults={'full_name': name, 'is_admin': is_admin}
    )
    if profile.is_admin != is_admin:
        profile.is_admin = is_admin
        profile.save()

    request.session['member_name'] = profile.full_name or name
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
    all_events = fetch_community_events(force_refresh=force_refresh)

    upcoming_events = [e for e in all_events if not e.get('is_past')]
    past_events = [e for e in all_events if e.get('is_past')]

    # Sort past events descending (most recent past event first)
    past_events.sort(key=lambda x: x.get('start_datetime') or datetime.datetime.min, reverse=True)

    # Prepare JSON serializable events for client-side interactive calendar navigation
    events_payload = []
    for e in all_events:
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
            "is_past": e.get("is_past", False),
        })

    return render(request, 'recommendations/events.html', {
        'events': upcoming_events,
        'upcoming_events': upcoming_events,
        'past_events': past_events,
        'all_events': all_events,
        'events_json': json.dumps(events_payload),
        'member_name': request.session.get('member_name', 'Member')
    })


@member_required
def profile_view(request):
    member_email = request.session.get('member_email', '').strip().lower()
    member_name = request.session.get('member_name', 'Member')

    if not member_email:
        member_email = f"member_{request.session.session_key or 'default'}@minimexitas.local"

    profile, _ = MemberProfile.objects.get_or_create(
        email=member_email,
        defaults={'full_name': member_name}
    )

    success_message = None

    if request.method == 'POST':
        full_name = request.POST.get('full_name', '').strip()
        phone_number = request.POST.get('phone_number', '').strip()
        region = request.POST.get('region', '').strip()
        city = request.POST.get('city', '').strip()
        family_info = request.POST.get('family_info', '').strip()
        bio = request.POST.get('bio', '').strip()

        # Handle selected interests checkboxes
        selected_interests = request.POST.getlist('interests')
        custom_interests = request.POST.get('interests_custom', '').strip()
        if custom_interests and custom_interests not in selected_interests:
            selected_interests.append(custom_interests)
        interests_str = ", ".join(selected_interests)

        profile.full_name = full_name or profile.full_name
        profile.phone_number = phone_number
        profile.region = region
        profile.city = city
        profile.family_info = family_info
        profile.interests = interests_str
        profile.bio = bio
        profile.save()

        # Update session member name if changed
        if profile.full_name:
            request.session['member_name'] = profile.full_name

        # Securely sync profile changes to Google Sheet
        try:
            sync_profile_to_google_sheet(profile)
        except Exception as sync_err:
            logger.warning(f"Google Sheet sync warning: {sync_err}")

        success_message = "Your profile and region details have been updated successfully!"

    # Parse selected interests for checkbox states in the UI
    current_interests = [i.strip() for i in profile.interests.split(",") if i.strip()] if profile.interests else []

    # Calculate region distribution stats across all community profiles
    total_profiles = MemberProfile.objects.exclude(region='').count()
    region_stats = []
    if total_profiles > 0:
        counts = MemberProfile.objects.exclude(region='').values('region').annotate(count=Count('region')).order_by('-count')
        region_dict = dict(MemberProfile.REGION_CHOICES)
        for c in counts:
            pct = round((c['count'] / total_profiles) * 100)
            region_stats.append({
                'region_key': c['region'],
                'region_label': region_dict.get(c['region'], c['region'].title()),
                'count': c['count'],
                'percentage': pct,
            })

    return render(request, 'recommendations/profile.html', {
        'profile': profile,
        'region_choices': MemberProfile.REGION_CHOICES,
        'current_interests': current_interests,
        'region_stats': region_stats,
        'total_region_responses': total_profiles,
        'success_message': success_message,
        'member_name': request.session.get('member_name', 'Member'),
        'member_email': member_email,
        'active_page': 'profile',
    })


@admin_required
def organizer_dashboard_view(request):
    profiles = MemberProfile.objects.all().order_by('-updated_at')
    
    total_members = profiles.count()
    total_with_region = profiles.exclude(region='').count()
    total_with_phone = profiles.exclude(phone_number='').count()

    # Calculate region distribution stats across all registered members
    counts = MemberProfile.objects.exclude(region='').values('region').annotate(count=Count('region')).order_by('-count')
    region_dict = dict(MemberProfile.REGION_CHOICES)
    region_stats = []
    top_region = "None yet"
    if total_with_region > 0:
        for idx, c in enumerate(counts):
            pct = round((c['count'] / total_with_region) * 100)
            label = region_dict.get(c['region'], c['region'].title())
            if idx == 0:
                top_region = f"{label} ({pct}%)"
            region_stats.append({
                'region_key': c['region'],
                'region_label': label,
                'count': c['count'],
                'percentage': pct,
            })

    return render(request, 'recommendations/organizers.html', {
        'profiles': profiles,
        'total_members': total_members,
        'total_with_region': total_with_region,
        'total_with_phone': total_with_phone,
        'region_stats': region_stats,
        'top_region': top_region,
        'region_choices': MemberProfile.REGION_CHOICES,
        'member_name': request.session.get('member_name', 'Organizer'),
        'active_page': 'organizers',
    })


@admin_required
def export_members_csv_view(request):
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="minimexitas_members_directory.csv"'

    writer = csv.writer(response)
    writer.writerow(['Full Name', 'Gmail', 'Region', 'City / Neighborhood', 'Phone / WhatsApp', 'Family & Kids', 'Interests', 'Bio', 'Role', 'Last Updated'])

    for p in MemberProfile.objects.all().order_by('full_name', 'email'):
        writer.writerow([
            p.full_name,
            p.email,
            p.get_region_display() if p.region else 'Not set',
            p.city,
            p.phone_number,
            p.family_info,
            p.interests,
            p.bio,
            'Organizer / Admin' if p.is_admin else 'Member',
            p.updated_at.strftime('%Y-%m-%d %H:%M') if p.updated_at else ''
        ])

    return response



