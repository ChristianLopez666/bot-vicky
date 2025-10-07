from flask import Flask, request, jsonify
import requests
import os
import logging

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class SimpleIMSSBot:
    def __init__(self):
        self.user_sessions = {}
        self.whatsapp_token = os.getenv('WHATSAPP_TOKEN')
        self.whatsapp_phone_id = os.getenv('WHATSAPP_PHONE_ID')

    def process_message(self, user_id, user_message):
        logger.info(f"🔍 MENSAJE RECIBIDO - User: {user_id}, Text: '{user_message}'")
        
        # Respuesta FIJA para probar si el webhook funciona
        response = "✅ BOT IMSS FUNCIONANDO - Mensaje recibido: " + user_message
        
        logger.info(f"📤 ENVIANDO RESPUESTA: {response}")
        return response

    def send_whatsapp_message(self, number, message):
        try:
            url = f"https://graph.facebook.com/v17.0/{self.whatsapp_phone_id}/messages"
            headers = {
                "Authorization": f"Bearer {self.whatsapp_token}",
                "Content-Type": "application/json"
            }
            payload = {
                "messaging_product": "whatsapp",
                "to": number,
                "text": {"body": message}
            }
            response = requests.post(url, json=payload, headers=headers)
            logger.info(f"📱 WhatsApp API Status: {response.status_code}")
            return response.status_code == 200
        except Exception as e:
            logger.error(f"❌ Error WhatsApp: {e}")
            return False

bot = SimpleIMSSBot()

@app.route('/')
def home():
    return "✅ Simple IMSS Bot Running"

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    verify_token = os.getenv('VERIFY_TOKEN')
    
    logger.info(f"🔐 Webhook verification - Mode: {mode}")
    
    if mode == "subscribe" and token == verify_token:
        return challenge
    return "Verification failed", 403

@app.route("/webhook", methods=["POST"])
def handle_webhook():
    try:
        data = request.get_json()
        logger.info(f"🔔 Webhook data received")
        
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                if "messages" in value:
                    for msg in value["messages"]:
                        phone = msg["from"]
                        text = msg.get("text", {}).get("body", "").strip()
                        
                        logger.info(f"📨 RAW MESSAGE from {phone}: '{text}'")
                        
                        # Procesar mensaje
                        response = bot.process_message(phone, text)
                        
                        # Enviar respuesta
                        bot.send_whatsapp_message(phone, response)
        
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error(f"❌ Webhook error: {e}")
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    logger.info("🚀 Starting Simple IMSS Bot...")
    app.run(host="0.0.0.0", port=5000, debug=False)
