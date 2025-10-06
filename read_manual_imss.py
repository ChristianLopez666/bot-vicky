
import os
import fitz  # PyMuPDF
import openai
import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io
import logging

# Cargar variables necesarias
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MANUAL_NAME = "Procedimiento IMSS febrero 2025 (3).pdf"

openai.api_key = OPENAI_API_KEY

# Función para descargar PDF desde Drive
def descargar_manual_desde_drive(nombre_archivo=MANUAL_NAME):
    try:
        creds = Credentials.from_service_account_info(eval(GOOGLE_CREDENTIALS_JSON))
        drive_service = build('drive', 'v3', credentials=creds)

        results = drive_service.files().list(
            q=f"name='{nombre_archivo}' and mimeType='application/pdf'",
            fields="files(id, name)",
            spaces='drive'
        ).execute()

        items = results.get('files', [])
        if not items:
            logging.warning("📂 Manual no encontrado en Drive.")
            return None

        file_id = items[0]['id']
        request = drive_service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)

        done = False
        while not done:
            status, done = downloader.next_chunk()

        local_path = f"/tmp/{nombre_archivo}"
        with open(local_path, 'wb') as f:
            f.write(fh.getbuffer())

        logging.info(f"📥 Manual descargado: {local_path}")
        return local_path

    except Exception as e:
        logging.error(f"❌ Error descargando manual desde Drive: {e}")
        return None

# Función para extraer texto del PDF
def extraer_texto_pdf(pdf_path):
    try:
        with fitz.open(pdf_path) as doc:
            texto = ""
            for page in doc:
                texto += page.get_text()
        return texto
    except Exception as e:
        logging.error(f"❌ Error extrayendo texto del PDF: {e}")
        return ""

# Función final para responder preguntas con base en el manual
def responder_con_manual(pregunta_usuario):
    try:
        pdf_path = descargar_manual_desde_drive()
        if not pdf_path:
            return "No pude acceder al manual en este momento. Intenta más tarde."

        contexto = extraer_texto_pdf(pdf_path)
        prompt = f"""
        Eres un asistente experto en préstamos IMSS Ley 73. Responde únicamente con base en el siguiente manual oficial:

        {contexto}

        Pregunta del usuario: {pregunta_usuario}
        """

        response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": "Responde como asesor IMSS solo usando el manual proporcionado."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            max_tokens=1000
        )

        return response.choices[0].message.content.strip()

    except Exception as e:
        logging.error(f"❌ Error al generar respuesta con GPT: {e}")
        return "Hubo un error procesando tu pregunta. Intenta más tarde."
