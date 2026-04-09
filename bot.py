import os, json, logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
import anthropic
from sheets import SheetsDB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"]
ALLOWED_USERS  = [int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x]

claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
db = SheetsDB()


def auth(update: Update) -> bool:
    uid = update.effective_user.id
    if ALLOWED_USERS and uid not in ALLOWED_USERS:
        return False
    return True


def fmt_mxn(n) -> str:
    return "$" + str(int(n or 0))


async def transcribe_voice(file_path: str) -> str:
    from groq import Groq
    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    with open(file_path, "rb") as f:
        result = client.audio.transcriptions.create(
            model="whisper-large-v3-turbo",
            file=f,
            language="es"
        )
    return result.text


async def extract_data(text: str) -> dict:
    prompt = (
        "Analiza este mensaje y extrae la informacion. "
        "Responde UNICAMENTE con JSON valido, sin texto adicional, sin markdown, sin explicaciones.\n\n"
        "Mensaje: " + text + "\n\n"
        "Si es una VENTA de servicio medico estetico, responde:\n"
        '{"tipo":"venta","datos":{"cliente":"nombre","servicio_id":"botox","servicio_nombre":"Botox","precio":3500,"metodo_pago":"Tarjeta","pagado":true,"notas":null},"resumen_confirmacion":"Botox Ana - $3500 Tarjeta"}\n\n'
        "Si es un GASTO u compra, responde:\n"
        '{"tipo":"gasto","datos":{"categoria":"Insumos","descripcion":"descripcion","monto":2800},"resumen_confirmacion":"Gasto Insumos - $2800"}\n\n'
        "Si es una PREGUNTA sobre el negocio, responde:\n"
        '{"tipo":"consulta","datos":{"pregunta":"texto de la pregunta"},"resumen_confirmacion":""}\n\n'
        "Servicios: botox, filler, laser, facial, prp, hidratacion, peeling, biorevitalizacion, consulta, otro\n"
        "Metodos de pago: Efectivo, Tarjeta, Transferencia\n"
        "Categorias de gasto: Insumos, Renta, Nomina, Servicios, Marketing, Equipo, Otro\n\n"
        "IMPORTANTE: Responde SOLO el JSON, nada mas."
    )

    msg = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = msg.content[0].text.strip()
    log.info("Claude raw response: " + raw)

    # Limpieza defensiva por si viene con markdown
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`").strip()
        if raw.startswith("json"):
            raw = raw[4:].strip()

    if raw.lower().startswith("json"):
        raw = raw[4:].strip()

