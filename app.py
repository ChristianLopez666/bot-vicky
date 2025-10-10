def handle_imss_flow(phone_number, user_message):
    """Gestiona el flujo completo del préstamo IMSS Ley 73."""
    msg = user_message.lower()

    imss_keywords = ["préstamo", "prestamo", "imss", "pensión", "pension", "ley 73", "1"]

    # Paso 1: activación inicial por palabras clave
    if any(keyword in msg for keyword in imss_keywords):
        current_state = user_state.get(phone_number)
        if current_state not in [
            "esperando_respuesta_imss",
            "esperando_monto_solicitado",
            "esperando_respuesta_nomina",
            "esperando_nombre_imss",
            "esperando_telefono_imss",
            "esperando_ciudad_imss"
        ]:
            send_message(phone_number,
                "👋 ¡Hola! Antes de continuar, necesito confirmar algo importante.\n\n"
                "¿Eres pensionado o jubilado del IMSS bajo la Ley 73? (Responde *sí* o *no*)"
            )
            user_state[phone_number] = "esperando_respuesta_imss"
        return True

    # Paso 2: validación de respuesta IMSS
    if user_state.get(phone_number) == "esperando_respuesta_imss":
        intent = interpret_response(msg)
        if intent == 'negative':
            send_message(phone_number,
                "Entiendo. Para el préstamo IMSS Ley 73 es necesario ser pensionado del IMSS. 😔\n\n"
                "Pero tengo otros servicios que pueden interesarte:"
            )
            send_main_menu(phone_number)
            user_state.pop(phone_number, None)
        elif intent == 'positive':
            send_message(phone_number,
                "Excelente 👏\n\n¿Qué monto de préstamo deseas solicitar? (puedes indicar cualquier cantidad, ejemplo: 65000)"
            )
            user_state[phone_number] = "esperando_monto_solicitado"
        else:
            send_message(phone_number, "Por favor responde *sí* o *no* para continuar.")
        return True

    # Paso 3: monto solicitado - ELIMINAR VALIDACIONES
    if user_state.get(phone_number) == "esperando_monto_solicitado":
        if is_thankyou_message(msg):
            send_message(phone_number,
                "¡Por nada! 😊\n\n"
                "Sigamos con tu solicitud...\n\n"
                "¿Qué monto deseas solicitar? (puedes indicar cualquier cantidad, ejemplo: 65000)"
            )
            return True

        monto = extract_number(msg)
        if monto is not None:
            # ✅ ACEPTAR CUALQUIER MONTO SIN VALIDACIONES
            user_data[phone_number] = {"monto_solicitado": monto}

            send_message(phone_number,
                f"🎉 *¡FELICIDADES!* Tu monto solicitado ha sido registrado: ${monto:,.0f}\n\n"
                "🌟 *BENEFICIOS DE TU PRÉSTAMO:*\n"
                "• Sin aval\n• Sin revisión en Buró\n"
                "• Descuento directo de tu pensión\n"
                "• Tasa preferencial"
            )

            send_message(phone_number,
                "💳 *PARA ACCEDER A BENEFICIOS ADICIONALES EXCLUSIVOS*:\n\n"
                "¿Tienes tu pensión depositada en Inbursa o estarías dispuesto a cambiarla?\n\n"
                "🌟 *BENEFICIOS ADICIONALES CON NÓMINA INBURSA:*\n"
                "• Rendimientos del 80% de Cetes\n"
                "• Devolución del 20% de intereses por pago puntual\n"
                "• Anticipo de nómina hasta el 50%\n"
                "• Seguro de vida y Medicall Home (telemedicina 24/7)\n"
                "• Descuentos en Sanborns y 6,000 comercios\n"
                "• Retiros sin comisión en +28,000 puntos\n\n"
                "💡 *No necesitas cancelar tu cuenta actual*\n"
                "👉 ¿Aceptas cambiar tu nómina a Inbursa? (sí/no)"
            )
            user_state[phone_number] = "esperando_respuesta_nomina"
        else:
            send_message(phone_number, "Por favor indica el monto deseado, ejemplo: 65000")
        return True

    # Paso 4: validación nómina - AGREGAR NUEVOS PASOS DESPUÉS
    if user_state.get(phone_number) == "esperando_respuesta_nomina":
        if is_thankyou_message(msg):
            send_message(phone_number,
                "¡De nada! 😊\n\n"
                "Para continuar, por favor responde *sí* o *no*:\n\n"
                "¿Aceptas cambiar tu nómina a Inbursa para acceder a beneficios adicionales?"
            )
            return True

        intent = interpret_response(msg)
        data = user_data.get(phone_number, {})
        monto_solicitado = data.get('monto_solicitado', 'N/D')

        # Siempre continuar al siguiente paso (nombre)
        user_data[phone_number]["nomina_inbursa"] = "ACEPTADA" if intent == "positive" else "NO POR AHORA"
        send_message(phone_number, "👤 ¿Cuál es tu nombre completo?")
        user_state[phone_number] = "esperando_nombre_imss"
        return True

    # Paso 5: Captura nombre completo
    if user_state.get(phone_number) == "esperando_nombre_imss":
        if is_valid_name(user_message):
            user_data[phone_number]["nombre_contacto"] = user_message.title()
            send_message(phone_number,
                f"✅ Nombre registrado: {user_message.title()}\n\n"
                "📞 ¿En qué número telefónico podemos contactarte?\n\n"
                "💡 Puedes proporcionar el mismo número de WhatsApp o uno diferente"
            )
            user_state[phone_number] = "esperando_telefono_imss"
        else:
            send_message(phone_number,
                "Por favor ingresa un nombre válido (solo letras y espacios):\n\n"
                "Ejemplo: Juan Pérez García"
            )
        return True

    # Paso 6: Captura teléfono de contacto
    if user_state.get(phone_number) == "esperando_telefono_imss":
        if is_valid_phone(user_message):
            user_data[phone_number]["telefono_contacto"] = user_message
            send_message(phone_number,
                f"✅ Teléfono registrado: {user_message}\n\n"
                "🏙️ ¿En qué ciudad vives?"
            )
            user_state[phone_number] = "esperando_ciudad_imss"
        else:
            send_message(phone_number,
                "Por favor ingresa un número de teléfono válido (10 dígitos mínimo):\n\n"
                "Ejemplo: 6681234567 o +526681234567"
            )
        return True

    # Paso 7: Captura ciudad
    if user_state.get(phone_number) == "esperando_ciudad_imss":
        user_data[phone_number]["ciudad"] = user_message.title()
        data = user_data.get(phone_number, {})
        monto_solicitado = data.get('monto_solicitado', 'N/D')
        nombre_contacto = data.get("nombre_contacto", "N/D")
        telefono_contacto = data.get("telefono_contacto", phone_number)
        ciudad = data.get("ciudad", "N/D")
        nomina_inbursa = data.get("nomina_inbursa", "N/D")

        send_message(phone_number,
            f"🎉 *¡Excelente!* Hemos registrado tu solicitud de préstamo IMSS Ley 73.\n\n"
            "📞 *Un asesor te contactará* para:\n"
            "• Confirmar los detalles de tu préstamo\n"
            "• Explicarte el proceso de desembolso\n"
            "• Orientarte sobre los beneficios\n\n"
            "¡Gracias por confiar en Inbursa! 🏦"
        )

        mensaje_asesor = (
            f"🔥 *NUEVO PROSPECTO IMSS LEY 73 - INFORMACIÓN COMPLETA*\n\n"
            f"👤 Nombre: {nombre_contacto}\n"
            f"📞 Teléfono WhatsApp: {phone_number}\n"
            f"📱 Teléfono contacto: {telefono_contacto}\n"
            f"🏙️ Ciudad: {ciudad}\n"
            f"💵 Monto solicitado: ${monto_solicitado:,.0f}\n"
            f"🏦 Nómina Inbursa: {nomina_inbursa}\n\n"
            f"🎯 *Cliente potencial para préstamo IMSS Ley 73*"
        )
        send_message(ADVISOR_NUMBER, mensaje_asesor)

        user_state.pop(phone_number, None)
        user_data.pop(phone_number, None)
        return True

    return False
