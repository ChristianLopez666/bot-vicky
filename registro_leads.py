
import os
import json
import logging
from datetime import datetime
import pytz
import gspread
from google.oauth2.service_account import Credentials

# Zona horaria (puedes ajustar si es necesario)
tz = pytz.timezone('America/Mazatlan')

# Configurar acceso con credenciales JSON
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")

def registrar_lead(whatsapp, nombre="", campaña="", producto="", monto="", solicita_contacto=""):
    try:
        creds = Credentials.from_service_account_info(json.loads(GOOGLE_CREDENTIALS_JSON))
        client = gspread.authorize(creds)
        sheet = client.open(SHEET_NAME).sheet1

        fecha = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")

        row = [fecha, whatsapp, nombre, campaña, producto, monto, solicita_contacto]
        sheet.append_row(row)

        logging.info(f"✅ Lead registrado en Google Sheets: {row}")
        return True

    except Exception as e:
        logging.error(f"❌ Error al registrar lead en Sheets: {e}")
        return False
