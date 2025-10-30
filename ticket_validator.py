# ticket_validator.py
#!/usr/bin/env python3
import os, io, re, json, time, uuid, shutil, requests, base64
from pathlib import Path
from typing import List, Dict, Any, Optional
from PIL import Image
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# -------------------------------
# Config & carpetas
# -------------------------------
MODEL       = os.getenv("OPENAI_VISION_MODEL", "gpt-4o")
API_KEY     = os.getenv("OPENAI_API_KEY")
TIMEOUT_S   = int(os.getenv("OPENAI_TIMEOUT", "45"))
RETRY       = int(os.getenv("OPENAI_RETRY", "2"))

DIR_TO_PROCESS = "images_to_process"
DIR_PROCESSED  = "images_processed"
os.makedirs(DIR_TO_PROCESS, exist_ok=True)
os.makedirs(DIR_PROCESSED,  exist_ok=True)

# -------------------------------
# WhatsApp Graph helpers
# -------------------------------
def obtener_media_url(media_id: str, token: str) -> Optional[str]:
    url = f"https://graph.facebook.com/v20.0/{media_id}"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, timeout=20)
    if resp.ok:
        return resp.json().get("url")
    print(f"[obtener_media_url] {resp.status_code} {resp.text}")
    return None

def descargar_imagen_local(media_id: str, token: str, telefono: str) -> Optional[str]:
    media_url = obtener_media_url(media_id, token)
    if not media_url:
        return None
    resp = requests.get(media_url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    if not resp.ok:
        print(f"[descargar_imagen_local] {resp.status_code} al descargar media")
        return None
    nombre = f"{uuid.uuid4()}_{telefono}.jpg"
    ruta = os.path.join(DIR_TO_PROCESS, nombre)  # <- ORIGINAL se guarda aquí (tu /catalogo_img usa esta carpeta)
    with open(ruta, "wb") as f:
        f.write(resp.content)
    return ruta

# -------------------------------
# Prompt (extrae TOTAL y renglones cuando existen)
# -------------------------------
SYSTEM_PROMPT = '''
Eres un extractor experto de facturas/tickets mexicanos. Devuelve SIEMPRE JSON válido.

OBJETIVO:
- Extraer el TOTAL FINAL (monto a pagar).
- Si es posible, extrae renglones de productos con su importe por línea.

REGLAS:
- No incluyas líneas de impuestos, subtotal, IVA, retenciones, propinas, envío, etc. como productos.
- El "total" debe ser el total final del documento (no subtotales ni totales por tasa).

SALIDA JSON (siempre):
{
  "total": number_or_null,
  "currency": "MXN",
  "products": [
    { "description": "texto", "line_total": number_or_null }
  ],
  "confidence_score": number_1_to_10
}
Responde ÚNICAMENTE JSON válido, sin texto adicional.
'''

# -------------------------------
# Helpers
# -------------------------------
def to_float(x):
    if x is None: return None
    if isinstance(x, (int, float)): return float(x)
    try:
        return float(str(x).replace(",", ""))
    except Exception:
        return None

def clean_json_response(content: str) -> str:
    start = content.find('{')
    end = content.rfind('}') + 1
    if start != -1 and end != 0:
        j = content[start:end]
        j = re.sub(r',\s*}', '}', j)
        j = re.sub(r',\s*]', ']', j)
        j = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', j)
        return j
    return content

def img_to_b64(path: Path) -> str:
    with Image.open(path) as im:
        im = im.convert("RGB")
        from io import BytesIO
        buf = BytesIO()
        im.save(buf, format="JPEG", quality=90)
        return base64.b64encode(buf.getvalue()).decode("utf-8")

# -------------------------------
# Llamada a OpenAI (una imagen)
# -------------------------------
def call_openai_for_image(client: OpenAI, img_b64: str) -> Dict[str, Any]:
    last_err = None
    for attempt in range(1 + RETRY):
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": [
                        {"type": "text",
                         "text": "Analiza este ticket/factura y devuelve el JSON indicado."},
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
                    ]}
                ],
                response_format={"type": "json_object"},
                max_tokens=2000,
                temperature=0.1,
            )
            content = resp.choices[0].message.content
            content = clean_json_response(content)
            data = json.loads(content)
            # Normaliza mínimos esperados
            out = {
                "total": to_float(data.get("total")),
                "currency": (data.get("currency") or "MXN"),
                "products": data.get("products") or [],
                "confidence_score": to_float(data.get("confidence_score")) or 5.0,
            }
            return out
        except Exception as e:
            last_err = e
            print(f"[OpenAI] intento {attempt + 1} falló: {e}")
            if attempt < RETRY:
                time.sleep(1.2)
    raise RuntimeError(f"OpenAI error después de {RETRY + 1} intentos: {last_err}")

# -------------------------------
# API pública para tu APP
# -------------------------------
def validar_ticket_desde_media(media_id: str, token: str, telefono: str) -> Dict[str, Any]:
    """
    Devuelve: {valido: bool, monto: float|0.0, nombre_archivo: str, motivo: str, ocr_detectado: bool}
    - Descarga la imagen ORIGINAL en images_to_process (tu app sirve /catalogo_img desde aquí).
    - Copia la imagen a images_processed para auditoría.
    - Usa OpenAI Vision para extraer el total.
    """
    out = {"valido": False, "monto": 0.0, "nombre_archivo": "", "motivo": "", "ocr_detectado": False}

    if not API_KEY:
        out["motivo"] = "Falta OPENAI_API_KEY"
        return out

    # 1) Descargar imagen original en images_to_process/
    ruta_original = descargar_imagen_local(media_id, token, telefono)
    if not ruta_original:
        out["motivo"] = "No se pudo descargar la imagen"
        return out

    nombre_archivo = os.path.basename(ruta_original)
    out["nombre_archivo"] = nombre_archivo

    # 2) Copiar a images_processed/ (trabajo/auditoría)
    ruta_trabajo = os.path.join(DIR_PROCESSED, nombre_archivo)
    try:
        shutil.copy2(ruta_original, ruta_trabajo)
    except Exception as e:
        print(f"[validar_ticket_desde_media] Error copiando a processed: {e}")
        # Si falla la copia seguimos, ya tenemos el original

    # 3) Llamada a OpenAI Vision
    try:
        client = OpenAI(api_key=API_KEY, timeout=TIMEOUT_S)
        b64 = img_to_b64(Path(ruta_trabajo if os.path.exists(ruta_trabajo) else ruta_original))
        data = call_openai_for_image(client, b64)
    except Exception as e:
        out["motivo"] = f"Error analizando imagen: {e}"
        return out

    total = data.get("total")
    if total is None:
        out["motivo"] = "No se encontró el total en el ticket"
        return out

    # 4) Mapear resultado esperado por la app
    out["monto"] = float(total)
    out["valido"] = True               # La app ya valida montos mínimos en el flujo
    out["ocr_detectado"] = True
    out["motivo"] = f"Monto detectado: ${out['monto']:,.2f}"

    # 5) Guardar JSON junto a la copia en processed (best-effort)
    try:
        with open(os.path.join(DIR_PROCESSED, f"{nombre_archivo}.ai.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    return out