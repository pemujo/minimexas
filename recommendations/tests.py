import datetime
from unittest.mock import patch, MagicMock
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.core.cache import cache

from recommendations.models import MemberProfile, MembershipRequest
from recommendations.auth_helpers import is_gmail_allowed
from recommendations.sheets import (
    _normalize_record_keys,
    get_gspread_client,
    fetch_recommendations,
    sync_profile_to_google_sheet,
)
from recommendations.calendar_sync import (
    get_google_calendar_add_url,
    _parse_event_datetime,
    fetch_community_events,
)
from recommendations.views import get_whatsapp_url


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
        mock_ws.get_all_records.return_value = [
            {"Shared By": "Carlos", "Category": "Doctors", "Recommendation": "Dr. Smith"}
        ]
        mock_sh = MagicMock()
        mock_sh.sheet1 = mock_ws
        mock_client = MagicMock()
        mock_client.open.return_value = mock_sh
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
        self.assertContains(response, "Unable to load Google Sheet")

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

    def test_join_request_post_success_creates_pending_request(self):
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
            self.assertContains(response, "pending organizer approval")

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

    @patch('recommendations.views.sync_profile_to_google_sheet')
    def test_organizer_approve_request_creates_profile_and_syncs(self, mock_sync):
        mock_sync.return_value = True

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

    def test_organizer_approve_request_nonexistent_returns_404(self):
        session = self.client.session
        session['is_verified_member'] = True
        session['is_admin'] = True
        session['member_name'] = 'Admin Organizer'
        session['member_email'] = 'admin@minimexitas.local'
        session.save()

        response = self.client.post(reverse('organizer_approve_request', kwargs={'request_id': 99999}))
        self.assertEqual(response.status_code, 404)

    def test_organizer_reject_request(self):
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

        response = self.client.post(reverse('organizer_reject_request', kwargs={'request_id': req.id}))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('organizer_dashboard'), response.url)

        req.refresh_from_db()
        self.assertEqual(req.status, MembershipRequest.STATUS_REJECTED)
        self.assertEqual(req.reviewed_by, 'Admin Organizer')
        self.assertFalse(MemberProfile.objects.filter(email='spam@gmail.com').exists())

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
