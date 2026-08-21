import datetime
from urllib.parse import quote_plus
from unittest.mock import patch, MagicMock
from django.test import TestCase, Client, RequestFactory, override_settings
from django.urls import reverse
from django.core import mail
from django.core.cache import cache

from recommendations.models import (
    MemberProfile, 
    MembershipRequest, 
    MembershipAuditLog, 
    CommunitySurvey, 
    SurveyOption, 
    SurveyVote,
    CommunityEventRSVP,
    CommunityEventBroadcast,
    CommunityEvent,
)
from recommendations.auth_helpers import is_gmail_allowed
from recommendations.sheets import (
    _normalize_record_keys,
    get_gspread_client,
    open_google_spreadsheet,
    fetch_recommendations,
    sync_profile_to_google_sheet,
    sync_pending_request_to_google_sheet,
    update_pending_request_status_in_google_sheet,
    sync_audit_log_to_google_sheet,
    backfill_audit_logs_to_google_sheet,
    sync_survey_to_google_sheet,
    delete_survey_from_google_sheet,
    delete_profile_from_google_sheet,
    reconcile_members_with_google_sheet,
    sync_event_rsvp_to_google_sheet,
    sync_community_event_to_google_sheet,
    delete_community_event_from_google_sheet,
    sync_recommendation_to_google_sheet,
)
from recommendations.calendar_sync import (
    get_google_calendar_add_url,
    _parse_event_datetime,
    fetch_community_events,
)
from recommendations.views import get_whatsapp_url
from recommendations.notifications import (
    send_membership_approval_email,
    send_membership_rejection_email,
    send_admin_new_request_notification,
    build_whatsapp_approval_link,
    build_whatsapp_decline_link,
    clean_phone_for_whatsapp,
    generate_event_rsvp_token,
    verify_event_rsvp_token,
    send_event_broadcast_email,
    send_survey_broadcast_email,
    get_subscribed_members,
    _get_from_email,
)


class ModelsTestCase(TestCase):
    def test_member_profile_str_representation(self):
        profile_with_region = MemberProfile.objects.create(
            email="sofia@minimexitas.local",
            full_name="Sofia Ramirez",
            region="south_bay",
            phone_number="+52 55 1234 5678"
        )
        self.assertIn("Sofia Ramirez", str(profile_with_region))
        self.assertIn("South Bay", str(profile_with_region))

        profile_no_region = MemberProfile.objects.create(
            email="noregion@minimexitas.local",
            full_name="No Region User",
            region="",
            phone_number="+1 415 555 1234"
        )
        self.assertIn("No Region User", str(profile_no_region))
        self.assertIn("No region set", str(profile_no_region))

    def test_membership_request_str_representation(self):
        req_pending = MembershipRequest.objects.create(
            full_name="Applicant Mateo",
            email="mateo@gmail.com",
            phone_number="+52 55 9999 8888",
            status=MembershipRequest.STATUS_PENDING
        )
        self.assertIn("Applicant Mateo", str(req_pending))
        self.assertIn("Pending Approval", str(req_pending))

        req_approved = MembershipRequest.objects.create(
            full_name="Applicant Laura",
            email="laura@gmail.com",
            phone_number="+1 408 555 1234",
            status=MembershipRequest.STATUS_APPROVED
        )
        self.assertIn("Approved", str(req_approved))

        req_rejected = MembershipRequest.objects.create(
            full_name="Applicant Spam",
            email="spam@gmail.com",
            phone_number="+1 555 000 0000",
            status=MembershipRequest.STATUS_REJECTED
        )
        self.assertIn("Declined", str(req_rejected))


@override_settings(GOOGLE_SHEET_KEY="mock_test_sheet_key_123")
class AuthHelpersTestCase(TestCase):
    @patch('recommendations.auth_helpers.get_gspread_client')
    def test_is_gmail_allowed_admin_and_member_roles(self, mock_get_client):
        mock_ws = MagicMock()
        mock_ws.get_all_records.return_value = [
            {'Name': 'Admin User', 'Gmail': 'admin@gmail.com', 'Role': 'Admin'},
            {'Name': 'Organizer User', 'Gmail': 'organizer@gmail.com', 'Role': 'ORGANIZER'},
            {'Name': 'Regular User', 'Gmail': 'regular@gmail.com', 'Role': 'Member'},
        ]
        mock_sh = MagicMock()
        mock_sh.worksheet.return_value = mock_ws
        mock_client = MagicMock()
        mock_client.open.return_value = mock_sh
        mock_client.open_by_key.return_value = mock_sh
        mock_get_client.return_value = mock_client

        # Admin user check
        allowed, name, is_admin = is_gmail_allowed('admin@gmail.com')
        self.assertTrue(allowed)
        self.assertTrue(is_admin)
        self.assertEqual(name, 'Admin User')

        # Organizer user check
        allowed, name, is_admin = is_gmail_allowed('organizer@gmail.com')
        self.assertTrue(allowed)
        self.assertTrue(is_admin)
        self.assertEqual(name, 'Organizer User')

        # Regular member check
        allowed, name, is_admin = is_gmail_allowed('regular@gmail.com')
        self.assertTrue(allowed)
        self.assertFalse(is_admin)
        self.assertEqual(name, 'Regular User')

        # Non-member check
        allowed, name, is_admin = is_gmail_allowed('stranger@gmail.com')
        self.assertFalse(allowed)
        self.assertFalse(is_admin)
        self.assertIsNone(name)

    @patch('recommendations.auth_helpers.get_gspread_client')
    def test_is_gmail_allowed_handles_whitespace_and_case(self, mock_get_client):
        mock_ws = MagicMock()
        mock_ws.get_all_records.return_value = [
            {'Name': '  Carlos M.  ', 'Gmail': '  CARLOS@gmail.com  ', 'Role': '  Admin  '},
        ]
        mock_sh = MagicMock()
        mock_sh.worksheet.return_value = mock_ws
        mock_client = MagicMock()
        mock_client.open.return_value = mock_sh
        mock_client.open_by_key.return_value = mock_sh
        mock_get_client.return_value = mock_client

        allowed, name, is_admin = is_gmail_allowed('   carlos@gmail.com   ')
        self.assertTrue(allowed)
        self.assertTrue(is_admin)
        self.assertEqual(name, 'Carlos M.')

    def test_is_gmail_allowed_empty_or_none_email(self):
        self.assertEqual(is_gmail_allowed(""), (False, None, False))
        self.assertEqual(is_gmail_allowed(None), (False, None, False))

    @patch('recommendations.auth_helpers.get_gspread_client')
    def test_is_gmail_allowed_handles_gspread_exception(self, mock_get_client):
        mock_get_client.side_effect = Exception("Google API connection error")
        allowed, name, is_admin = is_gmail_allowed('user@gmail.com')
        self.assertFalse(allowed)
        self.assertIsNone(name)
        self.assertFalse(is_admin)


@override_settings(GOOGLE_SHEET_KEY="mock_test_sheet_key_123")
class SheetsSyncTestCase(TestCase):
    def test_normalize_record_keys(self):
        raw_row = {
            "Shared By": "Maria G.",
            "Timestamp": "2026-08-15",
            "Category": "Culinary",
            "Content": "Great tacos in Mission",
            "Random Key": "Value"
        }
        normalized = _normalize_record_keys(raw_row)
        self.assertEqual(normalized.get("Shared_By"), "Maria G.")
        self.assertEqual(normalized.get("Date"), "2026-08-15")
        self.assertEqual(normalized.get("Subject"), "Culinary")
        self.assertEqual(normalized.get("Recommendation"), "Great tacos in Mission")
        self.assertEqual(normalized.get("Random_Key"), "Value")

    def test_get_gspread_client_raises_value_error_without_credentials(self):
        with override_settings(GOOGLE_SERVICE_ACCOUNT_INFO=None, GOOGLE_CREDENTIALS_PATH=None):
            with self.assertRaises(ValueError):
                get_gspread_client()

    @patch('recommendations.sheets.gspread.service_account_from_dict')
    def test_get_gspread_client_uses_service_account_dict(self, mock_sa_dict):
        fake_dict = {'type': 'service_account', 'project_id': 'test'}
        with override_settings(GOOGLE_SERVICE_ACCOUNT_INFO=fake_dict):
            mock_sa_dict.return_value = MagicMock()
            client = get_gspread_client()
            mock_sa_dict.assert_called_once_with(fake_dict)

    @patch('recommendations.sheets.get_gspread_client')
    def test_fetch_recommendations_normalized(self, mock_get_client):
        mock_ws = MagicMock()
        mock_ws.title = "Recomendaciones"
        mock_ws.get_all_records.return_value = [
            {"Shared By": "Carlos", "Category": "Doctors", "Recommendation": "Dr. Smith"}
        ]
        mock_sh = MagicMock()
        mock_sh.sheet1 = mock_ws
        mock_sh.worksheet.return_value = mock_ws
        mock_sh.worksheets.return_value = [mock_ws]
        mock_client = MagicMock()
        mock_client.open.return_value = mock_sh
        mock_client.open_by_key.return_value = mock_sh
        mock_get_client.return_value = mock_client

        results = fetch_recommendations()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["Shared_By"], "Carlos")
        self.assertEqual(results[0]["Subject"], "Doctors")
        self.assertEqual(results[0]["Recommendation"], "Dr. Smith")

    def test_sync_profile_none_or_empty_email_returns_false(self):
        self.assertFalse(sync_profile_to_google_sheet(None))
        empty_profile = MemberProfile(email="")
        self.assertFalse(sync_profile_to_google_sheet(empty_profile))

    @patch('recommendations.sheets.get_gspread_client')
    def test_sync_profile_to_google_sheet_updates_existing_row(self, mock_get_client):
        mock_ws = MagicMock()
        mock_ws.get_all_values.return_value = [
            ['Name', 'Gmail', 'Region', 'City', 'Phone', 'Family Info', 'Interests', 'Role', 'Last Updated'],
            ['Maria Gonzalez', 'maria@gmail.com', 'Peninsula', 'Palo Alto', '+1 650 555 1234', '2 kids', 'Tacos', 'Member', '2026-08-01']
        ]
        mock_sh = MagicMock()
        mock_sh.worksheet.return_value = mock_ws
        mock_client = MagicMock()
        mock_client.open.return_value = mock_sh
        mock_client.open_by_key.return_value = mock_sh
        mock_get_client.return_value = mock_client

        profile = MemberProfile(
            email="maria@gmail.com",
            full_name="Maria Gonzalez Updated",
            region="south_bay",
            city="San Jose",
            phone_number="+1 408 555 9999",
            family_info="3 kids",
            interests="Culinary",
            is_admin=False
        )

        res = sync_profile_to_google_sheet(profile)
        self.assertTrue(res)
        mock_ws.update.assert_called()

    @patch('recommendations.sheets.get_gspread_client')
    def test_sync_profile_to_google_sheet_appends_new_row(self, mock_get_client):
        mock_ws = MagicMock()
        mock_ws.get_all_values.return_value = [
            ['Name', 'Gmail', 'Region', 'City', 'Phone', 'Family Info', 'Interests', 'Role', 'Last Updated'],
            ['Existing Member', 'existing@gmail.com', 'Peninsula', 'Palo Alto', '+1 650 555 1234', '', '', 'Member', '2026-08-01']
        ]
        mock_sh = MagicMock()
        mock_sh.worksheet.return_value = mock_ws
        mock_client = MagicMock()
        mock_client.open.return_value = mock_sh
        mock_client.open_by_key.return_value = mock_sh
        mock_get_client.return_value = mock_client

        profile = MemberProfile(
            email="brand_new@gmail.com",
            full_name="New Member",
            region="san_francisco",
            city="Mission",
            phone_number="+52 55 1234 5678",
            is_admin=False
        )

        res = sync_profile_to_google_sheet(profile)
        self.assertTrue(res)
        mock_ws.append_row.assert_called_once()

    @patch('recommendations.sheets.get_gspread_client')
    def test_sync_profile_to_google_sheet_handles_exception_safely(self, mock_get_client):
        mock_get_client.side_effect = Exception("Google API timeout")
        profile = MemberProfile(email="error@gmail.com", full_name="Error User")
        res = sync_profile_to_google_sheet(profile)
        self.assertFalse(res)

    @patch('recommendations.sheets.get_gspread_client')
    def test_sync_pending_request_to_google_sheet_appends_new_row(self, mock_get_client):
        mock_ws = MagicMock()
        mock_ws.get_all_values.return_value = [
            ['Name', 'Gmail', 'Phone', 'Region', 'City', 'Referral Source', 'Status', 'Submitted At', 'Reviewed By', 'Reviewed At', 'Review Notes']
        ]
        mock_sh = MagicMock()
        mock_sh.worksheet.return_value = mock_ws
        mock_client = MagicMock()
        mock_client.open.return_value = mock_sh
        mock_client.open_by_key.return_value = mock_sh
        mock_get_client.return_value = mock_client

        req = MembershipRequest(
            full_name="New Applicant",
            email="applicant@gmail.com",
            phone_number="+52 55 1111 2222",
            region="south_bay",
            city="San Jose",
            referral_source="Friend",
            status=MembershipRequest.STATUS_PENDING
        )

        res = sync_pending_request_to_google_sheet(req)
        self.assertTrue(res)
        mock_ws.append_row.assert_called_once()

    @patch('recommendations.sheets.get_gspread_client')
    def test_sync_pending_request_to_google_sheet_updates_existing_row(self, mock_get_client):
        mock_ws = MagicMock()
        mock_ws.get_all_values.return_value = [
            ['Name', 'Gmail', 'Phone', 'Region', 'City', 'Referral Source', 'Status', 'Submitted At', 'Reviewed By', 'Reviewed At', 'Review Notes'],
            ['Old Name', 'applicant@gmail.com', '+52 55 1111 2222', 'South Bay', 'San Jose', 'Friend', 'Pending', '2026-08-01', '', '', '']
        ]
        mock_sh = MagicMock()
        mock_sh.worksheet.return_value = mock_ws
        mock_client = MagicMock()
        mock_client.open.return_value = mock_sh
        mock_client.open_by_key.return_value = mock_sh
        mock_get_client.return_value = mock_client

        req = MembershipRequest(
            full_name="Updated Applicant",
            email="applicant@gmail.com",
            phone_number="+52 55 1111 2222",
            region="san_francisco",
            city="Mission",
            referral_source="Updated referral",
            status=MembershipRequest.STATUS_APPROVED,
            reviewed_by="Admin"
        )

        res = sync_pending_request_to_google_sheet(req)
        self.assertTrue(res)
        mock_ws.update.assert_called_once()

    @patch('recommendations.sheets.get_gspread_client')
    def test_update_pending_request_status_in_google_sheet(self, mock_get_client):
        mock_ws = MagicMock()
        mock_ws.get_all_values.return_value = [
            ['Name', 'Gmail', 'Phone', 'Region', 'City', 'Referral Source', 'Status', 'Submitted At', 'Reviewed By', 'Reviewed At', 'Review Notes'],
            ['Applicant', 'target@gmail.com', '+1 415 555 1234', 'Peninsula', '', 'Referral', 'Pending', '2026-08-01', '', '', '']
        ]
        mock_sh = MagicMock()
        mock_sh.worksheet.return_value = mock_ws
        mock_client = MagicMock()
        mock_client.open.return_value = mock_sh
        mock_client.open_by_key.return_value = mock_sh
        mock_get_client.return_value = mock_client

        res = update_pending_request_status_in_google_sheet('target@gmail.com', 'Declined', 'Admin Maria', review_notes='Outside Bay Area')
        self.assertTrue(res)
        mock_ws.update.assert_called_once()

    @patch('recommendations.sheets.get_gspread_client')
    def test_sync_pending_request_to_google_sheet_handles_exception_safely(self, mock_get_client):
        mock_get_client.side_effect = Exception("Google Sheets connection error")
        req = MembershipRequest(email="error@gmail.com", full_name="Error User")
        res = sync_pending_request_to_google_sheet(req)
        self.assertFalse(res)

    @patch('recommendations.sheets.get_gspread_client')
    def test_sync_audit_log_to_google_sheet_appends_row(self, mock_get_client):
        mock_ws = MagicMock()
        mock_ws.get_all_values.return_value = [
            ['Timestamp', 'Action', 'Target Member', 'Target Email', 'Performed By', 'Admin Email', 'Notes']
        ]
        mock_sh = MagicMock()
        mock_sh.worksheet.return_value = mock_ws
        mock_client = MagicMock()
        mock_client.open.return_value = mock_sh
        mock_client.open_by_key.return_value = mock_sh
        mock_get_client.return_value = mock_client

        log = MembershipAuditLog(
            action=MembershipAuditLog.ACTION_APPROVED,
            target_email="audit.target@gmail.com",
            target_name="Audit Target",
            actor_name="Admin Maria",
            actor_email="admin@gmail.com",
            notes="Approved after interview"
        )
        res = sync_audit_log_to_google_sheet(log)
        self.assertTrue(res)
        mock_ws.append_row.assert_called_once()
        args, _ = mock_ws.append_row.call_args
        self.assertIn("Request Approved", args[0])
        self.assertIn("audit.target@gmail.com", args[0])
        self.assertIn("Admin Maria", args[0])

    def test_sync_audit_log_none_returns_false(self):
        self.assertFalse(sync_audit_log_to_google_sheet(None))

    @patch('recommendations.sheets.sync_audit_log_to_google_sheet')
    def test_backfill_audit_logs_to_google_sheet(self, mock_sync):
        mock_sync.return_value = True
        MembershipAuditLog.objects.create(
            action=MembershipAuditLog.ACTION_SUBMITTED,
            target_email="backfill1@gmail.com",
            target_name="Backfill 1"
        )
        MembershipAuditLog.objects.create(
            action=MembershipAuditLog.ACTION_APPROVED,
            target_email="backfill2@gmail.com",
            target_name="Backfill 2"
        )
        count = backfill_audit_logs_to_google_sheet()
        self.assertGreaterEqual(count, 2)


class CalendarSyncTestCase(TestCase):
    def setUp(self):
        cache.clear()

    def test_get_google_calendar_add_url_formatting(self):
        start_dt = datetime.datetime(2026, 9, 16, 18, 0, 0, tzinfo=datetime.timezone.utc)
        end_dt = datetime.datetime(2026, 9, 16, 21, 0, 0, tzinfo=datetime.timezone.utc)
        url = get_google_calendar_add_url(
            title="Mexican Independence Day Picnic",
            start_dt=start_dt,
            end_dt=end_dt,
            description="Bring traditional Mexican dishes!",
            location="Golden Gate Park, SF"
        )
        self.assertIn("https://calendar.google.com/calendar/render?action=TEMPLATE", url)
        self.assertIn("Mexican+Independence+Day+Picnic", url)
        self.assertIn("20260916T180000Z/20260916T210000Z", url)
        self.assertIn("Golden+Gate+Park%2C+SF", url)

    def test_parse_event_datetime_formats(self):
        # ISO string with UTC Z
        dt = _parse_event_datetime("2026-09-15T14:00:00Z")
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.month, 9)
        self.assertEqual(dt.day, 15)
        self.assertEqual(dt.hour, 14)

        # Date only (YYYY-MM-DD)
        dt_date = _parse_event_datetime("2026-10-31")
        self.assertEqual(dt_date.year, 2026)
        self.assertEqual(dt_date.month, 10)
        self.assertEqual(dt_date.day, 31)

        # Fallback default date
        default_dt = datetime.datetime(2026, 1, 1, 12, 0)
        dt_fallback = _parse_event_datetime("invalid-string", default_date=default_dt)
        self.assertEqual(dt_fallback, default_dt)

    def test_fetch_community_events_caching_and_empty(self):
        # When environment vars are not set
        with patch.dict('os.environ', {}, clear=True):
            with override_settings(GOOGLE_SERVICE_ACCOUNT_INFO=None):
                events = fetch_community_events()
                self.assertEqual(events, [])

                # Cache check
                cached_events = cache.get("community_events_cache")
                self.assertEqual(cached_events, [])


class AuthenticationAndAccessViewsTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        MemberProfile.objects.all().delete()
        MembershipRequest.objects.all().delete()

    def test_unauthenticated_home_view_redirects_to_login(self):
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)

    def test_login_page_renders_for_unauthenticated_users(self):
        response = self.client.get(reverse('login_page'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "MiniMexitas")
        self.assertContains(response, "Sign in with Google")
        self.assertContains(response, "Request to Join")

    def test_login_page_redirects_if_already_authenticated(self):
        session = self.client.session
        session['is_verified_member'] = True
        session['member_name'] = 'Logged In Member'
        session.save()

        response = self.client.get(reverse('login_page'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('home'))

    def test_google_login_view_redirects_to_google_oauth(self):
        response = self.client.get(reverse('gmail_login'))
        self.assertEqual(response.status_code, 302)
        self.assertIn("accounts.google.com/o/oauth2/v2/auth", response.url)
        self.assertTrue(self.client.session.get('oauth_state'))

    def test_google_callback_missing_state_or_code_returns_error(self):
        # Missing state and code -> redirects to login page with error param
        response = self.client.get(reverse('google_callback'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)

    @patch('recommendations.views.requests.post')
    def test_google_callback_token_exchange_failure(self, mock_post):
        mock_post.return_value.status_code = 400
        mock_post.return_value.json.return_value = {'error_description': 'Invalid grant'}

        session = self.client.session
        session['oauth_state'] = 'test_state_123'
        session.save()

        response = self.client.get(reverse('google_callback') + '?code=bad_code&state=test_state_123')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invalid grant")

    @patch('recommendations.views.requests.post')
    @patch('recommendations.views.requests.get')
    def test_google_callback_unverified_email_returns_error(self, mock_get, mock_post):
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {'access_token': 'valid_token'}

        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {'email': 'unverified@gmail.com', 'verified_email': False}

        session = self.client.session
        session['oauth_state'] = 'test_state_456'
        session.save()

        response = self.client.get(reverse('google_callback') + '?code=code_456&state=test_state_456')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "not verified by Google")

    @patch('recommendations.views.requests.post')
    @patch('recommendations.views.requests.get')
    @patch('recommendations.views.is_gmail_allowed')
    def test_google_callback_allowed_member_login(self, mock_allowed, mock_get, mock_post):
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {'access_token': 'valid_token'}

        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {'email': 'maria@gmail.com', 'verified_email': True}

        mock_allowed.return_value = (True, 'Maria Gonzalez', False)

        session = self.client.session
        session['oauth_state'] = 'valid_state_789'
        session.save()

        response = self.client.get(reverse('google_callback') + '?code=code_789&state=valid_state_789')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('home'))

        # Check session
        self.assertTrue(self.client.session.get('is_verified_member'))
        self.assertFalse(self.client.session.get('is_admin'))
        self.assertEqual(self.client.session.get('member_name'), 'Maria Gonzalez')

        # Check profile in DB
        profile = MemberProfile.objects.get(email='maria@gmail.com')
        self.assertEqual(profile.full_name, 'Maria Gonzalez')
        self.assertFalse(profile.is_admin)

    @patch('recommendations.views.requests.post')
    @patch('recommendations.views.requests.get')
    @patch('recommendations.views.is_gmail_allowed')
    def test_google_callback_allowed_admin_login(self, mock_allowed, mock_get, mock_post):
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {'access_token': 'valid_token'}

        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {'email': 'admin@gmail.com', 'verified_email': True}

        mock_allowed.return_value = (True, 'Admin User', True)

        session = self.client.session
        session['oauth_state'] = 'admin_state_999'
        session.save()

        response = self.client.get(reverse('google_callback') + '?code=admin_code&state=admin_state_999')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('home'))

        self.assertTrue(self.client.session.get('is_verified_member'))
        self.assertTrue(self.client.session.get('is_admin'))

        profile = MemberProfile.objects.get(email='admin@gmail.com')
        self.assertTrue(profile.is_admin)

    @patch('recommendations.views.requests.post')
    @patch('recommendations.views.requests.get')
    @patch('recommendations.views.is_gmail_allowed')
    def test_google_callback_unregistered_pending_request_shows_pending_alert(self, mock_allowed, mock_get, mock_post):
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {'access_token': 'valid_token'}

        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {'email': 'pending.user@gmail.com', 'verified_email': True}

        mock_allowed.return_value = (False, None, False)

        MembershipRequest.objects.create(
            full_name="Pending User",
            email="pending.user@gmail.com",
            phone_number="+52 55 9999 8888",
            status=MembershipRequest.STATUS_PENDING
        )

        session = self.client.session
        session['oauth_state'] = 'pending_state_111'
        session.save()

        response = self.client.get(reverse('google_callback') + '?code=p_code&state=pending_state_111')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Membership Request Pending")
        self.assertContains(response, "currently pending organizer review")

    @patch('recommendations.views.requests.post')
    @patch('recommendations.views.requests.get')
    @patch('recommendations.views.is_gmail_allowed')
    def test_google_callback_unregistered_rejected_request_shows_sorry_note_and_reason(self, mock_allowed, mock_get, mock_post):
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {'access_token': 'valid_token'}

        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {'email': 'rejected.user@gmail.com', 'verified_email': True}

        mock_allowed.return_value = (False, None, False)

        MembershipRequest.objects.create(
            full_name="Rejected User",
            email="rejected.user@gmail.com",
            phone_number="+1 415 555 9999",
            status=MembershipRequest.STATUS_REJECTED,
            review_notes="Location is outside the San Francisco Bay Area community scope."
        )

        session = self.client.session
        session['oauth_state'] = 'rejected_state_222'
        session.save()

        response = self.client.get(reverse('google_callback') + '?code=r_code&state=rejected_state_222')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Membership Request Status: Not Approved")
        self.assertContains(response, "Location is outside the San Francisco Bay Area community scope.")
        self.assertContains(response, "Submit an Updated Request")

    def test_logout_clears_session_and_redirects_to_login(self):
        session = self.client.session
        session['is_verified_member'] = True
        session['is_admin'] = True
        session['member_name'] = 'Carlos Member'
        session['member_email'] = 'carlos@minimexitas.local'
        session.save()

        response = self.client.get(reverse('logout'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('login_page'))
        self.assertFalse(self.client.session.get('is_verified_member', False))


class MemberProfileViewsTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        MemberProfile.objects.all().delete()
        self.profile = MemberProfile.objects.create(
            email="testuser@minimexitas.local",
            full_name="Maria Gonzalez",
            region="peninsula",
            city="Palo Alto",
            phone_number="+1 650 555 1234",
            family_info="2 kids (ages 3 & 6)",
            interests="Cultural & Traditional Celebrations",
            bio="Software engineer and mom.",
            is_admin=False
        )

    def test_unauthenticated_profile_redirects_to_login(self):
        response = self.client.get(reverse('profile'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)

    def test_authenticated_profile_get_view(self):
        session = self.client.session
        session['is_verified_member'] = True
        session['member_name'] = 'Maria Gonzalez'
        session['member_email'] = 'testuser@minimexitas.local'
        session.save()

        response = self.client.get(reverse('profile'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Maria Gonzalez")
        self.assertContains(response, "Palo Alto")
        self.assertContains(response, "Bay Area Region")

    @patch('recommendations.views.sync_profile_to_google_sheet')
    def test_authenticated_profile_post_update_success(self, mock_sync):
        mock_sync.return_value = True

        session = self.client.session
        session['is_verified_member'] = True
        session['member_name'] = 'Maria Gonzalez'
        session['member_email'] = 'testuser@minimexitas.local'
        session.save()

        post_data = {
            'full_name': 'Maria Gonzalez Updated',
            'phone_number': '+52 55 1234 5678',
            'region': 'south_bay',
            'city': 'Sunnyvale',
            'family_info': '3 kids',
            'interests': ['Culinary & Dining', 'Tech & Professional Networking'],
            'bio': 'Updated bio description.',
        }

        response = self.client.post(reverse('profile'), post_data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Your profile and region details have been updated successfully!")

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.full_name, 'Maria Gonzalez Updated')
        self.assertEqual(self.profile.phone_number, '+52 55 1234 5678')
        self.assertEqual(self.profile.region, 'south_bay')
        self.assertEqual(self.profile.city, 'Sunnyvale')
        self.assertIn('Culinary & Dining', self.profile.interests)
        mock_sync.assert_called_once_with(self.profile)

    def test_profile_post_cannot_escalate_role_to_admin(self):
        session = self.client.session
        session['is_verified_member'] = True
        session['is_admin'] = False
        session['member_name'] = 'Maria Gonzalez'
        session['member_email'] = 'testuser@minimexitas.local'
        session.save()

        post_data = {
            'full_name': 'Maria Hacker',
            'phone_number': '+1 650 555 1234',
            'region': 'peninsula',
            'is_admin': 'True',
            'role': 'Admin',
        }

        response = self.client.post(reverse('profile'), post_data)
        self.assertEqual(response.status_code, 200)

        self.profile.refresh_from_db()
        self.assertFalse(self.profile.is_admin)

    def test_profile_post_requires_valid_phone_number(self):
        session = self.client.session
        session['is_verified_member'] = True
        session['member_name'] = 'Maria Gonzalez'
        session['member_email'] = 'testuser@minimexitas.local'
        session.save()

        # Missing phone
        post_data = {
            'full_name': 'Maria Incomplete',
            'phone_number': '',
            'region': 'south_bay',
        }
        response = self.client.post(reverse('profile'), post_data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "A valid WhatsApp phone number")

        # Invalid short phone (<10 digits)
        post_data['phone_number'] = '12345'
        response = self.client.post(reverse('profile'), post_data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "A valid WhatsApp phone number")

    def test_profile_post_requires_full_name(self):
        session = self.client.session
        session['is_verified_member'] = True
        session['member_name'] = 'Maria Gonzalez'
        session['member_email'] = 'testuser@minimexitas.local'
        session.save()

        post_data = {
            'full_name': '',
            'phone_number': '+1 650 555 1234',
            'region': 'south_bay',
        }
        response = self.client.post(reverse('profile'), post_data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please provide your full name.")

    @patch('recommendations.views.sync_profile_to_google_sheet')
    def test_profile_email_notifications_toggle_off_and_on(self, mock_sync):
        mock_sync.return_value = True

        session = self.client.session
        session['is_verified_member'] = True
        session['member_name'] = 'Maria Gonzalez'
        session['member_email'] = 'testuser@minimexitas.local'
        session.save()

        # Check default is True
        self.assertTrue(self.profile.email_notifications)

        # 1. Post form with email_notifications omitted (unchecked) -> should turn False
        post_data_off = {
            'full_name': 'Maria Gonzalez',
            'phone_number': '+1 650 555 1234',
            'region': 'peninsula',
            'city': 'Palo Alto',
            'family_info': '2 kids',
            'bio': 'Software engineer and mom.',
        }
        response = self.client.post(reverse('profile'), post_data_off)
        self.assertEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.email_notifications)

        # GET request should display unsubscribed / muted status
        response_get = self.client.get(reverse('profile'))
        self.assertEqual(response_get.status_code, 200)
        self.assertContains(response_get, "Unsubscribed (Muted)")

        # 2. Post form with email_notifications checked -> should turn True
        post_data_on = {
            'full_name': 'Maria Gonzalez',
            'phone_number': '+1 650 555 1234',
            'region': 'peninsula',
            'city': 'Palo Alto',
            'family_info': '2 kids',
            'bio': 'Software engineer and mom.',
            'email_notifications': '1',
        }
        response = self.client.post(reverse('profile'), post_data_on)
        self.assertEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.email_notifications)

        # GET request should display subscribed / active status
        response_get2 = self.client.get(reverse('profile'))
        self.assertEqual(response_get2.status_code, 200)
        self.assertContains(response_get2, "Subscribed (Active)")


class MemberDirectoryViewsTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        MemberProfile.objects.all().delete()
        self.member1 = MemberProfile.objects.create(
            email="secret1@gmail.com",
            full_name="Carlos Santana",
            region="south_bay",
            city="San Jose",
            phone_number="+1 408 555 9999",
            family_info="2 teenagers",
            interests="Culinary & Dining",
            is_admin=False,
        )
        self.member2 = MemberProfile.objects.create(
            email="secretadmin@gmail.com",
            full_name="Lucia Mendez",
            region="san_francisco",
            city="Mission SF",
            phone_number="+52 55 1234 5678",
            family_info="1 toddler",
            interests="Cultural & Traditional Celebrations",
            is_admin=True,
        )

    def test_unauthenticated_directory_redirects_to_login(self):
        response = self.client.get(reverse('member_directory'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)

    def test_regular_member_can_view_directory(self):
        session = self.client.session
        session['is_verified_member'] = True
        session['is_admin'] = False
        session['member_name'] = 'Carlos Santana'
        session['member_email'] = 'secret1@gmail.com'
        session.save()

        response = self.client.get(reverse('member_directory'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Community Member Directory")
        self.assertContains(response, "Carlos Santana")
        self.assertContains(response, "Lucia Mendez")
        self.assertContains(response, "San Jose")
        self.assertContains(response, "Mission SF")

    def test_directory_renders_whatsapp_buttons_and_masks_email_and_role(self):
        session = self.client.session
        session['is_verified_member'] = True
        session['is_admin'] = False
        session['member_name'] = 'Carlos Santana'
        session['member_email'] = 'secret1@gmail.com'
        session.save()

        response = self.client.get(reverse('member_directory'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')

        self.assertIn("https://wa.me/14085559999", content)
        self.assertIn("https://wa.me/525512345678", content)
        self.assertIn("Message on WhatsApp", content)

        # Ensure private emails and admin roles are not exposed in directory
        self.assertNotIn("secret1@gmail.com", content)
        self.assertNotIn("secretadmin@gmail.com", content)
        self.assertNotIn("Organizer / Admin", content)

    def test_directory_handles_members_without_region_or_phone_gracefully(self):
        MemberProfile.objects.create(
            email="noregion@minimexitas.local",
            full_name="New Blank Profile",
            region="",
            phone_number=""
        )

        session = self.client.session
        session['is_verified_member'] = True
        session['is_admin'] = False
        session['member_name'] = 'Carlos Santana'
        session['member_email'] = 'secret1@gmail.com'
        session.save()

        response = self.client.get(reverse('member_directory'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "New Blank Profile")

    def test_get_whatsapp_url_mexico_and_international_formats(self):
        # Mexico formats
        self.assertEqual(get_whatsapp_url("+52 55 1234 5678"), "https://wa.me/525512345678")
        self.assertEqual(get_whatsapp_url("+52 (55) 1234-5678"), "https://wa.me/525512345678")
        self.assertEqual(get_whatsapp_url("+52 1 55 1234 5678"), "https://wa.me/5215512345678")
        self.assertEqual(get_whatsapp_url("+52 81 8345 6789"), "https://wa.me/528183456789")
        self.assertEqual(get_whatsapp_url("+52 33 3612 3456"), "https://wa.me/523336123456")
        self.assertEqual(get_whatsapp_url("525512345678"), "https://wa.me/525512345678")
        self.assertEqual(get_whatsapp_url("011 52 55 1234 5678"), "https://wa.me/525512345678")
        self.assertEqual(get_whatsapp_url("0052 55 1234 5678"), "https://wa.me/525512345678")

        # US / Canada formats
        self.assertEqual(get_whatsapp_url("+1 415 555 1234"), "https://wa.me/14155551234")
        self.assertEqual(get_whatsapp_url("(415) 555-1234"), "https://wa.me/14155551234")
        self.assertEqual(get_whatsapp_url("4155551234"), "https://wa.me/14155551234")

        # International formats
        self.assertEqual(get_whatsapp_url("+34 612 345 678"), "https://wa.me/34612345678")
        self.assertEqual(get_whatsapp_url("+57 300 123 4567"), "https://wa.me/573001234567")

        # Blank / Invalid input
        self.assertEqual(get_whatsapp_url(""), "")
        self.assertEqual(get_whatsapp_url(None), "")
        self.assertEqual(get_whatsapp_url("N/A"), "")


class NotesAndEventsViewsTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        cache.clear()

    def test_unauthenticated_recommendations_redirects_to_login(self):
        response = self.client.get(reverse('recommendations:notes_list'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)

    @patch('recommendations.views.fetch_recommendations')
    def test_authenticated_recommendations_view(self, mock_fetch):
        mock_fetch.return_value = [
            {"Shared_By": "Carlos", "Subject": "Food", "Recommendation": "Taqueria Cancun"}
        ]
        session = self.client.session
        session['is_verified_member'] = True
        session['member_name'] = 'Carlos Member'
        session.save()

        response = self.client.get(reverse('recommendations:notes_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Taqueria Cancun")

    @patch('recommendations.views.fetch_recommendations')
    def test_recommendations_view_handles_fetch_exception(self, mock_fetch):
        mock_fetch.side_effect = Exception("Google Sheet offline")
        session = self.client.session
        session['is_verified_member'] = True
        session['member_name'] = 'Carlos Member'
        session.save()

        response = self.client.get(reverse('recommendations:notes_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Unable to load recommendations")

    def test_unauthenticated_events_redirects_to_login(self):
        response = self.client.get(reverse('events'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)

    @patch('recommendations.views.fetch_community_events')
    def test_authenticated_events_view(self, mock_fetch_events):
        mock_fetch_events.return_value = [
            {
                "id": "event_1",
                "title": "Dia de los Muertos Festival",
                "start_datetime": datetime.datetime(2026, 11, 1, 14, 0),
                "end_datetime": datetime.datetime(2026, 11, 1, 18, 0),
                "date_formatted": "Sunday, November 01, 2026",
                "time_formatted": "2:00 PM - 6:00 PM",
                "location": "Mission SF",
                "location_url": "https://maps.google.com",
                "category": "Cultural & Heritage",
                "description": "Annual community altar exhibition.",
                "organizer": "MiniMexitas",
                "google_calendar_link": "https://calendar.google.com",
                "year": 2026,
                "month": 11,
                "day": 1,
                "is_past": False,
            }
        ]

        session = self.client.session
        session['is_verified_member'] = True
        session['member_name'] = 'Carlos Member'
        session.save()

        response = self.client.get(reverse('events'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Dia de los Muertos Festival")
        self.assertContains(response, "Mission SF")


class OrganizerDashboardViewsTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        MemberProfile.objects.all().delete()
        MembershipRequest.objects.all().delete()

        self.admin_profile = MemberProfile.objects.create(
            email="organizer@minimexitas.local",
            full_name="Organizer Admin",
            is_admin=True,
            region="san_francisco",
            city="Mission"
        )
        self.member_profile = MemberProfile.objects.create(
            email="regular@minimexitas.local",
            full_name="Regular Member",
            is_admin=False,
            region="peninsula",
            city="San Mateo"
        )

    def test_unauthenticated_organizer_redirects_to_login(self):
        response = self.client.get(reverse('organizer_dashboard'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)

    def test_regular_member_forbidden_from_organizer_dashboard(self):
        session = self.client.session
        session['is_verified_member'] = True
        session['is_admin'] = False
        session['member_name'] = 'Regular Member'
        session['member_email'] = 'regular@minimexitas.local'
        session.save()

        response = self.client.get(reverse('organizer_dashboard'))
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "Access Denied", status_code=403)

    def test_admin_access_to_organizer_dashboard(self):
        session = self.client.session
        session['is_verified_member'] = True
        session['is_admin'] = True
        session['member_name'] = 'Organizer Admin'
        session['member_email'] = 'organizer@minimexitas.local'
        session.save()

        response = self.client.get(reverse('organizer_dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Organizer Dashboard")
        self.assertContains(response, "Membership Requests Queue")
        self.assertContains(response, "Regular Member")
        self.assertContains(response, "San Mateo")


class MembershipRequestWorkflowTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        MemberProfile.objects.all().delete()
        MembershipRequest.objects.all().delete()

        self.admin_profile = MemberProfile.objects.create(
            email="admin@minimexitas.local",
            full_name="Admin Organizer",
            is_admin=True,
            region="san_francisco",
            city="Mission SF"
        )

    def test_join_request_get_renders_form(self):
        response = self.client.get(reverse('join_request'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Request to Join MiniMexitas")
        self.assertContains(response, "Full Name")
        self.assertContains(response, "Google / Gmail Address")
        self.assertContains(response, "WhatsApp / Mobile Phone")
        self.assertContains(response, "Bay Area Region")

    @patch('recommendations.views.sync_pending_request_to_google_sheet')
    def test_join_request_post_success_creates_pending_request(self, mock_pending_sync):
        mock_pending_sync.return_value = True

        post_data = {
            'full_name': 'Sofia Ramirez',
            'email': 'sofia.ramirez@gmail.com',
            'phone_number': '+52 55 9876 5432',
            'region': 'south_bay',
            'city': 'San Jose',
            'referral_source': 'Referred by Carlos from Guadalajara WhatsApp group.',
        }

        response = self.client.post(reverse('join_request'), post_data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Request Received!")
        self.assertContains(response, "Pending Organizer Review")
        self.assertContains(response, "sofia.ramirez@gmail.com")

        req = MembershipRequest.objects.get(email='sofia.ramirez@gmail.com')
        self.assertEqual(req.full_name, 'Sofia Ramirez')
        self.assertEqual(req.status, MembershipRequest.STATUS_PENDING)
        self.assertEqual(req.region, 'south_bay')
        self.assertEqual(req.city, 'San Jose')

        mock_pending_sync.assert_called_once_with(req)

    def test_join_request_post_validation_errors(self):
        # Missing full name
        post_data = {
            'full_name': '',
            'email': 'sofia@gmail.com',
            'phone_number': '+1 415 555 1234',
            'region': 'south_bay',
        }
        response = self.client.post(reverse('join_request'), post_data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please enter your full name.")

        # Invalid email
        post_data = {
            'full_name': 'Sofia Ramirez',
            'email': 'invalid-email',
            'phone_number': '+1 415 555 1234',
            'region': 'south_bay',
        }
        response = self.client.post(reverse('join_request'), post_data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please enter a valid Google / Gmail address")

        # Invalid short phone (< 10 digits)
        post_data = {
            'full_name': 'Sofia Ramirez',
            'email': 'sofia.ramirez@gmail.com',
            'phone_number': '12345',
            'region': 'south_bay',
        }
        response = self.client.post(reverse('join_request'), post_data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please enter a valid WhatsApp phone number")
        self.assertEqual(MembershipRequest.objects.count(), 0)

    def test_join_request_post_already_registered_member(self):
        MemberProfile.objects.create(
            email="existing@gmail.com",
            full_name="Existing Member",
            region="peninsula"
        )

        post_data = {
            'full_name': 'Existing Member',
            'email': 'existing@gmail.com',
            'phone_number': '+1 415 555 1234',
            'region': 'peninsula',
            'referral_source': 'Already here',
        }

        response = self.client.post(reverse('join_request'), post_data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "is already registered! You can sign in directly")

    def test_join_request_post_duplicate_pending_request(self):
        MembershipRequest.objects.create(
            full_name="Pending Applicant",
            email="pending.user@gmail.com",
            phone_number="+1 415 555 9999",
            region="east_bay",
            referral_source="Already submitted earlier",
            status=MembershipRequest.STATUS_PENDING
        )

        post_data = {
            'full_name': 'Pending Applicant',
            'email': 'pending.user@gmail.com',
            'phone_number': '+1 415 555 9999',
            'region': 'east_bay',
            'referral_source': 'Submitting again',
        }

        response = self.client.post(reverse('join_request'), post_data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "has already been submitted and is currently awaiting organizer review")

    def test_google_login_callback_shows_pending_notice_when_request_exists(self):
        MembershipRequest.objects.create(
            full_name="Awaiting Applicant",
            email="awaiting@gmail.com",
            phone_number="+52 55 1111 2222",
            status=MembershipRequest.STATUS_PENDING
        )

        with patch('recommendations.views.requests.post') as mock_post, \
             patch('recommendations.views.requests.get') as mock_get, \
             patch('recommendations.views.is_gmail_allowed') as mock_allowed:

            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {'access_token': 'fake_token'}

            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = {'email': 'awaiting@gmail.com', 'verified_email': True}

            mock_allowed.return_value = (False, None, False)

            session = self.client.session
            session['oauth_state'] = 'xyz123'
            session.save()

            response = self.client.get(reverse('google_callback') + '?code=auth_code&state=xyz123')
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Membership Request Pending")
            self.assertContains(response, "pending organizer review")

    def test_organizer_dashboard_displays_pending_requests(self):
        MembershipRequest.objects.create(
            full_name="Applicant Mateo",
            email="mateo@gmail.com",
            phone_number="+52 55 1234 5678",
            region="peninsula",
            city="Burlingame",
            referral_source="Referred by Sofía.",
            status=MembershipRequest.STATUS_PENDING
        )

        session = self.client.session
        session['is_verified_member'] = True
        session['is_admin'] = True
        session['member_name'] = 'Admin Organizer'
        session['member_email'] = 'admin@minimexitas.local'
        session.save()

        response = self.client.get(reverse('organizer_dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Membership Requests Queue")
        self.assertContains(response, "Applicant Mateo")
        self.assertContains(response, "mateo@gmail.com")
        self.assertContains(response, "+52 55 1234 5678")
        self.assertContains(response, "https://wa.me/525512345678")
        self.assertContains(response, "Referred by Sofía.")

    @patch('recommendations.views.update_pending_request_status_in_google_sheet')
    @patch('recommendations.views.sync_profile_to_google_sheet')
    def test_organizer_approve_request_creates_profile_and_syncs(self, mock_sync, mock_pending_update):
        mock_sync.return_value = True
        mock_pending_update.return_value = True

        req = MembershipRequest.objects.create(
            full_name="New Member Laura",
            email="laura@gmail.com",
            phone_number="+52 81 8888 9999",
            region="san_francisco",
            city="Marina SF",
            referral_source="Friend from Monterrey",
            status=MembershipRequest.STATUS_PENDING
        )

        session = self.client.session
        session['is_verified_member'] = True
        session['is_admin'] = True
        session['member_name'] = 'Admin Organizer'
        session['member_email'] = 'admin@minimexitas.local'
        session.save()

        response = self.client.post(reverse('organizer_approve_request', kwargs={'request_id': req.id}))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('organizer_dashboard'), response.url)

        req.refresh_from_db()
        self.assertEqual(req.status, MembershipRequest.STATUS_APPROVED)
        self.assertEqual(req.reviewed_by, 'Admin Organizer')
        self.assertIsNotNone(req.reviewed_at)

        profile = MemberProfile.objects.get(email='laura@gmail.com')
        self.assertEqual(profile.full_name, 'New Member Laura')
        self.assertEqual(profile.phone_number, '+52 81 8888 9999')
        self.assertEqual(profile.region, 'san_francisco')
        self.assertEqual(profile.city, 'Marina SF')

        mock_sync.assert_called_once_with(profile)
        mock_pending_update.assert_called_once_with('laura@gmail.com', 'Approved', 'Admin Organizer')

        # Check approval welcome email was sent
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('laura@gmail.com', mail.outbox[0].to)
        self.assertIn('¡Bienvenido(a) a MiniMexitas!', mail.outbox[0].subject)
        self.assertIn('Laura', mail.outbox[0].body)

    def test_organizer_approve_request_nonexistent_returns_404(self):
        session = self.client.session
        session['is_verified_member'] = True
        session['is_admin'] = True
        session['member_name'] = 'Admin Organizer'
        session['member_email'] = 'admin@minimexitas.local'
        session.save()

        response = self.client.post(reverse('organizer_approve_request', kwargs={'request_id': 99999}))
        self.assertEqual(response.status_code, 404)

    @patch('recommendations.views.update_pending_request_status_in_google_sheet')
    def test_organizer_reject_request(self, mock_pending_update):
        mock_pending_update.return_value = True

        req = MembershipRequest.objects.create(
            full_name="Spam User",
            email="spam@gmail.com",
            phone_number="+1 555 000 0000",
            region="other",
            referral_source="Random bot",
            status=MembershipRequest.STATUS_PENDING
        )

        session = self.client.session
        session['is_verified_member'] = True
        session['is_admin'] = True
        session['member_name'] = 'Admin Organizer'
        session['member_email'] = 'admin@minimexitas.local'
        session.save()

        response = self.client.post(
            reverse('organizer_reject_request', kwargs={'request_id': req.id}),
            {'review_notes': 'Outside Bay Area scope.'}
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('organizer_dashboard'), response.url)

        req.refresh_from_db()
        self.assertEqual(req.status, MembershipRequest.STATUS_REJECTED)
        self.assertEqual(req.reviewed_by, 'Admin Organizer')
        self.assertEqual(req.review_notes, 'Outside Bay Area scope.')
        self.assertFalse(MemberProfile.objects.filter(email='spam@gmail.com').exists())
        mock_pending_update.assert_called_once_with('spam@gmail.com', 'Declined', 'Admin Organizer', review_notes='Outside Bay Area scope.')

        # Check rejection email was sent
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('spam@gmail.com', mail.outbox[0].to)
        self.assertIn('Actualización sobre tu solicitud', mail.outbox[0].subject)
        self.assertIn('Outside Bay Area scope.', mail.outbox[0].body)

    def test_organizer_reject_request_nonexistent_returns_404(self):
        session = self.client.session
        session['is_verified_member'] = True
        session['is_admin'] = True
        session['member_name'] = 'Admin Organizer'
        session['member_email'] = 'admin@minimexitas.local'
        session.save()

        response = self.client.post(reverse('organizer_reject_request', kwargs={'request_id': 99999}))
        self.assertEqual(response.status_code, 404)

    @patch('recommendations.views.sync_profile_to_google_sheet')
    def test_organizer_direct_add_member(self, mock_sync):
        mock_sync.return_value = True

        session = self.client.session
        session['is_verified_member'] = True
        session['is_admin'] = True
        session['member_name'] = 'Admin Organizer'
        session['member_email'] = 'admin@minimexitas.local'
        session.save()

        post_data = {
            'full_name': 'Direct Member Juan',
            'email': 'juan.direct@gmail.com',
            'phone_number': '+52 33 1234 5678',
            'region': 'east_bay',
            'city': 'Berkeley',
            'role': 'Admin'
        }

        response = self.client.post(reverse('organizer_direct_add_member'), post_data)
        self.assertEqual(response.status_code, 302)

        profile = MemberProfile.objects.get(email='juan.direct@gmail.com')
        self.assertEqual(profile.full_name, 'Direct Member Juan')
        self.assertEqual(profile.phone_number, '+52 33 1234 5678')
        self.assertEqual(profile.region, 'east_bay')
        self.assertTrue(profile.is_admin)

        mock_sync.assert_called_once_with(profile)

    def test_organizer_direct_add_member_validation_error(self):
        session = self.client.session
        session['is_verified_member'] = True
        session['is_admin'] = True
        session['member_name'] = 'Admin Organizer'
        session['member_email'] = 'admin@minimexitas.local'
        session.save()

        # Missing name and invalid phone
        post_data = {
            'full_name': '',
            'email': 'incomplete@gmail.com',
            'phone_number': '123',
            'region': 'east_bay',
        }

        response = self.client.post(reverse('organizer_direct_add_member'), post_data)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(MemberProfile.objects.filter(email='incomplete@gmail.com').exists())

    def test_non_admin_cannot_approve_reject_or_direct_add(self):
        req = MembershipRequest.objects.create(
            full_name="Target Applicant",
            email="target@gmail.com",
            phone_number="+1 415 555 1111",
            status=MembershipRequest.STATUS_PENDING
        )

        session = self.client.session
        session['is_verified_member'] = True
        session['is_admin'] = False
        session['member_name'] = 'Regular Member'
        session['member_email'] = 'reg@minimexitas.local'
        session.save()

        # Try approving
        resp = self.client.post(reverse('organizer_approve_request', kwargs={'request_id': req.id}))
        self.assertEqual(resp.status_code, 403)

        # Try rejecting
        resp = self.client.post(reverse('organizer_reject_request', kwargs={'request_id': req.id}))
        self.assertEqual(resp.status_code, 403)

        # Try direct adding
        resp = self.client.post(reverse('organizer_direct_add_member'), {'email': 'hacked@gmail.com'})
        self.assertEqual(resp.status_code, 403)

        req.refresh_from_db()
        self.assertEqual(req.status, MembershipRequest.STATUS_PENDING)


class NotificationsTestCase(TestCase):
    def test_clean_phone_for_whatsapp(self):
        self.assertEqual(clean_phone_for_whatsapp(""), "")
        self.assertEqual(clean_phone_for_whatsapp(None), "")
        # 10 digits adds 1
        self.assertEqual(clean_phone_for_whatsapp("(415) 555-1234"), "14155551234")
        # International with country code preserved
        self.assertEqual(clean_phone_for_whatsapp("+52 55 1234 5678"), "525512345678")

    def test_build_whatsapp_approval_link(self):
        link = build_whatsapp_approval_link("Carlos Ruiz", "+52 55 9999 8888", portal_url="https://minimexitas.org")
        self.assertTrue(link.startswith("https://wa.me/525599998888?text="))
        self.assertIn("Carlos%20Ruiz", link)
        self.assertIn("aprobada", link)
        self.assertIn("https%3A//minimexitas.org/login/", link)
        self.assertNotIn("login/login", link)

        # Also verify when portal_url already ends with /login/
        link_with_login = build_whatsapp_approval_link("Carlos Ruiz", "+52 55 9999 8888", portal_url="https://minimexitas.org/login/")
        self.assertIn("https%3A//minimexitas.org/login/", link_with_login)
        self.assertNotIn("login/login", link_with_login)

    def test_build_whatsapp_approval_link_empty_phone(self):
        link = build_whatsapp_approval_link("Carlos Ruiz", "")
        self.assertEqual(link, "")

    def test_build_whatsapp_decline_link(self):
        link = build_whatsapp_decline_link("Pedro Soto", "+1 (415) 555-4321", reason="Outside service area")
        self.assertTrue(link.startswith("https://wa.me/14155554321?text="))
        self.assertIn("Pedro%20Soto", link)
        self.assertIn("Outside%20service%20area", link)

    def test_send_membership_approval_email_success(self):
        req = MembershipRequest.objects.create(
            full_name="Lucia Gomez",
            email="lucia.gomez@gmail.com",
            phone_number="+52 33 1111 2222",
            region="south_bay"
        )
        mail.outbox.clear()
        success = send_membership_approval_email(req, portal_url="https://portal.minimexitas.org/login/")
        self.assertTrue(success)
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, ["lucia.gomez@gmail.com"])
        self.assertIn("¡Bienvenido(a) a MiniMexitas!", sent.subject)
        self.assertIn("Lucia Gomez", sent.body)
        self.assertIn("https://portal.minimexitas.org/login/", sent.body)
        # Verify HTML alternative
        self.assertEqual(len(sent.alternatives), 1)
        html_body, mime = sent.alternatives[0]
        self.assertEqual(mime, "text/html")
        self.assertIn("Lucia Gomez", html_body)

    def test_send_membership_approval_email_empty_recipient(self):
        req = MembershipRequest.objects.create(
            full_name="No Email User",
            email="",
            status=MembershipRequest.STATUS_PENDING
        )
        success = send_membership_approval_email(req)
        self.assertFalse(success)

    @patch('recommendations.notifications.EmailMultiAlternatives.send')
    def test_send_membership_approval_email_handles_backend_exception(self, mock_send):
        mock_send.side_effect = Exception("SMTP Server Connection Timeout")
        req = MembershipRequest.objects.create(
            full_name="Timeout User",
            email="timeout.user@gmail.com",
            status=MembershipRequest.STATUS_PENDING
        )
        success = send_membership_approval_email(req)
        self.assertFalse(success)

    def test_send_membership_rejection_email_success(self):
        req = MembershipRequest.objects.create(
            full_name="Mateo Perez",
            email="mateo.perez@gmail.com",
            phone_number="+1 415 555 3333",
            region="east_bay"
        )
        mail.outbox.clear()
        success = send_membership_rejection_email(req, reason="Location is outside Bay Area.")
        self.assertTrue(success)
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, ["mateo.perez@gmail.com"])
        self.assertIn("Actualización sobre tu solicitud", sent.subject)
        self.assertIn("Mateo Perez", sent.body)
        self.assertIn("Location is outside Bay Area.", sent.body)
        # Verify HTML alternative
        self.assertEqual(len(sent.alternatives), 1)
        html_body, mime = sent.alternatives[0]
        self.assertEqual(mime, "text/html")
        self.assertIn("Mateo Perez", html_body)
        self.assertIn("Location is outside Bay Area.", html_body)

    def test_send_membership_rejection_email_empty_recipient(self):
        req = MembershipRequest.objects.create(
            full_name="Blank Email",
            email="",
            status=MembershipRequest.STATUS_PENDING
        )
        success = send_membership_rejection_email(req)
        self.assertFalse(success)

    @patch('recommendations.notifications.EmailMultiAlternatives.send')
    def test_send_membership_rejection_email_handles_backend_exception(self, mock_send):
        mock_send.side_effect = Exception("SMTP Rejection Error")
        req = MembershipRequest.objects.create(
            full_name="Error User",
            email="error.rejection@gmail.com",
            status=MembershipRequest.STATUS_PENDING
        )
        success = send_membership_rejection_email(req)
        self.assertFalse(success)


class StaticFilesServingTestCase(TestCase):
    def test_static_files_serve_under_debug_false(self):
        response = self.client.get('/static/images/banners/welcome.png')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/png')

    def test_manifest_serves_under_debug_false(self):
        response = self.client.get('/static/manifest.json')
        self.assertEqual(response.status_code, 200)


@override_settings(GOOGLE_SHEET_KEY="mock_test_sheet_key_123")
class MemberDeletionTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.member = MemberProfile.objects.create(
            full_name="Diego Rivera",
            email="diego.rivera@gmail.com",
            phone_number="+1 415 555 9999",
            region="san_francisco",
            city="Mission District",
            is_admin=False
        )
        self.admin = MemberProfile.objects.create(
            full_name="Frida Admin",
            email="frida.admin@gmail.com",
            phone_number="+52 55 1234 5678",
            region="south_bay",
            is_admin=True
        )

    @patch('recommendations.views.delete_profile_from_google_sheet')
    def test_member_self_deletion_flow(self, mock_sheet_delete):
        mock_sheet_delete.return_value = True
        
        # Log in as member
        session = self.client.session
        session['is_verified_member'] = True
        session['member_email'] = "diego.rivera@gmail.com"
        session['member_name'] = "Diego Rivera"
        session['is_admin'] = False
        session.save()

        # POST to self-deletion endpoint
        response = self.client.post(reverse('profile_delete_self'), follow=True)
        self.assertEqual(response.status_code, 200)

        # Verify DB deletion
        self.assertFalse(MemberProfile.objects.filter(email="diego.rivera@gmail.com").exists())

        # Verify Google Sheet deletion was triggered
        mock_sheet_delete.assert_called_once_with("diego.rivera@gmail.com")

        # Verify redirect to login page and flash message displayed
        self.assertContains(response, "Account & Profile Deleted")
        self.assertContains(response, "Your profile has been removed from MiniMexitas")

    def test_member_self_deletion_get_rejected(self):
        # Log in as member
        session = self.client.session
        session['is_verified_member'] = True
        session['member_email'] = "diego.rivera@gmail.com"
        session.save()

        # GET should redirect back to profile page without deleting
        response = self.client.get(reverse('profile_delete_self'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('profile'))
        self.assertTrue(MemberProfile.objects.filter(email="diego.rivera@gmail.com").exists())

    @patch('recommendations.views.delete_profile_from_google_sheet')
    def test_admin_delete_member_flow(self, mock_sheet_delete):
        mock_sheet_delete.return_value = True

        # Log in as admin
        session = self.client.session
        session['is_verified_member'] = True
        session['member_email'] = "frida.admin@gmail.com"
        session['member_name'] = "Frida Admin"
        session['is_admin'] = True
        session.save()

        # Admin deletes Diego
        response = self.client.post(reverse('organizer_delete_member', kwargs={'member_id': self.member.id}), follow=True)
        self.assertEqual(response.status_code, 200)

        # Verify member is deleted
        self.assertFalse(MemberProfile.objects.filter(id=self.member.id).exists())
        mock_sheet_delete.assert_called_once_with("diego.rivera@gmail.com")
        self.assertContains(response, "Member &#x27;Diego Rivera&#x27; has been successfully removed")

    def test_admin_cannot_delete_self_from_organizer_dashboard(self):
        # Log in as admin
        session = self.client.session
        session['is_verified_member'] = True
        session['member_email'] = "frida.admin@gmail.com"
        session['member_name'] = "Frida Admin"
        session['is_admin'] = True
        session.save()

        # Admin tries to delete herself via organizer dashboard
        response = self.client.post(reverse('organizer_delete_member', kwargs={'member_id': self.admin.id}), follow=True)
        self.assertEqual(response.status_code, 200)

        # Verify admin profile was NOT deleted
        self.assertTrue(MemberProfile.objects.filter(id=self.admin.id).exists())
        self.assertContains(response, "organizers cannot delete themselves from the organizer dashboard")

    def test_unauthenticated_cannot_delete_member(self):
        response = self.client.post(reverse('organizer_delete_member', kwargs={'member_id': self.member.id}))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('login_page'), response.url)
        self.assertTrue(MemberProfile.objects.filter(id=self.member.id).exists())

    def test_regular_member_cannot_delete_other_member(self):
        session = self.client.session
        session['is_verified_member'] = True
        session['member_email'] = "diego.rivera@gmail.com"
        session['is_admin'] = False
        session.save()

        response = self.client.post(reverse('organizer_delete_member', kwargs={'member_id': self.admin.id}))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(MemberProfile.objects.filter(id=self.admin.id).exists())

    @patch('recommendations.sheets.get_gspread_client')
    def test_delete_profile_from_google_sheet_unit(self, mock_get_client):
        mock_ws = MagicMock()
        mock_ws.get_all_values.return_value = [
            ['Name', 'Gmail', 'Region', 'Phone'],
            ['Diego Rivera', 'diego.rivera@gmail.com', 'San Francisco', '+1 415 555 9999'],
            ['Frida Admin', 'frida.admin@gmail.com', 'South Bay', '+52 55 1234 5678']
        ]
        mock_sh = MagicMock()
        mock_sh.worksheet.return_value = mock_ws
        mock_gc = MagicMock()
        mock_gc.open.return_value = mock_sh
        mock_gc.open_by_key.return_value = mock_sh
        mock_get_client.return_value = mock_gc

        success = delete_profile_from_google_sheet("diego.rivera@gmail.com")
        self.assertTrue(success)
        mock_ws.delete_rows.assert_called_once_with(2)

    @patch('recommendations.sheets.get_gspread_client')
    def test_delete_profile_from_google_sheet_not_found(self, mock_get_client):
        mock_ws = MagicMock()
        mock_ws.get_all_values.return_value = [
            ['Name', 'Gmail', 'Region', 'Phone'],
            ['Frida Admin', 'frida.admin@gmail.com', 'South Bay', '+52 55 1234 5678']
        ]
        mock_sh = MagicMock()
        mock_sh.worksheet.return_value = mock_ws
        mock_gc = MagicMock()
        mock_gc.open.return_value = mock_sh
        mock_gc.open_by_key.return_value = mock_sh
        mock_get_client.return_value = mock_gc

        success = delete_profile_from_google_sheet("absent.user@gmail.com")
        self.assertTrue(success)
        mock_ws.delete_rows.assert_not_called()


@override_settings(GOOGLE_SHEET_KEY="mock_test_sheet_key_123")
class ReconciliationSyncTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        # Diego is in SQLite but will NOT be in Google Sheet (orphan)
        self.orphan_member = MemberProfile.objects.create(
            full_name="Diego Orphan",
            email="diego.orphan@gmail.com",
            phone_number="+1 415 555 1111",
            region="san_francisco",
            is_admin=False
        )
        # Frida is in both SQLite and Google Sheet
        self.admin = MemberProfile.objects.create(
            full_name="Frida Admin",
            email="frida.admin@gmail.com",
            phone_number="+52 55 1234 5678",
            region="south_bay",
            is_admin=True
        )

    @patch('recommendations.sheets.get_gspread_client')
    def test_reconcile_purges_orphan_and_imports_new(self, mock_get_client):
        # Google Sheet contains Frida Admin and a NEW member Carlos New, but NOT Diego Orphan
        mock_ws = MagicMock()
        mock_ws.get_all_values.return_value = [
            ['Name', 'Gmail', 'Region', 'City', 'Phone', 'Role'],
            ['Frida Admin', 'frida.admin@gmail.com', 'South Bay', 'San Jose', '+52 55 1234 5678', 'Admin'],
            ['Carlos New', 'carlos.new@gmail.com', 'Peninsula', 'Palo Alto', '+1 650 555 2222', 'Member']
        ]
        mock_sh = MagicMock()
        mock_sh.worksheet.return_value = mock_ws
        mock_gc = MagicMock()
        mock_gc.open.return_value = mock_sh
        mock_gc.open_by_key.return_value = mock_sh
        mock_get_client.return_value = mock_gc

        result = reconcile_members_with_google_sheet()
        self.assertTrue(result['success'])
        self.assertEqual(result['purged_count'], 1)
        self.assertIn('diego.orphan@gmail.com', result['purged_emails'])
        self.assertEqual(result['created_count'], 1)

        # Verify Diego is purged from SQLite
        self.assertFalse(MemberProfile.objects.filter(email="diego.orphan@gmail.com").exists())

        # Verify Carlos is created in SQLite
        carlos = MemberProfile.objects.filter(email="carlos.new@gmail.com").first()
        self.assertIsNotNone(carlos)
        self.assertEqual(carlos.full_name, "Carlos New")
        self.assertEqual(carlos.region, "peninsula")
        self.assertEqual(carlos.city, "Palo Alto")

    @patch('recommendations.sheets.get_gspread_client')
    def test_reconcile_handles_network_error_safely(self, mock_get_client):
        mock_get_client.side_effect = Exception("Google API timeout")

        result = reconcile_members_with_google_sheet()
        self.assertFalse(result['success'])
        # Safety guard: existing records should not be deleted on network error
        self.assertTrue(MemberProfile.objects.filter(email="diego.orphan@gmail.com").exists())

    @patch('recommendations.views.reconcile_members_with_google_sheet')
    def test_organizer_sync_sheets_post_view(self, mock_reconcile):
        mock_reconcile.return_value = {
            'success': True,
            'purged_count': 1,
            'created_count': 2,
            'updated_count': 0,
            'total_sheet_members': 3,
            'purged_emails': ['diego.orphan@gmail.com'],
        }

        # Log in as admin
        session = self.client.session
        session['is_verified_member'] = True
        session['member_email'] = "frida.admin@gmail.com"
        session['member_name'] = "Frida Admin"
        session['is_admin'] = True
        session.save()

        response = self.client.post(reverse('organizer_sync_sheets'), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Directory Sync Complete: 1 purged")
        self.assertContains(response, "2 new members imported")

    def test_member_required_invalidates_session_for_purged_profile(self):
        # Diego is logged in with active session
        session = self.client.session
        session['is_verified_member'] = True
        session['member_email'] = "diego.orphan@gmail.com"
        session['member_name'] = "Diego Orphan"
        session['is_admin'] = False
        session.save()

        # Delete Diego's profile (simulating purge)
        self.orphan_member.delete()

        # Diego tries to access member directory
        response = self.client.get(reverse('member_directory'))
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "Your access was revoked because your account is no longer registered", status_code=403)


class OpenGoogleSpreadsheetConfigTestCase(TestCase):
    def test_open_by_sheet_key_when_configured(self):
        mock_gc = MagicMock()
        with override_settings(GOOGLE_SHEET_KEY="1abc_test_sheet_key_123"):
            open_google_spreadsheet(mock_gc)
            mock_gc.open_by_key.assert_called_once_with("1abc_test_sheet_key_123")

    def test_open_raises_value_error_when_key_is_missing(self):
        mock_gc = MagicMock()
        with override_settings(GOOGLE_SHEET_KEY=""):
            with patch.dict('os.environ', {'GOOGLE_SHEET_KEY': '', 'GOOGLE_SHEET_ID': ''}):
                with self.assertRaises(ValueError) as ctx:
                    open_google_spreadsheet(mock_gc)
                self.assertIn("GOOGLE_SHEET_KEY is not configured", str(ctx.exception))


@override_settings(GOOGLE_SHEET_KEY="mock_test_sheet_key_123", PORTAL_BASE_URL="https://testportal.minimexitas.org")
class AdminNotificationAndAuditLogTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        # Create 2 admins and 1 regular member
        self.admin1 = MemberProfile.objects.create(
            email="organizer1@minimexitas.org",
            full_name="Admin One",
            is_admin=True,
            region="san_francisco"
        )
        self.admin2 = MemberProfile.objects.create(
            email="organizer2@minimexitas.org",
            full_name="Admin Two",
            is_admin=True,
            region="east_bay"
        )
        self.regular_member = MemberProfile.objects.create(
            email="regular@gmail.com",
            full_name="Regular Member",
            is_admin=False,
            region="south_bay"
        )

    def test_membership_audit_log_str(self):
        log = MembershipAuditLog.objects.create(
            action=MembershipAuditLog.ACTION_APPROVED,
            target_email="newbie@gmail.com",
            target_name="Newbie Member",
            actor_name="Admin One",
            actor_email="organizer1@minimexitas.org",
            notes="Approved after phone interview"
        )
        self.assertIn("Request Approved", str(log))
        self.assertIn("Newbie Member", str(log))
        self.assertIn("Admin One", str(log))

    def test_send_admin_new_request_notification_dispatches_to_all_admins(self):
        req = MembershipRequest.objects.create(
            full_name="Carlos Santana",
            email="carlos.guitar@gmail.com",
            phone_number="+1 415 555 7788",
            region="san_francisco",
            city="Mission District",
            referral_source="Friend in the Bay Area Mexican meetup",
            status=MembershipRequest.STATUS_PENDING
        )

        mail.outbox.clear()
        sent = send_admin_new_request_notification(req)
        self.assertTrue(sent)
        self.assertEqual(len(mail.outbox), 1)

        email = mail.outbox[0]
        self.assertIn("Carlos Santana", email.subject)
        self.assertIn("organizer1@minimexitas.org", email.to)
        self.assertIn("organizer2@minimexitas.org", email.to)
        self.assertNotIn("regular@gmail.com", email.to)
        self.assertIn("Carlos Santana", email.body)
        self.assertIn("carlos.guitar@gmail.com", email.body)
        self.assertIn("Mission District", email.body)
        self.assertIn("Friend in the Bay Area", email.body)
        self.assertIn("https://testportal.minimexitas.org/organizers/", email.body)

    def test_send_admin_new_request_notification_no_admins_returns_false(self):
        MemberProfile.objects.filter(is_admin=True).delete()
        req = MembershipRequest.objects.create(
            full_name="Orphan Applicant",
            email="orphan@gmail.com",
            status=MembershipRequest.STATUS_PENDING
        )
        mail.outbox.clear()
        sent = send_admin_new_request_notification(req)
        self.assertFalse(sent)
        self.assertEqual(len(mail.outbox), 0)

    def test_send_admin_new_request_notification_uses_request_host(self):
        req = MembershipRequest.objects.create(
            full_name="Lucia Mendez",
            email="lucia.m@gmail.com",
            status=MembershipRequest.STATUS_PENDING
        )
        mail.outbox.clear()
        factory = RequestFactory()
        mock_request = factory.get('/join/')
        with override_settings(PORTAL_BASE_URL=""):
            sent = send_admin_new_request_notification(req, portal_url="", request=mock_request)
            self.assertTrue(sent)
            self.assertEqual(len(mail.outbox), 1)
            email = mail.outbox[0]
            self.assertIn("http://testserver/organizers/", email.body)
            self.assertNotIn("http:///organizers/", email.body)
            self.assertNotIn("href=\"/organizers/\"", email.alternatives[0][0])
            self.assertIn("http://testserver/organizers/", email.alternatives[0][0])

    @patch('recommendations.views.sync_pending_request_to_google_sheet')
    def test_join_request_view_creates_audit_log_and_emails_admins(self, mock_sync_pending):
        mock_sync_pending.return_value = True
        mail.outbox.clear()

        response = self.client.post(reverse('join_request'), {
            'full_name': 'Gabriela Mistral',
            'email': 'gabriela.poet@gmail.com',
            'phone_number': '+52 55 1234 5678',
            'region': 'peninsula',
            'city': 'Palo Alto',
            'referral_source': 'Found on community Instagram',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Request Received!")
        self.assertContains(response, "Pending Organizer Review")

        # Verify request record in DB
        req = MembershipRequest.objects.get(email='gabriela.poet@gmail.com')
        self.assertEqual(req.status, MembershipRequest.STATUS_PENDING)

        # Verify Audit Log entry created
        audit_log = MembershipAuditLog.objects.filter(target_email='gabriela.poet@gmail.com').first()
        self.assertIsNotNone(audit_log)
        self.assertEqual(audit_log.action, MembershipAuditLog.ACTION_SUBMITTED)
        self.assertEqual(audit_log.target_name, 'Gabriela Mistral')
        self.assertIn('Palo Alto', audit_log.notes)

        # Verify email dispatched to admins
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Gabriela Mistral", mail.outbox[0].subject)
        self.assertIn("organizer1@minimexitas.org", mail.outbox[0].to)

    @patch('recommendations.views.sync_profile_to_google_sheet')
    @patch('recommendations.views.update_pending_request_status_in_google_sheet')
    def test_organizer_approve_request_creates_audit_log_and_records_reviewer(self, mock_update_status, mock_sync_profile):
        mock_update_status.return_value = True
        mock_sync_profile.return_value = True

        req = MembershipRequest.objects.create(
            full_name="Valentina Gomez",
            email="valentina@gmail.com",
            phone_number="+1 415 555 9900",
            region="east_bay",
            status=MembershipRequest.STATUS_PENDING
        )

        session = self.client.session
        session['is_verified_member'] = True
        session['member_email'] = "organizer1@minimexitas.org"
        session['member_name'] = "Admin One"
        session['is_admin'] = True
        session.save()

        response = self.client.post(reverse('organizer_approve_request', kwargs={'request_id': req.id}))
        self.assertRedirects(response, reverse('organizer_dashboard') + '?approved=1')

        # Verify request updated with reviewer info
        req.refresh_from_db()
        self.assertEqual(req.status, MembershipRequest.STATUS_APPROVED)
        self.assertEqual(req.reviewed_by, "Admin One")
        self.assertIsNotNone(req.reviewed_at)

        # Verify Audit Log entry created
        audit_log = MembershipAuditLog.objects.filter(target_email='valentina@gmail.com', action=MembershipAuditLog.ACTION_APPROVED).first()
        self.assertIsNotNone(audit_log)
        self.assertEqual(audit_log.actor_name, "Admin One")
        self.assertEqual(audit_log.actor_email, "organizer1@minimexitas.org")

    @patch('recommendations.views.update_pending_request_status_in_google_sheet')
    def test_organizer_reject_request_creates_audit_log_and_records_reviewer_notes(self, mock_update_status):
        mock_update_status.return_value = True

        req = MembershipRequest.objects.create(
            full_name="Spam Bot",
            email="spambot@gmail.com",
            phone_number="+1 555 000 0000",
            region="south_bay",
            status=MembershipRequest.STATUS_PENDING
        )

        session = self.client.session
        session['is_verified_member'] = True
        session['member_email'] = "organizer2@minimexitas.org"
        session['member_name'] = "Admin Two"
        session['is_admin'] = True
        session.save()

        response = self.client.post(reverse('organizer_reject_request', kwargs={'request_id': req.id}), {
            'review_notes': 'Applicant does not appear to be located in the Bay Area.'
        })
        self.assertRedirects(response, reverse('organizer_dashboard') + '?declined=1')

        # Verify request updated with reviewer info & decline notes
        req.refresh_from_db()
        self.assertEqual(req.status, MembershipRequest.STATUS_REJECTED)
        self.assertEqual(req.reviewed_by, "Admin Two")
        self.assertEqual(req.review_notes, "Applicant does not appear to be located in the Bay Area.")
        self.assertIsNotNone(req.reviewed_at)

        # Verify Audit Log entry created
        audit_log = MembershipAuditLog.objects.filter(target_email='spambot@gmail.com', action=MembershipAuditLog.ACTION_REJECTED).first()
        self.assertIsNotNone(audit_log)
        self.assertEqual(audit_log.actor_name, "Admin Two")
        self.assertEqual(audit_log.actor_email, "organizer2@minimexitas.org")
        self.assertIn("does not appear to be located in the Bay Area", audit_log.notes)

    @patch('recommendations.views.delete_profile_from_google_sheet')
    def test_organizer_delete_member_creates_audit_log(self, mock_delete_sheet):
        mock_delete_sheet.return_value = True

        target_member = MemberProfile.objects.create(
            email="to_be_deleted@gmail.com",
            full_name="Member To Delete",
            is_admin=False
        )

        session = self.client.session
        session['is_verified_member'] = True
        session['member_email'] = "organizer1@minimexitas.org"
        session['member_name'] = "Admin One"
        session['is_admin'] = True
        session.save()

        response = self.client.post(reverse('organizer_delete_member', kwargs={'member_id': target_member.id}))
        self.assertRedirects(response, reverse('organizer_dashboard') + '?member_deleted=1')

        audit_log = MembershipAuditLog.objects.filter(target_email='to_be_deleted@gmail.com', action=MembershipAuditLog.ACTION_DELETED).first()
        self.assertIsNotNone(audit_log)
        self.assertEqual(audit_log.actor_name, "Admin One")
        self.assertEqual(audit_log.actor_email, "organizer1@minimexitas.org")

    def test_organizer_dashboard_displays_audit_log_and_review_history(self):
        # Create sample audit logs
        MembershipAuditLog.objects.create(
            action=MembershipAuditLog.ACTION_APPROVED,
            target_email="test.approved@gmail.com",
            target_name="Approved User",
            actor_name="Admin One",
            actor_email="organizer1@minimexitas.org",
            notes="Approved after review"
        )
        MembershipAuditLog.objects.create(
            action=MembershipAuditLog.ACTION_REJECTED,
            target_email="test.rejected@gmail.com",
            target_name="Rejected User",
            actor_name="Admin Two",
            actor_email="organizer2@minimexitas.org",
            notes="No connection to Bay Area"
        )

        session = self.client.session
        session['is_verified_member'] = True
        session['member_email'] = "organizer1@minimexitas.org"
        session['member_name'] = "Admin One"
        session['is_admin'] = True
        session.save()

        response = self.client.get(reverse('organizer_dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Review History & Audit Log")
        self.assertContains(response, "test.approved@gmail.com")
        self.assertContains(response, "test.rejected@gmail.com")
        self.assertContains(response, "Admin One")
        self.assertContains(response, "Admin Two")
        self.assertContains(response, "Approved after review")
        self.assertContains(response, "No connection to Bay Area")


@override_settings(GOOGLE_SHEET_KEY="mock_test_sheet_key_123")
class SurveysFeatureTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()

        self.voter_profile = MemberProfile.objects.create(
            email="voter@gmail.com",
            full_name="Voter Member",
            is_admin=False
        )
        self.admin_profile = MemberProfile.objects.create(
            email="admin@minimexitas.org",
            full_name="Super Admin",
            is_admin=True
        )
        self.foodie_profile = MemberProfile.objects.create(
            email="foodie@gmail.com",
            full_name="Foodie Member",
            is_admin=False
        )

        self.survey = CommunitySurvey.objects.create(
            title="Where should we host the 2026 Fall Picnic?",
            description="Cast your vote for our next community gathering.",
            category=CommunitySurvey.CATEGORY_EVENT,
            is_multiple_choice=False,
            is_active=True,
            created_by="Admin Maria",
            created_by_email="admin.maria@gmail.com"
        )
        self.opt1 = SurveyOption.objects.create(survey=self.survey, text="Coyote Point (San Mateo)", order=0)
        self.opt2 = SurveyOption.objects.create(survey=self.survey, text="Vasona Lake (Los Gatos)", order=1)
        self.opt3 = SurveyOption.objects.create(survey=self.survey, text="Lake Merritt (Oakland)", order=2)

    def test_survey_models_and_properties(self):
        self.assertEqual(self.survey.options.count(), 3)
        self.assertEqual(self.survey.total_votes, 0)
        self.assertEqual(self.survey.unique_voters_count, 0)
        self.assertFalse(self.survey.has_user_voted("member@gmail.com"))

        SurveyVote.objects.create(
            survey=self.survey,
            option=self.opt1,
            voter_email="member@gmail.com",
            voter_name="Member One"
        )
        self.assertEqual(self.survey.total_votes, 1)
        self.assertEqual(self.survey.unique_voters_count, 1)
        self.assertTrue(self.survey.has_user_voted("member@gmail.com"))
        self.assertEqual(self.opt1.vote_count, 1)
        self.assertEqual(self.opt1.percentage(1), 100.0)
        self.assertEqual(self.opt2.percentage(1), 0.0)
        self.assertIn("Member One", self.opt1.voter_names)

    def test_surveys_list_view_requires_auth(self):
        response = self.client.get(reverse('surveys'))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('login_page'), response.url)

    def test_surveys_list_view_authenticated(self):
        session = self.client.session
        session['is_verified_member'] = True
        session['member_email'] = "voter@gmail.com"
        session['member_name'] = "Voter Member"
        session.save()

        response = self.client.get(reverse('surveys'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Surveys & Decision Center")
        self.assertContains(response, "Where should we host the 2026 Fall Picnic?")
        self.assertContains(response, "Coyote Point (San Mateo)")
        self.assertContains(response, "Posted by Admin Maria")
        self.assertNotContains(response, "{{ survey.created_by")

    @patch('recommendations.views.sync_survey_to_google_sheet')
    def test_survey_vote_view_single_choice(self, mock_sync):
        mock_sync.return_value = True
        session = self.client.session
        session['is_verified_member'] = True
        session['member_email'] = "voter@gmail.com"
        session['member_name'] = "Voter Member"
        session.save()

        response = self.client.post(reverse('survey_vote', args=[self.survey.id]), {
            'options': [str(self.opt2.id)]
        })
        self.assertEqual(response.status_code, 302)
        self.assertIn('voted=1', response.url)
        self.assertEqual(SurveyVote.objects.filter(survey=self.survey).count(), 1)
        vote = SurveyVote.objects.get(survey=self.survey)
        self.assertEqual(vote.option, self.opt2)
        self.assertEqual(vote.voter_email, "voter@gmail.com")
        mock_sync.assert_called_once_with(self.survey)

    @patch('recommendations.views.sync_survey_to_google_sheet')
    def test_survey_vote_view_multiple_choice(self, mock_sync):
        mock_sync.return_value = True
        multi_survey = CommunitySurvey.objects.create(
            title="Which cuisines should we feature?",
            is_multiple_choice=True,
            is_active=True
        )
        opt_a = SurveyOption.objects.create(survey=multi_survey, text="Oaxacan", order=0)
        opt_b = SurveyOption.objects.create(survey=multi_survey, text="Yucatecan", order=1)

        session = self.client.session
        session['is_verified_member'] = True
        session['member_email'] = "foodie@gmail.com"
        session['member_name'] = "Foodie Member"
        session.save()

        response = self.client.post(reverse('survey_vote', args=[multi_survey.id]), {
            'options': [str(opt_a.id), str(opt_b.id)]
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(SurveyVote.objects.filter(survey=multi_survey).count(), 2)

    @patch('recommendations.views.sync_survey_to_google_sheet')
    def test_survey_vote_view_update_existing_vote(self, mock_sync):
        mock_sync.return_value = True
        session = self.client.session
        session['is_verified_member'] = True
        session['member_email'] = "voter@gmail.com"
        session['member_name'] = "Voter Member"
        session.save()

        # Vote 1
        self.client.post(reverse('survey_vote', args=[self.survey.id]), {
            'options': [str(self.opt1.id)]
        })
        self.assertEqual(SurveyVote.objects.filter(survey=self.survey).count(), 1)
        self.assertEqual(self.opt1.votes.count(), 1)

        # Vote 2 (Change to opt2)
        self.client.post(reverse('survey_vote', args=[self.survey.id]), {
            'options': [str(self.opt2.id)]
        })
        self.assertEqual(SurveyVote.objects.filter(survey=self.survey).count(), 1)
        self.assertEqual(self.opt1.votes.count(), 0)
        self.assertEqual(self.opt2.votes.count(), 1)

    def test_survey_vote_view_rejects_closed_survey(self):
        self.survey.is_active = False
        self.survey.save()

        session = self.client.session
        session['is_verified_member'] = True
        session['member_email'] = "voter@gmail.com"
        session.save()

        response = self.client.post(reverse('survey_vote', args=[self.survey.id]), {
            'options': [str(self.opt1.id)]
        })
        self.assertEqual(response.status_code, 302)
        self.assertIn('error=survey_closed', response.url)
        self.assertEqual(SurveyVote.objects.count(), 0)

    @patch('recommendations.views.sync_survey_to_google_sheet')
    def test_organizer_survey_create_view(self, mock_sync):
        mock_sync.return_value = True
        session = self.client.session
        session['is_verified_member'] = True
        session['is_admin'] = True
        session['member_email'] = "admin@minimexitas.org"
        session['member_name'] = "Super Admin"
        session.save()

        response = self.client.post(reverse('survey_create'), {
            'title': "Favorite Dia de los Muertos activity?",
            'description': "Let organizers know your preference.",
            'category': 'community',
            'is_multiple_choice': 'on',
            'options': ['Altar Workshop', 'Catrina Contest', 'Pan de Muerto Tasting'],
            'notify_members': '1'
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('surveys'))

        new_survey = CommunitySurvey.objects.get(title="Favorite Dia de los Muertos activity?")
        self.assertTrue(new_survey.is_multiple_choice)
        self.assertEqual(new_survey.options.count(), 3)
        self.assertTrue(MembershipAuditLog.objects.filter(action=MembershipAuditLog.ACTION_SURVEY_CREATED).exists())

    @patch('recommendations.views.send_survey_broadcast_email')
    @patch('recommendations.views.sync_audit_log_to_google_sheet')
    def test_organizer_survey_broadcast_view(self, mock_audit_sync, mock_send_email):
        mock_send_email.return_value = 5
        session = self.client.session
        session['is_verified_member'] = True
        session['is_admin'] = True
        session['member_email'] = "admin@minimexitas.org"
        session['member_name'] = "Super Admin"
        session.save()

        response = self.client.post(reverse('survey_broadcast', args=[self.survey.id]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('surveys'))
        mock_send_email.assert_called_once()
        self.assertIn("survey_flash_message", self.client.session)

    def test_organizer_survey_create_requires_admin(self):
        session = self.client.session
        session['is_verified_member'] = True
        session['is_admin'] = False
        session.save()

        response = self.client.post(reverse('survey_create'), {
            'title': "Non admin test",
            'options': ['Option 1', 'Option 2']
        })
        self.assertEqual(response.status_code, 403)

    @patch('recommendations.views.sync_survey_to_google_sheet')
    def test_organizer_survey_toggle_status_view(self, mock_sync):
        mock_sync.return_value = True
        session = self.client.session
        session['is_verified_member'] = True
        session['is_admin'] = True
        session['member_email'] = "admin@minimexitas.org"
        session.save()

        self.assertTrue(self.survey.is_active)
        response = self.client.post(reverse('survey_toggle_status', args=[self.survey.id]))
        self.assertEqual(response.status_code, 302)
        self.survey.refresh_from_db()
        self.assertFalse(self.survey.is_active)

    @patch('recommendations.views.delete_survey_from_google_sheet')
    def test_organizer_survey_delete_view(self, mock_delete_sheet):
        mock_delete_sheet.return_value = True
        session = self.client.session
        session['is_verified_member'] = True
        session['is_admin'] = True
        session['member_email'] = "admin@minimexitas.org"
        session.save()

        survey_id = self.survey.id
        response = self.client.post(reverse('survey_delete', args=[survey_id]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(CommunitySurvey.objects.filter(id=survey_id).exists())
        mock_delete_sheet.assert_called_once_with(survey_id)

    @patch('recommendations.sheets.get_gspread_client')
    def test_sync_survey_to_google_sheet(self, mock_get_client):
        mock_ws = MagicMock()
        mock_ws.get_all_values.return_value = [
            ['Survey ID', 'Survey Question', 'Category', 'Mode', 'Status', 'Option Text', 'Votes Count', 'Percentage', 'Voter Names', 'Created By', 'Created At']
        ]
        mock_sh = MagicMock()
        mock_sh.worksheet.return_value = mock_ws
        mock_client = MagicMock()
        mock_client.open.return_value = mock_sh
        mock_client.open_by_key.return_value = mock_sh
        mock_get_client.return_value = mock_client

        res = sync_survey_to_google_sheet(self.survey)
        self.assertTrue(res)
        mock_ws.update.assert_called_once()

    @patch('recommendations.sheets.get_gspread_client')
    def test_delete_survey_from_google_sheet(self, mock_get_client):
        mock_ws = MagicMock()
        mock_ws.get_all_values.return_value = [
            ['Survey ID', 'Survey Question', 'Category', 'Mode', 'Status', 'Option Text', 'Votes Count', 'Percentage', 'Voter Names', 'Created By', 'Created At'],
            [str(self.survey.id), 'Where to host?', 'Event & Meetup', 'Single Choice', 'Active', 'Coyote Point', '1', '100%', 'Maria', 'Admin', '2026-08-17 10:00']
        ]
        mock_sh = MagicMock()
        mock_sh.worksheet.return_value = mock_ws
        mock_client = MagicMock()
        mock_client.open.return_value = mock_sh
        mock_client.open_by_key.return_value = mock_sh
        mock_get_client.return_value = mock_client

        res = delete_survey_from_google_sheet(self.survey.id)
        self.assertTrue(res)
        mock_ws.update.assert_called_once()

    def test_survey_detail_view_requires_auth_and_preserves_next(self):
        response = self.client.get(reverse('survey_detail', args=[self.survey.id]))
        self.assertEqual(response.status_code, 302)
        expected_next = f"/surveys/{self.survey.id}/"
        self.assertIn(f"next={quote_plus(expected_next)}", response.url)

    def test_survey_detail_view_authenticated(self):
        session = self.client.session
        session['is_verified_member'] = True
        session['member_email'] = "voter@gmail.com"
        session['member_name'] = "Voter Member"
        session.save()

        response = self.client.get(reverse('survey_detail', args=[self.survey.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.survey.title)
        self.assertContains(response, f'id="survey-{self.survey.id}"')
        self.assertContains(response, "Shared Survey Direct Link")
        self.assertContains(response, "copy-survey-link-btn")
        self.assertContains(response, "data-survey-url=")
        self.assertNotContains(response, "Share on WhatsApp")

    def test_survey_detail_view_closed_survey_visible(self):
        self.survey.is_active = False
        self.survey.save()

        session = self.client.session
        session['is_verified_member'] = True
        session['member_email'] = "voter@gmail.com"
        session['member_name'] = "Voter Member"
        session.save()

        response = self.client.get(reverse('survey_detail', args=[self.survey.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.survey.title)
        self.assertContains(response, "Shared Survey Direct Link")

    def test_surveys_list_view_with_survey_query_param(self):
        session = self.client.session
        session['is_verified_member'] = True
        session['member_email'] = "voter@gmail.com"
        session['member_name'] = "Voter Member"
        session.save()

        response = self.client.get(f"{reverse('surveys')}?survey={self.survey.id}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.survey.title)
        self.assertContains(response, "Shared Survey Direct Link")

    @patch('recommendations.views.requests.post')
    @patch('recommendations.views.requests.get')
    @patch('recommendations.views.is_gmail_allowed')
    def test_full_oauth_next_redirect_to_survey(self, mock_is_allowed, mock_get, mock_post):
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {'access_token': 'valid_token_123'}

        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {'email': 'voter@gmail.com', 'name': 'Voter Member', 'verified_email': True}

        mock_is_allowed.return_value = (True, 'Voter Member', False)

        # 1. Unauthenticated hit on survey_detail redirects to login with ?next=
        target_path = f"/surveys/{self.survey.id}/"
        unauth_resp = self.client.get(target_path)
        self.assertEqual(unauth_resp.status_code, 302)
        self.assertIn(f"next={quote_plus(target_path)}", unauth_resp.url)

        # 2. Login page renders sign-in button carrying next param
        login_resp = self.client.get(f"/login/?next={quote_plus(target_path)}")
        self.assertEqual(login_resp.status_code, 200)
        self.assertContains(login_resp, f"next={target_path}")

        # 3. Initiating Gmail login stores next into session
        init_login_resp = self.client.get(f"{reverse('gmail_login')}?next={quote_plus(target_path)}")
        self.assertEqual(init_login_resp.status_code, 302)
        self.assertEqual(self.client.session.get('oauth_next_url'), target_path)

        # 4. OAuth callback redirects to the stored next survey URL
        session = self.client.session
        session['oauth_state'] = 'test_state_123'
        session['oauth_next_url'] = target_path
        session.save()

        callback_resp = self.client.get(f"{reverse('google_callback')}?code=test_code&state=test_state_123")
        self.assertEqual(callback_resp.status_code, 302)
        self.assertEqual(callback_resp.url, target_path)

    @patch('recommendations.notifications.EmailMultiAlternatives')
    @patch('recommendations.notifications.get_subscribed_members')
    def test_survey_broadcast_email_contains_direct_survey_url(self, mock_get_subs, mock_email_class):
        mock_get_subs.return_value = [{'email': 'voter@gmail.com', 'full_name': 'Voter Member'}]

        from recommendations.notifications import send_survey_broadcast_email
        request = RequestFactory().get(f'/surveys/{self.survey.id}/')
        request.get_host = lambda: 'minimexas.pythonanywhere.com'
        request.is_secure = lambda: True

        sent_count = send_survey_broadcast_email(self.survey, request=request)
        self.assertEqual(sent_count, 1)

        # Verify email body contains direct link
        called_args, called_kwargs = mock_email_class.call_args
        body_text = called_kwargs.get('body', '')
        self.assertIn(f"/surveys/{self.survey.id}/", body_text)


@override_settings(GOOGLE_SHEET_KEY="mock_test_sheet_key_123")
class EventRSVPAndBroadcastTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()
        self.factory = RequestFactory()
        
        self.member = MemberProfile.objects.create(
            full_name="Carlos Santana",
            email="carlos@minimexitas.org",
            is_admin=False
        )
        self.admin = MemberProfile.objects.create(
            full_name="Elena Organizer",
            email="elena@minimexitas.org",
            is_admin=True
        )
        self.mock_event = {
            'id': 'evt_tacos_2026',
            'title': 'Mexican Food Tour & Taco Picnic',
            'category': 'Culinary & Social',
            'date': 'Aug 25, 2026',
            'time': '12:00 PM',
            'location': 'Mission Dolores Park, SF',
            'locurl': 'https://maps.google.com/?q=Mission+Dolores+Park',
            'desc': 'Join us for tacos and cultural chats!',
            'gcal': 'https://calendar.google.com/calendar/render?action=TEMPLATE&text=Tacos',
            'organizer': 'Elena Organizer',
        }

    def test_rsvp_model_str_and_broadcast_model(self):
        rsvp = CommunityEventRSVP.objects.create(
            event_id="evt_123",
            event_title="Taco Picnic",
            member_email="carlos@minimexitas.org",
            member_name="Carlos Santana",
            status=CommunityEventRSVP.STATUS_GOING,
            notes="Bringing guacamole"
        )
        self.assertIn("Carlos Santana", str(rsvp))
        self.assertIn("Taco Picnic", str(rsvp))
        self.assertIn("Going", str(rsvp))

        bcast = CommunityEventBroadcast.objects.create(
            event_id="evt_123",
            event_title="Taco Picnic",
            broadcast_by="Elena Organizer",
            broadcast_by_email="elena@minimexitas.org",
            recipient_count=10
        )
        self.assertIn("Taco Picnic", str(bcast))
        self.assertIn("10 recipients", str(bcast))

    def test_rsvp_signed_token_generation_and_verification(self):
        token = generate_event_rsvp_token(
            event_id="evt_tacos_2026",
            email="carlos@minimexitas.org",
            status="going"
        )
        self.assertIsInstance(token, str)
        self.assertTrue(len(token) > 20)

        data = verify_event_rsvp_token(token)
        self.assertIsNotNone(data)
        self.assertEqual(data['event_id'], "evt_tacos_2026")
        self.assertEqual(data['email'], "carlos@minimexitas.org")
        self.assertEqual(data['status'], "going")

        # Invalid token verification
        invalid_data = verify_event_rsvp_token("tampered_token_invalid_signature")
        self.assertIsNone(invalid_data)

    def test_send_event_broadcast_email(self):
        mail.outbox = []
        request = self.factory.get('/events/')
        
        sent_count = send_event_broadcast_email(
            event_data=self.mock_event,
            broadcast_by_name="Elena Organizer",
            broadcast_by_email="elena@minimexitas.org",
            request=request
        )
        self.assertEqual(sent_count, 2)
        self.assertEqual(len(mail.outbox), 2)
        
        email_sent = mail.outbox[0]
        self.assertIn("Mexican Food Tour & Taco Picnic", email_sent.subject)
        self.assertIn("Mission Dolores Park", email_sent.body)
        self.assertIn("Google Calendar", email_sent.body)
        self.assertIn("/events/rsvp/?token=", email_sent.body)
        self.assertIn("/profile/", email_sent.body)

    def test_send_event_broadcast_skips_opted_out_members(self):
        # Opt-out carlos from email notifications
        self.member.email_notifications = False
        self.member.save()

        mail.outbox = []
        request = self.factory.get('/events/')
        
        sent_count = send_event_broadcast_email(
            event_data=self.mock_event,
            broadcast_by_name="Elena Organizer",
            broadcast_by_email="elena@minimexitas.org",
            request=request
        )
        # Only Elena should receive the email broadcast
        self.assertEqual(sent_count, 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["elena@minimexitas.org"])

    def test_from_email_dynamic_resolution(self):
        with override_settings(EMAIL_HOST_USER="community@gmail.com", DEFAULT_FROM_EMAIL="MiniMexitas Community <no-reply@minimexitas.org>"):
            resolved = _get_from_email()
            self.assertEqual(resolved, "MiniMexitas Community <community@gmail.com>")

        with override_settings(EMAIL_HOST_USER="community@gmail.com", DEFAULT_FROM_EMAIL="Custom Sender <custom@verified.org>"):
            resolved = _get_from_email()
            self.assertEqual(resolved, "Custom Sender <custom@verified.org>")

    def test_get_subscribed_members_auto_materializes_approved_membership(self):
        # Create an approved membership request that doesn't have a MemberProfile yet
        MembershipRequest.objects.create(
            full_name="Auto Materialized",
            email="auto_new@gmail.com",
            phone_number="+1 555 123 4567",
            status="approved"
        )
        members = get_subscribed_members()
        member_emails = [m['email'] for m in members]
        self.assertIn("auto_new@gmail.com", member_emails)
        self.assertTrue(MemberProfile.objects.filter(email="auto_new@gmail.com").exists())

    def test_send_survey_broadcast_email(self):
        survey = CommunitySurvey.objects.create(
            title="Next Meetup Location",
            description="Where should we meet for tacos?",
            category="meetup",
            is_active=True
        )
        SurveyOption.objects.create(survey=survey, text="Mission District", order=0)
        SurveyOption.objects.create(survey=survey, text="Fruitvale Oakland", order=1)

        mail.outbox = []
        request = self.factory.get('/surveys/')

        sent_count = send_survey_broadcast_email(
            survey_obj=survey,
            broadcast_by_name="Admin Maria",
            broadcast_by_email="admin@minimexitas.org",
            request=request
        )
        self.assertEqual(sent_count, 2)
        self.assertEqual(len(mail.outbox), 2)

        email_sent = mail.outbox[0]
        self.assertIn("Next Meetup Location", email_sent.subject)
        self.assertIn("Mission District", email_sent.body)
        self.assertIn("Fruitvale Oakland", email_sent.body)
        self.assertIn("/surveys/", email_sent.body)
        self.assertIn("/profile/", email_sent.body)

    def test_send_survey_broadcast_skips_opted_out_members(self):
        self.member.email_notifications = False
        self.member.save()

        survey = CommunitySurvey.objects.create(
            title="Next Meetup Location",
            category="meetup",
            is_active=True
        )
        SurveyOption.objects.create(survey=survey, text="Option 1", order=0)
        SurveyOption.objects.create(survey=survey, text="Option 2", order=1)

        mail.outbox = []
        request = self.factory.get('/surveys/')

        sent_count = send_survey_broadcast_email(
            survey_obj=survey,
            broadcast_by_name="Admin Maria",
            broadcast_by_email="admin@minimexitas.org",
            request=request
        )
        # Only Elena has email_notifications=True
        self.assertEqual(sent_count, 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["elena@minimexitas.org"])

    @patch('recommendations.sheets.get_gspread_client')
    def test_sync_event_rsvp_to_google_sheet(self, mock_get_client):
        mock_ws = MagicMock()
        mock_ws.get_all_values.return_value = [
            ['Event ID', 'Event Title', 'Member Name', 'Member Email', 'RSVP Status', 'Notes', 'Updated At'],
            ['evt_old', 'Old Event', 'Old Member', 'old@test.org', 'maybe', '', '2026-08-10 10:00']
        ]
        mock_sh = MagicMock()
        mock_sh.worksheet.return_value = mock_ws
        mock_client = MagicMock()
        mock_client.open.return_value = mock_sh
        mock_client.open_by_key.return_value = mock_sh
        mock_get_client.return_value = mock_client

        rsvp = CommunityEventRSVP.objects.create(
            event_id="evt_tacos_2026",
            event_title="Mexican Food Tour & Taco Picnic",
            member_email="carlos@minimexitas.org",
            member_name="Carlos Santana",
            status="going"
        )

        res = sync_event_rsvp_to_google_sheet(rsvp)
        self.assertTrue(res)
        mock_ws.append_row.assert_called_once()

    @patch('recommendations.views.fetch_community_events')
    def test_events_view_loads_rsvps_and_broadcast_status(self, mock_fetch_events):
        mock_fetch_events.return_value = [{
            'id': 'evt_tacos_2026',
            'title': 'Mexican Food Tour & Taco Picnic',
            'category': 'Culinary & Social',
            'start_datetime': datetime.datetime(2026, 8, 25, 12, 0),
            'year': 2026,
            'month': 8,
            'day': '25',
            'date_formatted': 'Aug 25, 2026',
            'time_formatted': '12:00 PM',
            'location': 'Mission Dolores Park, SF',
            'location_url': 'https://maps.google.com/?q=Mission+Dolores+Park',
            'description': 'Join us for tacos!',
            'google_calendar_link': 'https://calendar.google.com/calendar/render?action=TEMPLATE&text=Tacos',
            'organizer': 'Elena Organizer',
            'is_past': False,
        }]

        CommunityEventRSVP.objects.create(
            event_id="evt_tacos_2026",
            event_title="Mexican Food Tour & Taco Picnic",
            member_email="carlos@minimexitas.org",
            member_name="Carlos Santana",
            status="going"
        )
        CommunityEventBroadcast.objects.create(
            event_id="evt_tacos_2026",
            event_title="Mexican Food Tour & Taco Picnic",
            broadcast_by="Elena Organizer",
            broadcast_by_email="elena@minimexitas.org",
            recipient_count=2
        )

        session = self.client.session
        session['is_verified_member'] = True
        session['member_email'] = "carlos@minimexitas.org"
        session['member_name'] = "Carlos Santana"
        session.save()

        response = self.client.get(reverse('events'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Mexican Food Tour")
        self.assertContains(response, "Going")
        self.assertContains(response, "Carlos Santana")

    @patch('recommendations.views.sync_event_rsvp_to_google_sheet')
    def test_event_rsvp_view_token_get(self, mock_sheet_sync):
        mock_sheet_sync.return_value = True
        
        token = generate_event_rsvp_token(
            event_id="evt_tacos_2026",
            email="carlos@minimexitas.org",
            status="going"
        )

        response = self.client.get(f"{reverse('event_rsvp')}?token={token}")
        self.assertEqual(response.status_code, 302)
        self.assertIn('/events/', response.url)

        rsvp = CommunityEventRSVP.objects.get(event_id="evt_tacos_2026", member_email="carlos@minimexitas.org")
        self.assertEqual(rsvp.status, "going")
        self.assertEqual(rsvp.member_name, "Carlos Santana")
        mock_sheet_sync.assert_called_once()

    @patch('recommendations.views.sync_event_rsvp_to_google_sheet')
    def test_event_rsvp_view_ajax_post(self, mock_sheet_sync):
        mock_sheet_sync.return_value = True

        session = self.client.session
        session['is_verified_member'] = True
        session['member_email'] = "carlos@minimexitas.org"
        session['member_name'] = "Carlos Santana"
        session.save()

        response = self.client.post(
            reverse('event_rsvp'),
            {
                'event_id': 'evt_tacos_2026',
                'event_title': 'Mexican Food Tour & Taco Picnic',
                'status': 'maybe',
                'ajax': '1',
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['status'], 'maybe')

        rsvp = CommunityEventRSVP.objects.get(event_id="evt_tacos_2026", member_email="carlos@minimexitas.org")
        self.assertEqual(rsvp.status, "maybe")

    @patch('recommendations.views.send_event_broadcast_email')
    @patch('recommendations.views.sync_audit_log_to_google_sheet')
    def test_organizer_event_broadcast_view_authorized_admin(self, mock_sheet_audit, mock_send_email):
        mock_send_email.return_value = 2
        mock_sheet_audit.return_value = True

        session = self.client.session
        session['is_verified_member'] = True
        session['is_admin'] = True
        session['member_email'] = "elena@minimexitas.org"
        session['member_name'] = "Elena Organizer"
        session.save()

        response = self.client.post(reverse('event_broadcast'), {
            'event_id': 'evt_tacos_2026',
            'event_title': 'Mexican Food Tour & Taco Picnic',
            'date_formatted': 'Aug 25, 2026',
            'time_formatted': '12:00 PM',
            'location': 'Mission Dolores Park, SF',
            'location_url': 'https://maps.google.com/?q=Mission+Dolores+Park',
            'category': 'Culinary & Social',
            'description': 'Join us for tacos!',
            'google_calendar_link': 'https://calendar.google.com/calendar/render?action=TEMPLATE&text=Tacos',
        })

        self.assertEqual(response.status_code, 302)
        self.assertIn('/events/', response.url)
        mock_send_email.assert_called_once()
        
        # Verify broadcast record
        bcast = CommunityEventBroadcast.objects.get(event_id="evt_tacos_2026")
        self.assertEqual(bcast.recipient_count, 2)
        self.assertEqual(bcast.broadcast_by_email, "elena@minimexitas.org")

        # Verify audit log
        audit = MembershipAuditLog.objects.filter(action=MembershipAuditLog.ACTION_EVENT_BROADCAST).first()
        self.assertIsNotNone(audit)
        self.assertIn("Mexican Food Tour & Taco Picnic", audit.target_name)
        self.assertIn("2 active community members", audit.notes)

    def test_organizer_event_broadcast_view_unauthorized_non_admin(self):
        session = self.client.session
        session['is_verified_member'] = True
        session['is_admin'] = False
        session['member_email'] = "carlos@minimexitas.org"
        session.save()

        response = self.client.post(reverse('event_broadcast'), {
            'event_id': 'evt_tacos_2026',
            'event_title': 'Mexican Food Tour & Taco Picnic',
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(CommunityEventBroadcast.objects.count(), 0)


@override_settings(GOOGLE_SHEET_KEY="mock_sheet_key_community_events")
class CommunityEventDualSourceTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = MemberProfile.objects.create(
            full_name="Elena Gomez",
            email="elena.organizer@minimexitas.org",
            region="san_francisco",
            is_admin=True
        )
        self.member = MemberProfile.objects.create(
            full_name="Mateo Silva",
            email="mateo.silva@minimexitas.org",
            region="east_bay",
            is_admin=False
        )

    def test_community_event_model_creation_and_str(self):
        event = CommunityEvent.objects.create(
            title="Noche Mexicana",
            category="Cultural & Heritage",
            start_datetime=datetime.datetime(2026, 9, 15, 18, 0, tzinfo=datetime.timezone.utc),
            end_datetime=datetime.datetime(2026, 9, 15, 21, 0, tzinfo=datetime.timezone.utc),
            location="Mission Cultural Center, SF",
            location_url="https://maps.google.com/?q=Mission+Cultural+Center",
            description="Celebrate Mexican Independence Day with live music and appetizers.",
            created_by_email="elena.organizer@minimexitas.org"
        )
        self.assertEqual(str(event), "Noche Mexicana (2026-09-15 18:00)")
        self.assertTrue(event.is_active)
        self.assertEqual(event.category, "Cultural & Heritage")

    @patch('recommendations.views.sync_community_event_to_google_sheet')
    @patch('recommendations.views.sync_audit_log_to_google_sheet')
    def test_organizer_create_event_view_success(self, mock_sheet_audit, mock_sheet_event):
        mock_sheet_audit.return_value = True
        mock_sheet_event.return_value = True

        session = self.client.session
        session['is_verified_member'] = True
        session['is_admin'] = True
        session['member_email'] = "elena.organizer@minimexitas.org"
        session['member_name'] = "Elena Gomez"
        session.save()

        response = self.client.post(reverse('event_create'), {
            'title': 'Dia de Muertos Community Altar',
            'category': 'Cultural & Heritage',
            'start_date': '2026-11-01',
            'start_time': '16:00',
            'end_date': '2026-11-01',
            'end_time': '20:00',
            'location': 'Garfield Square, SF',
            'location_url': 'https://maps.google.com/?q=Garfield+Square',
            'description': 'Community altar gathering with pan de muerto and hot chocolate.',
        })

        self.assertEqual(response.status_code, 302)
        self.assertIn('/events/', response.url)

        # Verify CommunityEvent in database
        event = CommunityEvent.objects.get(title="Dia de Muertos Community Altar")
        self.assertEqual(event.location, "Garfield Square, SF")
        self.assertEqual(event.created_by_email, "elena.organizer@minimexitas.org")
        self.assertEqual(event.created_by_name, "Elena Gomez")
        self.assertTrue(event.is_active)

        # Verify Google Sheet sync was invoked
        mock_sheet_event.assert_called_once_with(event)

        # Verify MembershipAuditLog
        audit = MembershipAuditLog.objects.filter(action=MembershipAuditLog.ACTION_EVENT_CREATED).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.target_name, "Event: Dia de Muertos Community Altar")
        self.assertEqual(audit.actor_email, "elena.organizer@minimexitas.org")

    @patch('recommendations.views.send_event_broadcast_email')
    @patch('recommendations.views.sync_community_event_to_google_sheet')
    @patch('recommendations.views.sync_audit_log_to_google_sheet')
    def test_organizer_create_event_view_with_instant_broadcast(self, mock_sheet_audit, mock_sheet_event, mock_send_bcast):
        mock_sheet_audit.return_value = True
        mock_sheet_event.return_value = True
        mock_send_bcast.return_value = 2

        session = self.client.session
        session['is_verified_member'] = True
        session['is_admin'] = True
        session['member_email'] = "elena.organizer@minimexitas.org"
        session['member_name'] = "Elena Gomez"
        session.save()

        response = self.client.post(reverse('event_create'), {
            'title': 'Tech & Tacos Networking',
            'category': 'Networking & Tech',
            'start_date': '2026-10-10',
            'start_time': '18:30',
            'end_date': '2026-10-10',
            'end_time': '21:00',
            'location': 'Salesforce Park, SF',
            'description': 'Connect with Mexican engineers and designers in the Bay.',
            'broadcast_now': '1',
        })

        self.assertEqual(response.status_code, 302)
        self.assertIn('/events/', response.url)

        event = CommunityEvent.objects.get(title="Tech & Tacos Networking")
        mock_send_bcast.assert_called_once()
        
        # Verify CommunityEventBroadcast was recorded
        bcast = CommunityEventBroadcast.objects.get(event_id=f"portal_{event.id}")
        self.assertEqual(bcast.recipient_count, 2)
        self.assertEqual(bcast.broadcast_by_email, "elena.organizer@minimexitas.org")

    def test_organizer_create_event_view_missing_fields(self):
        session = self.client.session
        session['is_verified_member'] = True
        session['is_admin'] = True
        session['member_email'] = "elena.organizer@minimexitas.org"
        session.save()

        response = self.client.post(reverse('event_create'), {
            'title': '',  # missing title
            'start_date': '2026-10-10',
            'start_time': '18:30',
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(CommunityEvent.objects.count(), 0)

    def test_organizer_create_event_view_forbidden_for_non_admin(self):
        session = self.client.session
        session['is_verified_member'] = True
        session['is_admin'] = False
        session['member_email'] = "mateo.silva@minimexitas.org"
        session.save()

        response = self.client.post(reverse('event_create'), {
            'title': 'Unauthorized Event',
            'start_date': '2026-10-10',
            'start_time': '18:30',
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(CommunityEvent.objects.count(), 0)

    def test_organizer_create_event_view_unauthenticated(self):
        response = self.client.post(reverse('event_create'), {
            'title': 'Unauthorized Event',
        })
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)

    @patch('recommendations.views.sync_community_event_to_google_sheet')
    @patch('recommendations.views.sync_audit_log_to_google_sheet')
    def test_organizer_edit_event_view_success(self, mock_sheet_audit, mock_sheet_event):
        mock_sheet_audit.return_value = True
        mock_sheet_event.return_value = True

        event = CommunityEvent.objects.create(
            title="Initial Title",
            category="Community Gathering",
            start_datetime=datetime.datetime(2026, 9, 20, 15, 0, tzinfo=datetime.timezone.utc),
            location="Dolores Park",
            description="Initial description",
            created_by_email="elena.organizer@minimexitas.org"
        )

        session = self.client.session
        session['is_verified_member'] = True
        session['is_admin'] = True
        session['member_email'] = "elena.organizer@minimexitas.org"
        session['member_name'] = "Elena Gomez"
        session.save()

        response = self.client.post(reverse('event_edit', kwargs={'event_id': event.id}), {
            'title': 'Updated Fiesta de Primavera',
            'category': 'Cultural & Heritage',
            'start_date': '2026-09-21',
            'start_time': '16:00',
            'end_date': '2026-09-21',
            'end_time': '19:00',
            'location': 'Yerba Buena Gardens, SF',
            'location_url': 'https://maps.google.com/?q=Yerba+Buena',
            'description': 'Updated description with music and food.',
        })

        self.assertEqual(response.status_code, 302)
        self.assertIn('/events/', response.url)

        event.refresh_from_db()
        self.assertEqual(event.title, "Updated Fiesta de Primavera")
        self.assertEqual(event.category, "Cultural & Heritage")
        self.assertEqual(event.location, "Yerba Buena Gardens, SF")

        # Verify audit log
        audit = MembershipAuditLog.objects.filter(action=MembershipAuditLog.ACTION_EVENT_UPDATED).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.target_name, "Event: Updated Fiesta de Primavera")
        self.assertEqual(audit.actor_email, "elena.organizer@minimexitas.org")

    def test_organizer_edit_event_view_forbidden_for_non_admin(self):
        event = CommunityEvent.objects.create(
            title="Sample Event",
            category="Community Gathering",
            start_datetime=datetime.datetime(2026, 9, 20, 15, 0, tzinfo=datetime.timezone.utc),
            location="Dolores Park",
            description="Sample description"
        )
        session = self.client.session
        session['is_verified_member'] = True
        session['is_admin'] = False
        session['member_email'] = "mateo.silva@minimexitas.org"
        session.save()

        response = self.client.post(reverse('event_edit', kwargs={'event_id': event.id}), {
            'title': 'Attempted Update',
        })
        self.assertEqual(response.status_code, 403)

    def test_organizer_edit_event_view_not_found(self):
        session = self.client.session
        session['is_verified_member'] = True
        session['is_admin'] = True
        session['member_email'] = "elena.organizer@minimexitas.org"
        session.save()

        response = self.client.post(reverse('event_edit', kwargs={'event_id': 99999}), {
            'title': 'Non-existent Event',
            'start_date': '2026-09-21',
            'start_time': '16:00',
        })
        self.assertEqual(response.status_code, 404)

    @patch('recommendations.views.delete_community_event_from_google_sheet')
    @patch('recommendations.views.sync_audit_log_to_google_sheet')
    def test_organizer_delete_event_view_success(self, mock_sheet_audit, mock_sheet_delete):
        mock_sheet_audit.return_value = True
        mock_sheet_delete.return_value = True

        event = CommunityEvent.objects.create(
            title="Event to be Cancelled",
            category="Culinary & Social",
            start_datetime=datetime.datetime(2026, 9, 25, 12, 0, tzinfo=datetime.timezone.utc),
            location="Presidio Picnic",
            description="Cancelled picnic event",
            created_by_email="elena.organizer@minimexitas.org"
        )

        session = self.client.session
        session['is_verified_member'] = True
        session['is_admin'] = True
        session['member_email'] = "elena.organizer@minimexitas.org"
        session['member_name'] = "Elena Gomez"
        session.save()

        response = self.client.post(reverse('event_delete', kwargs={'event_id': event.id}))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/events/', response.url)

        event.refresh_from_db()
        self.assertFalse(event.is_active)

        mock_sheet_delete.assert_called_once_with(event.id)

        # Verify audit log
        audit = MembershipAuditLog.objects.filter(action=MembershipAuditLog.ACTION_EVENT_DELETED).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.target_name, "Event: Event to be Cancelled")
        self.assertEqual(audit.actor_email, "elena.organizer@minimexitas.org")

    def test_organizer_delete_event_view_forbidden_for_non_admin(self):
        event = CommunityEvent.objects.create(
            title="Event to Keep",
            category="Culinary & Social",
            start_datetime=datetime.datetime(2026, 9, 25, 12, 0, tzinfo=datetime.timezone.utc),
            location="Presidio Picnic",
            description="Picnic event"
        )
        session = self.client.session
        session['is_verified_member'] = True
        session['is_admin'] = False
        session['member_email'] = "mateo.silva@minimexitas.org"
        session.save()

        response = self.client.post(reverse('event_delete', kwargs={'event_id': event.id}))
        self.assertEqual(response.status_code, 403)
        event.refresh_from_db()
        self.assertTrue(event.is_active)

    @patch('recommendations.calendar_sync._fetch_from_google_calendar_api')
    def test_fetch_community_events_dual_source_merging(self, mock_gcal):
        cache.clear()
        
        # 1. Create a portal event
        now = datetime.datetime.now(datetime.timezone.utc)
        portal_event = CommunityEvent.objects.create(
            title="Portal Artisan Fair",
            category="Cultural & Heritage",
            start_datetime=now + datetime.timedelta(days=2),
            end_datetime=now + datetime.timedelta(days=2, hours=3),
            location="Mission Dolores, SF",
            location_url="https://maps.google.com/?q=Mission+Dolores",
            description="Handmade crafts from Oaxaca and Jalisco.",
            created_by_name="Elena Gomez"
        )

        # Inactive portal event (should NOT appear)
        CommunityEvent.objects.create(
            title="Cancelled Event",
            category="Cultural & Heritage",
            start_datetime=now + datetime.timedelta(days=1),
            location="SF",
            description="Cancelled",
            is_active=False
        )

        # 2. Mock Google Calendar return
        mock_gcal.return_value = [
            {
                'id': 'gcal_event_101',
                'title': 'Google Cal Mezcal Tasting',
                'category': 'Culinary & Social',
                'date_formatted': (now + datetime.timedelta(days=5)).strftime('%b %d, %Y'),
                'time_formatted': '7:00 PM - 9:00 PM',
                'location': 'Oakland, CA',
                'location_url': 'https://maps.google.com/?q=Oakland',
                'description': 'Mezcal tasting workshop.',
                'organizer': 'MiniMexitas Calendar',
                'google_calendar_link': 'https://calendar.google.com',
                'start_datetime': now + datetime.timedelta(days=5),
                'end_datetime': now + datetime.timedelta(days=5, hours=2),
                'day': (now + datetime.timedelta(days=5)).strftime('%d'),
                'is_past': False,
                'source': 'google_calendar',
                'is_portal_event': False,
                'portal_event_pk': None,
            }
        ]

        with patch.dict('os.environ', {'GOOGLE_CALENDAR_ID': 'mock_calendar_id_123'}):
            with override_settings(GOOGLE_SERVICE_ACCOUNT_INFO={'type': 'service_account', 'client_email': 'mock@test.iam.gserviceaccount.com'}):
                events = fetch_community_events(force_refresh=True)
                self.assertEqual(len(events), 2)

        # Verify portal event properties
        p_evt = next(e for e in events if e['title'] == 'Portal Artisan Fair')
        self.assertTrue(p_evt['is_portal_event'])
        self.assertEqual(p_evt['portal_event_pk'], portal_event.id)
        self.assertEqual(p_evt['source'], 'portal')
        self.assertEqual(p_evt['organizer'], 'Elena Gomez')

        # Verify Google Cal event properties
        g_evt = next(e for e in events if e['title'] == 'Google Cal Mezcal Tasting')
        self.assertFalse(g_evt['is_portal_event'])
        self.assertEqual(g_evt['source'], 'google_calendar')


@override_settings(GOOGLE_SHEET_KEY="mock_test_sheet_key_123")
class CommunityEventSheetSyncTestCase(TestCase):
    @patch('recommendations.sheets.get_gspread_client')
    def test_sync_community_event_to_google_sheet_new_row(self, mock_get_client):
        mock_worksheet = MagicMock()
        mock_worksheet.get_all_values.return_value = [
            ['Event ID', 'Event Title', 'Category', 'Start Date & Time', 'End Date & Time', 'Location', 'Location URL', 'Description', 'Created By', 'Created At', 'Status']
        ]
        mock_worksheet.title = "Events"
        
        mock_sheet = MagicMock()
        mock_sheet.worksheets.return_value = [mock_worksheet]
        mock_sheet.worksheet.return_value = mock_worksheet
        
        mock_client = MagicMock()
        mock_client.open.return_value = mock_sheet
        mock_client.open_by_key.return_value = mock_sheet
        mock_get_client.return_value = mock_client

        event = CommunityEvent(
            id=42,
            title="Pan de Muerto Baking Workshop",
            category="Culinary & Social",
            start_datetime=datetime.datetime(2026, 10, 28, 14, 0, tzinfo=datetime.timezone.utc),
            end_datetime=datetime.datetime(2026, 10, 28, 17, 0, tzinfo=datetime.timezone.utc),
            location="San Jose, CA",
            location_url="https://maps.google.com/?q=San+Jose",
            description="Learn authentic baking techniques.",
            created_by_name="Chef Maria",
            created_by_email="maria@minimexitas.org",
            is_active=True
        )

        success = sync_community_event_to_google_sheet(event)
        self.assertTrue(success)
        mock_worksheet.append_row.assert_called_once()

    @patch('recommendations.sheets.get_gspread_client')
    def test_delete_community_event_from_google_sheet(self, mock_get_client):
        mock_worksheet = MagicMock()
        mock_worksheet.get_all_values.return_value = [
            ['Event ID', 'Event Title', 'Status'],
            ['portal_42', 'Pan de Muerto Baking Workshop', 'Active']
        ]
        mock_worksheet.title = "Events"

        mock_sheet = MagicMock()
        mock_sheet.worksheets.return_value = [mock_worksheet]
        mock_sheet.worksheet.return_value = mock_worksheet

        mock_client = MagicMock()
        mock_client.open.return_value = mock_sheet
        mock_client.open_by_key.return_value = mock_sheet
        mock_get_client.return_value = mock_client

        success = delete_community_event_from_google_sheet("portal_42")
        self.assertTrue(success)
        mock_worksheet.update_cell.assert_called_once_with(2, 3, "Deleted (Inactive)")


@override_settings(GOOGLE_SHEET_KEY="mock_test_sheet_key_123")
class OrganizerCreateRecommendationTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()
        self.admin_profile = MemberProfile.objects.create(
            email="admin.rec@minimexitas.org",
            full_name="Admin Organizer",
            is_admin=True,
        )
        self.regular_profile = MemberProfile.objects.create(
            email="member.rec@minimexitas.org",
            full_name="Regular Member",
            is_admin=False,
        )

    def test_sync_recommendation_to_google_sheet_missing_inputs(self):
        self.assertFalse(sync_recommendation_to_google_sheet("", "Doctors", "Great clinic"))
        self.assertFalse(sync_recommendation_to_google_sheet("Maria", "Doctors", ""))

    @patch('recommendations.sheets.get_gspread_client')
    def test_sync_recommendation_to_google_sheet_success(self, mock_get_client):
        mock_ws = MagicMock()
        mock_ws.title = "Recomendaciones"
        mock_ws.get_all_values.return_value = [
            ['Date', 'Subject', 'Shared By', 'Recommendation']
        ]

        mock_sh = MagicMock()
        mock_sh.worksheets.return_value = [mock_ws]
        mock_sh.worksheet.return_value = mock_ws
        mock_client = MagicMock()
        mock_client.open.return_value = mock_sh
        mock_client.open_by_key.return_value = mock_sh
        mock_get_client.return_value = mock_client

        success = sync_recommendation_to_google_sheet(
            shared_by="Dr. Laura",
            subject="Pediatricians",
            recommendation="Bilingual clinic in Mission SF: (415) 555-0199",
            date_str="2026-08-18"
        )
        self.assertTrue(success)
        mock_ws.append_row.assert_called_once()
        row_arg = mock_ws.append_row.call_args[0][0]
        self.assertEqual(row_arg, ["2026-08-18", "Pediatricians", "Dr. Laura", "Bilingual clinic in Mission SF: (415) 555-0199"])

    def test_notes_list_view_shows_add_button_for_admin_only(self):
        # Non-admin member
        session = self.client.session
        session['is_verified_member'] = True
        session['member_email'] = self.regular_profile.email
        session['member_name'] = self.regular_profile.full_name
        session['is_admin'] = False
        session.save()

        with patch('recommendations.views.fetch_recommendations', return_value=[]):
            response = self.client.get(reverse('recommendations:notes_list'))
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, 'data-bs-target="#createRecommendationModal"')

        # Admin member
        session = self.client.session
        session['is_verified_member'] = True
        session['member_email'] = self.admin_profile.email
        session['member_name'] = self.admin_profile.full_name
        session['is_admin'] = True
        session.save()

        with patch('recommendations.views.fetch_recommendations', return_value=[]):
            response = self.client.get(reverse('recommendations:notes_list'))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'data-bs-target="#createRecommendationModal"')
            self.assertContains(response, 'Add Recommendation')

    def test_organizer_create_recommendation_forbidden_for_non_admin(self):
        session = self.client.session
        session['is_verified_member'] = True
        session['member_email'] = self.regular_profile.email
        session['is_admin'] = False
        session.save()

        response = self.client.post(reverse('recommendations:recommendation_create'), {
            'category': 'Food & Dining',
            'shared_by': 'Carlos',
            'recommendation': 'Best Birria tacos in San Jose: Birrieria Jalisco',
        })
        # admin_required redirects unauthorized users
        self.assertIn(response.status_code, [302, 403])
        self.assertEqual(MembershipAuditLog.objects.count(), 0)

    @patch('recommendations.views.sync_recommendation_to_google_sheet', return_value=True)
    def test_organizer_create_recommendation_success(self, mock_sync_sheet):
        session = self.client.session
        session['is_verified_member'] = True
        session['member_email'] = self.admin_profile.email
        session['member_name'] = self.admin_profile.full_name
        session['is_admin'] = True
        session.save()

        response = self.client.post(reverse('recommendations:recommendation_create'), {
            'category': 'Food & Dining',
            'shared_by': 'Chef Mateo',
            'recommendation': 'Autentica Taqueria en Palo Alto: Tacos El Grullo',
            'date': '2026-08-18',
            'location': '2288 Mission St, San Francisco'
        })
        self.assertRedirects(response, reverse('recommendations:notes_list'))
        mock_sync_sheet.assert_called_once_with(
            shared_by='Chef Mateo',
            subject='Food & Dining',
            recommendation='Autentica Taqueria en Palo Alto: Tacos El Grullo',
            date_str='2026-08-18',
            location='2288 Mission St, San Francisco'
        )

        audit = MembershipAuditLog.objects.filter(action=MembershipAuditLog.ACTION_RECOMMENDATION_CREATED).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.actor_email, self.admin_profile.email)
        self.assertEqual(audit.target_name, 'Food & Dining')
        self.assertIn('Chef Mateo', audit.notes)

    @patch('recommendations.views.sync_recommendation_to_google_sheet', return_value=True)
    def test_organizer_create_recommendation_custom_category(self, mock_sync_sheet):
        session = self.client.session
        session['is_verified_member'] = True
        session['member_email'] = self.admin_profile.email
        session['member_name'] = self.admin_profile.full_name
        session['is_admin'] = True
        session.save()

        response = self.client.post(reverse('recommendations:recommendation_create'), {
            'category': 'Other',
            'custom_category': 'Bilingual Pediatricians',
            'shared_by': 'Lucia R.',
            'recommendation': 'Dr. Sandra Gomez in Redwood City',
        })
        self.assertRedirects(response, reverse('recommendations:notes_list'))
        mock_sync_sheet.assert_called_once()
        self.assertEqual(mock_sync_sheet.call_args[1]['subject'], 'Bilingual Pediatricians')
        self.assertEqual(mock_sync_sheet.call_args[1]['location'], '')

    def test_parse_location_and_coords_variations(self):
        from recommendations.sheets import parse_location_and_coords

        # Direct coordinate string
        parsed = parse_location_and_coords("37.7599, -122.4148")
        self.assertAlmostEqual(parsed['lat'], 37.7599, places=3)
        self.assertAlmostEqual(parsed['lng'], -122.4148, places=3)
        self.assertTrue(parsed['has_location'])
        self.assertIn("37.7599", parsed['location_url'])

        # Google Maps coordinates URL
        parsed_url = parse_location_and_coords("https://www.google.com/maps/@37.7749,-122.4194,15z")
        self.assertAlmostEqual(parsed_url['lat'], 37.7749, places=3)
        self.assertAlmostEqual(parsed_url['lng'], -122.4194, places=3)
        self.assertTrue(parsed_url['has_location'])

        # Google Maps protobuf data url (!3d!4d)
        parsed_data = parse_location_and_coords("https://www.google.com/maps/place/Taqueria+El+Farolito/data=!4m6!3m5!1s0x0:0x0!8m2!3d37.7526735!4d-122.4182956")
        self.assertAlmostEqual(parsed_data['lat'], 37.75267, places=3)
        self.assertAlmostEqual(parsed_data['lng'], -122.41829, places=3)
        self.assertEqual(parsed_data['location_display'], "Taqueria El Farolito")

        # Bay Area city name fallback
        parsed_sf = parse_location_and_coords("Mission District, San Francisco")
        self.assertIsNotNone(parsed_sf['lat'])
        self.assertIsNotNone(parsed_sf['lng'])
        self.assertTrue(parsed_sf['has_location'])

        # Short URL / arbitrary URL fallback
        with patch('recommendations.sheets.resolve_google_maps_url', return_value="https://www.google.com/maps/place/Taqueria+Cancun/@37.7599,-122.4148,17z"):
            parsed_short = parse_location_and_coords("https://maps.app.goo.gl/abcdefg")
            self.assertAlmostEqual(parsed_short['lat'], 37.7599, places=3)
            self.assertAlmostEqual(parsed_short['lng'], -122.4148, places=3)
            self.assertEqual(parsed_short['location_display'], "Taqueria Cancun")

        # Empty / None
        parsed_empty = parse_location_and_coords("", "No place mentioned")
        self.assertFalse(parsed_empty['has_location'])
        self.assertIsNone(parsed_empty['lat'])
        self.assertIsNone(parsed_empty['lng'])

    @patch('recommendations.sheets.get_gspread_client')
    def test_fetch_recommendations_resilient_to_missing_header_column(self, mock_get_client):
        from recommendations.sheets import fetch_recommendations

        mock_ws = MagicMock()
        mock_ws.title = "Recomendaciones"
        # 4 headers, but rows have 5 columns including location
        mock_ws.get_all_values.return_value = [
            ['Date', 'Subject', 'Shared By', 'Recommendation'],
            ['2026-08-19', 'Food & Dining', 'Carlos', 'Great tacos in SF', 'https://maps.google.com/@37.7749,-122.4194']
        ]

        mock_sh = MagicMock()
        mock_sh.worksheets.return_value = [mock_ws]
        mock_sh.worksheet.return_value = mock_ws
        mock_client = MagicMock()
        mock_client.open.return_value = mock_sh
        mock_client.open_by_key.return_value = mock_sh
        mock_get_client.return_value = mock_client

        results = fetch_recommendations()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['Subject'], 'Food & Dining')
        self.assertEqual(results[0]['Location'], 'https://maps.google.com/@37.7749,-122.4194')
        self.assertAlmostEqual(results[0]['lat'], 37.7749, places=3)
        self.assertTrue(results[0]['has_location'])

    @patch('recommendations.sheets.get_gspread_client')
    def test_sync_recommendation_with_location_to_google_sheet(self, mock_get_client):
        from recommendations.sheets import sync_recommendation_to_google_sheet

        mock_ws = MagicMock()
        mock_ws.title = "Recomendaciones"
        mock_ws.get_all_values.return_value = [
            ['Date', 'Subject', 'Shared By', 'Recommendation', 'Location']
        ]

        mock_sh = MagicMock()
        mock_sh.worksheets.return_value = [mock_ws]
        mock_sh.worksheet.return_value = mock_ws
        mock_client = MagicMock()
        mock_client.open.return_value = mock_sh
        mock_client.open_by_key.return_value = mock_sh
        mock_get_client.return_value = mock_client

        success = sync_recommendation_to_google_sheet(
            shared_by="Dr. Laura",
            subject="Pediatricians",
            recommendation="Bilingual clinic in Mission SF: (415) 555-0199",
            date_str="2026-08-18",
            location="2288 Mission St, San Francisco"
        )
        self.assertTrue(success)
        mock_ws.append_row.assert_called_once()
        row_arg = mock_ws.append_row.call_args[0][0]
        self.assertEqual(row_arg, [
            "2026-08-18",
            "Pediatricians",
            "Dr. Laura",
            "Bilingual clinic in Mission SF: (415) 555-0199",
            "2288 Mission St, San Francisco"
        ])

    @patch('recommendations.views.fetch_recommendations')
    def test_notes_list_view_renders_map_and_notes_json(self, mock_fetch):
        mock_fetch.return_value = [
            {
                'Date': '2026-08-18',
                'Subject': 'Food & Dining',
                'Shared_By': 'Carlos',
                'Recommendation': 'Best tacos in Mission SF',
                'Location': 'Taqueria Cancun, 2288 Mission St, San Francisco',
                'location_display': 'Taqueria Cancun, 2288 Mission St, San Francisco',
                'location_url': 'https://maps.google.com/?q=Taqueria+Cancun',
                'lat': 37.7599,
                'lng': -122.4148,
                'has_location': True,
                'index': 0,
            },
            {
                'Date': '2026-08-17',
                'Subject': 'Doctors',
                'Shared_By': 'Ana',
                'Recommendation': 'Online consultations only',
                'Location': '',
                'location_display': '',
                'location_url': '',
                'lat': None,
                'lng': None,
                'has_location': False,
                'index': 1,
            }
        ]

        session = self.client.session
        session['is_verified_member'] = True
        session['member_email'] = self.regular_profile.email
        session['member_name'] = self.regular_profile.full_name
        session['is_admin'] = False
        session.save()

        response = self.client.get(reverse('recommendations:notes_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="recommendationsMap"')
        self.assertContains(response, 'Taqueria Cancun')
        self.assertContains(response, 'loc-pill-btn')
        self.assertEqual(response.context['mapped_count'], 1)
        self.assertIn('notes_json', response.context)



class EventDirectURLAndSharingTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()
        self.member = MemberProfile.objects.create(
            full_name="Valentina Morales",
            email="valentina@minimexitas.org",
            region="san_francisco",
            is_admin=False
        )

    def _login_member(self):
        session = self.client.session
        session['is_verified_member'] = True
        session['member_email'] = self.member.email
        session['member_name'] = self.member.full_name
        session['is_admin'] = False
        session.save()

    def test_unauthenticated_user_redirected_to_login(self):
        response = self.client.get(reverse('event_detail', kwargs={'event_id': 'evt_tacos_2026'}))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)
        self.assertIn('next=', response.url)

    @patch('recommendations.views.fetch_community_events')
    def test_event_detail_direct_url_renders_focused_event(self, mock_fetch_events):
        mock_fetch_events.return_value = [
            {
                'id': 'evt_tacos_2026',
                'title': 'Mexican Food Tour & Taco Picnic',
                'category': 'Culinary & Social',
                'start_datetime': datetime.datetime(2026, 8, 25, 12, 0),
                'year': 2026,
                'month': 8,
                'day': '25',
                'date_formatted': 'Aug 25, 2026',
                'time_formatted': '12:00 PM',
                'location': 'Mission Dolores Park, SF',
                'location_url': 'https://maps.google.com/?q=Mission+Dolores+Park',
                'description': 'Join us for tacos and cultural chats in the park!',
                'google_calendar_link': 'https://calendar.google.com/calendar/render?action=TEMPLATE&text=Tacos',
                'organizer': 'Elena Organizer',
                'is_past': False,
                'source': 'portal',
                'is_portal_event': True,
                'portal_event_pk': 1,
            }
        ]

        self._login_member()
        response = self.client.get(reverse('event_detail', kwargs={'event_id': 'evt_tacos_2026'}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'focused-event-container')
        self.assertContains(response, 'Mexican Food Tour')
        self.assertContains(response, 'View All Events')
        self.assertContains(response, 'copy-event-link-btn')
        self.assertContains(response, 'data-event-url=')
        self.assertNotContains(response, 'Share on WhatsApp')

    @patch('recommendations.views.fetch_community_events')
    def test_event_detail_supports_portal_prefix_and_raw_pk(self, mock_fetch_events):
        mock_fetch_events.return_value = [
            {
                'id': 'portal_5',
                'title': 'Dia de Muertos Workshop',
                'category': 'Cultural & Heritage',
                'start_datetime': datetime.datetime(2026, 11, 1, 14, 0),
                'year': 2026,
                'month': 11,
                'day': '01',
                'date_formatted': 'Nov 01, 2026',
                'time_formatted': '2:00 PM',
                'location': 'Mission Cultural Center, SF',
                'location_url': 'https://maps.google.com/?q=Mission+Cultural+Center',
                'description': 'Altar making workshop for families.',
                'google_calendar_link': 'https://calendar.google.com',
                'organizer': 'Comunidad MiniMexitas',
                'is_past': False,
                'source': 'portal',
                'is_portal_event': True,
                'portal_event_pk': 5,
            }
        ]

        self._login_member()

        # Test lookup by portal_5
        res1 = self.client.get('/events/portal_5/')
        self.assertEqual(res1.status_code, 200)
        self.assertContains(res1, 'Dia de Muertos Workshop')
        self.assertContains(res1, 'focused-event-container')

        # Test lookup by raw pk 5
        res2 = self.client.get('/events/5/')
        self.assertEqual(res2.status_code, 200)
        self.assertContains(res2, 'Dia de Muertos Workshop')
        self.assertContains(res2, 'focused-event-container')

    @patch('recommendations.views.fetch_community_events')
    def test_events_list_view_with_query_param(self, mock_fetch_events):
        mock_fetch_events.return_value = [
            {
                'id': 'evt_tacos_2026',
                'title': 'Mexican Food Tour & Taco Picnic',
                'category': 'Culinary & Social',
                'start_datetime': datetime.datetime(2026, 8, 25, 12, 0),
                'year': 2026,
                'month': 8,
                'day': '25',
                'date_formatted': 'Aug 25, 2026',
                'time_formatted': '12:00 PM',
                'location': 'Mission Dolores Park, SF',
                'location_url': 'https://maps.google.com/?q=Mission+Dolores+Park',
                'description': 'Join us for tacos and cultural chats!',
                'google_calendar_link': 'https://calendar.google.com',
                'organizer': 'Elena Organizer',
                'is_past': False,
                'source': 'google_calendar',
                'is_portal_event': False,
                'portal_event_pk': None,
            }
        ]

        self._login_member()
        response = self.client.get(f"{reverse('events')}?event=evt_tacos_2026")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'focused-event-container')
        self.assertContains(response, 'Mexican Food Tour')

    @patch('recommendations.views.fetch_community_events')
    def test_event_detail_nonexistent_event_graceful(self, mock_fetch_events):
        mock_fetch_events.return_value = [
            {
                'id': 'evt_existing',
                'title': 'Existing Event',
                'category': 'Cultural & Heritage',
                'start_datetime': datetime.datetime(2026, 8, 25, 12, 0),
                'year': 2026,
                'month': 8,
                'day': '25',
                'date_formatted': 'Aug 25, 2026',
                'time_formatted': '12:00 PM',
                'location': 'SF',
                'location_url': '',
                'description': '',
                'google_calendar_link': '',
                'organizer': 'Community',
                'is_past': False,
            }
        ]

        self._login_member()
        response = self.client.get(reverse('event_detail', kwargs={'event_id': 'evt_missing_999'}))
        self.assertEqual(response.status_code, 200)
        # Should render normal events page with alert
        self.assertContains(response, 'could not be found or has concluded')

    @patch('recommendations.views.sync_event_rsvp_to_google_sheet', return_value=True)
    def test_event_rsvp_token_redirects_to_event_detail(self, mock_sync_sheet):
        token = generate_event_rsvp_token(
            event_id="evt_tacos_2026",
            email="valentina@minimexitas.org",
            status="going"
        )
        response = self.client.get(f"{reverse('event_rsvp')}?token={token}")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/events/evt_tacos_2026/')


class EditRecommendationTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()
        self.admin_profile = MemberProfile.objects.create(
            full_name="Elena Organizer",
            email="elena.admin@minimexitas.org",
            region="san_francisco",
            is_admin=True
        )
        self.regular_profile = MemberProfile.objects.create(
            full_name="Carlos Member",
            email="carlos.member@minimexitas.org",
            region="san_francisco",
            is_admin=False
        )

    def _login_as(self, profile):
        session = self.client.session
        session['is_verified_member'] = True
        session['member_email'] = profile.email
        session['member_name'] = profile.full_name
        session['is_admin'] = profile.is_admin
        session.save()

    @patch('recommendations.sheets.get_gspread_client')
    def test_update_recommendation_in_google_sheet_by_row_index(self, mock_get_client):
        from recommendations.sheets import update_recommendation_in_google_sheet

        mock_ws = MagicMock()
        mock_ws.title = "Recomendaciones"
        mock_ws.get_all_values.return_value = [
            ['Date', 'Subject', 'Shared By', 'Recommendation', 'Location'],
            ['2026-08-10', 'Food & Dining', 'Carlos', 'Original tacos info', '2288 Mission St'],
            ['2026-08-11', 'Doctors & Health', 'Ana', 'Original dentist info', '']
        ]

        mock_sh = MagicMock()
        mock_sh.worksheets.return_value = [mock_ws]
        mock_sh.worksheet.return_value = mock_ws
        mock_client = MagicMock()
        mock_client.open.return_value = mock_sh
        mock_client.open_by_key.return_value = mock_sh
        mock_get_client.return_value = mock_client

        success = update_recommendation_in_google_sheet(
            row_index=2,
            shared_by="Carlos Updated",
            subject="Food & Dining",
            recommendation="Updated tacos details and phone: (415) 555-1234",
            date_str="2026-08-19",
            location="https://maps.app.goo.gl/example"
        )
        self.assertTrue(success)
        mock_ws.update.assert_called_once()
        update_call = mock_ws.update.call_args
        self.assertEqual(update_call[1]['range_name'], 'A2:E2')
        self.assertEqual(update_call[1]['values'], [[
            "2026-08-19",
            "Food & Dining",
            "Carlos Updated",
            "Updated tacos details and phone: (415) 555-1234",
            "https://maps.app.goo.gl/example"
        ]])

    @patch('recommendations.sheets.get_gspread_client')
    def test_update_recommendation_fallback_matching(self, mock_get_client):
        from recommendations.sheets import update_recommendation_in_google_sheet

        mock_ws = MagicMock()
        mock_ws.title = "Recomendaciones"
        mock_ws.get_all_values.return_value = [
            ['Date', 'Subject', 'Shared By', 'Recommendation', 'Location'],
            ['2026-08-10', 'Food & Dining', 'Carlos', 'First row info', 'SF'],
            ['2026-08-11', 'Doctors & Health', 'Ana', 'Original dentist recommendation that moved', 'Oakland']
        ]

        mock_sh = MagicMock()
        mock_sh.worksheets.return_value = [mock_ws]
        mock_sh.worksheet.return_value = mock_ws
        mock_client = MagicMock()
        mock_client.open.return_value = mock_sh
        mock_client.open_by_key.return_value = mock_sh
        mock_get_client.return_value = mock_client

        # Row index invalid (99), but original_recommendation matches row 3
        success = update_recommendation_in_google_sheet(
            row_index=99,
            shared_by="Ana Doctor",
            subject="Doctors & Health",
            recommendation="Revised dental clinic notes",
            date_str="2026-08-19",
            location="Oakland, CA",
            original_recommendation="Original dentist recommendation that moved"
        )
        self.assertTrue(success)
        mock_ws.update.assert_called_once()
        update_call = mock_ws.update.call_args
        self.assertEqual(update_call[1]['range_name'], 'A3:E3')

    def test_organizer_edit_view_requires_admin(self):
        # Authenticated non-admin gets 403 Forbidden
        self._login_as(self.regular_profile)
        response = self.client.post(reverse('recommendations:recommendation_edit'), {
            'row_index': '2',
            'category': 'Food & Dining',
            'shared_by': 'Carlos',
            'recommendation': 'Unauthorized edit attempt'
        })
        self.assertEqual(response.status_code, 403)

        # Unauthenticated user gets redirected to login
        self.client.session.flush()
        res_unauth = self.client.post(reverse('recommendations:recommendation_edit'), {
            'row_index': '2',
            'category': 'Food & Dining',
            'shared_by': 'Carlos',
            'recommendation': 'Unauthorized edit attempt'
        })
        self.assertEqual(res_unauth.status_code, 302)
        self.assertIn('/login/', res_unauth.url)

    @patch('recommendations.views.update_recommendation_in_google_sheet')
    def test_organizer_edit_view_success_and_audit_log(self, mock_update_sheet):
        mock_update_sheet.return_value = True
        self._login_as(self.admin_profile)

        cache.set('whatsapp_recommendations_cache', [{'dummy': 1}])

        response = self.client.post(reverse('recommendations:recommendation_edit'), {
            'row_index': '2',
            'category': 'Doctors & Health',
            'custom_category': '',
            'shared_by': 'Dr. Laura',
            'date': '2026-08-19',
            'location': '2288 Mission St, San Francisco',
            'recommendation': 'Updated pediatric recommendation with weekend hours.',
            'original_subject': 'Doctors & Health',
            'original_shared_by': 'Dr. Laura',
            'original_recommendation': 'Old pediatric recommendation'
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('recommendations:notes_list'))
        mock_update_sheet.assert_called_once()
        self.assertIsNone(cache.get('whatsapp_recommendations_cache'))

        # Check audit log
        log_entry = MembershipAuditLog.objects.filter(
            action=MembershipAuditLog.ACTION_RECOMMENDATION_UPDATED
        ).first()
        self.assertIsNotNone(log_entry)
        self.assertEqual(log_entry.actor_email, self.admin_profile.email)
        self.assertEqual(log_entry.target_name, 'Doctors & Health')
        self.assertIn('2288 Mission St', log_entry.notes)

    @patch('recommendations.views.fetch_recommendations')
    def test_notes_list_view_renders_edit_button_for_admin_only(self, mock_fetch):
        mock_fetch.return_value = [
            {
                'Date': '2026-08-19',
                'Subject': 'Food & Dining',
                'Shared_By': 'Carlos',
                'Recommendation': 'Delicious tacos on 24th St',
                'Location': '24th St SF',
                'location_display': '24th St SF',
                'location_url': 'https://maps.google.com',
                'lat': 37.75,
                'lng': -122.41,
                'has_location': True,
                'index': 0,
            }
        ]

        # 1. As regular member: NO edit buttons or edit modal
        self._login_as(self.regular_profile)
        res_member = self.client.get(reverse('recommendations:notes_list'))
        self.assertEqual(res_member.status_code, 200)
        self.assertNotContains(res_member, 'edit-rec-btn')
        self.assertNotContains(res_member, 'id="editRecommendationModal"')

        # 2. As admin: HAS edit button and edit modal
        self._login_as(self.admin_profile)
        res_admin = self.client.get(reverse('recommendations:notes_list'))
        self.assertEqual(res_admin.status_code, 200)
        self.assertContains(res_admin, 'edit-rec-btn')
        self.assertContains(res_admin, 'id="editRecommendationModal"')
        self.assertContains(res_admin, 'data-row-num="2"')


class GeminiWeatherTestCase(TestCase):
    """
    Unit tests for Google Gemini Flash 3.7 Weather Forecast & Outfit Advisor
    with Google Search Grounding and 6-hour caching.
    """

    def setUp(self):
        cache.clear()
        self.profile = MemberProfile.objects.create(
            email='member.weather@example.com',
            full_name='Weather Tester',
            is_admin=False
        )

    def _login(self):
        session = self.client.session
        session['is_verified_member'] = True
        session['member_email'] = self.profile.email
        session['member_name'] = self.profile.full_name
        session['is_admin'] = False
        session.save()

    def test_weather_endpoint_requires_authentication(self):
        response = self.client.get(reverse('event_weather'), {'location': 'San Francisco'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)

    def test_weather_missing_location_returns_error(self):
        self._login()
        response = self.client.get(reverse('event_weather'))
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertFalse(data['available'])
    @patch('google.genai.Client')
    def test_weather_past_event_prevents_api_call(self, mock_client_cls):
        self._login()
        from recommendations.weather import get_event_weather_forecast, is_date_in_past
        
        # Test past dates detection
        self.assertTrue(is_date_in_past('2020-01-01'))
        self.assertTrue(is_date_in_past('2020-01-01 10:00'))
        self.assertTrue(is_date_in_past('Wed, Jan 01, 2020 10:00 AM'))
        self.assertFalse(is_date_in_past('2099-12-31 18:00'))

        # Query for past event: should return past_event status and never call Gemini
        result = get_event_weather_forecast(location='Mission Dolores Park, SF', datetime_str='2020-01-01 14:00')
        self.assertFalse(result['available'])
        self.assertEqual(result['status'], 'past_event')
        self.assertIn('past events', result['message'])
        mock_client_cls.assert_not_called()

    @override_settings(GEMINI_MODEL='gemini-3.7-flash')
    @patch('google.genai.Client')
    def test_weather_success_and_caching(self, mock_client_cls):
        self._login()
        from recommendations.weather import get_event_weather_forecast

        # Mock Gemini SDK client response
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        mock_candidate = MagicMock()
        mock_candidate.grounding_metadata.web_search_queries = ['weather Mission Dolores SF 2026-08-22']
        
        mock_chunk = MagicMock()
        mock_chunk.web.title = 'National Weather Service'
        mock_chunk.web.uri = 'https://forecast.weather.gov'
        mock_candidate.grounding_metadata.grounding_chunks = [mock_chunk]

        mock_response = MagicMock()
        mock_response.text = (
            "### Expected Weather\n"
            "* Temperature: 68°F - 72°F, Sunny with mild coastal breeze.\n"
            "### What to Wear\n"
            "* **Adults**: Light layers, comfortable sneakers, sunglasses.\n"
            "* **Kids**: T-shirt + light hoodie, sun hat, and bring a windbreaker."
        )
        mock_response.candidates = [mock_candidate]
        mock_client.models.generate_content.return_value = mock_response

        # 1. First Call: Cache miss -> calls Gemini API
        res1 = get_event_weather_forecast(
            location='Mission Dolores Park, SF',
            datetime_str='2026-08-22 14:00',
            force_refresh=False,
            provider='gemini'
        )
        self.assertTrue(res1['available'])
        self.assertFalse(res1['cached'])
        self.assertEqual(res1['model'], 'gemini-3.7-flash')
        self.assertIn('Expected Weather', res1['recommendation'])
        self.assertEqual(len(res1['sources']), 1)
        self.assertEqual(res1['sources'][0]['title'], 'National Weather Service')
        mock_client.models.generate_content.assert_called_once()

        # Verify Google Search tool was configured
        call_kwargs = mock_client.models.generate_content.call_args[1]
        self.assertEqual(call_kwargs['model'], 'gemini-3.7-flash')
        self.assertTrue(hasattr(call_kwargs['config'], 'tools'))

        # 2. Second Call: Cache hit -> returns cached data, 0 new API calls (token saving)
        mock_client.models.generate_content.reset_mock()
        res2 = get_event_weather_forecast(
            location='Mission Dolores Park, SF',
            datetime_str='2026-08-22 14:00',
            force_refresh=False,
            provider='gemini'
        )
        self.assertTrue(res2['available'])
        self.assertTrue(res2['cached'])
        mock_client.models.generate_content.assert_not_called()

        # 3. Third Call with force_refresh=True -> bypasses cache and calls Gemini again
        res3 = get_event_weather_forecast(
            location='Mission Dolores Park, SF',
            datetime_str='2026-08-22 14:00',
            force_refresh=True,
            provider='gemini'
        )
        self.assertTrue(res3['available'])
        self.assertFalse(res3['cached'])
        mock_client.models.generate_content.assert_called_once()

    @patch('recommendations.weather.requests.get')
    def test_standard_openmeteo_weather(self, mock_requests_get):
        self._login()
        from recommendations.weather import get_openmeteo_weather_forecast

        # Mock Nominatim geocoding & Open-Meteo forecast responses
        def side_effect(url, **kwargs):
            mock_resp = MagicMock()
            if 'nominatim' in url:
                mock_resp.json.return_value = [{
                    'lat': '37.7596',
                    'lon': '-122.4260',
                    'display_name': 'Mission Dolores Park, San Francisco, CA'
                }]
            elif 'open-meteo' in url:
                mock_resp.json.return_value = {
                    'current_weather': {
                        'temperature': 65.2,
                        'weathercode': 2,
                        'windspeed': 10.5,
                    },
                    'daily': {
                        'time': ['2026-08-22'],
                        'temperature_2m_max': [68.0],
                        'temperature_2m_min': [54.0],
                        'weathercode': [2],
                        'precipitation_probability_max': [5],
                        'windspeed_10m_max': [12.0],
                    }
                }
            return mock_resp

        mock_requests_get.side_effect = side_effect

        # 1. Fetch Open-Meteo forecast
        res = get_openmeteo_weather_forecast('Mission Dolores Park, SF', '2026-08-22 10:00', force_refresh=True)
        self.assertTrue(res['available'])
        self.assertEqual(res['provider'], 'standard')
        self.assertEqual(res['temp_current'], 65.2)
        self.assertEqual(res['temp_max'], 68.0)
        self.assertEqual(res['temp_min'], 54.0)
        self.assertEqual(res['condition'], 'Partly Cloudy')
        self.assertEqual(res['rain_chance'], 5)
        self.assertIn('Adults', res['recommendation'])
        self.assertIn('Kids', res['recommendation'])

    @override_settings(GEMINI_API_KEY='test-key')
    @patch('recommendations.views.get_event_weather_forecast')
    def test_weather_view_endpoint_integration(self, mock_get_weather):
        self._login()
        mock_get_weather.return_value = {
            'available': True,
            'status': 'success',
            'location': 'Golden Gate Park, SF',
            'datetime_str': 'Sunday 11:00 AM',
            'recommendation': 'Partly cloudy, 64°F. Wear light jacket.',
            'cached': False,
            'sources': [],
        }

        response = self.client.get(reverse('event_weather'), {
            'location': 'Golden Gate Park, SF',
            'datetime': 'Sunday 11:00 AM'
        })
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['available'])
        mock_get_weather.assert_called_once_with(
            location='Golden Gate Park, SF',
            datetime_str='Sunday 11:00 AM',
            force_refresh=False,
            provider='standard'
        )

        # 1. Non-admin request with refresh=1 -> force_refresh is False
        mock_get_weather.reset_mock()
        response = self.client.get(reverse('event_weather'), {
            'location': 'Golden Gate Park, SF',
            'datetime': 'Sunday 11:00 AM',
            'refresh': '1'
        })
        self.assertEqual(response.status_code, 200)
        mock_get_weather.assert_called_once_with(
            location='Golden Gate Park, SF',
            datetime_str='Sunday 11:00 AM',
            force_refresh=False,
            provider='standard'
        )

        # 2. Admin request with refresh=1 -> force_refresh is True
        session = self.client.session
        session['is_admin'] = True
        session.save()

        mock_get_weather.reset_mock()
        response = self.client.get(reverse('event_weather'), {
            'location': 'Golden Gate Park, SF',
            'datetime': 'Sunday 11:00 AM',
            'refresh': '1'
        })
        self.assertEqual(response.status_code, 200)
        mock_get_weather.assert_called_once_with(
            location='Golden Gate Park, SF',
            datetime_str='Sunday 11:00 AM',
            force_refresh=True,
            provider='standard'
        )

        # 3. Explicit provider request
        mock_get_weather.reset_mock()
        response = self.client.get(reverse('event_weather'), {
            'location': 'Golden Gate Park, SF',
            'datetime': 'Sunday 11:00 AM',
            'provider': 'both'
        })
        self.assertEqual(response.status_code, 200)
        mock_get_weather.assert_called_once_with(
            location='Golden Gate Park, SF',
            datetime_str='Sunday 11:00 AM',
            force_refresh=False,
            provider='both'
        )













