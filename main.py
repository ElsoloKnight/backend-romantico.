import os
import json
import shutil
import uuid
import psycopg2
from fastapi import FastAPI, UploadFile, File, HTTPException
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


# ==========================================
# MODELOS DE DATOS
# ==========================================
class Mensaje(BaseModel):
    texto: str
    remitente: str


class FraseNueva(BaseModel):
    texto: str
    rol: str


# ==========================================
# RUTAS DE LA APP
# ==========================================

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

# --- RUTAS PARA CONTADORES ---
@app.get("/contadores")
def obtener_contadores():
    conn = obtener_conexion()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO contadores (tipo, valor) VALUES ('te_amo', 0) "
        "ON CONFLICT (tipo) DO NOTHING;"
    )
    conn.commit()
    cursor.execute("SELECT tipo, valor FROM contadores;")
    filas = cursor.fetchall()
    conn.close()
    
    # Convertimos las filas de Postgres en un diccionario JSON
    return {fila[0]: fila[1] for fila in filas}

@app.post("/contadores/{tipo}")
def sumar_contador(tipo: str):
    conn = obtener_conexion()
    cursor = conn.cursor()
    
    # Sumamos 1 directamente en SQL y pedimos que nos devuelva (RETURNING) el nuevo valor
    cursor.execute(
        "UPDATE contadores SET valor = valor + 1 WHERE tipo = %s RETURNING valor;",
        (tipo,)
    )
    nuevo_valor = cursor.fetchone()
    
    conn.commit()
    conn.close()

    if nuevo_valor:
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

@app.post("/fotos")
async def subir_foto(foto: UploadFile = File(...)):
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
    cursor.execute("INSERT INTO fotos (url) VALUES (%s) RETURNING id;", (url_foto,))
    nuevo_id = cursor.fetchone()[0]
    conn.commit()
    conn.close()
    
    return {"id": nuevo_id, "url": url_foto}