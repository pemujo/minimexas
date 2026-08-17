from unittest.mock import patch, MagicMock
from django.test import TestCase, Client
from django.urls import reverse
from .models import MemberProfile
from .auth_helpers import is_gmail_allowed


class HomeViewTestCase(TestCase):
    def setUp(self):
        self.client = Client()

    def test_unauthenticated_home_view_redirects_to_login(self):
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)

    def test_login_page_renders_for_unauthenticated_users(self):
        response = self.client.get(reverse('login_page'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "MiniMexitas")
        self.assertContains(response, "Sign in with Google")

    def test_authenticated_home_view_shows_full_dashboard(self):
        session = self.client.session
        session['is_verified_member'] = True
        session['member_name'] = 'Maria Gonzalez'
        session['member_email'] = 'maria@minimexitas.local'
        session.save()

        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Welcome, Maria Gonzalez")
        self.assertContains(response, "Member Directory")
        self.assertContains(response, "Events & Meetups")


class AuthHelpersTestCase(TestCase):
    @patch('recommendations.auth_helpers.get_gspread_client')
    def test_is_gmail_allowed_admin_role(self, mock_get_client):
        mock_ws = MagicMock()
        mock_ws.get_all_records.return_value = [
            {'Name': 'Admin User', 'Gmail': 'admin@gmail.com', 'Role': 'Admin'},
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

        # Regular user check
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
            {'Name': '  Carlos M.  ', 'Gmail': '  CARLOS@gmail.com  ', 'Role': '  ORGANIZER  '},
        ]
        mock_sh = MagicMock()
        mock_sh.worksheet.return_value = mock_ws
        mock_client = MagicMock()
        mock_client.open.return_value = mock_sh
        mock_get_client.return_value = mock_client

        allowed, name, is_admin = is_gmail_allowed('carlos@gmail.com')
        self.assertTrue(allowed)
        self.assertTrue(is_admin)
        self.assertEqual(name, 'Carlos M.')


class MemberProfileTestCase(TestCase):
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
            interests="Cultural & Traditional Celebrations, Family & Kids Outings",
            bio="Software engineer and mom of two.",
            is_admin=False
        )

    def test_profile_str_representation(self):
        self.assertIn("Maria Gonzalez", str(self.profile))
        self.assertIn("Peninsula", str(self.profile))

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

    def test_authenticated_profile_post_update(self):
        session = self.client.session
        session['is_verified_member'] = True
        session['member_name'] = 'Maria Gonzalez'
        session['member_email'] = 'testuser@minimexitas.local'
        session.save()

        post_data = {
            'full_name': 'Maria Gonzalez Updated',
            'phone_number': '+1 650 999 8888',
            'region': 'south_bay',
            'city': 'Sunnyvale',
            'family_info': '3 kids',
            'interests': ['Culinary & Dining', 'Tech & Professional Networking'],
            'bio': 'Updated bio content here.',
        }

        response = self.client.post(reverse('profile'), post_data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Your profile and region details have been updated successfully!")

        # Verify DB updated
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.full_name, 'Maria Gonzalez Updated')
        self.assertEqual(self.profile.region, 'south_bay')
        self.assertEqual(self.profile.city, 'Sunnyvale')
        self.assertIn('Culinary & Dining', self.profile.interests)

    def test_profile_post_cannot_escalate_role_to_admin(self):
        session = self.client.session
        session['is_verified_member'] = True
        session['is_admin'] = False
        session['member_name'] = 'Maria Gonzalez'
        session['member_email'] = 'testuser@minimexitas.local'
        session.save()

        # Attempt to inject role and is_admin
        post_data = {
            'full_name': 'Maria Hacker',
            'region': 'peninsula',
            'is_admin': 'True',
            'role': 'Admin',
        }

        response = self.client.post(reverse('profile'), post_data)
        self.assertEqual(response.status_code, 200)

        # Verify is_admin remains strictly False
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.is_admin)


class MemberDirectoryTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        MemberProfile.objects.all().delete()
        self.member1, _ = MemberProfile.objects.get_or_create(
            email="secret1@gmail.com",
            defaults={
                "full_name": "Carlos Santana",
                "region": "south_bay",
                "city": "San Jose",
                "phone_number": "+1 408 555 9999",
                "family_info": "2 teenagers",
                "interests": "Culinary & Dining, Tech & Professional Networking",
                "bio": "Passionate about Mexican cuisine and tech.",
                "is_admin": False,
            }
        )
        self.member2, _ = MemberProfile.objects.get_or_create(
            email="secretadmin@gmail.com",
            defaults={
                "full_name": "Lucia Mendez",
                "region": "san_francisco",
                "city": "Mission SF",
                "phone_number": "+1 415 555 7777",
                "family_info": "1 toddler",
                "interests": "Cultural & Traditional Celebrations",
                "bio": "Community organizer and artist.",
                "is_admin": True,
            }
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
        self.assertContains(response, "South Bay")
        self.assertContains(response, "San Francisco")
        self.assertContains(response, "Culinary")

    def test_directory_strictly_hides_private_fields(self):
        session = self.client.session
        session['is_verified_member'] = True
        session['is_admin'] = False
        session['member_name'] = 'Carlos Santana'
        session['member_email'] = 'secret1@gmail.com'
        session.save()

        response = self.client.get(reverse('member_directory'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')

        # Verify private phone numbers are NOT in the HTML
        self.assertNotIn("+1 408 555 9999", content)
        self.assertNotIn("+1 415 555 7777", content)

        # Verify private emails are NOT in the HTML
        self.assertNotIn("secret1@gmail.com", content)
        self.assertNotIn("secretadmin@gmail.com", content)

        # Verify roles are NOT exposed
        self.assertNotIn("Organizer / Admin", content)


class OrganizerDashboardTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        MemberProfile.objects.all().delete()
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
        self.assertContains(response, "Member Directory")
        self.assertContains(response, "Regular Member")
        self.assertContains(response, "San Mateo")


class EventsAndRecommendationsViewTestCase(TestCase):
    def setUp(self):
        self.client = Client()

    def test_unauthenticated_events_redirects_to_login(self):
        response = self.client.get(reverse('events'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)

    def test_authenticated_events_view(self):
        session = self.client.session
        session['is_verified_member'] = True
        session['member_name'] = 'Carlos Member'
        session.save()

        response = self.client.get(reverse('events'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Events")

    def test_unauthenticated_recommendations_redirects_to_login(self):
        response = self.client.get(reverse('recommendations:notes_list'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)

    @patch('recommendations.views.fetch_recommendations')
    def test_authenticated_recommendations_view(self, mock_fetch):
        mock_fetch.return_value = ([], ['Food', 'Services'])
        session = self.client.session
        session['is_verified_member'] = True
        session['member_name'] = 'Carlos Member'
        session.save()

        response = self.client.get(reverse('recommendations:notes_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Recommendations")


class LogoutTestCase(TestCase):
    def setUp(self):
        self.client = Client()

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

        # Verify session cleared
        self.assertFalse(self.client.session.get('is_verified_member', False))
        self.assertFalse(self.client.session.get('is_admin', False))


class GoogleSheetSyncTestCase(TestCase):
    def test_sync_profile_without_credentials_fails_gracefully(self):
        from .sheets import sync_profile_to_google_sheet
        profile = MemberProfile.objects.create(
            email="test_sync@minimexitas.local",
            full_name="Sync Tester",
            region="san_francisco"
        )
        # Should return False safely without raising uncaught exception when not configured in test env
        res = sync_profile_to_google_sheet(profile)
        self.assertIsInstance(res, bool)




