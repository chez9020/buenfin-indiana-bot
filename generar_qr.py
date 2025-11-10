import qrcode
from qrcode.constants import ERROR_CORRECT_H
from PIL import Image

# Lista de vendedores
vendedores = ["V095","V096","V097","V098","V099","V100","V101","V102","V103","V104","V105","V106","V107","V108","V109","V110","V111","V112"]

# Ruta del logo
logo_path = "logo_indiana_transparencia.png"  # cambia por el nombre real del logo

# Cargar logo
logo = Image.open(logo_path).convert("RGBA")

for v in vendedores:
    # --- Generar la URL ---
    url = f"https://34.226.49.191.sslip.io/qr?vendedor={v}"

    # --- Crear QR con corrección de errores alta ---
    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_H,
        box_size=10,
        border=4
    )
    qr.add_data(url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGBA")

    # --- Redimensionar y centrar logo ---
    qr_w, qr_h = qr_img.size
    logo_size = int(qr_w * 0.25)  # logo ≈18% del ancho del QR
    logo_resized = logo.copy()
    logo_resized.thumbnail((logo_size, logo_size), Image.LANCZOS)

    # Posicionar logo al centro
    pos = ((qr_w - logo_resized.width) // 2, (qr_h - logo_resized.height) // 2)
    qr_img.alpha_composite(logo_resized, dest=pos)

    # --- Guardar QR ---
    output_file = f"qr_{v}.png"
    qr_img.convert("RGB").save(output_file, dpi=(300, 300))
    print(f"✅ QR generado: {output_file} -> {url}")
