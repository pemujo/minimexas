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
    url = portal_url or getattr(settings, 'PORTAL_BASE_URL', 'https://pemujo.pythonanywhere.com')
    if not url.endswith('/login/'):
        url = url.rstrip('/') + '/login/'

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

    # Determine portal login URL
    if not portal_url:
        if request:
            portal_url = request.build_absolute_uri(reverse('login_page'))
        else:
            base = getattr(settings, 'PORTAL_BASE_URL', 'https://pemujo.pythonanywhere.com').rstrip('/')
            portal_url = f"{base}/login/"

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


def send_membership_rejection_email(request_obj, reason: str = None) -> bool:
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

    reason_text = reason.strip() if reason and reason.strip() else "No fue posible verificar la conexión con la comunidad o los requisitos de registro en este momento."

    subject = "Actualización sobre tu solicitud para unirte a MiniMexitas"
    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'MiniMexitas Community <no-reply@minimexitas.org>')

    plain_text_content = f"""¡Hola {full_name}!

Gracias por tu interés en formar parte de la comunidad privada de MiniMexitas en el Área de la Bahía.

Lamentamos informarte que en esta ocasión tu solicitud no pudo ser aprobada por el equipo de organizadores.

Detalles de la revisión:
"{reason_text}"

Si consideras que hubo algún error, o si deseas postularte más adelante con datos actualizados o mayor información sobre tu referencia en la comunidad, eres bienvenido(a) a enviar una nueva solicitud en:
https://pemujo.pythonanywhere.com/join/

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

