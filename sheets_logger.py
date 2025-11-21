# sheets_logger.py
import os
import json
import logging
import datetime as dt
import gspread

logging.basicConfig(level=logging.INFO)

# ---------- Config de credenciales ----------
# Usa GOOGLE_APPLICATION_CREDENTIALS (ruta al JSON del service account).
# Si no está, intenta "credentials/SHEETS_KEY.json".
SA_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "credentials/SHEETS_KEY.json")

# Lee múltiples Sheet IDs:
# Opción A: GOOGLE_SHEETS_IDS="id1,id2,id3"
# Opción B: GOOGLE_SHEETS_ID_1, GOOGLE_SHEETS_ID_2 (y también GOOGLE_SHEETS_ID como fallback)
def _resolve_sheet_ids():
    ids = []

    # A) Coma-separado
    coma = os.getenv("GOOGLE_SHEETS_IDS", "")
    if coma.strip():
        ids.extend([x.strip() for x in coma.split(",") if x.strip()])

    # C) Fallback simple
    single = os.getenv("GOOGLE_SHEETS_ID", "").strip()
    if single and single not in ids:
        ids.append(single)

    # Dedup en orden
    seen, out = set(), []
    for i in ids:
        if i and i not in seen:
            seen.add(i)
            out.append(i)
    return out

_client = None
_worksheets = None

def _get_client():
    global _client
    if _client is None:
        # gspread usará el JSON del service account
        _client = gspread.service_account(filename=SA_PATH)
    return _client

def _get_worksheets():
    """
    Abre y cachea sheet1 de todos los IDs configurados.
    Lo hacemos lazy (en tiempo de uso) para evitar fallas al importar.
    """
    global _worksheets
    if _worksheets is not None:
        return _worksheets

    sheet_ids = _resolve_sheet_ids()
    if not sheet_ids:
        logging.error("No hay Sheet IDs configurados. Revisa tu .env")
        _worksheets = []
        return _worksheets

    cli = _get_client()
    ws_list = []
    for sid in sheet_ids:
        try:
            ws = cli.open_by_key(sid).sheet1
            ws_list.append((sid, ws))
            logging.info(f"Conectado a Google Sheet: {sid}")
        except Exception as e:
            logging.error(f"No se pudo abrir la hoja {sid}: {e}")

    _worksheets = ws_list
    return _worksheets

def _armar_row(datos_generales: dict, ticket: dict):
    """
    Ajustado al flujo del Buen Fin Indiana.
    Mantiene el orden de columnas esperado en tu Google Sheet.
    """
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    telefono    = datos_generales.get("telefono", "")
    nombre      = datos_generales.get("nombre", "")
    tienda      = datos_generales.get("tienda", "")
    rfc_nombre  = datos_generales.get("rfc_nombre", "")
    correo      = datos_generales.get("correo", "")
    ocupacion   = datos_generales.get("ocupacion", "")
    medio     = datos_generales.get("medio", "")
    monto       = datos_generales.get("monto", "")
    premio      = datos_generales.get("premio", "")
    motivo      = datos_generales.get("motivo", "")
    vendedor   = datos_generales.get("vendedor", "")
    archivo     = datos_generales.get("nombre_archivo", "")
    raw         = json.dumps({"datos": datos_generales, "ticket": ticket}, ensure_ascii=False)

    # Orden de columnas sugerido:
    # Timestamp | Teléfono | Nombre | Tienda | RFC/Nombre Factura | Ocupación | Medio | Monto | Premio | Motivo | Archivo
    return [
        now,         # A: Timestamp
        telefono,    # B
        nombre,      # C
        tienda,      # D
        rfc_nombre,  # E
        correo,
        ocupacion,   # F
        medio,     # G
        monto,       # H
        premio,      # I
        motivo,      # J
        vendedor,
        archivo      # K
    ]

def registrar_ticket_en_sheets(datos_generales: dict, ticket: dict) -> bool:
    """
    Anexa la fila en TODOS los Google Sheets configurados.
    Devuelve True si al menos uno logró escribir.
    """
    ws_list = _get_worksheets()
    if not ws_list:
        logging.error("Sin worksheets disponibles; no se registró el ticket.")
        return False

    row = _armar_row(datos_generales, ticket)
    ok = False
    for sid, ws in ws_list:
        try:
            ws.append_row(row, value_input_option="USER_ENTERED")
            logging.info(f"Fila agregada en sheet {sid}")
            ok = True
        except Exception as e:
            logging.error(f"Error al escribir en sheet {sid}: {e}")
    return ok
