import logging
import secrets
from functools import wraps
import requests
from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from urllib.parse import quote_plus
from django.urls import reverse
from django.utils import timezone
from .sheets import (
    fetch_recommendations,
    sync_profile_to_google_sheet,
    sync_pending_request_to_google_sheet,
    update_pending_request_status_in_google_sheet,
    sync_audit_log_to_google_sheet,
    sync_survey_to_google_sheet,
    delete_survey_from_google_sheet,
    sync_event_rsvp_to_google_sheet,
    sync_community_event_to_google_sheet,
    delete_community_event_from_google_sheet,
    delete_profile_from_google_sheet,
    reconcile_members_with_google_sheet,
)
from .auth_helpers import is_gmail_allowed
from .models import (
    MemberProfile,
    MembershipRequest,
    MembershipAuditLog,
    CommunitySurvey,
    SurveyOption,
    SurveyVote,
    CommunityEvent,
    CommunityEventRSVP,
    CommunityEventBroadcast,
)
from .calendar_sync import get_google_calendar_add_url, CACHE_KEY_EVENTS
from .notifications import (
    send_membership_approval_email,
    send_membership_rejection_email,
    send_admin_new_request_notification,
    send_event_broadcast_email,
    verify_event_rsvp_token,
    generate_event_rsvp_token,
    build_whatsapp_approval_link,
    build_whatsapp_decline_link,
)

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
        
        # Verify member profile still exists in database (invalidates session if purged by Google Sheet reconciliation)
        user_email = request.session.get('member_email', '').strip().lower()
        if user_email and not MemberProfile.objects.filter(email__iexact=user_email).exists():
            request.session.flush()
            return render(request, 'recommendations/login.html', {
                'error': 'Your access was revoked because your account is no longer registered in the member directory.'
            }, status=403)

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
        # Check if there is a pending or rejected request for this email
        latest_req = MembershipRequest.objects.filter(email=user_email).order_by('-created_at').first()

        is_pending = False
        is_rejected = False
        rejection_reason = ""

        if latest_req and latest_req.status == MembershipRequest.STATUS_PENDING:
            error_msg = f"Your membership request for ({user_email}) was received on {latest_req.created_at.strftime('%b %d, %Y')} and is currently pending organizer review. We will notify you once approved!"
            is_pending = True
        elif latest_req and latest_req.status == MembershipRequest.STATUS_REJECTED:
            rejection_reason = latest_req.review_notes.strip() if latest_req.review_notes else "Application details could not be verified."
            error_msg = f"We are sorry, but your membership request for ({user_email}) could not be approved at this time."
            is_rejected = True
        else:
            error_msg = f"The Gmail account ({user_email}) is not registered in our verified member directory."

        return render(request, 'recommendations/login.html', {
            'error': error_msg,
            'is_pending_request': is_pending,
            'is_rejected_request': is_rejected,
            'rejection_reason': rejection_reason,
            'unregistered_email': user_email,
        })


def login_page_view(request):
    if is_authenticated_member(request):
        return redirect('home')

    account_deleted = request.session.pop('account_deleted_flash', False)
    account_deleted_email = request.session.pop('account_deleted_email', None)

    return render(request, 'recommendations/login.html', {
        'account_deleted': account_deleted,
        'account_deleted_email': account_deleted_email,
    })


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
import datetime


@member_required
def events_view(request):
    force_refresh = request.GET.get('refresh') == '1'
    all_events = fetch_community_events(force_refresh=force_refresh)

    member_email = request.session.get('member_email', '').strip().lower()
    member_name = request.session.get('member_name', 'Member')
    is_admin = request.session.get('is_admin', False)

    # Fetch all RSVPs and Broadcast records from database
    all_rsvps = list(CommunityEventRSVP.objects.all())
    broadcast_map = {b.event_id: b for b in CommunityEventBroadcast.objects.all()}
    total_members_count = MemberProfile.objects.exclude(email='').count()

    # Organize RSVPs by event_id
    rsvps_by_event = {}
    for r in all_rsvps:
        rsvps_by_event.setdefault(str(r.event_id), []).append(r)

    # Enrich all events with RSVP & broadcast metadata
    for e in all_events:
        e_id = str(e.get('id', ''))
        e_rsvps = rsvps_by_event.get(e_id, [])
        going_list = [r for r in e_rsvps if r.status == CommunityEventRSVP.STATUS_GOING]
        maybe_list = [r for r in e_rsvps if r.status == CommunityEventRSVP.STATUS_MAYBE]
        declined_list = [r for r in e_rsvps if r.status == CommunityEventRSVP.STATUS_DECLINED]

        my_rsvp_obj = next((r for r in e_rsvps if r.member_email.lower() == member_email), None)
        broadcast_obj = broadcast_map.get(e_id)

        e['going_count'] = len(going_list)
        e['maybe_count'] = len(maybe_list)
        e['declined_count'] = len(declined_list)
        e['total_rsvps'] = len(e_rsvps)
        e['attendees'] = [r.member_name for r in going_list if r.member_name]
        e['my_rsvp'] = my_rsvp_obj.status if my_rsvp_obj else None
        e['my_rsvp_notes'] = my_rsvp_obj.notes if my_rsvp_obj else ''
        e['broadcast_info'] = broadcast_obj
        e['is_broadcasted'] = broadcast_obj is not None

    upcoming_events = [e for e in all_events if not e.get('is_past')]
    past_events = [e for e in all_events if e.get('is_past')]

    # Sort past events descending (most recent past event first)
    past_events.sort(key=lambda x: x.get('start_datetime') or datetime.datetime.min, reverse=True)

    # Prepare JSON serializable events for client-side interactive calendar navigation
    events_payload = []
    for e in all_events:
        start_dt = e.get("start_datetime")
        end_dt = e.get("end_datetime")
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
            "going_count": e.get("going_count", 0),
            "my_rsvp": e.get("my_rsvp"),
            "is_broadcasted": e.get("is_broadcasted", False),
            "is_portal_event": e.get("is_portal_event", False),
            "portal_event_pk": e.get("portal_event_pk"),
            "source": e.get("source", "portal" if e.get("is_portal_event") else "google_calendar"),
            "start_date_input": start_dt.strftime("%Y-%m-%d") if hasattr(start_dt, 'strftime') else "",
            "start_time_input": start_dt.strftime("%H:%M") if hasattr(start_dt, 'strftime') else "",
            "end_date_input": end_dt.strftime("%Y-%m-%d") if hasattr(end_dt, 'strftime') else "",
            "end_time_input": end_dt.strftime("%H:%M") if hasattr(end_dt, 'strftime') else "",
        })

    # Flash / status messages
    rsvp_msg = request.session.pop('rsvp_flash_message', None)
    broadcast_msg = request.session.pop('broadcast_flash_message', None)
    event_flash_msg = request.session.pop('event_flash_message', None)
    event_error_msg = request.session.pop('event_error_message', None)

    return render(request, 'recommendations/events.html', {
        'events': upcoming_events,
        'upcoming_events': upcoming_events,
        'past_events': past_events,
        'all_events': all_events,
        'events_payload': events_payload,
        'events_json': json.dumps(events_payload),
        'member_name': member_name,
        'member_email': member_email,
        'is_admin': is_admin,
        'total_members_count': total_members_count,
        'rsvp_flash_message': rsvp_msg,
        'broadcast_flash_message': broadcast_msg,
        'event_flash_message': event_flash_msg,
        'event_error_message': event_error_msg,
    })


def event_rsvp_view(request):
    """
    Handles RSVP submissions from both in-app UI buttons/modals and 1-click signed email links.
    Dual-syncs response to Google Sheet 'Event_RSVPs' tab.
    """
    token = request.GET.get('token')

    # Path 1: 1-Click RSVP token from email link
    if token:
        payload = verify_event_rsvp_token(token)
        if not payload:
            return render(request, 'recommendations/login.html', {
                'error': 'The RSVP link is invalid or has expired. Please log in to view and respond to the event.'
            })

        event_id = payload.get('event_id')
        member_email = payload.get('email', '').strip().lower()
        status = payload.get('status', 'going').strip().lower()

        # Find member name from MemberProfile
        profile = MemberProfile.objects.filter(email__iexact=member_email).first()
        member_name = profile.full_name if profile else member_email.split('@')[0].capitalize()

        # Try to resolve event title from calendar cache/fetch
        all_events = fetch_community_events()
        event_match = next((e for e in all_events if str(e.get('id')) == str(event_id)), None)
        event_title = event_match.get('title', 'Community Event') if event_match else ''

        rsvp_obj, created = CommunityEventRSVP.objects.update_or_create(
            event_id=event_id,
            member_email=member_email,
            defaults={
                'event_title': event_title,
                'member_name': member_name,
                'status': status,
            }
        )

        # Dual-sync to Google Sheet
        try:
            sync_event_rsvp_to_google_sheet(rsvp_obj)
        except Exception as e:
            logger.warning(f"Failed to sync RSVP to Google Sheet: {e}")

        # Automatically log user into member session if profile exists
        if profile:
            request.session['is_verified_member'] = True
            request.session['member_email'] = profile.email
            request.session['member_name'] = profile.full_name
            request.session['is_admin'] = profile.is_admin

        status_text = {
            'going': '¡Confirmado! Asistirás al evento.',
            'maybe': 'Respuesta registrada: Tal vez asistas.',
            'declined': 'Respuesta registrada: No podrás asistir.',
        }.get(status, f'Respuesta registrada ({status}).')

        request.session['rsvp_flash_message'] = f"🎟️ {status_text} ({event_title or 'Evento'})"
        return redirect('events')

    # Path 2: In-app Authenticated RSVP POST
    if request.method == 'POST':
        if not is_authenticated_member(request):
            if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.POST.get('ajax') == '1':
                return JsonResponse({'success': False, 'error': 'Authentication required'}, status=401)
            return redirect('login_page')

        member_email = request.session.get('member_email', '').strip().lower()
        member_name = request.session.get('member_name', 'Member')

        event_id = request.POST.get('event_id', '').strip()
        event_title = request.POST.get('event_title', '').strip()
        status = request.POST.get('status', 'going').strip().lower()
        notes = request.POST.get('notes', '').strip()

        if not event_id or status not in [CommunityEventRSVP.STATUS_GOING, CommunityEventRSVP.STATUS_MAYBE, CommunityEventRSVP.STATUS_DECLINED]:
            if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.POST.get('ajax') == '1':
                return JsonResponse({'success': False, 'error': 'Invalid event or status'}, status=400)
            return redirect('events')

        rsvp_obj, created = CommunityEventRSVP.objects.update_or_create(
            event_id=event_id,
            member_email=member_email,
            defaults={
                'event_title': event_title,
                'member_name': member_name,
                'status': status,
                'notes': notes,
            }
        )

        # Dual-sync to Google Sheet
        try:
            sync_event_rsvp_to_google_sheet(rsvp_obj)
        except Exception as e:
            logger.warning(f"Failed to sync RSVP to Google Sheet: {e}")

        # Compute updated stats for AJAX response
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.POST.get('ajax') == '1':
            event_rsvps = CommunityEventRSVP.objects.filter(event_id=event_id)
            going_list = [r.member_name for r in event_rsvps if r.status == CommunityEventRSVP.STATUS_GOING and r.member_name]
            return JsonResponse({
                'success': True,
                'status': status,
                'status_display': rsvp_obj.get_status_display(),
                'going_count': len(going_list),
                'maybe_count': event_rsvps.filter(status=CommunityEventRSVP.STATUS_MAYBE).count(),
                'declined_count': event_rsvps.filter(status=CommunityEventRSVP.STATUS_DECLINED).count(),
                'attendees': going_list,
            })

        status_text = {
            'going': '¡Confirmado! Asistirás al evento.',
            'maybe': 'Respuesta registrada: Tal vez asistas.',
            'declined': 'Respuesta registrada: No podrás asistir.',
        }.get(status, f'Respuesta registrada ({status}).')

        request.session['rsvp_flash_message'] = f"🎟️ {status_text} ({event_title or 'Evento'})"
        return redirect('events')

    return redirect('events')


@admin_required
def organizer_event_broadcast_view(request):
    """
    Allows organizers to broadcast an email announcement to all registered community members
    for any community event, with 1-click RSVP action links and 'Add to Calendar' links.
    Records an immutable entry in MembershipAuditLog and dual-syncs to Google Sheet.
    """
    if request.method != 'POST':
        return redirect('events')

    admin_name = request.session.get('member_name', 'Organizer')
    admin_email = request.session.get('member_email', '').strip().lower()

    event_id = request.POST.get('event_id', '').strip()
    event_title = request.POST.get('event_title', 'Evento Comunitario').strip()
    date_formatted = request.POST.get('date_formatted', '').strip()
    time_formatted = request.POST.get('time_formatted', '').strip()
    location = request.POST.get('location', '').strip()
    location_url = request.POST.get('location_url', '').strip()
    category = request.POST.get('category', 'Comunidad').strip()
    description = request.POST.get('description', '').strip()
    gcal_link = request.POST.get('google_calendar_link', '').strip()

    if not event_id:
        return redirect('events')

    event_data = {
        'id': event_id,
        'title': event_title,
        'date_formatted': date_formatted,
        'time_formatted': time_formatted,
        'location': location,
        'location_url': location_url,
        'category': category,
        'description': description,
        'google_calendar_link': gcal_link,
    }

    sent_count = send_event_broadcast_email(
        event_data=event_data,
        broadcast_by_name=admin_name,
        broadcast_by_email=admin_email,
        request=request
    )

    # Record / Update Broadcast record
    CommunityEventBroadcast.objects.update_or_create(
        event_id=event_id,
        defaults={
            'event_title': event_title,
            'broadcast_by': admin_name,
            'broadcast_by_email': admin_email,
            'recipient_count': sent_count,
        }
    )

    # Create immutable audit log entry
    audit_entry = MembershipAuditLog.objects.create(
        action=MembershipAuditLog.ACTION_EVENT_BROADCAST,
        target_name=f"Event: {event_title}",
        target_email=f"broadcast_{event_id[:30]}",
        actor_name=admin_name,
        actor_email=admin_email,
        notes=f"Broadcasted event notification to {sent_count} active community members with 1-click RSVP."
    )

    # Dual-sync audit log to Google Sheet
    try:
        sync_audit_log_to_google_sheet(audit_entry)
    except Exception as e:
        logger.warning(f"Failed to dual-sync event broadcast audit log to Google Sheet: {e}")

    request.session['broadcast_flash_message'] = f"📢 ¡Notificación del evento '{event_title}' enviada exitosamente a {sent_count} miembros registrados!"
    return redirect('events')


@admin_required
def organizer_create_event_view(request):
    """
    Allows admins/organizers to create a new community event directly in the portal.
    Dual-syncs to Google Sheet 'Events' tab, logs to MembershipAuditLog,
    and optionally sends email broadcast to all members immediately.
    """
    if request.method != 'POST':
        return redirect('events')

    title = request.POST.get('title', '').strip()
    category = request.POST.get('category', 'Community Gathering').strip()
    start_date = request.POST.get('start_date', '').strip()
    start_time = request.POST.get('start_time', '').strip()
    end_date = request.POST.get('end_date', '').strip()
    end_time = request.POST.get('end_time', '').strip()
    location = request.POST.get('location', '').strip() or 'San Francisco Bay Area'
    location_url = request.POST.get('location_url', '').strip()
    description = request.POST.get('description', '').strip()
    broadcast_now = request.POST.get('broadcast_now') in ('on', 'true', '1', True)

    if not title or not start_date or not start_time:
        request.session['event_error_message'] = "Please provide event title, date, and start time."
        return redirect('events')

    admin_name = request.session.get('member_name', 'Organizer')
    admin_email = request.session.get('member_email', '').strip().lower()

    # Parse start and end datetimes
    try:
        start_str = f"{start_date} {start_time}"
        naive_start = datetime.datetime.strptime(start_str, "%Y-%m-%d %H:%M")
        start_dt = timezone.make_aware(naive_start) if timezone.is_naive(naive_start) else naive_start
    except Exception as e:
        logger.warning(f"Error parsing start datetime: {e}")
        request.session['event_error_message'] = "Invalid start date or time format."
        return redirect('events')

    end_dt = None
    if end_date and end_time:
        try:
            end_str = f"{end_date} {end_time}"
            naive_end = datetime.datetime.strptime(end_str, "%Y-%m-%d %H:%M")
            end_dt = timezone.make_aware(naive_end) if timezone.is_naive(naive_end) else naive_end
        except Exception:
            end_dt = start_dt + datetime.timedelta(hours=2)
    elif end_time:
        try:
            end_str = f"{start_date} {end_time}"
            naive_end = datetime.datetime.strptime(end_str, "%Y-%m-%d %H:%M")
            end_dt = timezone.make_aware(naive_end) if timezone.is_naive(naive_end) else naive_end
        except Exception:
            end_dt = start_dt + datetime.timedelta(hours=2)
    else:
        end_dt = start_dt + datetime.timedelta(hours=2)

    event = CommunityEvent.objects.create(
        title=title,
        category=category,
        start_datetime=start_dt,
        end_datetime=end_dt,
        location=location,
        location_url=location_url,
        description=description,
        created_by_name=admin_name,
        created_by_email=admin_email,
        is_active=True
    )

    # Invalidate events cache
    cache.delete(CACHE_KEY_EVENTS)

    # Audit log
    audit_entry = MembershipAuditLog.objects.create(
        action=MembershipAuditLog.ACTION_EVENT_CREATED,
        target_name=f"Event: {event.title}",
        target_email=f"portal_evt_{event.id}",
        actor_name=admin_name,
        actor_email=admin_email,
        notes=f"Created in-portal community event '{event.title}' scheduled for {start_dt.strftime('%Y-%m-%d %H:%M')}."
    )

    try:
        sync_audit_log_to_google_sheet(audit_entry)
    except Exception as e:
        logger.warning(f"Failed to dual-sync event creation audit log: {e}")

    try:
        sync_community_event_to_google_sheet(event)
    except Exception as e:
        logger.warning(f"Failed to dual-sync new event to Google Sheet: {e}")

    # If immediate broadcast requested
    if broadcast_now:
        event_data = {
            'id': f"portal_{event.id}",
            'title': event.title,
            'date_formatted': start_dt.strftime("%A, %B %d, %Y"),
            'time_formatted': f"{start_dt.strftime('%I:%M %p').lstrip('0')} - {end_dt.strftime('%I:%M %p').lstrip('0')}",
            'location': event.location,
            'location_url': event.location_url or f"https://www.google.com/maps/search/?api=1&query={quote_plus(event.location)}",
            'category': event.category,
            'description': event.description,
            'google_calendar_link': get_google_calendar_add_url(event.title, start_dt, end_dt, event.description, event.location),
        }
        sent_count = send_event_broadcast_email(
            event_data=event_data,
            broadcast_by_name=admin_name,
            broadcast_by_email=admin_email,
            request=request
        )
        CommunityEventBroadcast.objects.update_or_create(
            event_id=f"portal_{event.id}",
            defaults={
                'event_title': event.title,
                'broadcast_by': admin_name,
                'broadcast_by_email': admin_email,
                'recipient_count': sent_count,
            }
        )
        request.session['event_flash_message'] = f"🎉 ¡Evento '{event.title}' creado exitosamente y notificado a {sent_count} miembros!"
    else:
        request.session['event_flash_message'] = f"🎉 ¡Evento '{event.title}' creado exitosamente!"

    return redirect('events')


@admin_required
def organizer_edit_event_view(request, event_id):
    """
    Allows admins/organizers to edit an existing in-portal event.
    """
    if request.method != 'POST':
        return redirect('events')

    event = get_object_or_404(CommunityEvent, id=event_id)

    title = request.POST.get('title', '').strip()
    category = request.POST.get('category', event.category).strip()
    start_date = request.POST.get('start_date', '').strip()
    start_time = request.POST.get('start_time', '').strip()
    end_date = request.POST.get('end_date', '').strip()
    end_time = request.POST.get('end_time', '').strip()
    location = request.POST.get('location', '').strip()
    location_url = request.POST.get('location_url', '').strip()
    description = request.POST.get('description', '').strip()

    if title:
        event.title = title
    if category:
        event.category = category
    if location:
        event.location = location
    event.location_url = location_url
    event.description = description

    if start_date and start_time:
        try:
            start_str = f"{start_date} {start_time}"
            naive_start = datetime.datetime.strptime(start_str, "%Y-%m-%d %H:%M")
            event.start_datetime = timezone.make_aware(naive_start) if timezone.is_naive(naive_start) else naive_start
        except Exception as e:
            logger.warning(f"Error parsing edit start datetime: {e}")

    if end_date and end_time:
        try:
            end_str = f"{end_date} {end_time}"
            naive_end = datetime.datetime.strptime(end_str, "%Y-%m-%d %H:%M")
            event.end_datetime = timezone.make_aware(naive_end) if timezone.is_naive(naive_end) else naive_end
        except Exception:
            pass
    elif end_time and start_date:
        try:
            end_str = f"{start_date} {end_time}"
            naive_end = datetime.datetime.strptime(end_str, "%Y-%m-%d %H:%M")
            event.end_datetime = timezone.make_aware(naive_end) if timezone.is_naive(naive_end) else naive_end
        except Exception:
            pass

    event.save()
    cache.delete(CACHE_KEY_EVENTS)

    admin_name = request.session.get('member_name', 'Organizer')
    admin_email = request.session.get('member_email', '').strip().lower()

    # Audit log
    audit_entry = MembershipAuditLog.objects.create(
        action=MembershipAuditLog.ACTION_EVENT_UPDATED,
        target_name=f"Event: {event.title}",
        target_email=f"portal_evt_{event.id}",
        actor_name=admin_name,
        actor_email=admin_email,
        notes=f"Updated in-portal community event '{event.title}'."
    )

    try:
        sync_audit_log_to_google_sheet(audit_entry)
    except Exception as e:
        logger.warning(f"Failed to dual-sync event update audit log: {e}")

    try:
        sync_community_event_to_google_sheet(event)
    except Exception as e:
        logger.warning(f"Failed to dual-sync updated event to Google Sheet: {e}")

    request.session['event_flash_message'] = f"✏️ Evento '{event.title}' actualizado exitosamente."
    return redirect('events')


@admin_required
def organizer_delete_event_view(request, event_id):
    """
    Allows admins/organizers to delete/deactivate an in-portal event.
    """
    if request.method != 'POST':
        return redirect('events')

    event = get_object_or_404(CommunityEvent, id=event_id)
    event_title = event.title
    event.is_active = False
    event.save()
    cache.delete(CACHE_KEY_EVENTS)

    admin_name = request.session.get('member_name', 'Organizer')
    admin_email = request.session.get('member_email', '').strip().lower()

    # Audit log
    audit_entry = MembershipAuditLog.objects.create(
        action=MembershipAuditLog.ACTION_EVENT_DELETED,
        target_name=f"Event: {event_title}",
        target_email=f"portal_evt_{event.id}",
        actor_name=admin_name,
        actor_email=admin_email,
        notes=f"Deactivated/deleted in-portal community event '{event_title}'."
    )

    try:
        sync_audit_log_to_google_sheet(audit_entry)
    except Exception as e:
        logger.warning(f"Failed to dual-sync event deletion audit log: {e}")

    try:
        delete_community_event_from_google_sheet(event.id)
    except Exception as e:
        logger.warning(f"Failed to dual-sync event deletion to Google Sheet: {e}")

    request.session['event_flash_message'] = f"🗑️ Evento '{event_title}' eliminado exitosamente."
    return redirect('events')


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


@member_required
def profile_delete_self_view(request):
    """
    Self-service account and profile deletion for authenticated members.
    Deletes the member's profile and membership requests from SQLite,
    removes their row from the Google Sheet 'Members' tab, flushes the session,
    and redirects them to the login screen with confirmation.
    """
    if request.method != 'POST':
        return redirect('profile')

    user_email = request.session.get('member_email', '').strip().lower()
    user_name = request.session.get('member_name', '')

    if user_email:
        # Audit log before deletion
        audit_entry = MembershipAuditLog.objects.create(
            action=MembershipAuditLog.ACTION_DELETED,
            target_email=user_email,
            target_name=user_name,
            actor_name=user_name or "Self",
            actor_email=user_email,
            notes="Account self-deleted by member."
        )
        try:
            sync_audit_log_to_google_sheet(audit_entry)
        except Exception as e:
            logger.warning(f"Failed to sync audit log for self-removal ({user_email}) to Google Sheet: {e}")

        # Delete profile and requests from database
        MemberProfile.objects.filter(email__iexact=user_email).delete()
        MembershipRequest.objects.filter(email__iexact=user_email).delete()

        # Remove member row from Google Sheets 'Members' tab
        try:
            delete_profile_from_google_sheet(user_email)
        except Exception as e:
            logger.warning(f"Failed to delete profile from Google Sheet for self-removal ({user_email}): {e}")

    # Flush active session
    request.session.flush()
    request.session['account_deleted_flash'] = True
    request.session['account_deleted_email'] = user_email

    return redirect('login_page')


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
                    req_obj = MembershipRequest.objects.create(
                        full_name=full_name,
                        email=email,
                        phone_number=phone_number,
                        region=region,
                        city=city,
                        referral_source=referral_source,
                        status=MembershipRequest.STATUS_PENDING
                    )

                    # Log to MembershipAuditLog
                    audit_entry = MembershipAuditLog.objects.create(
                        action=MembershipAuditLog.ACTION_SUBMITTED,
                        target_email=email,
                        target_name=full_name,
                        actor_name=full_name,
                        actor_email=email,
                        notes=f"Referral: {referral_source} | Phone: {phone_number} | Region: {region} {f'({city})' if city else ''}".strip()
                    )
                    try:
                        sync_audit_log_to_google_sheet(audit_entry)
                    except Exception as e:
                        logger.warning(f"Failed to sync submission audit log for {email} to Google Sheet: {e}")

                    # Send notification email to all admins
                    try:
                        send_admin_new_request_notification(req_obj, request=request)
                    except Exception as e:
                        logger.warning(f"Failed to dispatch admin notification email for new request ({email}): {e}")

                    # Dual-sync pending request to Google Sheets for zero-data-loss protection
                    try:
                        sync_pending_request_to_google_sheet(req_obj)
                    except Exception as e:
                        logger.warning(f"Failed to dual-sync pending request for {email} to Google Sheet: {e}")

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
    login_url = request.build_absolute_uri(reverse('login_page'))
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
            'whatsapp_approval_url': build_whatsapp_approval_link(pr.full_name, pr.phone_number, portal_url=login_url),
            'created_at': pr.created_at,
        })
    pending_count = len(pending_requests)

    reviewed_requests = MembershipRequest.objects.exclude(status=MembershipRequest.STATUS_PENDING).order_by('-reviewed_at')[:20]
    audit_logs = MembershipAuditLog.objects.all().order_by('-created_at')[:50]

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

    # Flash messages from query params and session
    action_message = None
    action_error = None
    approved_data = None
    if request.GET.get('approved'):
        action_message = "Membership request approved successfully! Member added to directory and synced to Google Sheets."
        approved_name = request.session.pop('approved_flash_name', None)
        approved_email = request.session.pop('approved_flash_email', None)
        approved_wa_link = request.session.pop('approved_flash_wa_link', None)
        if approved_name or approved_email:
            approved_data = {
                'name': approved_name,
                'email': approved_email,
                'wa_link': approved_wa_link,
            }
    elif request.GET.get('declined'):
        declined_name = request.session.pop('declined_flash_name', None)
        declined_email = request.session.pop('declined_flash_email', None)
        if declined_name or declined_email:
            action_message = f"Membership request for {declined_name or declined_email} was declined. An email notification with the explanation has been dispatched to {declined_email}."
        else:
            action_message = "Membership request declined and notification email dispatched."
    elif request.GET.get('added'):
        action_message = "New member added directly and synced to Google Sheets!"
    elif request.GET.get('member_deleted'):
        deleted_name = request.session.pop('deleted_member_name', None)
        if deleted_name:
            action_message = f"Member '{deleted_name}' has been successfully removed from the community and Google Sheets."
        else:
            action_message = "Member has been successfully removed from the community and Google Sheets."
    elif request.GET.get('synced'):
        sync_data = request.session.pop('sync_flash_result', None)
        if sync_data:
            purged = sync_data.get('purged_count', 0)
            created = sync_data.get('created_count', 0)
            updated = sync_data.get('updated_count', 0)
            total = sync_data.get('total_sheet_members', 0)
            action_message = (
                f"Google Sheets Sync Complete: {purged} purged (no longer in spreadsheet), "
                f"{created} new members imported, {updated} updated ({total} total in spreadsheet)."
            )
        else:
            action_message = "Google Sheets reconciliation sync completed successfully."
    elif request.GET.get('sync_error'):
        action_error = request.session.pop('sync_flash_error', "Failed to synchronize with Google Sheets.")
    elif request.GET.get('error') == 'self_delete_forbidden':
        action_error = "For safety, organizers cannot delete themselves from the organizer dashboard. Use your Profile page if you wish to leave the community."

    return render(request, 'recommendations/organizers.html', {
        'profiles': profiles,
        'total_members': total_members,
        'total_with_region': total_with_region,
        'total_with_phone': total_with_phone,
        'pending_requests': pending_requests,
        'pending_count': pending_count,
        'reviewed_requests': reviewed_requests,
        'audit_logs': audit_logs,
        'region_stats': region_stats,
        'top_region': top_region,
        'region_choices': MemberProfile.REGION_CHOICES,
        'action_message': action_message,
        'action_error': action_error,
        'approved_data': approved_data,
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

    # Send confirmation approval email notification
    try:
        send_membership_approval_email(req, request=request)
    except Exception as e:
        logger.warning(f"Failed to dispatch approval notification email for {req.email}: {e}")

    # Audit Log Entry
    admin_email = request.session.get('member_email', '')
    audit_entry = MembershipAuditLog.objects.create(
        action=MembershipAuditLog.ACTION_APPROVED,
        target_email=req.email,
        target_name=req.full_name,
        actor_name=reviewer,
        actor_email=admin_email,
        notes="Membership request approved."
    )
    try:
        sync_audit_log_to_google_sheet(audit_entry)
    except Exception as e:
        logger.warning(f"Failed to sync audit log for approval ({req.email}) to Google Sheet: {e}")

    # Set session flash data for confirmation alert & direct WhatsApp button
    request.session['approved_flash_name'] = req.full_name or req.email
    request.session['approved_flash_email'] = req.email
    if req.phone_number:
        request.session['approved_flash_wa_link'] = build_whatsapp_approval_link(
            req.full_name, req.phone_number,
            portal_url=request.build_absolute_uri(reverse('login_page'))
        )

    # Sync to Members tab in Google Sheets
    try:
        sync_profile_to_google_sheet(profile)
    except Exception as e:
        logger.warning(f"Failed to sync approved member to Google Sheet: {e}")

    # Update Pending_Requests tab in Google Sheets
    try:
        update_pending_request_status_in_google_sheet(req.email, 'Approved', reviewer)
    except Exception as e:
        logger.warning(f"Failed to update pending request status in Google Sheet: {e}")

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

    # Send automated rejection notification email with the explanation/notes
    try:
        send_membership_rejection_email(req, reason=req.review_notes, request=request)
    except Exception as e:
        logger.warning(f"Failed to dispatch rejection email to {req.email}: {e}")

    # Audit Log Entry
    admin_email = request.session.get('member_email', '')
    audit_entry = MembershipAuditLog.objects.create(
        action=MembershipAuditLog.ACTION_REJECTED,
        target_email=req.email,
        target_name=req.full_name,
        actor_name=reviewer,
        actor_email=admin_email,
        notes=req.review_notes or "Membership request declined."
    )
    try:
        sync_audit_log_to_google_sheet(audit_entry)
    except Exception as e:
        logger.warning(f"Failed to sync audit log for rejection ({req.email}) to Google Sheet: {e}")

    # Update Pending_Requests tab in Google Sheets
    try:
        update_pending_request_status_in_google_sheet(req.email, 'Declined', reviewer, review_notes=req.review_notes)
    except Exception as e:
        logger.warning(f"Failed to update declined request status in Google Sheet: {e}")

    request.session['declined_flash_name'] = req.full_name
    request.session['declined_flash_email'] = req.email

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

    # Audit Log Entry
    actor_name = request.session.get('member_name', 'Organizer')
    actor_email = request.session.get('member_email', '')
    audit_entry = MembershipAuditLog.objects.create(
        action=MembershipAuditLog.ACTION_DIRECT_ADDED,
        target_email=email,
        target_name=full_name,
        actor_name=actor_name,
        actor_email=actor_email,
        notes=f"Added directly by admin. Role: {role} | Phone: {phone_number}"
    )
    try:
        sync_audit_log_to_google_sheet(audit_entry)
    except Exception as e:
        logger.warning(f"Failed to sync audit log for direct add ({email}) to Google Sheet: {e}")

    try:
        sync_profile_to_google_sheet(profile)
    except Exception as e:
        logger.warning(f"Failed to sync direct added member to Google Sheet: {e}")

    return redirect(reverse('organizer_dashboard') + '?added=1')


@admin_required
def organizer_delete_member_view(request, member_id):
    """
    Allows organizers/admins to delete a member from the portal and Google Sheets.
    Deletes the profile from the database, cleans up any associated membership requests,
    and deletes the member row from the Google Sheet 'Members' tab.
    """
    if request.method != 'POST':
        return redirect('organizer_dashboard')

    profile = get_object_or_404(MemberProfile, id=member_id)
    current_admin_email = request.session.get('member_email', '').strip().lower()

    # Safety: prevent admin from accidentally deleting their own active organizer session
    if profile.email.strip().lower() == current_admin_email:
        return redirect(reverse('organizer_dashboard') + '?error=self_delete_forbidden')

    target_email = profile.email
    target_name = profile.full_name or profile.email

    # Audit Log Entry
    actor_name = request.session.get('member_name', 'Organizer')
    actor_email = request.session.get('member_email', '')
    audit_entry = MembershipAuditLog.objects.create(
        action=MembershipAuditLog.ACTION_DELETED,
        target_email=target_email,
        target_name=target_name,
        actor_name=actor_name,
        actor_email=actor_email,
        notes="Member deleted from directory and Google Sheets by organizer."
    )
    try:
        sync_audit_log_to_google_sheet(audit_entry)
    except Exception as e:
        logger.warning(f"Failed to sync audit log for deletion ({target_email}) to Google Sheet: {e}")

    # 1. Delete profile from database
    profile.delete()

    # 2. Clean up any membership requests associated with this email
    MembershipRequest.objects.filter(email__iexact=target_email).delete()

    # 3. Delete from Google Sheet
    try:
        delete_profile_from_google_sheet(target_email)
    except Exception as e:
        logger.warning(f"Failed to delete member {target_email} from Google Sheet: {e}")

    request.session['deleted_member_name'] = target_name
    return redirect(reverse('organizer_dashboard') + '?member_deleted=1')


@admin_required
def organizer_sync_sheets_view(request):
    """
    On-demand two-way reconciliation with Google Sheets Members tab.
    Purges deleted members, imports new ones, and syncs roles.
    """
    if request.method != 'POST':
        return redirect('organizer_dashboard')

    result = reconcile_members_with_google_sheet()
    cache.set('last_organizer_auto_sync', True, 900)

    if result.get('success'):
        request.session['sync_flash_result'] = result
        return redirect(reverse('organizer_dashboard') + '?synced=1')
    else:
        err = result.get('error', 'Failed to communicate with Google Sheets.')
        request.session['sync_flash_error'] = err
        return redirect(reverse('organizer_dashboard') + '?sync_error=1')


def surveys_list_view(request):
    """
    Renders active and past community surveys.
    Allows verified members to cast and update votes with live result percentages and transparent voter lists.
    """
    if not is_authenticated_member(request):
        return redirect('login_page')

    user_email = request.session.get('member_email', '').strip().lower()
    is_admin = request.session.get('is_admin', False)

    category_filter = request.GET.get('category', '').strip()
    status_filter = request.GET.get('status', 'active').strip()

    surveys_qs = CommunitySurvey.objects.all().prefetch_related('options__votes', 'votes')

    if category_filter and category_filter in [c[0] for c in CommunitySurvey.CATEGORY_CHOICES]:
        surveys_qs = surveys_qs.filter(category=category_filter)

    if status_filter == 'active':
        surveys_qs = surveys_qs.filter(is_active=True)
    elif status_filter == 'closed':
        surveys_qs = surveys_qs.filter(is_active=False)

    surveys_data = []
    for s in surveys_qs:
        total_votes = s.total_votes
        unique_voters = s.unique_voters_count
        user_option_ids = s.user_voted_option_ids(user_email)
        has_voted = len(user_option_ids) > 0

        options_data = []
        for opt in s.options.all():
            opt_votes = opt.vote_count
            pct = round((opt_votes / total_votes * 100), 1) if total_votes > 0 else 0
            is_selected = opt.id in user_option_ids
            voters = opt.voter_names

            options_data.append({
                'id': opt.id,
                'text': opt.text,
                'vote_count': opt_votes,
                'percentage': pct,
                'is_selected': is_selected,
                'voters': voters,
            })

        surveys_data.append({
            'id': s.id,
            'title': s.title,
            'description': s.description,
            'category': s.category,
            'category_label': s.get_category_display(),
            'is_multiple_choice': s.is_multiple_choice,
            'is_active': s.is_active,
            'created_by': s.created_by,
            'created_at': s.created_at,
            'total_votes': total_votes,
            'unique_voters': unique_voters,
            'has_voted': has_voted,
            'user_option_ids': user_option_ids,
            'options': options_data,
        })

    active_count = CommunitySurvey.objects.filter(is_active=True).count()
    closed_count = CommunitySurvey.objects.filter(is_active=False).count()

    return render(request, 'recommendations/surveys.html', {
        'surveys': surveys_data,
        'category_filter': category_filter,
        'status_filter': status_filter,
        'category_choices': CommunitySurvey.CATEGORY_CHOICES,
        'active_count': active_count,
        'closed_count': closed_count,
        'is_admin': is_admin,
        'active_page': 'surveys',
    })


def survey_vote_view(request, survey_id):
    """
    Processes a member's vote for a survey.
    Supports single-choice and multiple-choice voting, vote updates, and Google Sheets synchronization.
    """
    if not is_authenticated_member(request):
        return redirect('login_page')

    if request.method != 'POST':
        return redirect('surveys')

    survey = get_object_or_404(CommunitySurvey, id=survey_id)
    if not survey.is_active:
        return redirect(reverse('surveys') + '?error=survey_closed')

    selected_option_ids = request.POST.getlist('options')
    if not selected_option_ids:
        return redirect(reverse('surveys') + '?error=no_selection')

    user_email = request.session.get('member_email', '').strip().lower()
    user_name = request.session.get('member_name', 'Member')

    # If single choice, restrict to first option selected
    if not survey.is_multiple_choice and len(selected_option_ids) > 1:
        selected_option_ids = selected_option_ids[:1]

    # Validate option IDs belong to this survey
    valid_options = survey.options.filter(id__in=selected_option_ids)
    if not valid_options.exists():
        return redirect(reverse('surveys') + '?error=invalid_option')

    # Clear previous votes for this user on this survey
    SurveyVote.objects.filter(survey=survey, voter_email__iexact=user_email).delete()

    # Create new votes
    for opt in valid_options:
        SurveyVote.objects.create(
            survey=survey,
            option=opt,
            voter_email=user_email,
            voter_name=user_name
        )

    # Sync to Google Sheets
    try:
        sync_survey_to_google_sheet(survey)
    except Exception as e:
        logger.warning(f"Failed to sync survey #{survey.id} to Google Sheet: {e}")

    return redirect(reverse('surveys') + '?voted=1')


@admin_required
def organizer_survey_create_view(request):
    """
    Allows admins/organizers to create a new survey with dynamic options.
    """
    if request.method != 'POST':
        return redirect('surveys')

    title = request.POST.get('title', '').strip()
    description = request.POST.get('description', '').strip()
    category = request.POST.get('category', CommunitySurvey.CATEGORY_EVENT).strip()
    is_multiple_choice = request.POST.get('is_multiple_choice') in ('on', 'true', '1', True)
    options_raw = request.POST.getlist('options')

    clean_options = [opt.strip() for opt in options_raw if opt.strip()]

    if not title:
        return redirect(reverse('surveys') + '?error=missing_title')

    if len(clean_options) < 2:
        return redirect(reverse('surveys') + '?error=min_options')

    admin_name = request.session.get('member_name', 'Organizer')
    admin_email = request.session.get('member_email', '')

    survey = CommunitySurvey.objects.create(
        title=title,
        description=description,
        category=category,
        is_multiple_choice=is_multiple_choice,
        is_active=True,
        created_by=admin_name,
        created_by_email=admin_email
    )

    for idx, opt_text in enumerate(clean_options):
        SurveyOption.objects.create(
            survey=survey,
            text=opt_text,
            order=idx
        )

    # Audit log
    MembershipAuditLog.objects.create(
        action=MembershipAuditLog.ACTION_SURVEY_CREATED,
        target_email=admin_email,
        target_name=survey.title,
        actor_name=admin_name,
        actor_email=admin_email,
        notes=f"Created survey '{survey.title}' with {len(clean_options)} options. Mode: {'Multiple Choice' if is_multiple_choice else 'Single Choice'}."
    )

    try:
        sync_survey_to_google_sheet(survey)
    except Exception as e:
        logger.warning(f"Failed to sync new survey to Google Sheet: {e}")

    return redirect(reverse('surveys') + '?created=1')


@admin_required
def organizer_survey_toggle_status_view(request, survey_id):
    """
    Allows admins to close or re-open a survey.
    """
    if request.method != 'POST':
        return redirect('surveys')

    survey = get_object_or_404(CommunitySurvey, id=survey_id)
    survey.is_active = not survey.is_active
    survey.save()

    admin_name = request.session.get('member_name', 'Organizer')
    admin_email = request.session.get('member_email', '')

    action_label = "Re-opened" if survey.is_active else "Closed"
    MembershipAuditLog.objects.create(
        action=MembershipAuditLog.ACTION_SURVEY_CLOSED,
        target_email=admin_email,
        target_name=survey.title,
        actor_name=admin_name,
        actor_email=admin_email,
        notes=f"Survey '{survey.title}' {action_label.lower()} by organizer."
    )

    try:
        sync_survey_to_google_sheet(survey)
    except Exception as e:
        logger.warning(f"Failed to sync toggled survey status to Google Sheet: {e}")

    return redirect(reverse('surveys') + '?toggled=1')


@admin_required
def organizer_survey_delete_view(request, survey_id):
    """
    Allows admins to delete a survey and purge its entries from Google Sheets.
    """
    if request.method != 'POST':
        return redirect('surveys')

    survey = get_object_or_404(CommunitySurvey, id=survey_id)
    survey_title = survey.title
    survey_id_num = survey.id

    admin_name = request.session.get('member_name', 'Organizer')
    admin_email = request.session.get('member_email', '')

    survey.delete()

    MembershipAuditLog.objects.create(
        action=MembershipAuditLog.ACTION_SURVEY_DELETED,
        target_email=admin_email,
        target_name=survey_title,
        actor_name=admin_name,
        actor_email=admin_email,
        notes=f"Survey '{survey_title}' deleted by organizer."
    )

    try:
        delete_survey_from_google_sheet(survey_id_num)
    except Exception as e:
        logger.warning(f"Failed to delete survey from Google Sheet: {e}")

    return redirect(reverse('surveys') + '?deleted=1')







