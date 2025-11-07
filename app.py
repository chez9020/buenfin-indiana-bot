# app.py — Chatbot Buen Fin Indiana 2025
from flask import Flask, request, jsonify, send_from_directory, render_template, redirect
from heyoo import WhatsApp
import redis, json, os, sys, time
from datetime import datetime
from dotenv import load_dotenv
from werkzeug.middleware.proxy_fix import ProxyFix
from ticket_validator import validar_ticket_desde_media
from sheets_logger import registrar_ticket_en_sheets
from sheets_utils import open_worksheet, parse_money
from control_inventario import obtener_premio_disponible, obtener_premio_especial
from vendedores import VENDEDORES
import uuid
import base64

# ------------------ Config básica ------------------
load_dotenv()
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

app = Flask(__name__, template_folder="templates")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Entorno / tokens
token_facebook       = os.getenv("WHATSAPP_TOKEN")
id_numero            = os.getenv("WHATSAPP_NUMBER_ID")
WEBHOOK_VERIFY_TOKEN = os.getenv("WEBHOOK_VERIFY_TOKEN")
URL_SERVER           = os.getenv("URL_SERVER")

# Ajustes Dashboard
AUTO_SYNC_ON_DASHBOARD = os.getenv("AUTO_SYNC_ON_DASHBOARD", "1") == "1"
AUTO_SYNC_MAX_AGE_S    = int(os.getenv("AUTO_SYNC_MAX_AGE_S", "3600"))  # 1h por defecto
r = redis.Redis(host='localhost', port=6379, decode_responses=True)
# WhatsApp
wa = WhatsApp(token_facebook, id_numero)

def dbg(*args):
    print(*args, file=sys.stdout, flush=True)

@app.route("/qr")
def qr_redirect():
    vendedor_id = request.args.get("vendedor")
    if not vendedor_id:
        return "❌ Falta el parámetro vendedor", 400

    # Registrar escaneo (opcional)
    r.incr(f"vendedor:{vendedor_id}:scans")
    r.expire(f"vendedor:{vendedor_id}:scans", 86400)

    vendedor_nombre = VENDEDORES.get(vendedor_id, "Sin vendedor")

    telefono_bot = "5217206266927"

    mensaje = (
        f"Hola, quiero participar con {vendedor_nombre} con codigo {vendedor_id}"
    )

    wa_link = f"https://wa.me/{telefono_bot}?text={mensaje}"

    print(f"🔗 QR generado → {wa_link}")
    return redirect(wa_link)

def wsend(to, text):
    try:
        resp = wa.send_message(text, to)
        dbg("Graph API send_message resp:", resp)
        return resp
    except Exception as e:
        dbg("❌ Error send_message:", e)
        return None

# ------------------ Sesiones ------------------
def cargar_sesion(telefono):
    datos = r.get(f"chatbot:{telefono}")
    return json.loads(datos) if datos else None

def guardar_sesion(telefono, datos):
    r.set(f"chatbot:{telefono}", json.dumps(datos), ex=86400)

def eliminar_sesion(telefono):
    r.delete(f"chatbot:{telefono}")

# ------------------ Helpers Sheets / Inventario ------------------
def contar_tiendas():
    """
    Lee el Sheet y devuelve (conteos_por_tienda: dict[str,int], total_registros: int).
    Usa la columna 'Tienda'. Ignora vacíos.
    """
    try:
        ws = open_worksheet()
        rows = ws.get_all_values() or []
        if not rows:
            return {}, 0

        headers = [h.strip().lower() for h in rows[0]]
        idx_tienda = None
        for i, h in enumerate(headers):
            if h == "tienda":
                idx_tienda = i
                break
        if idx_tienda is None:
            return {}, 0

        counts = {}
        total = 0
        for row in rows[1:]:
            if idx_tienda < len(row):
                tienda = (row[idx_tienda] or "").strip()
                if not tienda:
                    continue
                tienda_norm = " ".join(tienda.split())
                counts[tienda_norm] = counts.get(tienda_norm, 0) + 1
                total += 1
        return counts, total
    except Exception:
        return {}, 0

def contar_premios_asignados():
    """
    Lee el Sheet y devuelve (conteos_por_premio, total_asignados).
    Filtra valores que no son premios reales (ej: 'monto insuficiente', 'revisión manual', etc.).
    """
    try:
        ws = open_worksheet()
        rows = ws.get_all_values() or []
        if not rows:
            return {}, 0

        headers = [h.strip() for h in rows[0]]
        idx_premio = None
        for i, h in enumerate(headers):
            if h.strip().lower() == "premio":
                idx_premio = i
                break

        if idx_premio is None:
            return {}, 0

        counts = {}
        total = 0
        EXCLUDE_PREFIXES = (
            "monto insuficiente", "revisión manual", "revision manual",
            "sin premios", "sin premio", "rechazado"
        )

        for row in rows[1:]:
            if idx_premio < len(row):
                premio = (row[idx_premio] or "").strip()
                if not premio:
                    continue
                low = premio.lower()
                if any(low.startswith(pfx) for pfx in EXCLUDE_PREFIXES):
                    continue
                counts[premio] = counts.get(premio, 0) + 1
                total += 1

        return counts, total
    except Exception:
        return {}, 0

DEFAULT_PREMIOS = {
    "Pelacables": 350,
    "Amazon $5,000": 33,
    "Tablet premium": 10,
    "Smartphone": 25,
    "Amazon $500": 400,
    "Electrodoméstico o Tarjeta Liverpool": 250,
    "Motoneta": 2,
    'Pantalla 40"': 30,
    "Amazon $2,000": 100
}

def _union_premios(defaults: dict, asignados: dict):
    return sorted(set(defaults.keys()) | set(asignados.keys()), key=lambda x: x.lower())

def _build_inventario_from_sheets():
    """
    Devuelve:
      inventario: { nombre: {"totales": int, "asignados": int, "disponibles": int} }
      totales_globales: dict con sumas globales
    """
    asignados_map, total_asignados = contar_premios_asignados()
    inventario = {}
    total_totales = 0
    total_disponibles = 0

    for nombre in _union_premios(DEFAULT_PREMIOS, asignados_map):
        tot  = int(DEFAULT_PREMIOS.get(nombre, 0))
        asig = int(asignados_map.get(nombre, 0))
        disp = max(0, tot - asig)
        inventario[nombre] = {"totales": tot, "asignados": asig, "disponibles": disp}
        total_totales += tot
        total_disponibles += disp

    return inventario, {
        "total_items": len(inventario),
        "total_totales": total_totales,
        "total_asignados": total_asignados,
        "total_disponibles": total_disponibles,
    }

def _sync_redis_from_sheets(mode: str = "available", preview: bool = True, prefix: str = "premio:"):
    """
    mode:
      - "available" => escribir 'disponibles' en Redis (RECOMENDADO para el bot)
      - "assigned"  => escribir 'asignados'
    preview: True no escribe, solo muestra cambios.
    """
    mode = (mode or "available").lower()
    if mode not in ("available", "assigned"):
        mode = "available"

    inventario, sums = _build_inventario_from_sheets()

    cambios = []
    for nombre, data in inventario.items():
        target = data["disponibles"] if mode == "available" else data["asignados"]
        key = f"{prefix}{nombre}"
        try:
            actual = int(r.get(key) or 0)
        except Exception:
            actual = 0
        if actual != target:
            cambios.append({"key": key, "nombre": nombre, "old": actual, "new": target})
            if not preview:
                r.set(key, target)

    return {
        "mode": mode,
        "preview": preview,
        "changes": cambios,
        **sums,
    }

def auto_sync_from_sheets_if_stale(max_age_s=AUTO_SYNC_MAX_AGE_S, mode="available", force=False):
    """
    Sincroniza Redis desde Sheets si la última sync fue hace más de max_age_s.
    Guarda timestamp y usa un lock para evitar carreras.
    """
    now = int(time.time())
    try:
        last_ts = int(r.get("premio_sync:last_ts") or 0)
    except Exception:
        last_ts = 0

    if not force and (now - last_ts) < max_age_s:
        return {"ran": False, "last_ts": last_ts}

    if not r.set("premio_sync:lock", "1", nx=True, ex=30):
        return {"ran": False, "last_ts": last_ts, "locked": True}

    try:
        res = _sync_redis_from_sheets(mode=mode, preview=False)
        r.set("premio_sync:last_ts", now)
        return {"ran": True, "last_ts": now, "changes": res.get("changes", [])}
    finally:
        r.delete("premio_sync:lock")

# ------------------ Flujo Buen Fin Indiana ------------------
# Campos que se pedirán por texto/botón ANTES de la foto:
# 1) nombre, 2) tienda, 3) rfc_nombre, 4) ocupacion (botones), 5) festejo (botones)
CAMPOS = ["nombre", "tienda", "rfc_nombre", "ocupacion", "festejo", "medio"]
TOTAL_CAMPOS = len(CAMPOS)  # cuando paso == TOTAL_CAMPOS, esperamos la foto

BIENVENIDA = (
    "👋 ¡Hola!\nBienvenido al *Buen Fin Indiana* ⚡\n"
    "Para iniciar tu registro, escribe *QUIERO PARTICIPAR*"
)

PREGUNTAS = [
    "¡Listo! Por favor, escribe tu *nombre completo*.",
    "Cuéntanos, ¿*en qué tienda* realizaste tu compra?",
    "Ingresa el *RFC o Nombre completo* a quien está registrado el ticket o factura.\n"
    "No importa si lo estás registrando con autorización de alguien más."
]

VALIDACION_MSG = (
    "⏳ ¡Gracias! *Estamos validando tu ticket*.\n"
    "Nuestro equipo revisará tu compra y te contactará en un máximo de *24 horas*.\n"
    "Si tienes dudas, escríbenos al 📞 55 3478 4786 o 55 1954 2345."
)

# ------------------ Webhook ------------------
@app.route("/webhook", methods=["GET", "POST"])
@app.route("/webhook/", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        mode      = request.args.get('hub.mode')
        token     = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')
        if mode == "subscribe" and token == WEBHOOK_VERIFY_TOKEN:
            print("✅ Webhook verificado exitosamente")
            return challenge, 200
        return "❌ Token inválido", 403

    # POST: mensaje entrante
    data = request.get_json()
    try:
        change = data['entry'][0]['changes'][0]['value']
        if 'messages' not in change:
            return jsonify({"status": "no messages"}), 200

        mensaje  = change['messages'][0]
        telefono = mensaje['from']
        tipo     = mensaje['type']

        # Texto (botón o normal)
        texto = ""
        if "interactive" in mensaje and mensaje["interactive"].get("type") == "button_reply":
            btn_title = mensaje["interactive"]["button_reply"]["title"].strip()
            texto     = btn_title
            tipo      = "text"
        elif "text" in mensaje and "body" in mensaje["text"]:
            texto = mensaje["text"]["body"].strip()
            tipo  = "text"

        usuario = cargar_sesion(telefono)
        txt = (texto or "").strip().lower()

        # ---------------- A) Reinicio con QUIERO PARTICIPAR ----------------
        if "QUIERO PARTICIPAR" in texto.upper():
            usuario = {"paso": 0, "respuestas": {}, "tickets": []}

            import re
            # Detectar directamente el código "VXXX" en el mensaje
            m = re.search(r"\bV\d{3}\b", texto.upper())
            vendedor_id = m.group(0) if m else None

            if vendedor_id:
                vendedor_nombre = VENDEDORES.get(vendedor_id, vendedor_id)
            else:
                vendedor_nombre = "Sin vendedor"

            usuario["respuestas"]["vendedor"] = vendedor_nombre
            guardar_sesion(telefono, usuario)

            dbg(f"🧾 Vendedor detectado para {telefono}: {vendedor_nombre}")

            # Mensajes de bienvenida
            wsend(telefono, "👋 ¡Hola! Bienvenido al *Buen Fin Indiana* ⚡")
            wsend(telefono, PREGUNTAS[0])  # nombre
            return jsonify({"status": "inicio"}), 200

        # ---------------- B) No hay sesión todavía ----------------
        if not usuario:
            wsend(telefono, BIENVENIDA)
            return jsonify({"status": "esperando inicio"}), 200

        # ---------------- C) Comando SALIR ----------------
        if texto.upper() == "SALIR":
            usuario["paso"] = -1
            guardar_sesion(telefono, usuario)
            wsend(telefono, "✅ Gracias, puedes volver más tarde escribiendo *QUIERO PARTICIPAR*.")
            return jsonify({"status": "salir"}), 200

        # ---------------- D) Paso 99: ¿Otro ticket? (Sí/No) ----------------
        if usuario.get("paso") == 99:
            if txt in ("sí", "si"):
                # Conserva datos base (no se vuelven a pedir)
                usuario["paso"] = TOTAL_CAMPOS  # directamente pedir foto del 2º ticket
                guardar_sesion(telefono, usuario)
                wsend(telefono, "📸 Perfecto, envía una *foto clara* de tu *2º ticket* de compra participante.")
                return jsonify({"status": "esperando foto 2do ticket"}), 200

            if txt in ("no", "n"):
                usuario["paso"] = -1
                guardar_sesion(telefono, usuario)
                wsend(telefono, "🙌 ¡Gracias por participar en el *Buen Fin Indiana*! 🎁\nPronto recibirás noticias.")
                eliminar_sesion(telefono)
                return jsonify({"status": "fin"}), 200

            wsend(telefono, "Responde *Sí* si tienes otro ticket o *No* para terminar.")
            return jsonify({"status": "recordatorio paso 99"}), 200

        # ---------------- E) Flujo de preguntas (texto/botones) -------------
        if usuario.get("paso", 0) < TOTAL_CAMPOS:
            idx = usuario["paso"]
            campo = CAMPOS[idx]

            # 0) nombre
            if campo == "nombre":
                usuario["respuestas"]["nombre"] = texto
                usuario["paso"] += 1
                guardar_sesion(telefono, usuario)
                wsend(telefono, PREGUNTAS[1])  # tienda
                return jsonify({"status": "nombre ok"}), 200

            # 1) tienda
            if campo == "tienda":
                usuario["respuestas"]["tienda"] = texto
                usuario["paso"] += 1
                guardar_sesion(telefono, usuario)
                wsend(telefono, PREGUNTAS[2])  # rfc/nombre
                return jsonify({"status": "tienda ok"}), 200

            # 2) rfc_nombre
            if campo == "rfc_nombre":
                usuario["respuestas"]["rfc_nombre"] = texto
                usuario["paso"] += 1
                guardar_sesion(telefono, usuario)

                # Botones: Ocupación (orden solicitado: 1 Electricista, 2 Contratista, 3 Otro)
                wa.send_reply_button(
                    recipient_id=telefono,
                    button={
                        "type": "button",
                        "body": {"text": "¿Cuál es tu *ocupación principal*?"},
                        "action": {
                            "buttons": [
                                {"type": "reply", "reply": {"id": "1", "title": "Electricista"}},
                                {"type": "reply", "reply": {"id": "2", "title": "Contratista"}},
                                {"type": "reply", "reply": {"id": "3", "title": "Otro"}},
                            ]
                        },
                    },
                )
                return jsonify({"status": "rfc_nombre ok"}), 200

            # 3) ocupacion (botón)
            if campo == "ocupacion":
                usuario["respuestas"]["ocupacion"] = texto
                usuario["paso"] += 1
                guardar_sesion(telefono, usuario)

                # Botones: ¿Qué estamos festejando?
                wa.send_reply_button(
                    recipient_id=telefono,
                    button={
                        "type": "button",
                        "body": {"text": "🎉 ¿Qué *estamos festejando* con esta promoción?"},
                        "action": {
                            "buttons": [
                                {"type": "reply", "reply": {"id": "1", "title": "Buen Fin"}},
                                {"type": "reply", "reply": {"id": "2", "title": "14 de Feb"}},
                                {"type": "reply", "reply": {"id": "3", "title": "Pascua"}},
                            ]
                        },
                    },
                )
                return jsonify({"status": "ocupacion ok"}), 200
            # 4) medio (botón)
            if campo == "festejo":
                usuario["respuestas"]["festejo"] = texto
                usuario["paso"] += 1
                guardar_sesion(telefono, usuario)

                # Enviar mensaje con opciones numeradas (sin botones)
                wsend(
                    telefono,
                    "📢 ¿Por qué medio te enteraste de la promoción?\n\n"
                    "1️⃣ Radio\n"
                    "2️⃣ Cartel publicitario\n"
                    "3️⃣ En tienda\n"
                    "4️⃣ Redes sociales\n\n"
                    "Por favor, responde con el *número* de tu opción (1–4)."
                )
                return jsonify({"status": "pregunta medio enviada"}), 200

            # 5) medio (validación numérica 1–4)
            if campo == "medio":
                # Validar número
                if texto not in ("1", "2", "3", "4"):
                    wsend(
                        telefono,
                        "❌ Opción no válida. Por favor responde con un número del *1 al 4*:\n\n"
                        "1️⃣ Radio\n"
                        "2️⃣ Cartel publicitario\n"
                        "3️⃣ En tienda\n"
                        "4️⃣ Redes sociales"
                    )
                    return jsonify({"status": "respuesta inválida (medio)"}), 200

                opciones = {
                    "1": "Radio",
                    "2": "Cartel publicitario",
                    "3": "En tienda",
                    "4": "Redes sociales"
                }

                usuario["respuestas"]["medio"] = opciones[texto]
                usuario["paso"] += 1
                guardar_sesion(telefono, usuario)

                # Pasamos a pedir la foto del ticket
                wsend(
                    telefono,
                    "📸 ¡Genial!\nEnvía una *foto clara* de tu *ticket/factura* participante.\n"
                    "Procura que se vea completo y legible: *folio, razón social o nombre y producto Indiana* "
                    "por *monto mayor a $6,000 + IVA*.\n"
                    "Las *cotizaciones no participan*."
                )
                return jsonify({"status": "medio ok, pedir foto"}), 200

        # ---------------- F) Esperando FOTO (TOTAL_CAMPOS) ------------------
        if usuario and usuario.get("paso") == TOTAL_CAMPOS and tipo != "image":
            if tipo == "document":
                document = mensaje.get("document", {})
                filename = document.get("filename", "archivo")
                wsend(
                    telefono,
                    f"❌ Recibí un archivo ({filename}) pero necesito una *imagen* de tu ticket (JPG/PNG)."
                )
            elif tipo == "text":
                wsend(telefono, "❌ Recibí texto, pero necesito una *imagen* de tu ticket (JPG/PNG).")
            else:
                wsend(telefono, "❌ Tipo de archivo no válido. Envíe una *imagen* (JPG/PNG).")
            return jsonify({"status": f"archivo no válido: {tipo}"}), 200

        # ---------------- G) Procesar FOTO, asignar premio y loguear --------
        if tipo == "image" and usuario and usuario.get("paso") == TOTAL_CAMPOS:
            media_id = mensaje["image"]["id"]
            usuario["respuestas"]["ticket_photo"] = f"media:{media_id}"
            usuario["respuestas"]["timestamp"] = datetime.now().isoformat()

            # OCR / Validación
            wsend(telefono, '⏳ Procesando tu ticket, por favor espera...')
            resultado = validar_ticket_desde_media(media_id, token_facebook, telefono)
            print("Resultado OCR:", resultado)

            monto_ticket = resultado.get("monto")
            path_ticket = resultado.get("nombre_archivo")
            motivo_ocr  = resultado.get("motivo", "")

            nuevo_ticket = usuario["respuestas"].copy()

            if resultado.get("valido"):
                wsend(
                    telefono,
                    "✅ Tu ticket fue recibido y leído correctamente. "
                    "Será validado por nuestro equipo."
                )
                nuevo_ticket["premio"] = "Pendiente de validación"
            else:
                wsend(
                    telefono,
                    "❌ No pudimos leer correctamente tu ticket. "
                    "Será revisado manualmente por nuestro equipo."
                )
                nuevo_ticket["premio"] = "Revisión manual"

            wsend(telefono, VALIDACION_MSG)

            # Datos para Sheets
            datos_generales = {
                "telefono": telefono,
                "nombre": usuario["respuestas"].get("nombre", ""),
                "tienda": usuario["respuestas"].get("tienda", ""),
                "rfc_nombre": usuario["respuestas"].get("rfc_nombre", ""),
                "ocupacion": usuario["respuestas"].get("ocupacion", ""),
                "festejo": usuario["respuestas"].get("festejo", ""),
                "monto": monto_ticket,
                "motivo": motivo_ocr,
                "vendedor": usuario["respuestas"].get("vendedor", "Sin vendedor"),
                "nombre_archivo": f"{URL_SERVER}/catalogo_img/{path_ticket}" if path_ticket else "",
                "premio": nuevo_ticket.get("premio", "")
            }

            # Historial
            usuario.setdefault("tickets", []).append(nuevo_ticket)
            guardar_sesion(telefono, usuario)

            # Log a Sheets
            try:
                registrar_ticket_en_sheets(datos_generales, nuevo_ticket)
            except Exception as e:
                print("❌ registrar_ticket_en_sheets error:", e, flush=True)

            # Preguntar por otro ticket
            usuario["paso"] = 99
            guardar_sesion(telefono, usuario)
            wsend(telefono, "¿Tienes *otro ticket*? (Sí / No)")
            return jsonify({"status": "ticket recibido"}), 200

        # Nada más que hacer
        return jsonify({"status": "sin cambios"}), 200

    except Exception as e:
        print("❌ Error procesando mensaje:", e, flush=True)
        return jsonify({"error": str(e)}), 500

# ------------------ Catálogo de imágenes ------------------

@app.route("/tickets-pendientes")
def tickets_pendientes():
    ws = open_worksheet()
    rows = ws.get_all_records()
    pendientes = []

    for r in rows:
        # normalizamos encabezados
        row_norm = {k.strip().lower(): v for k, v in r.items()}
        premio = (row_norm.get("premio") or "").strip().lower()

        if premio in ("pendiente de validación", "revisión manual"):
            pendientes.append({
                "timestamp": row_norm.get("timestamp", ""),
                "nombre": row_norm.get("nombre", ""),
                "telefono": row_norm.get("telefono", ""),
                "tienda": row_norm.get("tienda", ""),
                "monto_ocr": row_norm.get("monto", row_norm.get("cantidad detectada", "")),
                "cantidad_detectada": row_norm.get("cantidad detectada", ""),
                "premio": row_norm.get("premio", ""),
                "ticket": row_norm.get("ticket", ""),
            })

    hora_actual = datetime.utcnow().strftime("%d/%m/%Y %H:%M:%S")
    ano_actual = datetime.utcnow().year
    if request.args.get("ajax"):
        return render_template("tickets_table.html", tickets=pendientes, hora_actual=hora_actual, ano_actual=ano_actual)

    return render_template("tickets.html", tickets=pendientes, hora_actual=hora_actual, ano_actual=ano_actual)

@app.route("/asignar-premio", methods=["POST"])
def asignar_premio():
    data = request.get_json()
    telefono = str(data.get("telefono", "")).strip()
    cantidad_detectada = float(data.get("cantidad_detectada", 0))

    if not telefono:
        return jsonify({"error": "Falta el número de teléfono"}), 400

    # Calcular premio según el monto detectado
    premio, tipo_premio = obtener_premio_especial(r, cantidad_detectada)
    if not premio:
        return jsonify({"error": "Sin premio disponible"}), 400

    # Conexión a Google Sheets
    ws = open_worksheet()
    rows = ws.get_all_values()
    headers = [h.strip().lower() for h in rows[0]]
    idx_tel = headers.index("telefono")
    idx_premio = headers.index("premio")
    idx_nombre = headers.index("nombre")
    idx_cantidad = headers.index("cantidad detectada") if "cantidad detectada" in headers else None

    actualizado = False

    # Buscar el registro con el teléfono correspondiente
    for i, row in enumerate(rows[1:], start=2):
        if row[idx_tel].strip() == telefono.strip():
            valor_actual = row[idx_premio].strip().lower()
            # Solo reemplazar si está "pendiente" o "revisión manual"
            if valor_actual in ("pendiente de validación", "revisión manual", "pendiente"):
                nombre = row[idx_nombre].strip()  # 👈 aquí obtienes el nombre
                ws.update_cell(i, idx_premio + 1, premio)
                if idx_cantidad:
                    ws.update_cell(i, idx_cantidad + 1, cantidad_detectada)
                actualizado = True
                break

    if not actualizado:
        return jsonify({"error": "No se encontró registro pendiente para ese número"}), 404

    # Enviar mensaje al WhatsApp
    msg = f"""
    🎉 ¡Felicidades, {nombre}!

    Tu participación en *El Buen Fin Indiana* ha sido validada con éxito ✅  
    Has ganado un *{premio}* 🏆

    Nuestro equipo se pondrá en contacto contigo para coordinar la entrega.
    Mantente pendiente de tu WhatsApp 📱
    Recuerda que entre más compres, ¡mayor puede ser tu recompensa! ⚡  

    🔗 Si deseas conocer más sobre la dinámica, visita:
    👉 www.buenfinindiana.com/bases

    ¡Gracias por participar!
    """
    wsend(telefono, msg)

    return jsonify({
        "status": "ok",
        "premio": premio,
        "telefono": telefono,
        "monto": cantidad_detectada
    })

@app.route("/catalogo")
def catalogo():
    query = request.args.get("q", "").lower()
    folder = "images_to_process"
    if not os.path.exists(folder):
        return "❌ Carpeta no encontrada", 404
    imgs = [f for f in os.listdir(folder) if f.lower().endswith((".jpg", ".png", ".jpeg"))]
    if query:
        imgs = [f for f in imgs if query in f.lower()]
    return render_template("catalogo.html", images=imgs, query=query)

@app.route("/catalogo_img/<filename>")
def catalogo_img(filename):
    return send_from_directory("images_to_process", filename)

# ------------------ Dashboard (inventario) ------------------
@app.route("/inventario.json", methods=["GET"])
def inventario_json():
    # auto-sync (cada hora por defecto); forzar con ?sync=1
    if AUTO_SYNC_ON_DASHBOARD:
        auto_sync_from_sheets_if_stale(
            force=(request.args.get("sync") == "1"),
            mode="available"
        )

    asignados_map, total_asignados = contar_premios_asignados()
    todos = sorted(set(DEFAULT_PREMIOS.keys()) | set(asignados_map.keys()), key=lambda x: x.lower())

    inventario = {}
    total_totales = 0
    total_disponibles = 0

    for name in todos:
        tot = int(DEFAULT_PREMIOS.get(name, 0))
        asig = int(asignados_map.get(name, 0))
        disp = max(0, tot - asig)
        inventario[name] = {"totales": tot, "asignados": asig, "disponibles": disp}
        total_totales += tot
        total_disponibles += disp

    try:
        last_ts = int(r.get("premio_sync:last_ts") or 0)
    except Exception:
        last_ts = 0

    return jsonify({
        "last_sync_ts": last_ts,
        "total_items": len(inventario),
        "total_totales": total_totales,
        "total_asignados": total_asignados,
        "total_disponibles": total_disponibles,
        "inventario": inventario
    }), 200

@app.route("/inventario", methods=["GET"])
def inventario_html():
    # auto-sync (cada hora por defecto); forzar con ?sync=1
    if AUTO_SYNC_ON_DASHBOARD:
        auto_sync_from_sheets_if_stale(
            force=(request.args.get("sync") == "1"),
            mode="available"
        )

    asignados_map, total_asignados = contar_premios_asignados()
    todos = sorted(set(DEFAULT_PREMIOS.keys()) | set(asignados_map.keys()), key=lambda x: x.lower())

    items = []
    total_totales = 0
    total_disponibles = 0
    max_qty = 0

    for nombre in todos:
        tot = int(DEFAULT_PREMIOS.get(nombre, 0))
        asig = int(asignados_map.get(nombre, 0))
        disp = max(0, tot - asig)
        items.append({"nombre": nombre, "totales": tot, "asignados": asig, "disponibles": disp})
        total_totales += tot
        total_disponibles += disp
        if disp > max_qty:
            max_qty = disp

    total_items = len(items)
    low_threshold = 5

    try:
        last_ts = int(r.get("premio_sync:last_ts") or 0)
    except Exception:
        last_ts = 0
    last_sync_dt = datetime.fromtimestamp(last_ts) if last_ts else None

    return render_template(
        "inventario.html",
        items=items,
        total_items=total_items,
        total_totales=total_totales,
        total_asignados=total_asignados,
        total_disponibles=total_disponibles,
        low_threshold=low_threshold,
        max_qty=max_qty or 1,
        last_update=datetime.now(),
        last_sync=last_sync_dt
    )

# ------------------ Utilidades Sheets (opcionales) ------------------
@app.get("/sheets/total-monto")
def total_monto():
    try:
        ws = open_worksheet()
        rows = ws.get_all_values() or []
        if not rows:
            return jsonify({"total": 0.0})

        headers = [h.strip().lower() for h in rows[0]]
        posibles = ("monto", "total", "importe", "cantidad detectada")
        idx = None
        for i, h in enumerate(headers):
            if any(p == h for p in posibles):
                idx = i
                break

        if idx is None:
            return jsonify({"total": 0.0, "error": "No se encontró la columna Monto/Total/Importe"})

        valores = [row[idx] for row in rows[1:] if idx < len(row)]
        total = round(sum(parse_money(v) for v in valores), 2)
        return jsonify({"total": total})
    except Exception as e:
        print("❌ /sheets/total-monto error:", e, flush=True)
        return jsonify({"total": 0.0, "error": str(e)}), 500

@app.get("/sheets/top-tiendas")
def top_tiendas():
    try:
        limit = int(request.args.get("limit", 8))
    except Exception:
        limit = 8

    counts, total = contar_tiendas()
    ordenadas = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    top = ordenadas[:max(0, limit)]

    return jsonify({
        "total_tiendas": len(counts),
        "total_registros": total,
        "items": [{"tienda": n, "registros": c} for n, c in top]
    }), 200

@app.get("/sheets/top-vendedores")
def top_vendedores():
    """
    Devuelve los vendedores con más registros, basado en la columna 'Vendedor' del Sheet.
    """
    try:
        limit = int(request.args.get("limit", 8))
    except Exception:
        limit = 8

    ws = open_worksheet()
    rows = ws.get_all_values() or []
    if not rows:
        return jsonify({"total_vendedores": 0, "total_registros": 0, "items": []}), 200

    headers = [h.strip().lower() for h in rows[0]]
    if "vendedor" not in headers:
        return jsonify({"error": "Columna 'Vendedor' no encontrada"}), 400

    idx_vendedor = headers.index("vendedor")

    counts = {}
    total = 0
    for row in rows[1:]:
        if idx_vendedor < len(row):
            vendedor = (row[idx_vendedor] or "").strip()
            if not vendedor:
                continue
            vendedor_norm = " ".join(vendedor.split())
            counts[vendedor_norm] = counts.get(vendedor_norm, 0) + 1
            total += 1

    ordenadas = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    top = ordenadas[:max(0, limit)]

    return jsonify({
        "total_vendedores": len(counts),
        "total_registros": total,
        "items": [{"vendedor": n, "registros": c} for n, c in top]
    }), 200

# ------------------ Raíz ------------------
@app.route("/")
def index():
    return "Chatbot Buen Fin Indiana 2025", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5002, debug=True)