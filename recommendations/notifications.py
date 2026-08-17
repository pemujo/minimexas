import logging
import urllib.parse
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
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


def build_whatsapp_approval_link(full_name: str, phone_number: str, portal_url: str = None) -> str:
    """Build a pre-filled WhatsApp message URL to welcome an approved applicant."""
    clean_phone = clean_phone_for_whatsapp(phone_number)
    if not clean_phone:
        return ""
    
    name = full_name.strip() if full_name else "amig@"
    base = (portal_url or getattr(settings, 'PORTAL_BASE_URL', '')).rstrip('/')
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

    # Determine portal login URL from PORTAL_BASE_URL in .env
    base = (portal_url or getattr(settings, 'PORTAL_BASE_URL', '')).rstrip('/')
    portal_url = f"{base}/login/" if base else "/login/"

    subject = "¡Bienvenido(a) a MiniMexitas! Tu solicitud ha sido aprobada 🎉"
    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'MiniMexitas Community <no-reply@minimexitas.org>')

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


def send_membership_rejection_email(request_obj, reason: str = None, portal_url: str = None) -> bool:
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

    # Determine portal join URL from PORTAL_BASE_URL in .env
    base = (portal_url or getattr(settings, 'PORTAL_BASE_URL', '')).rstrip('/')
    join_url = f"{base}/join/" if base else "/join/"

    reason_text = reason.strip() if reason and reason.strip() else "No fue posible verificar la conexión con la comunidad o los requisitos de registro en este momento."

    subject = "Actualización sobre tu solicitud para unirte a MiniMexitas"
    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'MiniMexitas Community <no-reply@minimexitas.org>')

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


def send_admin_new_request_notification(request_obj, admin_emails=None, portal_url: str = None) -> bool:
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

    base = (portal_url or getattr(settings, 'PORTAL_BASE_URL', '')).rstrip('/')
    dashboard_url = f"{base}/organizers/" if base else "/organizers/"

    subject = f"🔔 Nueva solicitud para unirse a MiniMexitas: {applicant_name}"
    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'MiniMexitas Community <no-reply@minimexitas.org>')

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


