import os
import json
import shutil
import uuid
import json as json_lib
import time
from urllib.request import Request, urlopen
import psycopg2
import httpx
import jwt
from fastapi import FastAPI, UploadFile, File, HTTPException, Header
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from datetime import date
from pydantic import BaseModel

app = FastAPI()

# Middleware para que tu app móvil pueda comunicarse con este backend sin bloqueos de seguridad
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuración de PostgreSQL. Render puede usar DATABASE_URL; localmente se usan
# los valores por defecto de la instalación de PostgreSQL del proyecto.
DB_CONFIG = {
    "dbname": os.getenv("DB_NAME", "app_romantica"),
    "user": os.getenv("DB_USER", "elsoloknight"),
    "password": os.getenv("DB_PASSWORD", ""),
    "host": os.getenv("DB_HOST", "localhost"),
    "port": os.getenv("DB_PORT", "5432"),
}

DATABASE_URL = os.getenv("DATABASE_URL")

# Crear carpeta para guardar fotos físicas
os.makedirs("uploads", exist_ok=True)
# Exponer la carpeta para que la app pueda ver las imágenes por URL
app.mount("/static", StaticFiles(directory="uploads"), name="static")

FRASE_FILE = os.path.join(os.path.dirname(__file__), "frase_del_dia.json")
FRASE_POR_DEFECTO = "No importa qué pase hoy, recuerda que eres increíble."

# Función auxiliar para conectarnos a la BD
def obtener_conexion():
    if DATABASE_URL:
        return psycopg2.connect(DATABASE_URL)
    return psycopg2.connect(**DB_CONFIG)


def inicializar_base_de_datos():
    conn = obtener_conexion()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS contadores (
                    tipo TEXT PRIMARY KEY,
                    valor INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS mensajes (
                    id SERIAL PRIMARY KEY,
                    texto TEXT NOT NULL,
                    remitente TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fotos (
                    id SERIAL PRIMARY KEY,
                    url TEXT NOT NULL,
                    usuario TEXT
                );
                ALTER TABLE fotos ADD COLUMN IF NOT EXISTS usuario TEXT;
                CREATE TABLE IF NOT EXISTS push_tokens (
                    usuario TEXT PRIMARY KEY,
                    token TEXT NOT NULL,
                    plataforma TEXT NOT NULL DEFAULT 'expo',
                    bundle_id TEXT,
                    actualizado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                ALTER TABLE push_tokens ADD COLUMN IF NOT EXISTS plataforma TEXT NOT NULL DEFAULT 'expo';
                ALTER TABLE push_tokens ADD COLUMN IF NOT EXISTS bundle_id TEXT;
                INSERT INTO contadores (tipo, valor) VALUES
                    ('te_extrano', 0), ('toma_agua', 0), ('te_amo', 0)
                ON CONFLICT (tipo) DO NOTHING;
                """
            )
        conn.commit()
    finally:
        conn.close()


@app.on_event("startup")
def preparar_base_de_datos():
    inicializar_base_de_datos()


# ==========================================
# MODELOS DE DATOS
# ==========================================
class Mensaje(BaseModel):
    texto: str
    remitente: str


class FraseNueva(BaseModel):
    texto: str
    rol: str


class LoginRequest(BaseModel):
    usuario: str
    password: str


class EstadoUsuario(BaseModel):
    usuario: str
    animo: str
    estres: str


class PushToken(BaseModel):
    usuario: str
    token: str
    plataforma: str = "expo"
    bundle_id: str | None = None


# Estado actual para la app (solo frontend + demo, sin sincronización real por ahora)
ESTADO_USUARIO = {
    "juan_carlos": {"animo": "normal", "estres": "normal"},
    "roro": {"animo": "normal", "estres": "normal"},
}

# Estado de la última vez que el usuario abrió la app (timestamp)
ULTIMO_ACCESO = {
    "juan_carlos": 0.0,
    "roro": 0.0,
}


def usuario_contrario(usuario: str) -> str:
    return "roro" if usuario == "juan_carlos" else "juan_carlos"


def enviar_push_a_usuario(usuario: str, titulo: str, cuerpo: str) -> None:
    try:
        conn = obtener_conexion()
        with conn.cursor() as cursor:
            cursor.execute("SELECT token, plataforma, bundle_id FROM push_tokens WHERE usuario = %s;", (usuario,))
            fila = cursor.fetchone()
        conn.close()
        if not fila:
            return

        if fila[1] == "apns":
            enviar_apns(fila[0], fila[2], titulo, cuerpo)
            return

        payload = json_lib.dumps({
            "to": fila[0],
            "title": titulo,
            "body": cuerpo,
            "sound": "default",
            "priority": "high",
            "channelId": "romantica-default",
        }).encode("utf-8")
        solicitud = Request(
            "https://exp.host/--/api/v2/push/send",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(solicitud, timeout=10):
            pass
    except (psycopg2.Error, OSError, httpx.HTTPError, jwt.PyJWTError) as error:
        print(f"No se pudo enviar push a {usuario}: {error}")


def enviar_apns(token: str, bundle_id: str | None, titulo: str, cuerpo: str) -> None:
    key_id = os.getenv("APNS_KEY_ID")
    team_id = os.getenv("APNS_TEAM_ID")
    private_key = os.getenv("APNS_PRIVATE_KEY")
    topic = bundle_id or os.getenv("APNS_BUNDLE_ID", "com.elsoloknight.appromantica")
    if not key_id or not team_id or not private_key:
        print("APNs no configurado: faltan APNS_KEY_ID, APNS_TEAM_ID o APNS_PRIVATE_KEY")
        return

    private_key = private_key.replace("\\n", "\n")
    access_token = jwt.encode(
        {"iss": team_id, "iat": int(time.time())},
        private_key,
        algorithm="ES256",
        headers={"kid": key_id},
    )
    environment = os.getenv("APNS_ENVIRONMENT", "production")
    host = "api.sandbox.push.apple.com" if environment == "sandbox" else "api.push.apple.com"
    response = httpx.post(
        f"https://{host}/3/device/{token}",
        headers={
            "authorization": f"bearer {access_token}",
            "apns-topic": topic,
            "apns-push-type": "alert",
            "apns-priority": "10",
        },
        json={"aps": {"alert": {"title": titulo, "body": cuerpo}, "sound": "default", "badge": 1}},
        timeout=10,
    )
    response.raise_for_status()


# ==========================================
# RUTAS DE LA APP
# ==========================================

@app.post("/login")
def login_usuario(login: LoginRequest):
    usuario = login.usuario.strip().lower()
    if usuario not in {"juan_carlos", "roro"}:
        raise HTTPException(status_code=401, detail="Usuario no válido")
    return {"ok": True, "usuario": usuario, "mensaje": "Acceso permitido"}


@app.post("/push-token")
def registrar_push_token(push: PushToken):
    usuario = push.usuario.strip().lower()
    token = push.token.strip()
    plataforma = push.plataforma.strip().lower()
    token_valido = plataforma == "expo" and token.startswith("ExponentPushToken[")
    token_valido = token_valido or plataforma == "apns" and len(token) >= 32
    if usuario not in {"juan_carlos", "roro"} or plataforma not in {"expo", "apns"} or not token_valido:
        raise HTTPException(status_code=400, detail="Usuario o token no válido")

    conn = obtener_conexion()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO push_tokens (usuario, token, plataforma, bundle_id, actualizado_en)
                VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (usuario) DO UPDATE SET
                    token = EXCLUDED.token,
                    plataforma = EXCLUDED.plataforma,
                    bundle_id = EXCLUDED.bundle_id,
                    actualizado_en = CURRENT_TIMESTAMP;
                """,
                (usuario, token, plataforma, push.bundle_id),
            )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "usuario": usuario}


@app.post("/apns-token")
def registrar_token_apns(push: PushToken):
    push.plataforma = "apns"
    return registrar_push_token(push)


@app.get("/estado/{usuario}")
def obtener_estado(usuario: str):
    clave = usuario.strip().lower()
    estado = ESTADO_USUARIO.get(clave, {"animo": "normal", "estres": "normal"})
    return {"usuario": clave, "animo": estado["animo"], "estres": estado["estres"]}


@app.post("/estado")
def guardar_estado(estado: EstadoUsuario):
    clave = estado.usuario.strip().lower()
    if clave not in {"juan_carlos", "roro"}:
        raise HTTPException(status_code=400, detail="Usuario no válido")

    ESTADO_USUARIO[clave] = {
        "animo": estado.animo.strip().lower(),
        "estres": estado.estres.strip().lower(),
    }

    enviar_push_a_usuario(
        usuario_contrario(clave),
        "Estado actualizado",
        f"{'Juan' if clave == 'juan_carlos' else 'Roro'} está {ESTADO_USUARIO[clave]['animo']} y {ESTADO_USUARIO[clave]['estres'] }.",
    )

    return {
        "usuario": clave,
        "animo": ESTADO_USUARIO[clave]["animo"],
        "estres": ESTADO_USUARIO[clave]["estres"],
        "mensaje": "Estado actualizado"
    }


@app.get("/frase-del-dia")
def obtener_frase():
    hoy = date.today().isoformat()
    if os.path.exists(FRASE_FILE):
        with open(FRASE_FILE, "r", encoding="utf-8") as archivo:
            frase_guardada = json.load(archivo)
        if frase_guardada.get("fecha") == hoy:
            return {"frase": frase_guardada["texto"], "puede_cambiar": False}

    return {"frase": FRASE_POR_DEFECTO, "puede_cambiar": True}


@app.post("/frase-del-dia")
def cambiar_frase(frase: FraseNueva):
    if frase.rol != "juan_carlos":
        raise HTTPException(status_code=403, detail="Solo Juan puede cambiar la frase")
    texto = frase.texto.strip()
    if not texto:
        raise HTTPException(status_code=400, detail="La frase no puede estar vacía")

    hoy = date.today().isoformat()
    if os.path.exists(FRASE_FILE):
        with open(FRASE_FILE, "r", encoding="utf-8") as archivo:
            frase_guardada = json.load(archivo)
        if frase_guardada.get("fecha") == hoy:
            raise HTTPException(status_code=409, detail="La frase ya fue cambiada hoy")

    with open(FRASE_FILE, "w", encoding="utf-8") as archivo:
        json.dump({"fecha": hoy, "texto": texto}, archivo, ensure_ascii=False, indent=2)

    return {"frase": texto, "puede_cambiar": False}

# --- RUTAS PARA SABER SI ESTÁN EN LÍNEA ---
@app.post("/ping")
def hacer_ping(usuario: str | None = Header(default=None, alias="X-Usuario")):
    clave = (usuario or "").strip().lower()
    if clave in ULTIMO_ACCESO:
        ULTIMO_ACCESO[clave] = time.time()
    return {"ok": True}

@app.get("/online/{usuario}")
def saber_si_esta_online(usuario: str):
    clave = usuario.strip().lower()
    if clave not in ULTIMO_ACCESO:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    # Si hizo ping en los últimos 20 segundos, consideramos que está en la app
    tiempo_pasado = time.time() - ULTIMO_ACCESO[clave]
    esta_online = tiempo_pasado < 20

    return {"usuario": clave, "online": esta_online}

# --- RUTAS PARA CONTADORES ---
@app.get("/contadores")
def obtener_contadores():
    try:
        conn = obtener_conexion()
        with conn.cursor() as cursor:
            cursor.execute("SELECT tipo, valor FROM contadores;")
            filas = cursor.fetchall()
        conn.close()
    except psycopg2.Error as error:
        raise HTTPException(status_code=503, detail="Base de datos no disponible") from error
    
    # Convertimos las filas de Postgres en un diccionario JSON
    return {fila[0]: fila[1] for fila in filas}

@app.post("/contadores/{tipo}")
def sumar_contador(tipo: str, usuario: str | None = Header(default=None, alias="X-Usuario")):
    try:
        conn = obtener_conexion()
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE contadores SET valor = valor + 1 WHERE tipo = %s RETURNING valor;",
                (tipo,)
            )
            nuevo_valor = cursor.fetchone()
        conn.commit()
        conn.close()
    except psycopg2.Error as error:
        raise HTTPException(status_code=503, detail="Base de datos no disponible") from error

    if nuevo_valor:
        actor = (usuario or "").strip().lower()
        if actor in {"juan_carlos", "roro"}:
            enviar_push_a_usuario(
                usuario_contrario(actor),
                "Contador actualizado",
                f"{actor.replace('_', ' ').title()} aumentó {tipo.replace('_', ' ')}.",
            )
        return {"mensaje": "Éxito", "nuevo_valor": nuevo_valor[0]}
    return {"error": "Contador no encontrado"}

# --- RUTAS PARA EL BUZÓN DE MENSAJES ---
@app.get("/mensajes")
def obtener_mensajes():
    conn = obtener_conexion()
    cursor = conn.cursor()
    cursor.execute("SELECT id, texto, remitente FROM mensajes ORDER BY id ASC;")
    filas = cursor.fetchall()
    conn.close()
    return [{"id": fila[0], "texto": fila[1], "remitente": fila[2]} for fila in filas]

@app.post("/mensajes")
def crear_mensaje(mensaje: Mensaje):
    conn = obtener_conexion()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO mensajes (texto, remitente) VALUES (%s, %s) RETURNING id;",
        (mensaje.texto, mensaje.remitente)
    )
    nuevo_id = cursor.fetchone()[0]
    conn.commit()
    conn.close()
    return {"id": nuevo_id, "texto": mensaje.texto, "remitente": mensaje.remitente}

# --- RUTAS PARA LA GALERÍA DE FOTOS ---
@app.get("/fotos")
def obtener_fotos():
    conn = obtener_conexion()
    cursor = conn.cursor()
    cursor.execute("SELECT id, url FROM fotos ORDER BY id DESC;")
    filas = cursor.fetchall()
    conn.close()
    return [{"id": fila[0], "url": fila[1]} for fila in filas]


@app.get("/galeria/{usuario}")
def obtener_galeria_por_usuario(usuario: str):
    clave = usuario.strip().lower()
    if clave not in {"juan_carlos", "roro"}:
        return []

    conn = obtener_conexion()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, url FROM fotos WHERE usuario = %s OR usuario IS NULL ORDER BY id DESC;",
        (clave,),
    )
    filas = cursor.fetchall()
    conn.close()
    return [{"id": fila[0], "url": fila[1]} for fila in filas]


@app.post("/fotos")
async def subir_foto(
    foto: UploadFile = File(...),
    usuario: str | None = Header(default=None, alias="X-Usuario"),
):
    # Generar un nombre único para que no choquen si suben 2 fotos llamadas "image.jpg"
    extension = foto.filename.split(".")[-1]
    nombre_archivo = f"{uuid.uuid4()}.{extension}"
    ruta_guardado = f"uploads/{nombre_archivo}"
    
    # Guardar el archivo en la Mac
    with open(ruta_guardado, "wb") as buffer:
        shutil.copyfileobj(foto.file, buffer)
        
    url_foto = f"/static/{nombre_archivo}"
    
    # Guardar la ruta en la base de datos
    conn = obtener_conexion()
    cursor = conn.cursor()
    propietario = (usuario or "roro").strip().lower()
    if propietario not in {"juan_carlos", "roro"}:
        propietario = "roro"
    cursor.execute(
        "INSERT INTO fotos (url, usuario) VALUES (%s, %s) RETURNING id;",
        (url_foto, propietario),
    )
    nuevo_id = cursor.fetchone()[0]
    conn.commit()
    conn.close()
    
    return {"id": nuevo_id, "url": url_foto}