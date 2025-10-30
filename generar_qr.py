import qrcode

vendedores = ["V001", "V002"]
for v in vendedores:
    url = f"https://seal-sweet-lamb.ngrok-free.app/qr?vendedor={v}"  # ⚠️ pon tu dominio real
    img = qrcode.make(url)
    img.save(f"qr_{v}.png")
    print(f"✅ QR generado: qr_{v}.png -> {url}")