import threading

@app.route("/webhook", methods=["POST"])
def handle_webhook():
    try:
        data = request.get_json()
        logger.info(f"Webhook received: {data}")
        
        # Verificar que es un mensaje de WhatsApp
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                if "messages" in value:
                    for msg in value["messages"]:
                        # Procesar el mensaje en un hilo separado
                        threading.Thread(target=process_message, args=(msg,)).start()
        
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"status": "error"}), 500

def process_message(msg):
    phone = msg["from"]
    text = msg.get("text", {}).get("body", "").strip()
    
    logger.info(f"Processing message from {phone}: '{text}'")
    
    # Aquí va la lógica de tu bot
    if text.lower() == 'menu':
        vicky.user_sessions[phone] = {
            'campaign': 'general',
            'state': 'menu',
            'data': {},
            'timestamp': datetime.now()
        }
        response = "🏦 INBURSA\n1. Préstamos IMSS\n2. Créditos empresariales\nEscribe el número de tu opción:"
    elif phone not in vicky.user_sessions:
        response = vicky.start_conversation(phone, text)
    else:
        session = vicky.user_sessions[phone]
        if session['campaign'] == 'imss':
            response = vicky.handle_imss_flow(phone, text)
        elif session['campaign'] == 'business':
            response = vicky.handle_business_flow(phone, text)
        else:
            response = vicky.handle_general_flow(phone, text)
    
    logger.info(f"Sending response to {phone}: {response}")
    vicky.send_whatsapp_message(phone, response)
