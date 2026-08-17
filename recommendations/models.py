from django.db import models


class MemberProfile(models.Model):
    REGION_CHOICES = [
        ('san_francisco', 'San Francisco'),
        ('peninsula', 'Peninsula (San Mateo, Burlingame, Redwood City, Palo Alto, Menlo Park)'),
        ('south_bay', 'South Bay / Silicon Valley (San Jose, Santa Clara, Sunnyvale, Mountain View)'),
        ('east_bay', 'East Bay (Oakland, Berkeley, Fremont, Dublin, Pleasanton)'),
        ('north_bay', 'North Bay (Marin, Sonoma, Napa, Sausalito)'),
        ('santa_cruz', 'Santa Cruz & South Coast'),
        ('other', 'Other / Outside Bay Area'),
    ]

    email = models.EmailField(unique=True, db_index=True, verbose_name="Google Email")
    full_name = models.CharField(max_length=150, blank=True, verbose_name="Full Name")
    phone_number = models.CharField(max_length=50, blank=True, verbose_name="WhatsApp / Phone Number")
    region = models.CharField(
        max_length=50, 
        choices=REGION_CHOICES, 
        blank=True, 
        default='', 
        verbose_name="Bay Area Region"
    )
    city = models.CharField(
        max_length=100, 
        blank=True, 
        verbose_name="City / Neighborhood",
        help_text="e.g., Sunnyvale, Mission SF, Berkeley Hills"
    )
    family_info = models.CharField(
        max_length=200, 
        blank=True, 
        verbose_name="Family & Kids Details",
        help_text="e.g., Kids (ages 3 & 7), Expecting, No kids"
    )
    interests = models.CharField(
        max_length=255, 
        blank=True, 
        verbose_name="Event Interests",
        help_text="Comma-separated or selected event interests"
    )
    bio = models.TextField(
        blank=True, 
        verbose_name="About Me / Notes",
        help_text="A few words about you, where you are originally from, hobbies, etc."
    )
    is_admin = models.BooleanField(
        default=False, 
        verbose_name="Organizer / Admin"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Member Profile"
        verbose_name_plural = "Member Profiles"
        ordering = ['full_name', 'email']

    def __str__(self):
        region_display = self.get_region_display() if self.region else 'No region set'
        return f"{self.full_name or self.email} ({region_display})"


class MembershipRequest(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_APPROVED = 'approved'
    STATUS_REJECTED = 'rejected'

    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending Approval'),
        (STATUS_APPROVED, 'Approved'),
        (STATUS_REJECTED, 'Declined'),
    ]

    full_name = models.CharField(max_length=150, verbose_name="Full Name")
    email = models.EmailField(db_index=True, verbose_name="Google / Gmail Address")
    phone_number = models.CharField(max_length=50, verbose_name="WhatsApp / Phone Number")
    region = models.CharField(
        max_length=50, 
        choices=MemberProfile.REGION_CHOICES, 
        blank=True, 
        default='', 
        verbose_name="Bay Area Region"
    )
    city = models.CharField(
        max_length=100, 
        blank=True, 
        verbose_name="City / Neighborhood"
    )
    referral_source = models.TextField(
        blank=True, 
        verbose_name="Referral Source / How did you hear about MiniMexitas?"
    )
    status = models.CharField(
        max_length=20, 
        choices=STATUS_CHOICES, 
        default=STATUS_PENDING, 
        db_index=True,
        verbose_name="Status"
    )
    reviewed_by = models.CharField(max_length=150, blank=True, verbose_name="Reviewed By")
    reviewed_at = models.DateTimeField(null=True, blank=True, verbose_name="Reviewed At")
    review_notes = models.TextField(blank=True, verbose_name="Internal Review Notes")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Membership Request"
        verbose_name_plural = "Membership Requests"
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.full_name} ({self.email}) - {self.get_status_display()}"
