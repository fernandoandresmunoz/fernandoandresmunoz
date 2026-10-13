#!/usr/bin/env python3
"""Genera libros.json con los mejores descuentos de varios dominios.

Por cada dominio: último upload -> top 100 de /featured/ (mayor descuento).
Luego consolida y deja los TOP mejores entre todos los dominios.

Uso:
    export ONESOURCE_TOKEN="tu_token"
    export SITE_URL="https://usuario.github.io/repo"   # opcional: imágenes absolutas en el JSON-LD
    pip install pillow   # una vez: reduce las portadas a miniaturas WebP
    python3 script_generator.py
"""
import hashlib
import io
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

try:
    from PIL import Image
except ImportError:  # sin Pillow guarda las portadas tal cual (mucho más pesadas)
    Image = None

BASE = "http://onesource.cl/api"
DOMINIOS = [4, 1, 16]
POR_DOMINIO = 250   # cuántos intenta traer de cada dominio
TOP = 250           # cuántos quedan en el consolidado
SALIDA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "libros.json")
TOKEN = os.environ.get("ONESOURCE_TOKEN")
CARPETA_IMG = "imagenes"
ANCHO_IMG, ALTO_IMG = 96, 128  # la tabla las muestra a 44x56 px; esto cubre pantallas retina
LD_MAX = 100                   # cuántos libros entran al JSON-LD
RUTA_IMG = os.path.join(os.path.dirname(SALIDA), CARPETA_IMG)
INDEX = os.path.join(os.path.dirname(SALIDA), "index.html")
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")
NOMBRES_TIENDA = {"buscalibre.cl": "Buscalibre", "antartica.cl": "Antártica",
                  "feriachilenadellibro.cl": "Feria Chilena del Libro"}
BLOQUE_LD = re.compile(r'<script type="application/ld\+json" id="ld-books">.*?</script>', re.S)


def get(path, **params):
    url = path if path.startswith("http") else f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Token {TOKEN}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def nombres_dominios():
    try:
        return {d["id"]: d["name"] for d in get("/domains/", page_size=80)["results"]}
    except Exception as e:
        print(f"  (no pude leer /domains/: {e})")
        return {}


def ultimo_upload(dominio):
    res = get("/uploads/", domain=dominio, page_size=1)["results"]
    if not res:
        raise RuntimeError("el dominio no tiene uploads")
    return res[0]


def mejores_del_dominio(upload_id):
    """Top POR_DOMINIO por variacion_promedio (más negativa = mayor descuento)."""
    items = []
    resp = get("/featured/", upload__id=upload_id, page_size=POR_DOMINIO,
               ordering="variacion_promedio", variacion_precio_minimo=0)
    while True:
        items += resp.get("results", [])
        if len(items) >= POR_DOMINIO or not resp.get("next"):
            break
        resp = get(resp["next"])  # por si el servidor limita el page_size
    return items[:POR_DOMINIO]


def arreglar_url(url, tienda):
    """Buscalibre guarda rutas relativas (sin dominio): les antepone el dominio."""
    if not url or url.startswith(("http://", "https://")):
        return url
    if "buscalibre" in (tienda or "").lower():
        return urllib.parse.urljoin("https://www.buscalibre.cl/", url)
    return url


def normalizar(it, dominio_id, tienda):
    return {
        "id": it.get("id"),
        "title": it.get("title"),
        "dominio_id": dominio_id,
        "tienda": tienda,
        "precio": it.get("precio"),
        "promedio": it.get("promedio"),
        "descuento": round(-(it.get("variacion_promedio") or 0), 1),
        "variacion_ultimo_precio": it.get("variacion_ultimo_precio"),
        "thumbnail": it.get("thumbnail"),
        "author": it.get("author"),
        "url": arreglar_url(it.get("link") or it.get("url"), tienda),
    }


EXT = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
       "image/webp": "webp", "image/gif": "gif", "image/avif": "avif"}


def descargar_imagen(url, tienda):
    """Baja la imagen a RUTA_IMG y devuelve su ruta relativa (None si falla).
    El nombre sale de un hash de la URL: es estable entre ejecuciones."""
    if not url:
        return None
    if url.startswith("//"):
        url = "https:" + url
    url = arreglar_url(url, tienda)
    if not url.startswith(("http://", "https://")):
        return None
    nombre = hashlib.sha1(url.encode()).hexdigest()[:12]
    for f in os.listdir(RUTA_IMG):  # ya descargada en una corrida anterior
        if f.startswith(nombre + ".") and (Image is None or f.endswith(".webp")):
            return f"{CARPETA_IMG}/{f}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            ext = EXT.get(r.headers.get_content_type())
            datos = r.read(5_000_000)
        if not ext:
            return None  # la respuesta no era una imagen
        if Image is not None:  # miniatura WebP: ~3 KB en vez de ~30 KB
            im = Image.open(io.BytesIO(datos))
            im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
            im.thumbnail((ANCHO_IMG, ALTO_IMG), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "WEBP", quality=70, method=6)
            datos, ext = buf.getvalue(), "webp"
        archivo = f"{nombre}.{ext}"
        with open(os.path.join(RUTA_IMG, archivo), "wb") as f:
            f.write(datos)
        return f"{CARPETA_IMG}/{archivo}"
    except Exception as e:
        print(f"  ✗ imagen {url[:70]}: {e}")
        return None


def guardar_imagenes(libros):
    """Descarga las imágenes de los libros finales y reemplaza 'thumbnail' por la ruta local."""
    os.makedirs(RUTA_IMG, exist_ok=True)
    with ThreadPoolExecutor(8) as ex:
        rutas = list(ex.map(lambda b: descargar_imagen(b.get("thumbnail"), b["tienda"]), libros))
    for b, ruta in zip(libros, rutas):
        b["thumbnail"] = ruta
    usadas = {os.path.basename(r) for r in rutas if r}
    for f in os.listdir(RUTA_IMG):  # borra las que ya no se muestran
        if f not in usadas and re.fullmatch(r"[0-9a-f]{12}\.\w+", f):
            os.remove(os.path.join(RUTA_IMG, f))
    print(f"{len(usadas)} imágenes en {RUTA_IMG}")


def json_ld(libros):
    """Schema.org: ItemList > ListItem > Product/Book con su Offer."""
    items = []
    for i, b in enumerate(libros, 1):
        oferta = {
            "@type": "Offer",
            "price": str(round(b["precio"])),
            "priceCurrency": "CLP",
            "availability": "https://schema.org/InStock",
            "seller": {"@type": "Organization",
                       "name": NOMBRES_TIENDA.get(b["tienda"], b["tienda"])},
        }
        if b.get("url"):
            oferta["url"] = b["url"]
        libro = {"@type": ["Product", "Book"], "name": b["title"], "offers": oferta}
        autor = b.get("author")
        if isinstance(autor, str) and autor.strip():
            libro["author"] = {"@type": "Person", "name": autor.strip()}
        if SITE_URL and b.get("thumbnail"):
            libro["image"] = f"{SITE_URL}/{b['thumbnail']}"
        items.append({"@type": "ListItem", "position": i, "item": libro})
    return {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "name": "Los mejores descuentos de libros en Chile",
        "numberOfItems": len(items),
        "itemListElement": items,
    }


def inyectar_json_ld(libros):
    """Escribe el JSON-LD dentro del <head> de index.html (estático, sin depender de JS)."""
    if not os.path.exists(INDEX):
        print(f"(no encontré {INDEX}; me salto el JSON-LD)")
        return
    with open(INDEX, encoding="utf-8") as f:
        html = f.read()
    datos = json.dumps(json_ld(libros[:LD_MAX]), ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    bloque = f'<script type="application/ld+json" id="ld-books">{datos}</script>'
    if BLOQUE_LD.search(html):
        html = BLOQUE_LD.sub(lambda m: bloque, html)  # reemplaza el de la corrida anterior
    else:
        html = html.replace("</head>", bloque + "\n</head>", 1)
    tmp = INDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, INDEX)
    print(f"JSON-LD con {min(len(libros), LD_MAX)} libros inyectado en {INDEX}")


def main():
    if not TOKEN:
        sys.exit("Falta el token: export ONESOURCE_TOKEN=\"...\"")

    nombres = nombres_dominios()
    dominios, libros = [], []

    for d in DOMINIOS:
        nombre = nombres.get(d, f"dominio {d}")
        try:
            up = ultimo_upload(d)
            items = mejores_del_dominio(up["id"])
        except Exception as e:
            print(f"✗ {nombre} ({d}): {e}")
            continue
        validos = [normalizar(i, d, nombre) for i in items]
        validos = [b for b in validos if b["precio"] and b["descuento"] > 0]
        libros += validos
        dominios.append({"id": d, "name": nombre, "upload_id": up["id"],
                         "upload_created_at": up.get("created_at"),
                         "traidos": len(validos)})
        print(f"✓ {nombre} ({d}): upload {up['id']} -> {len(validos)} con descuento")

    libros.sort(key=lambda b: (-b["descuento"], b["precio"]))
    libros = libros[:TOP]
    guardar_imagenes(libros)

    salida = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "por_dominio": POR_DOMINIO,
        "total": len(libros),
        "dominios": dominios,
        "libros": [{k: v for k, v in b.items() if v is not None} for b in libros],
    }
    tmp = SALIDA + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(salida, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, SALIDA)  # escritura atómica: el html nunca lee un JSON a medias
    inyectar_json_ld(libros)
    print(f"\n{len(libros)} libros guardados en {SALIDA}")


if __name__ == "__main__":
    main()
