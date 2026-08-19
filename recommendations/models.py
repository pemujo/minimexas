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
    email_notifications = models.BooleanField(
        default=True,
        verbose_name="Receive Email Notifications",
        help_text="Opt in or out of community event broadcasts and updates via email."
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


class MembershipAuditLog(models.Model):
    ACTION_SUBMITTED = 'submitted'
    ACTION_APPROVED = 'approved'
    ACTION_REJECTED = 'rejected'
    ACTION_DIRECT_ADDED = 'direct_added'
    ACTION_DELETED = 'deleted'
    ACTION_SURVEY_CREATED = 'survey_created'
    ACTION_SURVEY_CLOSED = 'survey_closed'
    ACTION_SURVEY_DELETED = 'survey_deleted'
    ACTION_EVENT_BROADCAST = 'event_broadcast'
    ACTION_EVENT_CREATED = 'event_created'
    ACTION_EVENT_UPDATED = 'event_updated'
    ACTION_EVENT_DELETED = 'event_deleted'
    ACTION_RECOMMENDATION_CREATED = 'recommendation_created'
    ACTION_RECOMMENDATION_UPDATED = 'recommendation_updated'

    ACTION_CHOICES = [
        (ACTION_SUBMITTED, 'Request Submitted'),
        (ACTION_APPROVED, 'Request Approved'),
        (ACTION_REJECTED, 'Request Rejected'),
        (ACTION_DIRECT_ADDED, 'Member Added Directly'),
        (ACTION_DELETED, 'Member Deleted'),
        (ACTION_SURVEY_CREATED, 'Survey Created'),
        (ACTION_SURVEY_CLOSED, 'Survey Closed/Reopened'),
        (ACTION_SURVEY_DELETED, 'Survey Deleted'),
        (ACTION_EVENT_BROADCAST, 'Event Notification Broadcast'),
        (ACTION_EVENT_CREATED, 'Event Created'),
        (ACTION_EVENT_UPDATED, 'Event Updated'),
        (ACTION_EVENT_DELETED, 'Event Deleted'),
        (ACTION_RECOMMENDATION_CREATED, 'Recommendation Created'),
        (ACTION_RECOMMENDATION_UPDATED, 'Recommendation Updated'),
    ]

    action = models.CharField(max_length=30, choices=ACTION_CHOICES, db_index=True, verbose_name="Action")
    target_email = models.EmailField(db_index=True, verbose_name="Target Member Email")
    target_name = models.CharField(max_length=150, blank=True, verbose_name="Target Member Name")
    actor_name = models.CharField(max_length=150, blank=True, verbose_name="Performed By (Name)")
    actor_email = models.CharField(max_length=150, blank=True, verbose_name="Performed By (Email)")
    notes = models.TextField(blank=True, verbose_name="Notes / Justification")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="Timestamp")

    class Meta:
        verbose_name = "Membership Audit Log"
        verbose_name_plural = "Membership Audit Logs"
        ordering = ['-created_at']

    def __str__(self):
        return f"[{self.created_at.strftime('%Y-%m-%d %H:%M')}] {self.get_action_display()}: {self.target_name or self.target_email} by {self.actor_name or self.actor_email or 'System'}"


class CommunitySurvey(models.Model):
    CATEGORY_EVENT = 'event'
    CATEGORY_FOOD = 'food'
    CATEGORY_COMMUNITY = 'community'
    CATEGORY_GENERAL = 'general'

    CATEGORY_CHOICES = [
        (CATEGORY_EVENT, 'Event & Meetup'),
        (CATEGORY_FOOD, 'Food & Culinary'),
        (CATEGORY_COMMUNITY, 'Community Initiative'),
        (CATEGORY_GENERAL, 'General Question'),
    ]

    title = models.CharField(max_length=255, verbose_name="Survey Question / Title")
    description = models.TextField(blank=True, verbose_name="Context & Details")
    category = models.CharField(max_length=30, choices=CATEGORY_CHOICES, default=CATEGORY_EVENT, verbose_name="Category")
    is_multiple_choice = models.BooleanField(default=False, verbose_name="Allow Multiple Choices")
    is_active = models.BooleanField(default=True, db_index=True, verbose_name="Active for Voting")
    created_by = models.CharField(max_length=150, blank=True, verbose_name="Created By (Admin Name)")
    created_by_email = models.EmailField(blank=True, verbose_name="Created By (Admin Email)")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Community Survey"
        verbose_name_plural = "Community Surveys"
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.title} ({'Active' if self.is_active else 'Closed'})"

    @property
    def total_votes(self):
        return self.votes.count()

    @property
    def unique_voters_count(self):
        return self.votes.values('voter_email').distinct().count()

    def user_voted_option_ids(self, email):
        if not email:
            return set()
        return set(self.votes.filter(voter_email__iexact=email.strip().lower()).values_list('option_id', flat=True))

    def has_user_voted(self, email):
        if not email:
            return False
        return self.votes.filter(voter_email__iexact=email.strip().lower()).exists()


class SurveyOption(models.Model):
    survey = models.ForeignKey(CommunitySurvey, on_delete=models.CASCADE, related_name='options')
    text = models.CharField(max_length=255, verbose_name="Option Text")
    order = models.PositiveIntegerField(default=0, verbose_name="Display Order")

    class Meta:
        verbose_name = "Survey Option"
        verbose_name_plural = "Survey Options"
        ordering = ['order', 'id']

    def __str__(self):
        return f"{self.survey.title} - {self.text}"

    @property
    def vote_count(self):
        return self.votes.count()

    def percentage(self, total_survey_votes):
        if not total_survey_votes:
            return 0
        return round((self.vote_count / total_survey_votes) * 100, 1)

    @property
    def voter_names(self):
        return [v.voter_name for v in self.votes.all() if v.voter_name]


class SurveyVote(models.Model):
    survey = models.ForeignKey(CommunitySurvey, on_delete=models.CASCADE, related_name='votes')
    option = models.ForeignKey(SurveyOption, on_delete=models.CASCADE, related_name='votes')
    voter_email = models.EmailField(db_index=True, verbose_name="Voter Email")
    voter_name = models.CharField(max_length=150, verbose_name="Voter Name")
    voted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Survey Vote"
        verbose_name_plural = "Survey Votes"
        unique_together = ('survey', 'option', 'voter_email')
        ordering = ['-voted_at']

    def __str__(self):
        return f"{self.voter_name} ({self.voter_email}) voted '{self.option.text}' for '{self.survey.title}'"


class CommunityEventRSVP(models.Model):
    STATUS_GOING = 'going'
    STATUS_MAYBE = 'maybe'
    STATUS_DECLINED = 'declined'

    STATUS_CHOICES = [
        (STATUS_GOING, 'Going / Asistiré'),
        (STATUS_MAYBE, 'Maybe / Tal vez'),
        (STATUS_DECLINED, 'Not Going / No podré'),
    ]

    event_id = models.CharField(max_length=255, db_index=True, verbose_name="Event ID")
    event_title = models.CharField(max_length=255, blank=True, verbose_name="Event Title")
    member_email = models.EmailField(db_index=True, verbose_name="Member Email")
    member_name = models.CharField(max_length=150, verbose_name="Member Name")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_GOING, verbose_name="RSVP Status")
    notes = models.TextField(blank=True, verbose_name="Notes / Guests / Comments")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Event RSVP"
        verbose_name_plural = "Event RSVPs"
        unique_together = ('event_id', 'member_email')
        ordering = ['-updated_at']

    def __str__(self):
        return f"{self.member_name} - {self.get_status_display()} for {self.event_title or self.event_id}"


class CommunityEventBroadcast(models.Model):
    event_id = models.CharField(max_length=255, unique=True, db_index=True, verbose_name="Event ID")
    event_title = models.CharField(max_length=255, verbose_name="Event Title")
    broadcast_by = models.CharField(max_length=150, verbose_name="Broadcasted By (Admin Name)")
    broadcast_by_email = models.EmailField(verbose_name="Broadcasted By (Admin Email)")
    recipient_count = models.PositiveIntegerField(default=0, verbose_name="Recipients Count")
    broadcast_at = models.DateTimeField(auto_now=True, verbose_name="Last Broadcasted At")

    class Meta:
        verbose_name = "Event Broadcast"
        verbose_name_plural = "Event Broadcasts"
        ordering = ['-broadcast_at']

    def __str__(self):
        return f"{self.event_title} ({self.recipient_count} recipients on {self.broadcast_at.strftime('%Y-%m-%d %H:%M')})"


class CommunityEvent(models.Model):
    CATEGORY_CULTURAL = 'Cultural & Heritage'
    CATEGORY_NETWORKING = 'Networking & Tech'
    CATEGORY_FAMILY = 'Family & Outdoors'
    CATEGORY_CULINARY = 'Culinary & Social'
    CATEGORY_COMMUNITY = 'Community Gathering'

    CATEGORY_CHOICES = [
        (CATEGORY_CULTURAL, 'Cultural & Heritage'),
        (CATEGORY_NETWORKING, 'Networking & Tech'),
        (CATEGORY_FAMILY, 'Family & Outdoors'),
        (CATEGORY_CULINARY, 'Culinary & Social'),
        (CATEGORY_COMMUNITY, 'Community Gathering'),
    ]

    title = models.CharField(max_length=255, verbose_name="Event Title")
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES, default=CATEGORY_COMMUNITY, verbose_name="Category")
    start_datetime = models.DateTimeField(verbose_name="Start Date & Time")
    end_datetime = models.DateTimeField(blank=True, null=True, verbose_name="End Date & Time")
    location = models.CharField(max_length=255, verbose_name="Location / Venue", default="San Francisco Bay Area")
    location_url = models.URLField(blank=True, verbose_name="Google Maps or Location URL")
    description = models.TextField(blank=True, verbose_name="Description / Details")
    created_by_name = models.CharField(max_length=150, blank=True, verbose_name="Created By (Name)")
    created_by_email = models.EmailField(blank=True, verbose_name="Created By (Email)")
    is_active = models.BooleanField(default=True, db_index=True, verbose_name="Active")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Community Event"
        verbose_name_plural = "Community Events"
        ordering = ['start_datetime']

    def __str__(self):
        return f"{self.title} ({self.start_datetime.strftime('%Y-%m-%d %H:%M')})"


