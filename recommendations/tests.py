from django.test import TestCase, Client
from django.urls import reverse
from .models import MemberProfile


class MemberProfileTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.profile = MemberProfile.objects.create(
            email="testuser@minimexitas.local",
            full_name="Maria Gonzalez",
            region="peninsula",
            city="Palo Alto",
            phone_number="+1 650 555 1234",
            family_info="2 kids (ages 3 & 6)",
            interests="Cultural & Traditional Celebrations, Family & Kids Outings",
            bio="Software engineer and mom of two."
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


class OrganizerDashboardTestCase(TestCase):
    def setUp(self):
        self.client = Client()
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

    def test_admin_export_members_csv(self):
        session = self.client.session
        session['is_verified_member'] = True
        session['is_admin'] = True
        session['member_name'] = 'Organizer Admin'
        session['member_email'] = 'organizer@minimexitas.local'
        session.save()

        response = self.client.get(reverse('export_members_csv'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')
        content = response.content.decode('utf-8')
        self.assertIn("Full Name,Gmail,Region", content)
        self.assertIn("Organizer Admin", content)
        self.assertIn("Regular Member", content)


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


