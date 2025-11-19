# control_inventario.py
import random

def items_con_stock(redis_conn, prefix="premio:"):
    """
    Devuelve lista [(key, qty)] con premios que tienen stock > 0
    y el total de unidades sumadas.
    """
    items, total = [], 0
    for k in redis_conn.scan_iter(f"{prefix}*"):
        try:
            qty = int(redis_conn.get(k) or 0)
        except (TypeError, ValueError):
            qty = 0
        if qty > 0:
            items.append((k, qty))
            total += qty
    return items, total


def obtener_premio_disponible(redis_conn, prefix="premio:"):
    """
    Devuelve un premio aleatorio ponderado (sin considerar rangos).
    Útil para rifas generales o premios de participación.
    """
    items, total = items_con_stock(redis_conn, prefix)
    if total == 0:
        return None

    target = random.randint(1, total)
    for key, qty in items:
        if target <= qty:
            new_val = redis_conn.decr(key)
            if new_val >= 0:
                return key.replace(prefix, "", 1)
            redis_conn.incr(key)
            return None
        target -= qty


def obtener_premio_especial(redis_conn, monto_factura):
    """
    Asigna premio según el rango de compra (MXN).
    Si no hay stock disponible para ese rango, devuelve (None, None).
    """

    # Definición de niveles y premios según rango de compra
    RANGOS_PREMIOS = [
        (6000,   9999,   "Pelacables"),
        (10000,  19999,  "Amazon $500"),
        (20000,  39999,  "Electrodomésticos"),
        (40000,  59999,  "Amazon $1500"),
        (60000,  99999,  'Pantalla 40"'),
        (100000, 149999, "Amazon $3500"),
        (150000, 199999, "Smartphone"),
        (200000, 299999, "Tablet premium"),
        (300000, 499999, "Motoneta"),
    ]

    # Buscar el rango correspondiente al monto de compra
    premio = None
    for (minimo, maximo, nombre) in RANGOS_PREMIOS:
        if minimo <= monto_factura <= maximo:
            premio = nombre
            break

    # Si no entra en ningún rango, no califica
    if not premio:
        return None, None

    # Validar stock en Redis
    key = f"premio:{premio}"
    try:
        qty = int(redis_conn.get(key) or 0)
        if qty > 0:
            new_val = redis_conn.decr(key)
            if new_val >= 0:
                return premio, "rango"
            else:
                # revertir si falló el decremento
                redis_conn.incr(key)
                return None, None
    except (TypeError, ValueError):
        pass

    # Sin stock
    return None, None