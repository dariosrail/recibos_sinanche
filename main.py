"""
Consulta de Recibos - H. Ayuntamiento de Sinanché
Lee la tabla TEARMO01 de MySQL (solo lectura).

Variables de entorno (Railway -> Variables):
  DB_URL    mysql://usuario:contraseña@host:puerto/base   (obligatoria)
  USUARIOS  usuarios con clave, uno por línea o separados por ;  (recomendado)
            formato:  numero|NOMBRE|clave
            ej.:      01|PRESIDENTE|1111;02|TESORERO|2222;03|SISTEMAS|3333
  PUEDEN_CANCELAR  números de usuario que pueden generar el NIP de cancelación (por defecto 01,02)
  NIP_FACTOR       multiplicador del NIP de cancelación (por defecto 9)
  APP_PIN   una sola clave general (solo si no usas USUARIOS)
  SECRET_KEY  (opcional) para que las sesiones no se cierren al redesplegar
  TABLA     nombre de la tabla (opcional, por defecto se busca TEARMO01)
  DEMO_CSV  ruta a un CSV exportado para probar sin MySQL (opcional)

Logo: sube un archivo llamado logo.png (o logo.jpg / logo.svg / logo.webp)
      junto a main.py en GitHub y aparece solo en el encabezado y el login.

Arranque:  python main.py
"""
import os
import io
import re
import csv
import hmac
import time
import datetime as dt
from collections import Counter, defaultdict
from urllib.parse import urlparse, unquote

from flask import (Flask, request, jsonify, send_file, session, redirect,
                   render_template_string, url_for)

DB_URL = os.getenv("DB_URL", "").strip()
APP_PIN = os.getenv("APP_PIN", "").strip()
TABLA_ENV = os.getenv("TABLA", "").strip()
DEMO_CSV = os.getenv("DEMO_CSV", "").strip()
MAX_FILAS = 20000
MAX_INTENTOS = 5          # intentos fallidos antes de bloquear
BLOQUEO_SEG = 5 * 60      # minutos de bloqueo


def cargar_usuarios():
    usuarios = {}
    crudo = os.getenv("USUARIOS", "").replace("\r", "")
    for parte in re.split(r"[;\n]+", crudo):
        campos = [c.strip() for c in parte.split("|")]
        if len(campos) == 3 and all(campos):
            usuarios[campos[0]] = {"nombre": campos[1], "clave": campos[2]}
    return usuarios


USUARIOS = cargar_usuarios()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def archivo_logo():
    for nombre in ("logo.png", "logo.jpg", "logo.jpeg", "logo.svg", "logo.webp"):
        ruta = os.path.join(BASE_DIR, nombre)
        if os.path.isfile(ruta):
            return ruta
    return None
PUEDEN_CANCELAR = {x.strip() for x in os.getenv("PUEDEN_CANCELAR", "01,02").split(",") if x.strip()}
NIP_FACTOR = int(os.getenv("NIP_FACTOR", "9") or 9)


def nip_cancelacion(recibo, importe):
    """(número de recibo sin letras + importe entero sin centavos) * factor"""
    digitos = re.sub(r"\D", "", str(recibo or "")) or "0"
    return (int(digitos) + int(num(importe))) * NIP_FACTOR
_intentos = {}

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or os.urandom(32)
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SECURE=bool(os.getenv("RAILWAY_ENVIRONMENT")))

COLUMNAS = ["id", "recibo", "fecha", "hora", "contribuyente", "concepto1", "concepto2",
            "importe", "descuento", "neto", "formapago", "status", "cuenta",
            "rfc", "direccion", "observaciones", "reftransfe"]


# ------------------------------------------------------------------ utilidades
def parse_url(url):
    s = (url or "").strip().strip('"').strip("'")
    i = s.find("mysql://")
    if i > 0:
        s = s[i:]
    u = urlparse(s)
    if u.scheme not in ("mysql", "mysql+pymysql") or not u.hostname:
        raise ValueError("DB_URL inválida: debe ser mysql://usuario:contraseña@host:puerto/base")
    return dict(host=u.hostname, port=u.port or 3306,
                user=unquote(u.username or "root"),
                password=unquote(u.password or ""),
                database=u.path.lstrip("/") or "railway")


def a_yymmdd(iso):
    d = dt.date.fromisoformat(iso)
    return d.strftime("%y%m%d")


def fecha_txt(valor):
    """Muestra dd/mm/aaaa venga como AAMMDD, AAAAMMDD, AAAA-MM-DD o tipo fecha."""
    if isinstance(valor, (dt.date, dt.datetime)):
        return valor.strftime("%d/%m/%Y")
    s = str(valor or "").strip()
    if len(s) == 6 and s.isdigit():
        return f"{s[4:6]}/{s[2:4]}/20{s[0:2]}"
    if len(s) == 8 and s.isdigit():
        return f"{s[6:8]}/{s[4:6]}/{s[0:4]}"
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return f"{s[8:10]}/{s[5:7]}/{s[0:4]}"
    return s


_formato_fecha = {}


def formato_fecha(conn, tabla):
    """Detecta cómo está guardada la columna fecha: 'yymmdd', 'yyyymmdd' o 'iso'."""
    if "f" not in _formato_fecha:
        with conn.cursor() as cur:
            cur.execute(f"SELECT `fecha` AS f FROM {tabla} ORDER BY `id` DESC LIMIT 20")
            valores = [r["f"] for r in cur.fetchall() if str(r["f"] or "").strip()]
        v = valores[0] if valores else None
        txt = str(v or "").strip()
        if isinstance(v, (dt.date, dt.datetime)) or (len(txt) >= 10 and txt[4] == "-"):
            _formato_fecha["f"] = "iso"
        elif len(txt) == 8 and txt.isdigit():
            _formato_fecha["f"] = "yyyymmdd"
        else:
            _formato_fecha["f"] = "yymmdd"
        print(f"[fecha] ejemplo={txt!r} formato={_formato_fecha['f']}", flush=True)
    return _formato_fecha["f"]


def rango_sql(desde, hasta, formato):
    a, b = sorted([dt.date.fromisoformat(desde), dt.date.fromisoformat(hasta)])
    if formato == "iso":
        return a.isoformat(), b.isoformat() + " 23:59:59"
    if formato == "yyyymmdd":
        return a.strftime("%Y%m%d"), b.strftime("%Y%m%d")
    return a.strftime("%y%m%d"), b.strftime("%y%m%d")


def num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def forma_pago(v):
    s = str(v or "").strip().upper()
    if "EFECTIVO" in s:
        return "EFECTIVO"
    if "TRANSF" in s:
        return "TRANSFERENCIA"
    if "CHEQUE" in s:
        return "CHEQUE"
    if "TARJETA" in s:
        return "TARJETA"
    return s or "SIN DATO"


def etiqueta_concepto(c):
    c = re.sub(r"\s+No\.?\s*\d+.*$", "", str(c or "").strip(), flags=re.I)
    return c[:70] or "SIN CONCEPTO"


def normalizar(r):
    concepto = str(r.get("concepto1") or "").strip()
    extra = str(r.get("concepto2") or "").strip()
    cancelado = str(r.get("status") or "").strip() == "1"
    return {
        "id": str(r.get("id") or "").strip(),
        "folio": str(r.get("recibo") or "").strip(),
        "fecha": fecha_txt(r.get("fecha")),
        "hora": str(r.get("hora") or "").strip(),
        "contribuyente": str(r.get("contribuyente") or "").strip(),
        "concepto": concepto,
        "detalle": extra,
        "importe": num(r.get("importe")),
        "descuento": num(r.get("descuento")),
        "neto": num(r.get("neto")),
        "forma_pago": forma_pago(r.get("formapago")),
        "cancelado": cancelado,
        "cuenta": str(r.get("cuenta") or "").strip(),
        "rfc": str(r.get("rfc") or "").strip(),
        "direccion": str(r.get("direccion") or "").strip(),
        "observaciones": str(r.get("observaciones") or "").strip(),
        "referencia": str(r.get("reftransfe") or "").strip(),
    }


# ------------------------------------------------------------------ datos
_tabla_cache = {}


def conectar(escritura=False):
    import pymysql
    from pymysql.cursors import DictCursor
    cfg = parse_url(DB_URL)
    conn = pymysql.connect(**cfg, cursorclass=DictCursor, connect_timeout=15,
                           read_timeout=90, charset="utf8mb4", autocommit=not escritura)
    if not escritura:
        with conn.cursor() as cur:
            cur.execute("SET SESSION TRANSACTION READ ONLY")
    return conn


def nombre_tabla(conn):
    if TABLA_ENV:
        return TABLA_ENV
    if "t" not in _tabla_cache:
        with conn.cursor() as cur:
            cur.execute("SELECT TABLE_NAME AS t FROM information_schema.TABLES "
                        "WHERE TABLE_SCHEMA = DATABASE() AND LOWER(TABLE_NAME) = 'tearmo01'")
            fila = cur.fetchone()
        _tabla_cache["t"] = fila["t"] if fila else "TEARMO01"
    return _tabla_cache["t"]


def consultar(desde, hasta, texto):
    d1, d2 = a_yymmdd(desde), a_yymmdd(hasta)
    if d1 > d2:
        d1, d2 = d2, d1
    texto = (texto or "").strip()

    if DEMO_CSV:
        with open(DEMO_CSV, encoding="utf-8-sig", newline="") as f:
            filas = [r for r in csv.DictReader(f) if d1 <= str(r["fecha"]).strip() <= d2]
        if texto:
            t = texto.upper()
            filas = [r for r in filas if t in r["contribuyente"].upper() or t in r["recibo"]]
        filas.sort(key=lambda r: (r["fecha"], r["hora"], r["recibo"]))
        return [normalizar(r) for r in filas[:MAX_FILAS]]

    conn = conectar()
    try:
        tabla = "`" + nombre_tabla(conn).replace("`", "``") + "`"
        d1, d2 = rango_sql(desde, hasta, formato_fecha(conn, tabla))
        sql = (f"SELECT {', '.join('`'+c+'`' for c in COLUMNAS)} FROM {tabla} "
               "WHERE `fecha` BETWEEN %s AND %s")
        params = [d1, d2]
        if texto:
            sql += " AND (`contribuyente` LIKE %s OR `recibo` LIKE %s)"
            params += [f"%{texto}%", f"%{texto}%"]
        sql += " ORDER BY `fecha`, `hora`, `recibo` LIMIT %s"
        params.append(MAX_FILAS)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [normalizar(r) for r in cur.fetchall()]
    finally:
        conn.close()


def buscar_folio(folio):
    folio = (folio or "").strip()
    if not folio:
        return []
    if DEMO_CSV:
        with open(DEMO_CSV, encoding="utf-8-sig", newline="") as f:
            todas = list(csv.DictReader(f))
        filas = [r for r in todas if r["recibo"].strip().upper() == folio.upper()]
        if not filas:
            filas = [r for r in todas if r["recibo"].strip().upper().endswith(folio.upper())][:10]
        return [normalizar(r) for r in filas]
    conn = conectar()
    try:
        tabla = "`" + nombre_tabla(conn).replace("`", "``") + "`"
        cols = ", ".join("`" + c + "`" for c in COLUMNAS)
        with conn.cursor() as cur:
            cur.execute(f"SELECT {cols} FROM {tabla} WHERE `recibo` = %s ORDER BY `fecha` DESC LIMIT 10", [folio])
            filas = cur.fetchall()
            if not filas:
                cur.execute(f"SELECT {cols} FROM {tabla} WHERE `recibo` LIKE %s ORDER BY `fecha` DESC LIMIT 10",
                            [f"%{folio}"])
                filas = cur.fetchall()
        return [normalizar(r) for r in filas]
    finally:
        conn.close()


def obtener_recibo(id_recibo):
    """Lee un recibo por su id (solo lectura)."""
    if DEMO_CSV:
        with open(DEMO_CSV, encoding="utf-8-sig", newline="") as f:
            r = next((x for x in csv.DictReader(f) if str(x["id"]) == str(id_recibo)), None)
        return r
    conn = conectar()
    try:
        tabla = "`" + nombre_tabla(conn).replace("`", "``") + "`"
        cols = ", ".join("`" + c + "`" for c in COLUMNAS)
        with conn.cursor() as cur:
            cur.execute(f"SELECT {cols} FROM {tabla} WHERE `id` = %s", [id_recibo])
            return cur.fetchone()
    finally:
        conn.close()


def registrar_autorizacion(r, usuario, motivo, ip):
    """Guarda quién generó el NIP, para qué recibo y por qué. Si falla, no bloquea."""
    if DEMO_CSV:
        return
    try:
        conn = conectar(escritura=True)
        try:
            with conn.cursor() as cur:
                cur.execute("""CREATE TABLE IF NOT EXISTS `autorizaciones_cancelacion` (
                    `id` INT AUTO_INCREMENT PRIMARY KEY,
                    `recibo_id` VARCHAR(30) NOT NULL,
                    `folio` VARCHAR(30),
                    `contribuyente` VARCHAR(255),
                    `importe` DECIMAL(14,2),
                    `usuario` VARCHAR(100),
                    `motivo` VARCHAR(255),
                    `ip` VARCHAR(64),
                    `fecha` DATETIME DEFAULT CURRENT_TIMESTAMP)""")
                cur.execute("INSERT INTO `autorizaciones_cancelacion` (`recibo_id`, `folio`, `contribuyente`, "
                            "`importe`, `usuario`, `motivo`, `ip`) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                            [str(r["id"]), str(r["recibo"]), str(r["contribuyente"])[:255], num(r["importe"]),
                             usuario, (motivo or "")[:255], ip])
            conn.commit()
        finally:
            conn.close()
    except Exception as ex:
        print(f"[nip] no se pudo guardar la bitácora: {ex}", flush=True)


def ultimo_recibo():
    """Fecha y folio del recibo más reciente, para orientar cuando un periodo sale vacío."""
    if DEMO_CSV:
        return None
    conn = conectar()
    try:
        tabla = "`" + nombre_tabla(conn).replace("`", "``") + "`"
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM {tabla}")
            n = cur.fetchone()["n"]
            cur.execute(f"SELECT `fecha`, `recibo` FROM {tabla} ORDER BY `id` DESC LIMIT 1")
            r = cur.fetchone()
        return {"total": int(n), "tabla": nombre_tabla(conn),
                "fecha": fecha_txt(r["fecha"]) if r else "", "folio": str(r["recibo"] if r else "").strip()}
    finally:
        conn.close()


def totales(filas):
    vig = [f for f in filas if not f["cancelado"]]
    return {
        "neto": round(sum(f["neto"] for f in vig), 2),
        "descuento": round(sum(f["descuento"] for f in vig), 2),
        "recibos": len(vig),
        "cancelados": len(filas) - len(vig),
    }


def resumen(filas):
    vig = [f for f in filas if not f["cancelado"]]
    por_pago = defaultdict(lambda: {"recibos": 0, "neto": 0.0})
    por_cuenta = defaultdict(lambda: {"recibos": 0, "neto": 0.0, "nombres": Counter()})
    for f in vig:
        p = por_pago[f["forma_pago"]]
        p["recibos"] += 1
        p["neto"] += f["neto"]
        c = por_cuenta[f["cuenta"] or "SIN CUENTA"]
        c["recibos"] += 1
        c["neto"] += f["neto"]
        c["nombres"][etiqueta_concepto(f["concepto"])] += 1
    pagos = [{"nombre": k, "recibos": v["recibos"], "neto": round(v["neto"], 2)}
             for k, v in por_pago.items()]
    conceptos = [{"cuenta": k, "nombre": v["nombres"].most_common(1)[0][0],
                  "recibos": v["recibos"], "neto": round(v["neto"], 2)}
                 for k, v in por_cuenta.items()]
    pagos.sort(key=lambda x: -x["neto"])
    conceptos.sort(key=lambda x: -x["neto"])
    return {"pagos": pagos, "conceptos": conceptos, "totales": totales(filas)}


def leer_filtros():
    hoy = dt.date.today().isoformat()
    return (request.args.get("desde") or hoy,
            request.args.get("hasta") or hoy,
            request.args.get("q") or "")


# ------------------------------------------------------------------ acceso
def ip_cliente():
    return (request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0]).strip()


@app.before_request
def exigir_login():
    if not (USUARIOS or APP_PIN) or request.endpoint in ("login", "static", "logo"):
        return None
    if session.get("usuario"):
        return None
    if request.path.startswith("/api/"):
        return jsonify(error="Sesión expirada, vuelve a entrar."), 401
    return redirect(url_for("login"))


@app.route("/entrar", methods=["GET", "POST"])
def login():
    if not (USUARIOS or APP_PIN):
        return redirect(url_for("inicio"))
    error = ""
    elegido = ""
    if request.method == "POST":
        elegido = request.form.get("usuario", "").strip()
        clave = request.form.get("clave", "").strip()
        llave = (ip_cliente(), elegido)
        n, hasta = _intentos.get(llave, (0, 0))
        if hasta > time.time():
            mins = int((hasta - time.time()) // 60) + 1
            error = f"Demasiados intentos. Espera {mins} min y vuelve a intentar."
        else:
            if USUARIOS:
                u = USUARIOS.get(elegido)
                ok = bool(u) and hmac.compare_digest(clave, u["clave"])
                datos = {"num": elegido, "nombre": u["nombre"]} if ok else None
            else:
                ok = hmac.compare_digest(clave, APP_PIN)
                datos = {"num": "", "nombre": ""} if ok else None
            if ok:
                _intentos.pop(llave, None)
                session.clear()
                session["usuario"] = datos
                session.permanent = True
                print(f"[acceso] OK usuario={elegido} ip={ip_cliente()}", flush=True)
                return redirect(url_for("inicio"))
            n += 1
            _intentos[llave] = (n, time.time() + BLOQUEO_SEG if n >= MAX_INTENTOS else 0)
            print(f"[acceso] FALLÓ usuario={elegido} ip={ip_cliente()} intento={n}", flush=True)
            error = "Usuario o clave incorrecta."
            if n >= MAX_INTENTOS:
                error = "Demasiados intentos. Espera 5 min y vuelve a intentar."
    lista = [(k, v["nombre"]) for k, v in sorted(USUARIOS.items())]
    return render_template_string(LOGIN_HTML, error=error, css=CSS, usuarios=lista, elegido=elegido,
                                  tiene_logo=bool(archivo_logo()))


@app.route("/logo")
def logo():
    ruta = archivo_logo()
    if not ruta:
        return "", 404
    return send_file(ruta, max_age=3600)


@app.route("/salir")
def salir():
    session.clear()
    return redirect(url_for("login"))


def usuario_actual():
    u = session.get("usuario") or {}
    return u.get("nombre", "")


def puede_cancelar():
    u = session.get("usuario") or {}
    return bool(USUARIOS) and u.get("num") in PUEDEN_CANCELAR


@app.route("/api/folio")
def api_folio():
    if not puede_cancelar():
        return jsonify(error="No tienes permiso para cancelar recibos."), 403
    try:
        return jsonify(filas=buscar_folio(request.args.get("folio", "")))
    except Exception as ex:
        return jsonify(error=f"No se pudo buscar: {ex}"), 500


@app.route("/api/nip", methods=["POST"])
def api_nip():
    if not puede_cancelar():
        return jsonify(error="No tienes permiso para autorizar cancelaciones."), 403
    if request.headers.get("X-Requested-With") != "fetch" or not request.is_json:
        return jsonify(error="Solicitud no válida."), 400
    datos = request.get_json(silent=True) or {}
    id_recibo = str(datos.get("id", "")).strip()
    if not id_recibo:
        return jsonify(error="Falta el recibo."), 400
    try:
        r = obtener_recibo(id_recibo)
    except Exception as ex:
        return jsonify(error=f"No se pudo leer el recibo: {ex}"), 500
    if not r:
        return jsonify(error="No se encontró el recibo."), 404
    if str(r.get("status") or "").strip() == "1":
        return jsonify(error="Ese recibo ya está cancelado."), 409
    u = session.get("usuario") or {}
    quien = f"{u.get('num', '')} {u.get('nombre', '')}".strip()
    motivo = str(datos.get("motivo", "")).strip()
    nip = nip_cancelacion(r["recibo"], r["importe"])
    registrar_autorizacion(r, quien, motivo, ip_cliente())
    print(f"[nip] generado folio={r['recibo']} recibo_id={id_recibo} usuario={quien} ip={ip_cliente()}", flush=True)
    return jsonify(ok=True, nip=str(nip), folio=str(r["recibo"]).strip())


# ------------------------------------------------------------------ rutas
@app.route("/")
def inicio():
    return render_template_string(PAGE_HTML, css=CSS, configurado=bool(DB_URL or DEMO_CSV),
                                  usuario=usuario_actual(), con_login=bool(USUARIOS or APP_PIN),
                                  puede_cancelar=puede_cancelar(), tiene_logo=bool(archivo_logo()))


@app.route("/api/recibos")
def api_recibos():
    try:
        filas = consultar(*leer_filtros())
    except Exception as ex:
        return jsonify(error=f"No se pudo consultar: {ex}"), 500
    ultimo = None
    if not filas:
        try:
            ultimo = ultimo_recibo()
        except Exception as ex:
            print(f"[ultimo] {ex}", flush=True)
    return jsonify(filas=filas, totales=totales(filas), limite=len(filas) >= MAX_FILAS, ultimo=ultimo)


@app.route("/api/resumen")
def api_resumen():
    try:
        filas = consultar(*leer_filtros())
    except Exception as ex:
        return jsonify(error=f"No se pudo consultar: {ex}"), 500
    return jsonify(resumen(filas))


def nombre_archivo(desde, hasta, ext):
    return f"recibos_{desde}_a_{hasta}.{ext}"


@app.route("/excel")
def excel():
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    desde, hasta, q = leer_filtros()
    filas = consultar(desde, hasta, q)
    t = totales(filas)

    wb = Workbook()
    ws = wb.active
    ws.title = "Recibos"
    ws["A1"] = "H. Ayuntamiento de Sinanché - Consulta de Recibos"
    ws["A1"].font = Font(bold=True, size=14, color="0054A6")
    ws["A2"] = f"Del {fecha_txt(a_yymmdd(desde))} al {fecha_txt(a_yymmdd(hasta))}" + (f"  ·  Contribuyente: {q}" if q else "")
    ws["A3"] = (f"Total neto: ${t['neto']:,.2f}   Descuento: ${t['descuento']:,.2f}   "
                f"Recibos: {t['recibos']}   Cancelados: {t['cancelados']}"
                + (f"   ·   Generado por: {usuario_actual()}" if usuario_actual() else ""))

    enc = ["Folio", "Fecha", "Hora", "Contribuyente", "Concepto", "Detalle", "Importe",
           "Descuento", "Neto", "Forma de pago", "Estado", "Cuenta", "RFC", "Observaciones"]
    ws.append([])
    ws.append(enc)
    fila_enc = ws.max_row
    for c in ws[fila_enc]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="0054A6")
        c.alignment = Alignment(vertical="center")
    rojo = Font(color="C0392B")
    for f in filas:
        ws.append([f["folio"], f["fecha"], f["hora"], f["contribuyente"], f["concepto"],
                   f["detalle"], f["importe"], f["descuento"], f["neto"], f["forma_pago"],
                   "CANCELADO" if f["cancelado"] else "VIGENTE", f["cuenta"], f["rfc"],
                   f["observaciones"]])
        if f["cancelado"]:
            for c in ws[ws.max_row]:
                c.font = rojo
    for col in (7, 8, 9):
        for c in ws.iter_rows(min_row=fila_enc + 1, min_col=col, max_col=col):
            c[0].number_format = '"$"#,##0.00'
    anchos = [9, 11, 9, 40, 45, 30, 12, 12, 12, 15, 12, 15, 15, 30]
    for i, w in enumerate(anchos, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = ws.cell(row=fila_enc + 1, column=1)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=nombre_archivo(desde, hasta, "xlsx"),
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/pdf")
def pdf():
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape

    desde, hasta, q = leer_filtros()
    filas = consultar(desde, hasta, q)
    t = totales(filas)

    vino = colors.HexColor("#0054A6")
    oro = colors.HexColor("#3C8DDB")
    st_t = ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=15, textColor=vino, leading=18)
    st_s = ParagraphStyle("s", fontName="Helvetica", fontSize=9.5, leading=13)
    st_c = ParagraphStyle("c", fontName="Helvetica", fontSize=7.5, leading=9)
    st_cr = ParagraphStyle("cr", parent=st_c, textColor=colors.HexColor("#C0392B"))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(letter), leftMargin=1.2 * cm,
                            rightMargin=1.2 * cm, topMargin=1.2 * cm, bottomMargin=1.2 * cm,
                            title="Consulta de Recibos")
    periodo = f"Del {fecha_txt(a_yymmdd(desde))} al {fecha_txt(a_yymmdd(hasta))}"
    if q:
        periodo += f" · Contribuyente: {escape(q)}"
    story = [
        Paragraph("H. Ayuntamiento de Sinanché · Consulta de Recibos", st_t),
        Paragraph(periodo, st_s),
        Paragraph(f"<b>Total neto: ${t['neto']:,.2f}</b> &nbsp;&nbsp; Descuento: ${t['descuento']:,.2f}"
                  f" &nbsp;&nbsp; Recibos: {t['recibos']} &nbsp;&nbsp; "
                  f"<font color='#C0392B'>Cancelados: {t['cancelados']}</font>", st_s),
        Spacer(1, 8),
    ]
    data = [["Folio", "Fecha", "Contribuyente", "Concepto", "Neto", "Forma de pago", "Descuento"]]
    estilos = [
        ("BACKGROUND", (0, 0), (-1, 0), vino),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (4, 1), (4, -1), "RIGHT"),
        ("ALIGN", (6, 1), (6, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, 0), 1.2, oro),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#EEF3F9")]),
    ]
    for i, f in enumerate(filas, 1):
        ps = st_cr if f["cancelado"] else st_c
        concepto = escape(f["concepto"]) + (" <b>(CANCELADO)</b>" if f["cancelado"] else "")
        data.append([f["folio"], f["fecha"], Paragraph(escape(f["contribuyente"]), ps),
                     Paragraph(concepto, ps), f"${f['neto']:,.2f}", f["forma_pago"],
                     f"${f['descuento']:,.2f}"])
        if f["cancelado"]:
            estilos.append(("TEXTCOLOR", (0, i), (-1, i), colors.HexColor("#C0392B")))
    if not filas:
        data.append(["", "", "Sin recibos en este periodo", "", "", "", ""])
    tabla = Table(data, repeatRows=1,
                  colWidths=[1.6 * cm, 2 * cm, 6.8 * cm, 8.6 * cm, 2.4 * cm, 2.8 * cm, 2.2 * cm])
    tabla.setStyle(TableStyle(estilos))
    story.append(tabla)

    quien = usuario_actual()

    def pie(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.grey)
        canvas.drawRightString(landscape(letter)[0] - 1.2 * cm, 0.7 * cm,
                               f"Página {doc_.page} · generado {dt.datetime.now():%d/%m/%Y %H:%M}"
                               + (f" por {quien}" if quien else ""))
        canvas.restoreState()

    doc.build(story, onFirstPage=pie, onLaterPages=pie)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=nombre_archivo(desde, hasta, "pdf"),
                     mimetype="application/pdf")


# ------------------------------------------------------------------ HTML
CSS = """
:root{
  --vino:#0054A6; --vino-osc:#003D7A; --oro:#3C8DDB; --verde:#002B5C;
  --fondo:#EEF3F9; --tinta:#1E2733; --gris:#5F6B7A; --linea:#DCE4EE; --rojo:#C0392B; --ok:#1E8A55;
}
*{box-sizing:border-box}
html,body{margin:0}
body{font-family:"Source Sans 3","Segoe UI",Roboto,Arial,sans-serif;background:var(--fondo);color:var(--tinta);font-size:16px}
h1,h2,h3,.marca{font-family:"Montserrat","Segoe UI",Arial,sans-serif}
.ancho{max-width:1120px;margin:0 auto;padding:0 20px}
header.top{background:#fff;border-top:4px solid var(--verde);border-bottom:3px solid var(--oro)}
header.top .ancho{display:flex;align-items:center;justify-content:space-between;min-height:82px;gap:16px}
.marca{display:flex;align-items:center;gap:14px;text-decoration:none}
.logo-img{height:56px;width:auto;max-width:130px;object-fit:contain;display:block}
.login .logo-img{height:84px;max-width:200px;margin:0 auto 12px}
.escudo{width:44px;height:44px;border-radius:50%;background:var(--vino);color:#fff;display:grid;place-items:center;font-weight:800;font-size:15px;border:2px solid var(--oro)}
.marca b{display:block;color:var(--vino);font-size:19px;letter-spacing:.06em}
.marca small{display:block;color:var(--gris);font-size:11.5px;font-weight:700;letter-spacing:.08em;margin-top:2px}
nav a{color:var(--tinta);text-decoration:none;font-weight:600;font-size:14px;margin-left:28px;padding-bottom:4px}
nav a.activo{border-bottom:2px solid var(--vino);color:var(--vino)}
.banda{background:var(--vino);border-bottom:4px solid var(--oro);color:#fff;padding:26px 0 30px}
.banda h1{margin:0 0 16px;font-weight:500;font-size:30px}
.filtros{display:grid;grid-template-columns:170px 170px 1fr auto auto auto;gap:14px;align-items:end}
.filtros label{display:block;font-size:14px;opacity:.9;margin-bottom:5px}
.filtros input{width:100%;height:42px;border:0;border-radius:3px;padding:0 12px;font:inherit;font-size:17px;color:var(--tinta)}
.btn{height:42px;border:0;border-radius:4px;padding:0 24px;font:inherit;font-weight:700;font-size:17px;cursor:pointer;white-space:nowrap}
.btn:focus-visible,.filtros input:focus-visible,.vista button:focus-visible{outline:3px solid var(--oro);outline-offset:2px}
.btn-buscar{background:var(--verde);color:#fff}
.btn-resumen{background:var(--oro);color:#fff}
.btn-hoy{background:transparent;color:#fff;border:1.5px solid #fff;font-weight:500}
.btn:hover{filter:brightness(1.08)}
main{padding:26px 0 50px}
.totales{background:#fff;border-left:5px solid var(--oro);border-radius:4px;padding:18px 20px;box-shadow:0 1px 3px rgba(0,40,90,.08)}
.totales .neto{font-size:22px}
.totales .neto span{color:var(--ok)}
.totales .desc{font-size:17px;margin-top:6px}
.totales .cuentas{font-size:14px;color:var(--gris);margin-top:8px}
.totales .cuentas .canc{color:var(--rojo)}
.barra{display:flex;justify-content:flex-end;align-items:center;gap:12px;margin:20px 0 16px;flex-wrap:wrap}
.vista{display:inline-flex;border:1px solid #B3C2D4;border-radius:4px;overflow:hidden}
.vista button{background:#fff;border:0;padding:7px 14px;font:inherit;font-size:14px;cursor:pointer;color:var(--gris)}
.vista button+button{border-left:1px solid #B3C2D4}
.vista button.on{background:#E1ECF8;color:var(--tinta);font-weight:600}
.btn-desc{height:auto;min-height:52px;padding:6px 18px;font-size:15px;font-weight:600;background:#fff;line-height:1.25}
.btn-xls{border:1.5px solid var(--ok);color:var(--ok)}
.btn-pdf{border:1.5px solid var(--rojo);color:var(--rojo)}
.mensaje{padding:28px;text-align:center;color:var(--gris)}
.mensaje.error{color:var(--rojo)}
.tabla-wrap{overflow-x:auto;background:#fff;border-left:4px solid var(--oro);border-radius:4px;box-shadow:0 1px 3px rgba(0,40,90,.08)}
table{border-collapse:collapse;width:100%;font-size:14px}
th{text-align:left;font-size:12.5px;font-weight:700;letter-spacing:.03em;padding:12px 10px;white-space:nowrap;border-bottom:2px solid var(--linea);position:sticky;top:0;background:#fff}
td{padding:10px;border-bottom:1px solid var(--linea);vertical-align:top}
td.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
tr.fila{cursor:pointer}
tr.fila:hover td{background:#F2F7FD}
tr.cancelado td{color:var(--rojo);text-decoration:line-through;text-decoration-thickness:1px}
tr.cancelado td.estado{text-decoration:none}
.tag{display:inline-block;font-size:11px;font-weight:700;padding:2px 7px;border-radius:3px;background:#FBE9E7;color:var(--rojo);text-decoration:none}
.tarjetas{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px}
.tarjeta{background:#fff;border-radius:4px;padding:14px 16px;border-top:3px solid var(--vino);box-shadow:0 1px 3px rgba(0,40,90,.08);cursor:pointer}
.tarjeta.cancelado{border-top-color:var(--rojo);opacity:.85}
.tarjeta .arriba{display:flex;justify-content:space-between;font-size:13px;color:var(--gris)}
.tarjeta .nombre{font-weight:700;margin:6px 0 4px;line-height:1.25}
.tarjeta .concepto{font-size:13.5px;color:var(--gris);line-height:1.3}
.tarjeta .abajo{display:flex;justify-content:space-between;align-items:baseline;margin-top:10px}
.tarjeta .monto{font-size:20px;font-weight:700;color:var(--ok);font-variant-numeric:tabular-nums}
.tarjeta.cancelado .monto{color:var(--rojo);text-decoration:line-through}
.tarjeta .pago{font-size:12px;color:var(--gris)}
dialog{border:0;border-radius:6px;padding:0;max-width:720px;width:calc(100% - 32px);box-shadow:0 10px 40px rgba(0,0,0,.25)}
dialog::backdrop{background:rgba(0,20,50,.45)}
.dlg-cab{background:var(--vino);color:#fff;padding:14px 20px;display:flex;justify-content:space-between;align-items:center;border-bottom:3px solid var(--oro)}
.dlg-cab h2{margin:0;font-size:19px;font-weight:600}
.dlg-cab button{background:none;border:0;color:#fff;font-size:26px;cursor:pointer;line-height:1}
.dlg-cuerpo{padding:18px 20px;max-height:70vh;overflow:auto}
.dlg-cuerpo h3{font-size:15px;color:var(--vino);margin:18px 0 8px}
.dlg-cuerpo h3:first-child{margin-top:0}
.det{display:grid;grid-template-columns:140px 1fr;gap:6px 14px;font-size:14.5px}
.det dt{color:var(--gris)}
.det dd{margin:0;word-break:break-word}
.login{min-height:100vh;display:grid;place-items:center;background:var(--vino)}
.login form{background:#fff;padding:28px;border-radius:6px;border-top:4px solid var(--oro);width:min(360px,92vw)}
.login h1{font-size:20px;color:var(--vino);margin:0 0 14px}
.login label{display:block;font-size:14px;color:var(--gris);margin-bottom:5px}
.login input,.login select{width:100%;height:44px;border:1px solid #ccc;border-radius:4px;padding:0 12px;font:inherit;font-size:17px;margin-bottom:14px;background:#fff}
.login .sub{color:var(--gris);font-size:14px;margin:-8px 0 18px}
.sesion{display:flex;align-items:center;gap:14px;font-size:14px}
.sesion .quien{color:var(--gris)}
.sesion .quien b{color:var(--tinta)}
.btn-cancelar-rec{background:#fff;color:var(--rojo);border:1.5px solid var(--rojo);border-radius:4px;padding:5px 12px;font:inherit;font-size:14px;font-weight:600;cursor:pointer}
.btn-cancelar-rec:hover{background:#FBE9E7}
.form-folio{display:flex;gap:10px;margin-bottom:14px}
.form-folio input,.campo-motivo{flex:1;height:42px;border:1px solid #ccc;border-radius:4px;padding:0 12px;font:inherit;font-size:17px;width:100%}
.opcion-folio{display:block;width:100%;text-align:left;background:#fff;border:1px solid var(--linea);border-radius:4px;padding:10px 12px;margin-bottom:8px;font:inherit;cursor:pointer}
.opcion-folio:hover{border-color:var(--vino)}
.acciones{display:flex;gap:10px;justify-content:flex-end;margin-top:16px;flex-wrap:wrap}
.btn-rojo{background:var(--rojo);color:#fff}
.btn-gris{background:#E2E8F0;color:var(--tinta)}
.aviso-confirma{background:#FBE9E7;border-left:4px solid var(--rojo);padding:14px 16px;border-radius:4px;font-size:16px;line-height:1.45}
.nip-caja{text-align:center;border:2px dashed var(--oro);border-radius:6px;padding:18px 14px;background:#F4F9FF}
.nip-tit{font-size:14px;color:var(--gris);font-weight:600;letter-spacing:.04em}
.nip-num{font-family:"Montserrat",Arial,sans-serif;font-size:46px;font-weight:800;color:var(--vino);letter-spacing:.12em;margin:6px 0;font-variant-numeric:tabular-nums;user-select:all}
.nip-sub{font-size:14px;color:var(--tinta)}
.ok-msg{background:#E7F5EC;border-left:4px solid var(--ok);padding:14px 16px;border-radius:4px;color:#145C38}
.sesion a.salir{color:var(--vino);font-weight:600;text-decoration:none;border:1px solid var(--vino);border-radius:4px;padding:5px 12px}
@media (max-width:900px){
  .filtros{grid-template-columns:1fr 1fr}
  .filtros .campo-texto{grid-column:1/-1}
  .filtros .btn{width:100%}
  .filtros .btn-hoy{grid-column:1/-1}
  nav a{margin-left:16px}
  .banda h1{font-size:25px}
}
@media (max-width:520px){
  .marca small{display:none}
  nav{display:none}
  .sesion .quien{display:none}
  .barra{display:grid;grid-template-columns:1fr 1fr}
  .barra .lbl-vista{display:none}
  .barra .vista{grid-column:1/-1}
  .barra .vista button{flex:1;padding:10px}
  .det{grid-template-columns:1fr}
  .det dt{font-weight:700;color:var(--vino)}
}
"""

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
         '<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@500;600;700;800'
         '&family=Source+Sans+3:wght@400;600;700&display=swap" rel="stylesheet">')

LOGIN_HTML = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Entrar · Consulta de Recibos</title>""" + FONTS + """
{% if tiene_logo %}<link rel="icon" href="/logo">{% endif %}<style>{{ css|safe }}</style></head>
<body><div class="login"><form method="post" autocomplete="off">
{% if tiene_logo %}<img class="logo-img" src="/logo" alt="Escudo de Sinanché">{% endif %}
<h1>Consulta de Recibos</h1>
<p class="sub">H. Ayuntamiento de Sinanché</p>
{% if usuarios %}
<label for="usuario">Usuario</label>
<select id="usuario" name="usuario" required>
  <option value="" disabled {% if not elegido %}selected{% endif %}>Elige tu usuario</option>
  {% for num, nombre in usuarios %}
  <option value="{{ num }}" {% if num == elegido %}selected{% endif %}>{{ num }} · {{ nombre }}</option>
  {% endfor %}
</select>
{% endif %}
<label for="clave">Clave</label>
<input type="password" id="clave" name="clave" inputmode="numeric" required {% if not usuarios or elegido %}autofocus{% endif %}>
{% if error %}<p style="color:var(--rojo);margin:0 0 12px">{{ error }}</p>{% endif %}
<button class="btn btn-buscar" style="width:100%">Entrar</button>
</form></div></body></html>"""

PAGE_HTML = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Consulta de Recibos · Sinanché</title>""" + FONTS + """
{% if tiene_logo %}<link rel="icon" href="/logo">{% endif %}<style>{{ css|safe }}</style></head>
<body>
<header class="top"><div class="ancho">
  <a class="marca" href="/">{% if tiene_logo %}<img class="logo-img" src="/logo" alt="Escudo de Sinanché">{% else %}<div class="escudo">SN</div>{% endif %}
    <div><b>SINANCHÉ</b><small>H. AYUNTAMIENTO</small></div></a>
  <div class="sesion">
    <nav><a class="activo" href="/">RECIBOS</a></nav>
    {% if puede_cancelar %}<button type="button" class="btn-cancelar-rec" id="btnCancelarRec">Cancelar recibo</button>{% endif %}
    {% if con_login %}
      {% if usuario %}<span class="quien">Usuario: <b>{{ usuario }}</b></span>{% endif %}
      <a class="salir" href="/salir">Salir</a>
    {% endif %}
  </div>
</div></header>

<section class="banda"><div class="ancho">
  <h1>Consulta de Recibos</h1>
  <form class="filtros" id="filtros">
    <div><label for="desde">Desde:</label><input type="date" id="desde" required></div>
    <div><label for="hasta">Hasta:</label><input type="date" id="hasta" required></div>
    <div class="campo-texto"><label for="q">Contribuyente o folio:</label>
      <input type="text" id="q" placeholder="Nombre opcional..." autocomplete="off"></div>
    <button class="btn btn-buscar" type="submit">Buscar</button>
    <button class="btn btn-resumen" type="button" id="btnResumen">Resumen</button>
    <button class="btn btn-hoy" type="button" id="btnHoy">Ir a Hoy</button>
  </form>
</div></section>

<main><div class="ancho">
  <div class="totales" aria-live="polite">
    <div class="neto">Total Neto: <span id="tNeto">$0.00</span></div>
    <div class="desc">Total Descuento: <span id="tDesc">$0.00</span></div>
    <div class="cuentas">Recibos: <span id="tRec">0</span> | <span class="canc">Cancelados: <span id="tCan">0</span></span></div>
  </div>

  <div class="barra">
    <span class="lbl-vista" style="color:var(--gris);font-size:15px">Vista en:</span>
    <div class="vista" role="group" aria-label="Tipo de vista">
      <button type="button" id="vTarjetas">Tarjetas</button><button type="button" id="vTabla" class="on">Tabla</button>
    </div>
    <button class="btn btn-desc btn-xls" type="button" id="btnExcel">Descargar<br>EXCEL</button>
    <button class="btn btn-desc btn-pdf" type="button" id="btnPdf">Descargar<br>PDF</button>
  </div>

  <div id="resultados"></div>
</div></main>

<dialog id="dlg"><div class="dlg-cab"><h2 id="dlgTitulo"></h2>
  <button type="button" aria-label="Cerrar" onclick="dlg.close()">×</button></div>
  <div class="dlg-cuerpo" id="dlgCuerpo"></div></dialog>

<script>
const CONFIGURADO = {{ 'true' if configurado else 'false' }};
const PUEDE_CANCELAR = {{ 'true' if puede_cancelar else 'false' }};
const $ = id => document.getElementById(id);
const dlg = $("dlg");
let filas = [], vista = "tabla", ULTIMO = null;

const dinero = n => "$" + Number(n || 0).toLocaleString("es-MX", {minimumFractionDigits: 2, maximumFractionDigits: 2});
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const hoyISO = () => { const d = new Date(); d.setMinutes(d.getMinutes() - d.getTimezoneOffset()); return d.toISOString().slice(0, 10); };
const params = () => new URLSearchParams({desde: $("desde").value, hasta: $("hasta").value, q: $("q").value.trim()});

function mensaje(txt, error) {
  $("resultados").innerHTML = `<div class="mensaje${error ? " error" : ""}">${esc(txt)}</div>`;
}

async function pedir(url) {
  const r = await fetch(url);
  if (r.status === 401) { location.href = "/entrar"; throw new Error("Sesión expirada"); }
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || "Error al consultar");
  return data;
}

async function buscar() {
  if (!CONFIGURADO) return mensaje("Falta configurar la variable DB_URL en Railway.", true);
  mensaje("Buscando recibos...");
  try {
    const data = await pedir("/api/recibos?" + params());
    filas = data.filas;
    ULTIMO = data.ultimo;
    const t = data.totales;
    $("tNeto").textContent = dinero(t.neto);
    $("tDesc").textContent = dinero(t.descuento);
    $("tRec").textContent = t.recibos;
    $("tCan").textContent = t.cancelados;
    pintar();
    if (data.limite) $("resultados").insertAdjacentHTML("afterbegin",
      '<div class="mensaje">Se muestran los primeros 20,000 recibos. Acorta el rango de fechas para ver todos.</div>');
  } catch (e) { mensaje(e.message, true); }
}

function pintar() {
  if (!filas.length) {
    let txt = "No hay recibos en este periodo. Cambia las fechas y toca Buscar.";
    if (ULTIMO) txt += ULTIMO.total
      ? ` (La tabla ${ULTIMO.tabla} tiene ${Number(ULTIMO.total).toLocaleString("es-MX")} recibos; el último es el folio ${ULTIMO.folio} del ${ULTIMO.fecha}.)`
      : ` (La tabla ${ULTIMO.tabla} está vacía en esta base.)`;
    return mensaje(txt);
  }
  if (vista === "tabla") {
    $("resultados").innerHTML = `<div class="tabla-wrap"><table><thead><tr>
      <th>FOLIO</th><th>FECHA</th><th>CONTRIBUYENTE</th><th>CONCEPTO</th>
      <th style="text-align:right">MONTO NETO</th><th>FORMA DE PAGO</th><th style="text-align:right">DESCUENTO</th></tr></thead><tbody>` +
      filas.map((f, i) => `<tr class="fila${f.cancelado ? " cancelado" : ""}" data-i="${i}">
        <td>${esc(f.folio)}</td><td>${esc(f.fecha)}</td><td>${esc(f.contribuyente)}</td>
        <td>${esc(f.concepto)}${f.cancelado ? ' <span class="tag estado">CANCELADO</span>' : ""}</td>
        <td class="num">${dinero(f.neto)}</td><td>${esc(f.forma_pago)}</td><td class="num">${dinero(f.descuento)}</td></tr>`).join("") +
      "</tbody></table></div>";
  } else {
    $("resultados").innerHTML = '<div class="tarjetas">' + filas.map((f, i) => `
      <div class="tarjeta${f.cancelado ? " cancelado" : ""}" data-i="${i}" tabindex="0">
        <div class="arriba"><span>Folio ${esc(f.folio)}</span><span>${esc(f.fecha)} ${esc(f.hora)}</span></div>
        <div class="nombre">${esc(f.contribuyente)}</div>
        <div class="concepto">${esc(f.concepto)}</div>
        <div class="abajo"><span class="monto">${dinero(f.neto)}</span>
          <span class="pago">${f.cancelado ? '<span class="tag">CANCELADO</span>' : esc(f.forma_pago)}</span></div>
      </div>`).join("") + "</div>";
  }
}

function verDetalle(f) {
  const campos = [["Folio", f.folio], ["Fecha", f.fecha + " " + f.hora], ["Estado", f.cancelado ? "CANCELADO" : "Vigente"],
    ["Contribuyente", f.contribuyente], ["RFC", f.rfc], ["Dirección", f.direccion], ["Concepto", f.concepto],
    ["Detalle", f.detalle], ["Importe", dinero(f.importe)], ["Descuento", dinero(f.descuento)], ["Neto", dinero(f.neto)],
    ["Forma de pago", f.forma_pago], ["Referencia", f.referencia], ["Cuenta", f.cuenta], ["Observaciones", f.observaciones]];
  $("dlgTitulo").textContent = "Recibo " + f.folio;
  $("dlgCuerpo").innerHTML = '<dl class="det">' + campos.filter(c => c[1] && String(c[1]).trim())
    .map(c => `<dt>${esc(c[0])}</dt><dd>${esc(c[1])}</dd>`).join("") + "</dl>";
  dlg.showModal();
}

async function verResumen() {
  if (!CONFIGURADO) return mensaje("Falta configurar la variable DB_URL en Railway.", true);
  $("dlgTitulo").textContent = "Resumen del periodo";
  $("dlgCuerpo").innerHTML = '<div class="mensaje">Calculando...</div>';
  dlg.showModal();
  try {
    const r = await pedir("/api/resumen?" + params());
    const tabla = (enc, items, fn) => `<div class="tabla-wrap"><table><thead><tr>${enc}</tr></thead><tbody>${items.map(fn).join("")}</tbody></table></div>`;
    $("dlgCuerpo").innerHTML = `
      <h3>Total neto ${dinero(r.totales.neto)} · ${r.totales.recibos} recibos · ${r.totales.cancelados} cancelados</h3>
      <h3>Por forma de pago</h3>` +
      tabla('<th>FORMA DE PAGO</th><th style="text-align:right">RECIBOS</th><th style="text-align:right">NETO</th>', r.pagos,
        p => `<tr><td>${esc(p.nombre)}</td><td class="num">${p.recibos}</td><td class="num">${dinero(p.neto)}</td></tr>`) +
      "<h3>Por concepto (cuenta)</h3>" +
      tabla('<th>CUENTA</th><th>CONCEPTO</th><th style="text-align:right">RECIBOS</th><th style="text-align:right">NETO</th>', r.conceptos,
        c => `<tr><td>${esc(c.cuenta)}</td><td>${esc(c.nombre)}</td><td class="num">${c.recibos}</td><td class="num">${dinero(c.neto)}</td></tr>`);
    if (!r.pagos.length) $("dlgCuerpo").innerHTML = '<div class="mensaje">No hay recibos vigentes en este periodo.</div>';
  } catch (e) { $("dlgCuerpo").innerHTML = `<div class="mensaje error">${esc(e.message)}</div>`; }
}

function cambiarVista(v) {
  vista = v;
  $("vTabla").classList.toggle("on", v === "tabla");
  $("vTarjetas").classList.toggle("on", v === "tarjetas");
  if (filas.length) pintar();
}

$("filtros").addEventListener("submit", e => { e.preventDefault(); buscar(); });
$("btnHoy").onclick = () => { $("desde").value = $("hasta").value = hoyISO(); buscar(); };
$("btnResumen").onclick = verResumen;
$("vTabla").onclick = () => cambiarVista("tabla");
$("vTarjetas").onclick = () => cambiarVista("tarjetas");
$("btnExcel").onclick = () => { location.href = "/excel?" + params(); };
$("btnPdf").onclick = () => { location.href = "/pdf?" + params(); };
$("resultados").addEventListener("click", e => {
  const el = e.target.closest("[data-i]"); if (el) verDetalle(filas[+el.dataset.i]);
});
$("resultados").addEventListener("keydown", e => {
  const el = e.target.closest("[data-i]"); if (el && e.key === "Enter") verDetalle(filas[+el.dataset.i]);
});

// ---------------- Cancelar recibo
let recSel = null;
function datosRecibo(f) {
  return '<dl class="det">' + [["Folio", f.folio], ["Fecha", f.fecha + " " + f.hora],
    ["Contribuyente", f.contribuyente], ["Concepto", f.concepto], ["Neto", dinero(f.neto)],
    ["Forma de pago", f.forma_pago], ["Estado", f.cancelado ? "CANCELADO" : "Vigente"]]
    .map(c => `<dt>${esc(c[0])}</dt><dd>${esc(c[1])}</dd>`).join("") + "</dl>";
}
function abrirCancelar() {
  recSel = null;
  $("dlgTitulo").textContent = "Cancelar recibo";
  $("dlgCuerpo").innerHTML = `<form class="form-folio" id="fFolio">
      <input id="inFolio" placeholder="Escribe el folio, ej. J00849" autocomplete="off" required>
      <button class="btn btn-buscar" type="submit">Buscar</button></form>
    <div id="cancelRes"></div>`;
  dlg.showModal();
  $("inFolio").focus();
  $("fFolio").onsubmit = async e => {
    e.preventDefault();
    $("cancelRes").innerHTML = '<div class="mensaje">Buscando...</div>';
    try {
      const r = await pedir("/api/folio?folio=" + encodeURIComponent($("inFolio").value.trim()));
      if (!r.filas.length) return $("cancelRes").innerHTML = '<div class="mensaje">No se encontró ningún recibo con ese folio.</div>';
      if (r.filas.length === 1) return mostrarRecibo(r.filas[0]);
      $("cancelRes").innerHTML = "<p>Se encontraron varios recibos, elige uno:</p>" + r.filas.map((f, i) =>
        `<button type="button" class="opcion-folio" data-k="${i}"><b>${esc(f.folio)}</b> · ${esc(f.fecha)} · ${esc(f.contribuyente)} · ${dinero(f.neto)}${f.cancelado ? " · CANCELADO" : ""}</button>`).join("");
      $("cancelRes").querySelectorAll(".opcion-folio").forEach(b => b.onclick = () => mostrarRecibo(r.filas[+b.dataset.k]));
    } catch (err) { $("cancelRes").innerHTML = `<div class="mensaje error">${esc(err.message)}</div>`; }
  };
}
async function mostrarRecibo(f) {
  recSel = f;
  if (f.cancelado) {
    $("cancelRes").innerHTML = datosRecibo(f) + '<div class="aviso-confirma" style="margin-top:14px">Este recibo ya está cancelado.</div>';
    return;
  }
  $("cancelRes").innerHTML = datosRecibo(f) + '<div class="mensaje">Generando NIP...</div>';
  try {
    const r = await fetch("/api/nip", {method: "POST",
      headers: {"Content-Type": "application/json", "X-Requested-With": "fetch"},
      body: JSON.stringify({id: f.id})});
    if (r.status === 401) { location.href = "/entrar"; return; }
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "No se pudo generar el NIP");
    $("cancelRes").innerHTML = datosRecibo(f) + `
      <div class="nip-caja" style="margin-top:14px">
        <div class="nip-tit">NIP de cancelación</div>
        <div class="nip-num">${esc(d.nip)}</div>
      </div>
      <p style="margin:12px 0 0;color:var(--gris)">Dale este NIP a la persona de tesorería que va a cancelar el recibo en caja.</p>
      <div class="acciones"><button type="button" class="btn btn-gris" onclick="dlg.close()">Cerrar</button>
      <button type="button" class="btn btn-buscar" id="btnOtro">Otro recibo</button></div>`;
    $("btnOtro").onclick = abrirCancelar;
  } catch (err) {
    $("cancelRes").innerHTML = datosRecibo(f) + `<div class="mensaje error">${esc(err.message)}</div>`;
  }
}
if (PUEDE_CANCELAR) $("btnCancelarRec").onclick = abrirCancelar;

$("desde").value = $("hasta").value = hoyISO();
if (window.innerWidth < 700) cambiarVista("tarjetas");
buscar();
</script>
</body></html>"""


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    try:
        from waitress import serve
        print(f"Sirviendo en http://0.0.0.0:{port}")
        serve(app, host="0.0.0.0", port=port, threads=8)
    except ImportError:
        app.run(host="0.0.0.0", port=port)
