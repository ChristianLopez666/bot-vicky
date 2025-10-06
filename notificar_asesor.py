
import os
import requests
import logging

# Variables de entorno necesarias
META_TOKEN = os.getenv("META_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
NOTIFICAR_ASESOR = os.getenv("NOTIFICAR_ASESOR")

def notificar_asesor(mensaje):
    try:
        url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
        headers = {
            "Authorization": f"Bearer {META_TOKEN}",
            "Content-Type": "application/json"
        }
        data = {
            "messaging_product": "whatsapp",
            "to": NOTIFICAR_ASESOR,
            "type": "text",
            "text": {
                "body": mensaje
            }
        }

        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        logging.info(f"✅ Notificación enviada al asesor: {mensaje}")
        return True

    except Exception as e:
        logging.error(f"❌ Error al notificar al asesor: {e}")
        return False
