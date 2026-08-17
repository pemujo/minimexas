import logging
import secrets
from functools import wraps
import requests
from django.conf import settings
from django.core.cache import cache
from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from .sheets import fetch_recommendations, sync_profile_to_google_sheet
from .auth_helpers import is_gmail_allowed
from .models import MemberProfile, MembershipRequest

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

    # 3. Check if Gmail is in allowed Google Sheet list and read Role
    allowed, member_name, is_admin = is_gmail_allowed(user_email)

    if allowed:
        # Prevent session fixation attacks
        request.session.cycle_key()

        request.session['is_verified_member'] = True
        request.session['member_email'] = user_email
        request.session['is_admin'] = is_admin

        profile, _ = MemberProfile.objects.get_or_create(
            email=user_email,
            defaults={'full_name': member_name or 'Member', 'is_admin': is_admin}
        )
        if profile.is_admin != is_admin:
            profile.is_admin = is_admin
            profile.save()

        display_name = profile.full_name or member_name or 'Member'
        request.session['member_name'] = display_name
        request.session.set_expiry(60 * 60 * 24 * 30)  # Logged in for 30 days
        return redirect('home')
    else:
        # Check if there is a pending or recent request for this email
        pending_req = MembershipRequest.objects.filter(
            email=user_email, 
            status=MembershipRequest.STATUS_PENDING
        ).first()

        if pending_req:
            error_msg = f"Your membership request for ({user_email}) was received on {pending_req.created_at.strftime('%b %d, %Y')} and is currently pending organizer approval. We will notify you once approved!"
            is_pending = True
        else:
            error_msg = f"The Gmail account ({user_email}) is not registered in our verified member directory."
            is_pending = False

        return render(request, 'recommendations/login.html', {
            'error': error_msg,
            'is_pending_request': is_pending,
            'unregistered_email': user_email,
        })


def login_page_view(request):
    if is_authenticated_member(request):
        return redirect('home')
    return render(request, 'recommendations/login.html')


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
        'events_payload': events_payload,
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
    error_message = None

    if request.method == 'POST':
        full_name = request.POST.get('full_name', '').strip()
        phone_number = request.POST.get('phone_number', '').strip()
        region = request.POST.get('region', '').strip()
        city = request.POST.get('city', '').strip()
        family_info = request.POST.get('family_info', '').strip()
        bio = request.POST.get('bio', '').strip()

        # Full name and Phone number are mandatory
        phone_digits = "".join(ch for ch in phone_number if ch.isdigit())
        if not full_name:
            error_message = "Please provide your full name."
        elif not phone_number or len(phone_digits) < 10:
            error_message = "A valid WhatsApp phone number (minimum 10 digits, e.g., +52 55 1234 5678 or +1 415 555 1234) is required."
        else:
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
        'error_message': error_message,
        'member_name': request.session.get('member_name', 'Member'),
        'member_email': member_email,
        'active_page': 'profile',
    })


def get_whatsapp_url(phone):
    """
    Constructs a direct WhatsApp messaging URL (https://wa.me/<digits>)
    from a member's phone number string.

    Supports:
    - Mexico (+52, +52 1, 52XXXXXXXXXX, etc.)
    - United States & Canada (+1, 10-digit local format)
    - International numbers (+34, +54, +57, +44, etc.)
    - Strips international exit codes (011, 00)
    """
    if not phone:
        return ""
    raw = str(phone).strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return ""

    # Remove standard international exit dialing prefixes (011 from US or 00 international)
    if digits.startswith("011"):
        digits = digits[3:]
    elif digits.startswith("00"):
        digits = digits[2:]

    # Mexico numbers (+52, +52 1, 52...)
    if digits.startswith("52"):
        return f"https://wa.me/{digits}"

    # Standard 10-digit number without country code defaults to North America (+1)
    if len(digits) == 10:
        digits = "1" + digits

    return f"https://wa.me/{digits}"


@member_required
def member_directory_view(request):
    """
    Community directory accessible to all verified members.
    Includes direct WhatsApp messaging links for connected members.
    """
    profiles = MemberProfile.objects.all().order_by('full_name', 'id')
    total_members = profiles.count()
    total_with_region = profiles.exclude(region='').count()

    # Calculate region distribution stats
    counts = MemberProfile.objects.exclude(region='').values('region').annotate(count=Count('region')).order_by('-count')
    region_dict = dict(MemberProfile.REGION_CHOICES)
    region_stats = []
    if total_with_region > 0:
        for c in counts:
            pct = round((c['count'] / total_with_region) * 100)
            region_stats.append({
                'region_key': c['region'],
                'region_label': region_dict.get(c['region'], c['region'].title()),
                'count': c['count'],
                'percentage': pct,
            })

    # Prepare member list with WhatsApp messaging links
    member_list = []
    for p in profiles:
        interests_list = [i.strip() for i in p.interests.split(',') if i.strip()] if p.interests else []
        member_list.append({
            'id': p.id,
            'full_name': p.full_name or 'Community Member',
            'region_key': p.region,
            'region_label': p.get_region_display() if p.region else '',
            'city': p.city,
            'family_info': p.family_info,
            'interests_list': interests_list,
            'interests_raw': p.interests,
            'bio': p.bio,
            'whatsapp_url': get_whatsapp_url(p.phone_number),
            'updated_at': p.updated_at,
        })

    return render(request, 'recommendations/directory.html', {
        'members': member_list,
        'total_members': total_members,
        'total_with_region': total_with_region,
        'region_stats': region_stats,
        'region_choices': MemberProfile.REGION_CHOICES,
        'member_name': request.session.get('member_name', 'Member'),
        'is_admin': request.session.get('is_admin', False),
        'active_page': 'directory',
    })


def join_request_view(request):
    """
    Public form for prospective members to request access to MiniMexitas.
    Submitted requests are placed in 'pending' status for organizer review.
    """
    if is_authenticated_member(request):
        return redirect('home')

    error_message = None
    success_submitted = False
    submitted_email = ""

    if request.method == 'POST':
        full_name = request.POST.get('full_name', '').strip()
        email = request.POST.get('email', '').strip().lower()
        phone_number = request.POST.get('phone_number', '').strip()
        region = request.POST.get('region', '').strip()
        city = request.POST.get('city', '').strip()
        referral_source = request.POST.get('referral_source', '').strip()

        # Validation
        phone_digits = "".join(ch for ch in phone_number if ch.isdigit())
        if not full_name:
            error_message = "Please enter your full name."
        elif not email or '@' not in email or '.' not in email.split('@')[-1]:
            error_message = "Please enter a valid Google / Gmail address."
        elif not phone_number or len(phone_digits) < 10:
            error_message = "Please enter a valid WhatsApp phone number (minimum 10 digits, e.g. +52 55 1234 5678 or +1 415 555 1234)."
        elif not region:
            error_message = "Please select your primary Bay Area region."
        elif not referral_source:
            error_message = "Please let us know how you heard about MiniMexitas or who referred you."
        else:
            # Check if user is already an approved member
            if MemberProfile.objects.filter(email=email).exists():
                error_message = f"An account for ({email}) is already registered! You can sign in directly using your Google account."
            else:
                # Check if there is already a pending request
                existing_req = MembershipRequest.objects.filter(email=email).order_by('-created_at').first()
                if existing_req and existing_req.status == MembershipRequest.STATUS_PENDING:
                    error_message = f"A membership request for ({email}) has already been submitted and is currently awaiting organizer review. We will contact you soon!"
                else:
                    MembershipRequest.objects.create(
                        full_name=full_name,
                        email=email,
                        phone_number=phone_number,
                        region=region,
                        city=city,
                        referral_source=referral_source,
                        status=MembershipRequest.STATUS_PENDING
                    )
                    success_submitted = True
                    submitted_email = email

    return render(request, 'recommendations/join.html', {
        'region_choices': MemberProfile.REGION_CHOICES,
        'error_message': error_message,
        'success_submitted': success_submitted,
        'submitted_email': submitted_email,
    })


@admin_required
def organizer_dashboard_view(request):
    profiles = MemberProfile.objects.all().order_by('-updated_at')
    
    total_members = profiles.count()
    total_with_region = profiles.exclude(region='').count()
    total_with_phone = profiles.exclude(phone_number='').count()

    # Pending & reviewed membership requests
    pending_requests_qs = MembershipRequest.objects.filter(status=MembershipRequest.STATUS_PENDING).order_by('-created_at')
    pending_requests = []
    for pr in pending_requests_qs:
        pending_requests.append({
            'id': pr.id,
            'full_name': pr.full_name,
            'email': pr.email,
            'phone_number': pr.phone_number,
            'region_label': pr.get_region_display() if pr.region else '',
            'city': pr.city,
            'referral_source': pr.referral_source,
            'whatsapp_url': get_whatsapp_url(pr.phone_number),
            'created_at': pr.created_at,
        })
    pending_count = len(pending_requests)

    reviewed_requests = MembershipRequest.objects.exclude(status=MembershipRequest.STATUS_PENDING).order_by('-reviewed_at')[:20]

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

    # Flash messages from query params
    action_message = None
    if request.GET.get('approved'):
        action_message = "Membership request approved successfully! Member added to directory and synced to Google Sheets."
    elif request.GET.get('declined'):
        action_message = "Membership request declined."
    elif request.GET.get('added'):
        action_message = "New member added directly and synced to Google Sheets!"

    return render(request, 'recommendations/organizers.html', {
        'profiles': profiles,
        'total_members': total_members,
        'total_with_region': total_with_region,
        'total_with_phone': total_with_phone,
        'pending_requests': pending_requests,
        'pending_count': pending_count,
        'reviewed_requests': reviewed_requests,
        'region_stats': region_stats,
        'top_region': top_region,
        'region_choices': MemberProfile.REGION_CHOICES,
        'action_message': action_message,
        'member_name': request.session.get('member_name', 'Organizer'),
        'active_page': 'organizers',
    })


@admin_required
def organizer_approve_request_view(request, request_id):
    if request.method != 'POST':
        return redirect('organizer_dashboard')

    req = get_object_or_404(MembershipRequest, id=request_id, status=MembershipRequest.STATUS_PENDING)
    reviewer = request.session.get('member_name') or request.session.get('member_email', 'Organizer')

    req.status = MembershipRequest.STATUS_APPROVED
    req.reviewed_by = reviewer
    req.reviewed_at = timezone.now()
    req.save()

    # Create / update MemberProfile
    profile, created = MemberProfile.objects.get_or_create(
        email=req.email.strip().lower(),
        defaults={
            'full_name': req.full_name,
            'phone_number': req.phone_number,
            'region': req.region,
            'city': req.city,
            'is_admin': False
        }
    )
    if not created:
        profile.full_name = req.full_name or profile.full_name
        profile.phone_number = req.phone_number or profile.phone_number
        profile.region = req.region or profile.region
        profile.city = req.city or profile.city
        profile.save()

    # Sync to Google Sheets
    try:
        sync_profile_to_google_sheet(profile)
    except Exception as e:
        logger.warning(f"Failed to sync approved member to Google Sheet: {e}")

    return redirect(reverse('organizer_dashboard') + '?approved=1')


@admin_required
def organizer_reject_request_view(request, request_id):
    if request.method != 'POST':
        return redirect('organizer_dashboard')

    req = get_object_or_404(MembershipRequest, id=request_id, status=MembershipRequest.STATUS_PENDING)
    reviewer = request.session.get('member_name') or request.session.get('member_email', 'Organizer')

    req.status = MembershipRequest.STATUS_REJECTED
    req.reviewed_by = reviewer
    req.reviewed_at = timezone.now()
    req.review_notes = request.POST.get('review_notes', '').strip()
    req.save()

    return redirect(reverse('organizer_dashboard') + '?declined=1')


@admin_required
def organizer_direct_add_member_view(request):
    if request.method != 'POST':
        return redirect('organizer_dashboard')

    full_name = request.POST.get('full_name', '').strip()
    email = request.POST.get('email', '').strip().lower()
    phone_number = request.POST.get('phone_number', '').strip()
    phone_digits = "".join(ch for ch in phone_number if ch.isdigit())
    region = request.POST.get('region', '').strip()
    city = request.POST.get('city', '').strip()
    role = request.POST.get('role', 'Member').strip()
    is_admin = (role.lower() in ('admin', 'organizer'))

    if not email or '@' not in email or not full_name or len(phone_digits) < 10:
        return redirect('organizer_dashboard')

    profile, _ = MemberProfile.objects.get_or_create(
        email=email,
        defaults={
            'full_name': full_name,
            'phone_number': phone_number,
            'region': region,
            'city': city,
            'is_admin': is_admin
        }
    )
    profile.full_name = full_name or profile.full_name
    profile.phone_number = phone_number or profile.phone_number
    profile.region = region or profile.region
    profile.city = city or profile.city
    profile.is_admin = is_admin
    profile.save()

    try:
        sync_profile_to_google_sheet(profile)
    except Exception as e:
        logger.warning(f"Failed to sync direct added member to Google Sheet: {e}")

    return redirect(reverse('organizer_dashboard') + '?added=1')




