import logging
import urllib.parse
from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.urls import reverse

logger = logging.getLogger(__name__)


def clean_phone_for_whatsapp(phone_number: str) -> str:
    """Extract digits and normalize US 10-digit phone to include country code."""
    if not phone_number:
        return ""
    raw_digits = "".join(filter(str.isdigit, str(phone_number)))
    if len(raw_digits) == 10:
        return f"1{raw_digits}"
    return raw_digits


def _resolve_base_portal_url(portal_url: str = None, request = None) -> str:
    """
    Safely resolves the absolute portal base URL (scheme + host without subpaths or trailing slash).
    Prefers the dynamic request host (works in any environment), then explicit portal_url, then PORTAL_BASE_URL setting.
    Strips trailing slashes and common subpaths like /login, /join, /organizers if accidentally included.
    """
    if request is not None:
        try:
            built = request.build_absolute_uri('/')
            if built and '://' in built:
                return built.rstrip('/')
        except Exception:
            pass

    if portal_url and str(portal_url).strip():
        url = str(portal_url).strip().rstrip('/')
        if not url.startswith(('http://', 'https://')):
            url = f"https://{url}"
        for sub in ['/login', '/join', '/organizers', '/events', '/surveys']:
            if url.endswith(sub):
                url = url[:-len(sub)].rstrip('/')
        return url

    env_base = getattr(settings, 'PORTAL_BASE_URL', '').strip().rstrip('/')
    if env_base:
        if not env_base.startswith(('http://', 'https://')):
            env_base = f"https://{env_base}"
        for sub in ['/login', '/join', '/organizers', '/events', '/surveys']:
            if env_base.endswith(sub):
                env_base = env_base[:-len(sub)].rstrip('/')
        return env_base

    return ""


def build_whatsapp_approval_link(full_name: str, phone_number: str, portal_url: str = None, request = None) -> str:
    """Build a pre-filled WhatsApp message URL to welcome an approved applicant."""
    clean_phone = clean_phone_for_whatsapp(phone_number)
    if not clean_phone:
        return ""
    
    name = full_name.strip() if full_name else "amig@"
    base = _resolve_base_portal_url(portal_url, request)
    url = f"{base}/login/" if base else "/login/"

    message = (
        f"¡Hola {name}! 🎉 Tu solicitud para unirte a MiniMexitas ha sido aprobada. "
        f"Tu cuenta de Gmail ya tiene acceso autorizado y ya puedes ingresar al portal en {url} "
        f"para ver recomendaciones verificadas, eventos y el directorio. ¡Bienvenid@ a la comunidad!"
    )
    return f"https://wa.me/{clean_phone}?text={urllib.parse.quote(message)}"


def build_whatsapp_decline_link(full_name: str, phone_number: str, reason: str = None) -> str:
    """Build a pre-filled WhatsApp message URL to send a polite decline notice."""
    clean_phone = clean_phone_for_whatsapp(phone_number)
    if not clean_phone:
        return ""
    
    name = full_name.strip() if full_name else "amig@"
    reason_text = reason.strip() if reason and reason.strip() else "No pudimos verificar tu conexión con la comunidad en este momento."
    message = (
        f"Hola {name}, gracias por tu interés y solicitud para unirte a MiniMexitas. "
        f"Lamentamos informarte que tu solicitud no pudo ser aprobada en este momento por la siguiente razón:\n\n"
        f"\"{reason_text}\"\n\n"
        f"Si consideras que hubo un error o deseas actualizar tus datos, con gusto nos puedes responder por aquí."
    )
    return f"https://wa.me/{clean_phone}?text={urllib.parse.quote(message)}"


def _get_from_email() -> str:
    """
    Resolves a safe FROM email address.
    If EMAIL_HOST_USER is configured, ensures the sender matches or uses EMAIL_HOST_USER
    so that Google/Gmail SMTP does not reject the message for sender mismatch.
    """
    default_from = getattr(settings, 'DEFAULT_FROM_EMAIL', '').strip()
    host_user = getattr(settings, 'EMAIL_HOST_USER', '').strip()
    if default_from and 'no-reply@minimexitas.org' not in default_from:
        return default_from
    if host_user:
        return f"MiniMexitas Community <{host_user}>"
    if default_from:
        return default_from
    return "MiniMexitas Community <no-reply@minimexitas.org>"


def send_membership_approval_email(request_obj, portal_url: str = None, request = None) -> bool:
    """
    Sends an automated, welcoming approval confirmation email to an approved member.
    Safe against failure; logs warnings instead of crashing.
    """
    recipient_email = getattr(request_obj, 'email', None)
    if not recipient_email or not recipient_email.strip():
        logger.warning("Cannot send approval email: empty recipient email.")
        return False

    full_name = getattr(request_obj, 'full_name', '').strip() or "Nuevo Miembro"
    recipient_email = recipient_email.strip().lower()

    # Determine absolute portal login URL
    base = _resolve_base_portal_url(portal_url, request)
    portal_url = f"{base}/login/" if base else "/login/"

    subject = "¡Bienvenido(a) a MiniMexitas! Tu solicitud ha sido aprobada 🎉"
    from_email = _get_from_email()

    plain_text_content = f"""¡Hola {full_name}!

Nos da mucho gusto informarte que tu solicitud para unirte a la comunidad privada de MiniMexitas en el Área de la Bahía ha sido aprobada por los organizadores.

Tu cuenta de Gmail ({recipient_email}) ya tiene acceso autorizado al portal.

Puedes ingresar en cualquier momento iniciando sesión con tu cuenta de Google en:
{portal_url}

En el portal encontrarás:
• Recomendaciones Verificadas: Restaurantes, doctores, servicios, escuelas y productos locales recomendados por miembros de la comunidad.
• Eventos y Reuniones: Calendario de convivencias, picnics y celebraciones comunitarias.
• Directorio de Miembros: Conecta con personas y familias de tu misma zona en la Bahía.

¡Te damos la más cordial bienvenida a MiniMexitas!

Atentamente,
Equipo de Organizadores de MiniMexitas
Área de la Bahía, California
"""

    html_content = f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Bienvenido a MiniMexitas</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f8fafc; color: #1e293b; margin: 0; padding: 0; }}
    .container {{ max-width: 600px; margin: 20px auto; background-color: #ffffff; border-radius: 16px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05), 0 2px 4px -2px rgba(0,0,0,0.05); }}
    .header {{ background: linear-gradient(135deg, #075e54 0%, #128c7e 100%); padding: 32px 24px; text-align: center; }}
    .header h1 {{ color: #ffffff; margin: 0; font-size: 24px; font-weight: 700; letter-spacing: -0.02em; }}
    .header p {{ color: #e2e8f0; margin: 6px 0 0; font-size: 14px; }}
    .content {{ padding: 32px 28px; line-height: 1.6; }}
    .greeting {{ font-size: 18px; font-weight: 600; color: #0f172a; margin-bottom: 16px; }}
    .intro {{ font-size: 15px; color: #334155; margin-bottom: 20px; }}
    .badge-box {{ background-color: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 12px; padding: 16px 20px; margin-bottom: 24px; }}
    .badge-box p {{ margin: 0; color: #166534; font-size: 14px; font-weight: 500; }}
    .cta-container {{ text-align: center; margin: 28px 0; }}
    .btn {{ display: inline-block; background-color: #25d366; color: #ffffff !important; font-size: 15px; font-weight: 600; text-decoration: none; padding: 14px 32px; border-radius: 50px; box-shadow: 0 4px 12px rgba(37,211,102,0.3); }}
    .features-list {{ background-color: #f8fafc; border-radius: 12px; padding: 20px 24px; margin: 24px 0; }}
    .features-list h4 {{ margin: 0 0 12px 0; font-size: 14px; color: #475569; text-transform: uppercase; letter-spacing: 0.05em; }}
    .feature-item {{ margin-bottom: 12px; font-size: 14px; color: #334155; }}
    .footer {{ background-color: #0f172a; color: #94a3b8; padding: 24px; text-align: center; font-size: 12px; line-height: 1.5; }}
    .footer a {{ color: #cbd5e1; text-decoration: underline; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>MiniMexitas</h1>
      <p>Comunidad del Área de la Bahía</p>
    </div>
    <div class="content">
      <div class="greeting">¡Hola {full_name}! 🎉</div>
      <p class="intro">
        Nos da mucho gusto informarte que tu solicitud para unirte a la comunidad privada de <strong>MiniMexitas</strong> ha sido aprobada por los organizadores.
      </p>

      <div class="badge-box">
        <p>✅ Tu cuenta de Google (<strong>{recipient_email}</strong>) ya tiene acceso autorizado al portal privado.</p>
      </div>

      <div class="cta-container">
        <a href="{portal_url}" class="btn">Ingresar al Portal de MiniMexitas</a>
      </div>

      <div class="features-list">
        <h4>Lo que puedes hacer en el portal:</h4>
        <div class="feature-item">🌮 <strong>Recomendaciones Vetted:</strong> Médicos, comida tradicional mexicana, servicios confiables y escuelas.</div>
        <div class="feature-item">📅 <strong>Eventos y Reuniones:</strong> Convivencias, picnics y celebraciones familiares en la Bahía.</div>
        <div class="feature-item">👥 <strong>Directorio de Miembros:</strong> Conoce a otras familias y profesionistas de tu zona.</div>
      </div>

      <p style="font-size: 14px; color: #64748b; margin-top: 24px;">
        Si tienes alguna duda o sugerencia, no dudes en contactar al equipo de organizadores por WhatsApp.
      </p>
    </div>
    <div class="footer">
      <p style="margin: 0 0 4px 0;"><strong>MiniMexitas Community Portal</strong></p>
      <p style="margin: 0;">San Francisco Bay Area • California</p>
    </div>
  </div>
</body>
</html>
"""

    try:
        msg = EmailMultiAlternatives(
            subject=subject,
            body=plain_text_content,
            from_email=from_email,
            to=[recipient_email]
        )
        msg.attach_alternative(html_content, "text/html")
        msg.send(fail_silently=False)
        logger.info(f"Membership approval email successfully sent to {recipient_email}")
        return True
    except Exception as e:
        logger.warning(f"Failed to send membership approval email to {recipient_email}: {e}")
        return False


def send_membership_rejection_email(request_obj, reason: str = None, portal_url: str = None, request = None) -> bool:
    """
    Sends an automated, polite rejection notification email to an applicant whose
    membership request was declined, including the reason/notes entered by the admin.
    Safe against failure; logs warnings instead of crashing.
    """
    recipient_email = getattr(request_obj, 'email', None)
    if not recipient_email or not recipient_email.strip():
        logger.warning("Cannot send rejection email: empty recipient email.")
        return False

    full_name = getattr(request_obj, 'full_name', '').strip() or "Estimad@ solicitante"
    recipient_email = recipient_email.strip().lower()

    # Determine absolute portal join URL
    base = _resolve_base_portal_url(portal_url, request)
    join_url = f"{base}/join/" if base else "/join/"

    reason_text = reason.strip() if reason and reason.strip() else "No fue posible verificar la conexión con la comunidad o los requisitos de registro en este momento."

    subject = "Actualización sobre tu solicitud para unirte a MiniMexitas"
    from_email = _get_from_email()

    plain_text_content = f"""¡Hola {full_name}!

Gracias por tu interés en formar parte de la comunidad privada de MiniMexitas en el Área de la Bahía.

Lamentamos informarte que en esta ocasión tu solicitud no pudo ser aprobada por el equipo de organizadores.

Detalles de la revisión:
"{reason_text}"

Si consideras que hubo algún error, o si deseas postularte más adelante con datos actualizados o mayor información sobre tu referencia en la comunidad, eres bienvenido(a) a enviar una nueva solicitud en:
{join_url}

Agradecemos tu tiempo y te deseamos lo mejor.

Atentamente,
Equipo de Organizadores de MiniMexitas
Área de la Bahía, California
"""

    html_content = f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Actualización de Solicitud - MiniMexitas</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f8fafc; color: #1e293b; margin: 0; padding: 0; }}
    .container {{ max-width: 600px; margin: 20px auto; background-color: #ffffff; border-radius: 16px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05), 0 2px 4px -2px rgba(0,0,0,0.05); }}
    .header {{ background: linear-gradient(135deg, #1e293b 0%, #334155 100%); padding: 32px 24px; text-align: center; }}
    .header h1 {{ color: #ffffff; margin: 0; font-size: 24px; font-weight: 700; letter-spacing: -0.02em; }}
    .header p {{ color: #cbd5e1; margin: 6px 0 0; font-size: 14px; }}
    .content {{ padding: 32px 28px; line-height: 1.6; }}
    .greeting {{ font-size: 18px; font-weight: 600; color: #0f172a; margin-bottom: 16px; }}
    .intro {{ font-size: 15px; color: #334155; margin-bottom: 20px; }}
    .reason-box {{ background-color: #fef2f2; border: 1px solid #fecaca; border-radius: 12px; padding: 18px 20px; margin: 24px 0; }}
    .reason-title {{ font-size: 13px; font-weight: 700; color: #991b1b; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 8px; }}
    .reason-text {{ margin: 0; color: #7f1d1d; font-size: 14px; line-height: 1.5; font-style: italic; }}
    .info-box {{ background-color: #f8fafc; border-radius: 12px; padding: 20px 24px; margin: 24px 0; border: 1px solid #e2e8f0; font-size: 14px; color: #475569; }}
    .footer {{ background-color: #0f172a; color: #94a3b8; padding: 24px; text-align: center; font-size: 12px; line-height: 1.5; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>MiniMexitas</h1>
      <p>Comunidad del Área de la Bahía</p>
    </div>
    <div class="content">
      <div class="greeting">¡Hola {full_name}!</div>
      <p class="intro">
        Queremos agradecerte por tu interés en unirte a la comunidad privada de <strong>MiniMexitas</strong>.
      </p>
      <p class="intro">
        Lamentamos informarte que en esta ocasión los organizadores no pudieron aprobar tu solicitud de registro para el portal de la comunidad.
      </p>

      <div class="reason-box">
        <div class="reason-title">Motivo / Notas de la Revisión:</div>
        <p class="reason-text">"{reason_text}"</p>
      </div>

      <div class="info-box">
        <strong style="color: #0f172a;">¿Deseas postularte más adelante?</strong>
        <p style="margin: 8px 0 0 0;">
          Si consideras que hubo algún malentendido, o si deseas postularte nuevamente con información adicional sobre tu conexión con la comunidad o tu referencia, eres bienvenido(a) a enviar una nueva solicitud en cualquier momento.
        </p>
      </div>

      <p style="font-size: 14px; color: #64748b; margin-top: 24px;">
        Agradecemos mucho tu tiempo y comprensión.
      </p>
    </div>
    <div class="footer">
      <p style="margin: 0 0 4px 0;"><strong>MiniMexitas Community Portal</strong></p>
      <p style="margin: 0;">San Francisco Bay Area • California</p>
    </div>
  </div>
</body>
</html>
"""

    try:
        msg = EmailMultiAlternatives(
            subject=subject,
            body=plain_text_content,
            from_email=from_email,
            to=[recipient_email]
        )
        msg.attach_alternative(html_content, "text/html")
        msg.send(fail_silently=False)
        logger.info(f"Membership rejection email successfully sent to {recipient_email}")
        return True
    except Exception as e:
        logger.warning(f"Failed to send membership rejection email to {recipient_email}: {e}")
        return False


def send_admin_new_request_notification(request_obj, admin_emails=None, portal_url: str = None, request = None) -> bool:
    """
    Sends an automated notification email to all community admins/organizers when
    a new membership request is submitted, containing applicant details and a direct
    review button to the Organizer Dashboard.
    """
    if admin_emails is None:
        from .models import MemberProfile
        admin_emails = list(
            MemberProfile.objects.filter(is_admin=True)
            .exclude(email='')
            .values_list('email', flat=True)
        )

    # Clean list of admin emails
    valid_admins = list({e.strip().lower() for e in admin_emails if e and '@' in e})
    if not valid_admins:
        logger.info("No admin emails found to notify for new join request.")
        return False

    applicant_name = getattr(request_obj, 'full_name', '').strip() or "Nuevo Solicitante"
    applicant_email = getattr(request_obj, 'email', '').strip()
    applicant_phone = getattr(request_obj, 'phone_number', '').strip()
    
    get_region_display = getattr(request_obj, 'get_region_display', None)
    applicant_region = get_region_display() if callable(get_region_display) else getattr(request_obj, 'region', '')
    applicant_city = getattr(request_obj, 'city', '').strip()
    referral = getattr(request_obj, 'referral_source', '').strip() or "No especificado"

    # Determine absolute dashboard review URL
    base = _resolve_base_portal_url(portal_url, request)
    dashboard_url = f"{base}/organizers/" if base else "/organizers/"

    subject = f"🔔 Nueva solicitud para unirse a MiniMexitas: {applicant_name}"
    from_email = _get_from_email()

    plain_text_content = f"""¡Hola equipo de Organizadores!

Se ha recibido una nueva solicitud para unirse al portal privado de la comunidad MiniMexitas:

• Nombre: {applicant_name}
• Gmail: {applicant_email}
• WhatsApp / Teléfono: {applicant_phone or 'No proporcionado'}
• Zona / Región: {applicant_region} {f'({applicant_city})' if applicant_city else ''}
• ¿Cómo nos encontró / Referencia?:
"{referral}"

Para revisar, aprobar o declinar esta solicitud, ingresa al Panel de Organizadores:
{dashboard_url}

Atentamente,
MiniMexitas Portal Bot
"""

    html_content = f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Nueva Solicitud - MiniMexitas</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f8fafc; color: #1e293b; margin: 0; padding: 0; }}
    .container {{ max-width: 600px; margin: 20px auto; background-color: #ffffff; border-radius: 16px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05), 0 2px 4px -2px rgba(0,0,0,0.05); }}
    .header {{ background: linear-gradient(135deg, #0284c7 0%, #0369a1 100%); padding: 28px 24px; text-align: center; }}
    .header h1 {{ color: #ffffff; margin: 0; font-size: 22px; font-weight: 700; }}
    .header p {{ color: #e0f2fe; margin: 4px 0 0; font-size: 13px; }}
    .content {{ padding: 28px 24px; line-height: 1.6; }}
    .badge-applicant {{ display: inline-block; background-color: #e0f2fe; color: #0369a1; font-weight: 600; font-size: 12px; padding: 4px 10px; border-radius: 20px; margin-bottom: 12px; }}
    .info-card {{ background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 12px; padding: 18px 20px; margin: 16px 0; }}
    .info-row {{ margin-bottom: 8px; font-size: 14px; }}
    .info-label {{ font-weight: 600; color: #475569; display: inline-block; width: 140px; }}
    .info-value {{ color: #0f172a; font-weight: 500; }}
    .referral-box {{ background-color: #fffbeb; border: 1px solid #fef3c7; border-radius: 8px; padding: 12px 16px; margin-top: 12px; }}
    .referral-box p {{ margin: 0; font-size: 13px; color: #92400e; font-style: italic; }}
    .cta-container {{ text-align: center; margin: 24px 0 12px; }}
    .btn {{ display: inline-block; background-color: #0284c7; color: #ffffff !important; font-size: 15px; font-weight: 600; text-decoration: none; padding: 12px 28px; border-radius: 50px; box-shadow: 0 4px 12px rgba(2,132,199,0.3); }}
    .footer {{ background-color: #0f172a; color: #94a3b8; padding: 20px; text-align: center; font-size: 12px; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>Panel de Organizadores</h1>
      <p>Notificación de Nueva Solicitud de Membresía</p>
    </div>
    <div class="content">
      <span class="badge-applicant">🔔 Pendiente de Revisión</span>
      <h2 style="margin: 0 0 16px 0; font-size: 18px; color: #0f172a;">Solicitud de {applicant_name}</h2>
      
      <div class="info-card">
        <div class="info-row"><span class="info-label">👤 Nombre:</span> <span class="info-value">{applicant_name}</span></div>
        <div class="info-row"><span class="info-label">✉️ Gmail:</span> <span class="info-value">{applicant_email}</span></div>
        <div class="info-row"><span class="info-label">📱 WhatsApp:</span> <span class="info-value">{applicant_phone or 'No indicado'}</span></div>
        <div class="info-row"><span class="info-label">📍 Zona / Región:</span> <span class="info-value">{applicant_region} {f'({applicant_city})' if applicant_city else ''}</span></div>
        
        <div class="referral-box">
          <strong style="font-size: 12px; color: #78350f; text-transform: uppercase;">Referencia / Motivo:</strong>
          <p>"{referral}"</p>
        </div>
      </div>

      <div class="cta-container">
        <a href="{dashboard_url}" class="btn">Revisar en el Panel de Organizadores</a>
      </div>
    </div>
    <div class="footer">
      <p style="margin: 0;">MiniMexitas Community Management System</p>
    </div>
  </div>
</body>
</html>
"""

    try:
        msg = EmailMultiAlternatives(
            subject=subject,
            body=plain_text_content,
            from_email=from_email,
            to=valid_admins
        )
        msg.attach_alternative(html_content, "text/html")
        msg.send(fail_silently=False)
        logger.info(f"Admin new request notification sent to {len(valid_admins)} admins for applicant {applicant_email}")
        return True
    except Exception as e:
        logger.warning(f"Failed to send admin notification email to {valid_admins}: {e}")
        return False


from django.core import signing

RSVP_SALT = "minimexitas.event.rsvp.token.salt"


def generate_event_rsvp_token(event_id: str, email: str, status: str) -> str:
    """
    Generates a secure, signed token for 1-click email RSVP actions.
    """
    payload = {
        'event_id': str(event_id).strip(),
        'email': str(email).strip().lower(),
        'status': str(status).strip().lower(),
    }
    return signing.dumps(payload, salt=RSVP_SALT)


def verify_event_rsvp_token(token: str, max_age: int = 86400 * 60):
    """
    Verifies and unpacks a signed RSVP token.
    Returns payload dict or None if invalid/expired.
    """
    try:
        payload = signing.loads(token, salt=RSVP_SALT, max_age=max_age)
        return payload
    except Exception as e:
        logger.warning(f"Invalid or expired RSVP token: {e}")
        return None


def get_subscribed_members() -> list:
    """
    Returns a deduplicated list of {'email': email, 'full_name': full_name} for all active
    community members who have email notifications enabled.
    Auto-materializes local MemberProfile records for approved MembershipRequests if needed.
    """
    from .models import MemberProfile, MembershipRequest

    # Step 1: Ensure approved requests have a corresponding MemberProfile (in 1 bulk check)
    try:
        approved_requests = list(
            MembershipRequest.objects.filter(status='approved')
            .exclude(email__isnull=True)
            .exclude(email='')
        )
        if approved_requests:
            existing_emails = set(
                em.strip().lower() for em in 
                MemberProfile.objects.exclude(email='').values_list('email', flat=True)
            )
            to_create = []
            for req in approved_requests:
                clean_email = req.email.strip().lower()
                if clean_email and clean_email not in existing_emails:
                    to_create.append(MemberProfile(
                        email=clean_email,
                        full_name=req.full_name or '',
                        phone_number=req.phone_number or '',
                        region=req.region or '',
                        city=req.city or '',
                        email_notifications=True
                    ))
                    existing_emails.add(clean_email)
            if to_create:
                MemberProfile.objects.bulk_create(to_create, ignore_conflicts=True)
    except Exception as e:
        logger.warning(f"Error checking approved membership requests for notification recipients: {e}")

    # Step 2: Fetch all profiles with email_notifications=True
    members_qs = (
        MemberProfile.objects.exclude(email__isnull=True)
        .exclude(email='')
        .filter(email_notifications=True)
        .values('email', 'full_name')
    )

    seen = set()
    unique_members = []
    for m in members_qs:
        em = m.get('email', '').strip().lower()
        if em and '@' in em and em not in seen:
            seen.add(em)
            name = m.get('full_name', '').strip() or 'Miembro'
            unique_members.append({'email': em, 'full_name': name})

    return unique_members


def send_event_broadcast_email(event_data: dict, broadcast_by_name: str = None, broadcast_by_email: str = None, request = None) -> int:
    """
    Broadcasts a rich event announcement email to all active, verified community members.
    Includes full event details, Google Maps link, 1-click Add to Google Calendar button,
    and personalized 1-click RSVP action links (Going, Maybe, Decline).
    Returns total count of successfully dispatched emails.
    """
    members = get_subscribed_members()
    if not members:
        logger.info("No active members with email notifications enabled found for event broadcast.")
        return 0

    event_id = str(event_data.get('id', '')).strip()
    event_title = str(event_data.get('title', 'Evento de la Comunidad')).strip()
    event_date = str(event_data.get('date_formatted') or event_data.get('date', '')).strip()
    event_time = str(event_data.get('time_formatted') or event_data.get('time', '')).strip()
    event_location = str(event_data.get('location', 'San Francisco Bay Area')).strip()
    location_url = event_data.get('location_url') or event_data.get('locurl') or f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote_plus(event_location)}"
    event_category = str(event_data.get('category', 'Comunidad')).strip()
    event_description = str(event_data.get('description') or event_data.get('desc', '')).strip()
    gcal_link = event_data.get('google_calendar_link') or event_data.get('gcal', '')

    base_url = _resolve_base_portal_url(request=request)
    from_email = _get_from_email()
    subject = f"🎉 Nuevo Evento MiniMexitas: {event_title}"

    sent_count = 0
    connection = None
    try:
        connection = get_connection()
        connection.open()
    except Exception as e:
        logger.warning(f"Could not open persistent mail connection for event broadcast: {e}")
        connection = None

    try:
        for m in members:
            member_email = m.get('email', '').strip().lower()
            if not member_email or '@' not in member_email:
                continue
            member_name = m.get('full_name', '').strip() or "Miembro"

            # Generate unique 1-click RSVP action links for this member
            token_going = generate_event_rsvp_token(event_id, member_email, 'going')
            token_maybe = generate_event_rsvp_token(event_id, member_email, 'maybe')
            token_declined = generate_event_rsvp_token(event_id, member_email, 'declined')

            base_rsvp_endpoint = f"{base_url}/events/rsvp/" if base_url else "/events/rsvp/"
            rsvp_url_going = f"{base_rsvp_endpoint}?token={token_going}"
            rsvp_url_maybe = f"{base_rsvp_endpoint}?token={token_maybe}"
            rsvp_url_declined = f"{base_rsvp_endpoint}?token={token_declined}"
            portal_events_url = f"{base_url}/events/{event_id}/" if (base_url and event_id) else (f"{base_url}/events/" if base_url else "/events/")
            portal_profile_url = f"{base_url}/profile/" if base_url else "/profile/"

            plain_text_content = f"""¡Hola {member_name}!

Hay un nuevo evento programado para la comunidad de MiniMexitas en el Área de la Bahía:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🎉 {event_title}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📅 Fecha: {event_date}
⏰ Horario: {event_time}
📍 Ubicación: {event_location}
🗺️ Ver en Google Maps: {location_url}
🏷️ Categoría: {event_category}

Detalles del Evento:
{event_description if event_description else "Acompáñanos a convivir y compartir con la comunidad."}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🎟️ CONFIRMA TU ASISTENCIA (1-Click RSVP):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• ✅ ¡Sí, Asistiré! (Going): {rsvp_url_going}
• 🟡 Tal Vez (Maybe): {rsvp_url_maybe}
• ❌ No Podré (Declined): {rsvp_url_declined}

🗓️ Agregar a tu Google Calendar:
{gcal_link}

Ver todos los eventos y detalles en el portal:
{portal_events_url}

Para configurar tus preferencias de notificaciones por correo:
{portal_profile_url}

¡Esperamos verte pronto!
Equipo de MiniMexitas
"""

            html_content = f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{event_title} - MiniMexitas</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f1f5f9; color: #1e293b; margin: 0; padding: 0; -webkit-font-smoothing: antialiased; }}
    .container {{ max-width: 600px; margin: 24px auto; background-color: #ffffff; border-radius: 18px; overflow: hidden; box-shadow: 0 10px 25px -5px rgba(0,0,0,0.08), 0 8px 10px -6px rgba(0,0,0,0.05); }}
    .header {{ background: linear-gradient(135deg, #0f172a 0%, #1e293b 50%, #0369a1 100%); padding: 36px 28px; text-align: center; color: #ffffff; }}
    .header-badge {{ display: inline-block; background-color: rgba(255,255,255,0.18); border: 1px solid rgba(255,255,255,0.3); border-radius: 50px; padding: 6px 16px; font-size: 13px; font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase; margin-bottom: 12px; }}
    .header h1 {{ margin: 0; font-size: 26px; font-weight: 800; line-height: 1.25; }}
    .content {{ padding: 32px 28px; }}
    .greeting {{ font-size: 17px; font-weight: 600; color: #0f172a; margin-bottom: 16px; }}
    .event-card {{ background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 14px; padding: 22px; margin: 20px 0; }}
    .meta-row {{ display: flex; margin-bottom: 12px; font-size: 14px; align-items: flex-start; }}
    .meta-icon {{ font-size: 16px; margin-right: 10px; min-width: 22px; }}
    .meta-label {{ font-weight: 700; color: #334155; margin-right: 6px; }}
    .meta-val {{ color: #0f172a; }}
    .desc-box {{ margin-top: 16px; padding-top: 16px; border-top: 1px dashed #cbd5e1; font-size: 14px; line-height: 1.6; color: #475569; }}
    
    /* RSVP Box */
    .rsvp-section {{ background: linear-gradient(145deg, #f0fdf4 0%, #dcfce7 100%); border: 1px solid #bbf7d0; border-radius: 16px; padding: 24px 20px; margin: 28px 0; text-align: center; }}
    .rsvp-title {{ font-size: 16px; font-weight: 800; color: #14532d; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.03em; }}
    .rsvp-subtitle {{ font-size: 13px; color: #166534; margin-bottom: 18px; }}
    .rsvp-buttons {{ display: flex; flex-wrap: wrap; gap: 10px; justify-content: center; }}
    .btn-rsvp {{ display: inline-block; padding: 12px 18px; border-radius: 10px; font-size: 14px; font-weight: 700; text-decoration: none; text-align: center; transition: transform 0.1s ease; }}
    .btn-going {{ background-color: #16a34a; color: #ffffff !important; box-shadow: 0 4px 10px rgba(22,163,74,0.3); }}
    .btn-maybe {{ background-color: #f59e0b; color: #ffffff !important; box-shadow: 0 4px 10px rgba(245,158,11,0.3); }}
    .btn-declined {{ background-color: #64748b; color: #ffffff !important; }}

    /* Secondary Action */
    .secondary-actions {{ text-align: center; margin: 24px 0 10px; }}
    .btn-cal {{ display: inline-block; background-color: #0284c7; color: #ffffff !important; font-size: 14px; font-weight: 600; text-decoration: none; padding: 10px 22px; border-radius: 50px; margin: 0 6px 8px; }}
    .btn-portal {{ display: inline-block; background-color: #ffffff; color: #334155 !important; border: 1px solid #cbd5e1; font-size: 14px; font-weight: 600; text-decoration: none; padding: 10px 22px; border-radius: 50px; margin: 0 6px 8px; }}

    .footer {{ background-color: #0f172a; color: #94a3b8; padding: 24px; text-align: center; font-size: 12px; line-height: 1.5; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div class="header-badge">📅 {event_category}</div>
      <h1>{event_title}</h1>
    </div>
    
    <div class="content">
      <div class="greeting">¡Hola {member_name}!</div>
      <p style="font-size: 15px; line-height: 1.5; color: #334155; margin-top: 0;">
        Te invitamos a participar en el próximo evento organizado para los miembros de <strong>MiniMexitas</strong> en el Área de la Bahía.
      </p>

      <!-- Event Details Card -->
      <div class="event-card">
        <div class="meta-row">
          <span class="meta-icon">📅</span>
          <div><span class="meta-label">Fecha:</span> <span class="meta-val">{event_date}</span></div>
        </div>
        <div class="meta-row">
          <span class="meta-icon">⏰</span>
          <div><span class="meta-label">Horario:</span> <span class="meta-val">{event_time}</span></div>
        </div>
        <div class="meta-row">
          <span class="meta-icon">📍</span>
          <div>
            <span class="meta-label">Lugar:</span> 
            <span class="meta-val">{event_location}</span>
            <div style="margin-top: 4px;">
              <a href="{location_url}" target="_blank" style="color: #0284c7; text-decoration: none; font-size: 13px; font-weight: 600;">🗺️ Ver mapa en Google Maps &rarr;</a>
            </div>
          </div>
        </div>

        {f'<div class="desc-box"><strong>Detalles:</strong><br>{event_description}</div>' if event_description else ''}
      </div>

      <!-- 1-Click RSVP Box -->
      <div class="rsvp-section">
        <div class="rsvp-title">🎟️ ¿Nos acompañas? Confirma tu asistencia</div>
        <div class="rsvp-subtitle">Haz clic en una opción para responder directamente con 1 solo clic:</div>
        
        <table align="center" border="0" cellpadding="0" cellspacing="0" style="margin: 0 auto;">
          <tr>
            <td style="padding: 5px;">
              <a href="{rsvp_url_going}" class="btn-rsvp btn-going" style="display: block;">✅ ¡Sí Asistiré!</a>
            </td>
            <td style="padding: 5px;">
              <a href="{rsvp_url_maybe}" class="btn-rsvp btn-maybe" style="display: block;">🟡 Tal Vez</a>
            </td>
            <td style="padding: 5px;">
              <a href="{rsvp_url_declined}" class="btn-rsvp btn-declined" style="display: block;">❌ No Podré</a>
            </td>
          </tr>
        </table>
      </div>

      <!-- Calendar & Portal Actions -->
      <div class="secondary-actions">
        {f'<a href="{gcal_link}" target="_blank" class="btn-cal">🗓️ Agregar a Google Calendar</a>' if gcal_link else ''}
        <a href="{portal_events_url}" class="btn-portal">Ver Evento en el Portal</a>
      </div>

    </div>

    <div class="footer">
      <p style="margin: 0 0 4px 0;"><strong>MiniMexitas Community</strong> • San Francisco Bay Area</p>
      <p style="margin: 0;">Recibes esta notificación como miembro activo registrado de la comunidad.</p>
      <p style="margin: 6px 0 0 0; font-size: 11px; color: #64748b;">¿Prefieres no recibir estos correos? Puedes configurar tus <a href="{portal_profile_url}" style="color: #cbd5e1; text-decoration: underline;">preferencias de notificación</a> en tu perfil.</p>
    </div>
  </div>
</body>
</html>
"""

            try:
                msg = EmailMultiAlternatives(
                    subject=subject,
                    body=plain_text_content,
                    from_email=from_email,
                    to=[member_email],
                    connection=connection
                )
                msg.attach_alternative(html_content, "text/html")
                msg.send(fail_silently=False)
                sent_count += 1
            except Exception as e:
                logger.warning(f"Failed to dispatch event broadcast email to {member_email}: {e}")
    finally:
        if connection:
            try:
                connection.close()
            except Exception:
                pass

    logger.info(f"Event broadcast '{event_title}' sent to {sent_count}/{len(members)} members.")
    return sent_count


def send_survey_broadcast_email(survey_obj, broadcast_by_name: str = None, broadcast_by_email: str = None, request = None) -> int:
    """
    Broadcasts a rich survey announcement email to all active, verified community members
    who have email notifications enabled. Includes the survey question, category, description,
    voting options, and a direct link to cast their vote in the portal.
    Returns total count of successfully dispatched emails.
    """
    members = get_subscribed_members()
    if not members:
        logger.info("No active members with email notifications enabled found for survey broadcast.")
        return 0

    survey_title = getattr(survey_obj, 'title', 'Nueva Consulta Comunitaria').strip()
    survey_desc = getattr(survey_obj, 'description', '').strip()
    
    get_cat = getattr(survey_obj, 'get_category_display', None)
    survey_category = get_cat() if callable(get_cat) else getattr(survey_obj, 'category', 'General')
    
    is_multi = getattr(survey_obj, 'is_multiple_choice', False)
    mode_label = "Selección Múltiple (puedes elegir varias opciones)" if is_multi else "Opción Única (elige 1 opción)"

    # Get options list
    options = []
    if hasattr(survey_obj, 'options'):
        try:
            options = list(survey_obj.options.all().order_by('order', 'id'))
        except Exception:
            options = []

    base_url = _resolve_base_portal_url(request=request)
    survey_id = getattr(survey_obj, 'id', None)
    if survey_id:
        portal_surveys_url = f"{base_url}/surveys/{survey_id}/" if base_url else f"/surveys/{survey_id}/"
    else:
        portal_surveys_url = f"{base_url}/surveys/" if base_url else "/surveys/"
    portal_profile_url = f"{base_url}/profile/" if base_url else "/profile/"
    from_email = _get_from_email()
    subject = f"🗳️ Nueva Encuesta MiniMexitas: {survey_title}"

    options_text_list = "\n".join([f"  {idx + 1}. {opt.text}" for idx, opt in enumerate(options)]) if options else "  (Opciones disponibles en el portal)"
    options_html_items = "".join([
        f'<li style="margin-bottom: 8px; font-size: 14px; color: #334155; line-height: 1.4;"><strong>{idx + 1}.</strong> {opt.text}</li>'
        for idx, opt in enumerate(options)
    ]) if options else '<li style="font-size: 14px; color: #64748b;">Opciones disponibles en el portal</li>'

    sent_count = 0
    connection = None
    try:
        connection = get_connection()
        connection.open()
    except Exception as e:
        logger.warning(f"Could not open persistent mail connection for survey broadcast: {e}")
        connection = None

    try:
        for m in members:
            member_email = m.get('email', '').strip().lower()
            if not member_email or '@' not in member_email:
                continue
            member_name = m.get('full_name', '').strip() or "Miembro"

            plain_text_content = f"""¡Hola {member_name}!

Hay una nueva consulta comunitaria abierta en el portal de MiniMexitas:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🗳️ {survey_title}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🏷️ Categoría: {survey_category}
⚙️ Modalidad: {mode_label}
{f"📝 Contexto: {survey_desc}" if survey_desc else ""}

Opciones de Votación:
{options_text_list}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
👉 ENTRA A VOTAR EN EL PORTAL:
{portal_surveys_url}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Tu voto es muy valioso para organizar nuestras próximas actividades y convivencias comunitarias.

Para configurar tus preferencias de notificaciones por correo:
{portal_profile_url}

¡Gracias por participar!
Equipo de MiniMexitas
"""

            html_content = f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{survey_title} - MiniMexitas</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f1f5f9; color: #1e293b; margin: 0; padding: 0; -webkit-font-smoothing: antialiased; }}
    .wrapper {{ max-width: 600px; margin: 24px auto; background-color: #ffffff; border-radius: 18px; overflow: hidden; box-shadow: 0 10px 25px -5px rgba(0,0,0,0.08), 0 8px 10px -6px rgba(0,0,0,0.05); }}
    .header {{ background: linear-gradient(135deg, #1e3a8a 0%, #0d9488 100%); padding: 36px 28px; text-align: center; color: #ffffff; }}
    .header .tag {{ display: inline-block; background: rgba(255,255,255,0.18); border: 1px solid rgba(255,255,255,0.3); border-radius: 50px; padding: 6px 16px; font-size: 13px; font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase; margin-bottom: 12px; }}
    .header h1 {{ margin: 0; font-size: 24px; font-weight: 800; line-height: 1.3; }}
    .content {{ padding: 32px 28px; }}
    .greeting {{ font-size: 17px; font-weight: 600; color: #0f172a; margin-bottom: 14px; }}
    .desc-box {{ background-color: #f8fafc; border-left: 4px solid #0d9488; border-radius: 8px; padding: 14px 18px; margin: 18px 0 22px 0; font-size: 14px; color: #334155; line-height: 1.6; }}
    .options-card {{ background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 14px; padding: 20px 22px; margin-bottom: 26px; }}
    .options-title {{ font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; color: #64748b; margin: 0 0 14px 0; }}
    .options-list {{ list-style-type: none; padding: 0; margin: 0; }}
    .cta-container {{ text-align: center; margin: 28px 0 14px 0; }}
    .btn-vote {{ display: inline-block; background: linear-gradient(135deg, #0d9488 0%, #047857 100%); color: #ffffff !important; font-size: 15px; font-weight: 700; text-decoration: none; padding: 14px 36px; border-radius: 50px; box-shadow: 0 4px 14px rgba(13,148,136,0.35); }}
    .footer {{ background-color: #0f172a; color: #94a3b8; padding: 24px; text-align: center; font-size: 12px; line-height: 1.5; }}
  </style>
</head>
<body>
  <div class="wrapper">
    <div class="header">
      <div class="tag">🗳️ Consulta Comunitaria</div>
      <h1>{survey_title}</h1>
      <div style="margin-top: 8px; font-size: 13px; color: #e2e8f0;">🏷️ {survey_category} &bull; {mode_label}</div>
    </div>

    <div class="content">
      <div class="greeting">¡Hola {member_name}!</div>
      <p style="font-size: 15px; line-height: 1.5; color: #334155; margin-top: 0;">
        Se ha publicado una nueva encuesta para la comunidad de <strong>MiniMexitas</strong>. Tu opinión es fundamental para tomar las mejores decisiones juntos.
      </p>

      {f'<div class="desc-box"><strong>Contexto:</strong><br>{survey_desc}</div>' if survey_desc else ''}

      <div class="options-card">
        <div class="options-title">Opciones a votar:</div>
        <ul class="options-list">
          {options_html_items}
        </ul>
      </div>

      <div class="cta-container">
        <a href="{portal_surveys_url}" class="btn-vote">
          🗳️ Participar y Votar en el Portal
        </a>
      </div>
    </div>

    <div class="footer">
      <p style="margin: 0 0 4px 0;"><strong>MiniMexitas Community</strong> • San Francisco Bay Area</p>
      <p style="margin: 0;">Recibes esta notificación como miembro activo registrado de la comunidad.</p>
      <p style="margin: 6px 0 0 0; font-size: 11px; color: #64748b;">¿Prefieres no recibir estos correos? Puedes configurar tus <a href="{portal_profile_url}" style="color: #cbd5e1; text-decoration: underline;">preferencias de notificación</a> en tu perfil.</p>
    </div>
  </div>
</body>
</html>
"""

            try:
                msg = EmailMultiAlternatives(
                    subject=subject,
                    body=plain_text_content,
                    from_email=from_email,
                    to=[member_email],
                    connection=connection
                )
                msg.attach_alternative(html_content, "text/html")
                msg.send(fail_silently=False)
                sent_count += 1
            except Exception as e:
                logger.warning(f"Failed to dispatch survey broadcast email to {member_email}: {e}")
    finally:
        if connection:
            try:
                connection.close()
            except Exception:
                pass

    logger.info(f"Survey broadcast '{survey_title}' sent to {sent_count}/{len(members)} members.")
    return sent_count




