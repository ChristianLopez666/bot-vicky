# Despliegue y restauración

**Nada de esto se ha ejecutado.** Cada paso que toca Render o Meta requiere autorización.

## 0. Antes de tocar nada (respaldo)

1. En Render, abrir el servicio del bot y anotar: commit desplegado, rama, Auto-Deploy, **Root Directory**, **Build Command**, **Start Command**, versión de Python, plan, y plan y persistencia del Key Value.
2. Guardar las variables de entorno en un gestor de contraseñas (nunca en el repositorio ni en un chat).
3. Código: existe el paquete `respaldo-bot-vicky-financiera-dc6f64d.bundle` en Descargas (SHA-256 `678e11da3d95120a01a9ea289d7e1d7beceb183d82432ff0ed9efdee171acfa1`). Conviene copiarlo también fuera de este equipo. Si Render corre un commit distinto de `dc6f64d`, hay que respaldar ese.

## 1. Requisitos bloqueantes

- Decidir qué pasa con lo que se apaga: `/ext/lead` (según el código, recibe un formulario de cohifis.com), `/ext/flow/imss` (Flow de IMSS), `/ext/boardroom/instruct`, la conexión con Radar, el puente de observaciones de Rodys, las conversaciones financieras abiertas y los anuncios de Meta que apunten a este número.
- Redis/Valkey **con persistencia** y política `noeviction`. El plan gratuito de Render pierde los datos al reiniciar.
- Instancia que no se suspenda por inactividad.
- Plantilla de utilidad aprobada, con un parámetro `{{1}}`, para el aviso al asesor.
- Pestaña `Prospectos GastroRadar` en una hoja compartida con la cuenta de servicio (permiso de edición).
- Aviso de privacidad actualizado: hoy no menciona el bot, Google Sheets ni OpenAI, y dice que los datos no se comparten con terceros.
- `ADVISOR_NUMBER` distinto de `BOT_WHATSAPP_NUMBER`.

## 2. Ensayo (sin tocar el número real)

Crear un servicio de ensayo desde la rama, con Root Directory `gastroradar`, Redis propio y un **número de prueba de Meta**.

- Build: `pip install -r requirements.txt`
- Start: `gunicorn app:app --workers 1 --threads 8 --timeout 60`
- `PYTHON_VERSION`: 3.13 (el mismo de la verificación en GitHub, cuando haya corrido).

**No cambiar la URL de webhook de la app de Meta que usa producción**: la cuenta de WhatsApp se comparte con SECOM. Revisar primero qué apps y URL reciben cada número.

Comprobar: `/ready` en 200; diagnóstico con token válido y número coincidente; consulta, registro, planes, banners, terminal, asesor, BAJA; aviso con estado `delivered`; fila en la hoja; reiniciar el servicio a mitad de una conversación; un mensaje después de 24 horas.

## 3. Corte

1. En un fin de semana, con alguien observando.
2. Desactivar Auto-Deploy.
3. Cambiar Root Directory, Build y Start; agregar las variables de `.env.example`. Conservar las anteriores.
4. Desplegar el commit aprobado y revisar `/ready` y el diagnóstico.
5. Enviar desde un número que no sea el del asesor: "hola", "planes", "asesor". Confirmar entrega real.

## 4. Restauración

1. Poner Root Directory, Build y Start **exactamente** como se anotaron en el paso 0. En GitHub el arranque figura como `gunicorn filtered_app:app --bind 0.0.0.0:$PORT --max-requests 200 --max-requests-jitter 50 --timeout 60`, pero manda lo que diga el panel.
2. Desplegar el commit anotado. No dar por hecho que un "Rollback" de Render revierte también la configuración: revisarla.
3. Verificar `/health` y una conversación real.
4. Las claves nuevas usan el prefijo `vgr:` y las anteriores `vicky:`, así que no chocan.
5. Antes de volver, exportar prospectos con `/admin/prospectos` y revisar quién pidió BAJA: el bot anterior no conoce esas bajas.

Si se pierde GitHub: `git clone respaldo-bot-vicky-financiera-dc6f64d.bundle -b main bot-vicky-restaurado`.

**Comprobado:** el paquete clona en `dc6f64d` y su suite da el mismo resultado que GitHub (567 pasan, 19 fallan). **No comprobado:** la restauración en Render.
