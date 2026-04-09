“””
Seleah Bot — Bot de Telegram para administración de consultorio de medicina estética.
Flujo: Nota de voz → Whisper (transcripción) → Claude (extracción) → Google Sheets (registro)
“””

import os, json, logging, asyncio
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
Application, CommandHandler, MessageHandler,
CallbackQueryHandler, ContextTypes, filters
)
import anthropic
import httpx
from sheets import SheetsDB

logging.basicConfig(level=logging.INFO, format=”%(asctime)s %(levelname)s %(message)s”)
log = logging.getLogger(**name**)

# ── config ────────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN   = os.environ[“TELEGRAM_TOKEN”]
ANTHROPIC_KEY    = os.environ[“ANTHROPIC_API_KEY”]
ALLOWED_USERS    = [int(x) for x in os.environ.get(“ALLOWED_USER_IDS”, “”).split(”,”) if x]

claude  = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
db      = SheetsDB()

# ── auth ──────────────────────────────────────────────────────────────────────

def auth(update: Update) -> bool:
uid = update.effective_user.id
if ALLOWED_USERS and uid not in ALLOWED_USERS:
return False
return True

# ── transcribe voice with Whisper via OpenAI-compatible endpoint ──────────────

async def transcribe_voice(file_path: str) -> str:
“”“Transcribe audio file using OpenAI Whisper API.”””
import openai
client = openai.AsyncOpenAI(api_key=os.environ[“OPENAI_API_KEY”])
with open(file_path, “rb”) as f:
result = await client.audio.transcriptions.create(
model=“whisper-1”, file=f, language=“es”
)
return result.text

# ── extract structured data with Claude ───────────────────────────────────────

SYSTEM_EXTRACT = “”“Eres el asistente contable del consultorio de medicina estética Seleah.
Extrae información de mensajes de voz transcritos y responde SOLO con JSON válido.

Servicios disponibles (usa estos IDs exactos):
botox, filler, laser, facial, prp, hidratacion, peeling, biorevitalizacion, consulta, otro

Formato de respuesta JSON:
{
“tipo”: “venta” | “gasto” | “consulta”,
“datos”: {
// Para VENTA:
“cliente”: “nombre o null”,
“servicio_id”: “id del servicio”,
“servicio_nombre”: “nombre legible”,
“precio”: número,
“metodo_pago”: “Efectivo” | “Tarjeta” | “Transferencia” | null,
“pagado”: true | false,
“notas”: “texto adicional o null”,

```
// Para GASTO:
"categoria": "Insumos" | "Renta" | "Nómina" | "Servicios" | "Marketing" | "Equipo" | "Otro",
"descripcion": "descripción",
"monto": número,

// Para CONSULTA (pregunta sobre el negocio):
"pregunta": "texto de la pregunta"
```

},
“confianza”: “alta” | “media” | “baja”,
“resumen_confirmacion”: “Frase corta para confirmar con la doctora, max 80 chars”
}

Si el mensaje es ambiguo, usa confianza “baja” y haz lo mejor posible.”””

async def extract_data(text: str) -> dict:
“”“Use Claude to extract structured data from transcribed text.”””
msg = claude.messages.create(
model=“claude-opus-4-5”,
max_tokens=500,
system=SYSTEM_EXTRACT,
messages=[{“role”: “user”, “content”: text}]
)
raw = msg.content[0].text.strip()
# strip markdown fences if present
if raw.startswith(”`"): raw = raw.split("`”)[1]
if raw.startswith(“json”):
raw = raw[4:]
return json.loads(raw.strip())

# ── handlers ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
if not auth(update): return
name = update.effective_user.first_name
await update.message.reply_text(
f”👋 Hola Dra. {name}! Soy tu asistente Seleah.\n\n”
“Puedes enviarme:\n”
“🎤 *Nota de voz* — registraré ventas y gastos\n”
“💬 *Texto* — si prefieres escribir\n\n”
“Ejemplos de lo que puedes decir:\n”
“• *‘Le apliqué botox a Ana García, pagó 3500 con tarjeta’*\n”
“• *‘Gasto de insumos 2800 pesos’*\n”
“• *‘Cuánto llevo de ingresos este mes?’*”,
parse_mode=“Markdown”
)

async def cmd_resumen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
if not auth(update): return
await update.message.reply_text(“⏳ Calculando resumen…”)
try:
stats = db.get_monthly_summary()
text = (
f”📊 *Resumen {stats[‘mes’]}*\n\n”
f”💰 Ingresos:  `{fmt_mxn(stats['ingresos'])}`\n”
f”📦 Costo svs: `{fmt_mxn(stats['costos'])}`\n”
f”🏢 Gastos:    `{fmt_mxn(stats['gastos'])}`\n”
f”{‘─’*28}\n”
f”✅ Utilidad:  `{fmt_mxn(stats['utilidad'])}`\n\n”
f”🗂 {stats[‘num_ventas’]} servicios realizados”
)
await update.message.reply_text(text, parse_mode=“Markdown”)
except Exception as e:
log.error(e)
await update.message.reply_text(“❌ Error al obtener el resumen.”)

async def cmd_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
if not auth(update): return
try:
top = db.get_top_services()
lines = [f”🏆 *Top Servicios del Mes*\n”]
for i, s in enumerate(top[:5], 1):
lines.append(f”{i}. {s[‘nombre’]} — `{fmt_mxn(s['ingreso'])}` ({s[‘sesiones’]} ses.)”)
await update.message.reply_text(”\n”.join(lines), parse_mode=“Markdown”)
except Exception as e:
log.error(e)
await update.message.reply_text(“❌ Error al obtener los servicios.”)

async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
if not auth(update): return
await update.message.reply_text(“🎤 Escuchando…”)

```
# download voice file
voice = update.message.voice or update.message.audio
file  = await ctx.bot.get_file(voice.file_id)
path  = f"/tmp/{voice.file_id}.ogg"
await file.download_to_drive(path)

try:
    # transcribe
    transcript = await transcribe_voice(path)
    log.info(f"Transcripción: {transcript}")
    await update.message.reply_text(f"📝 _{transcript}_", parse_mode="Markdown")
    
    # extract
    data = await extract_data(transcript)
    await process_extracted(update, ctx, data, transcript)
    
except Exception as e:
    log.error(f"Error en voz: {e}")
    await update.message.reply_text("❌ No pude procesar el audio. ¿Puedes repetirlo o escribirlo?")
finally:
    if os.path.exists(path):
        os.remove(path)
```

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
if not auth(update): return
text = update.message.text
if text.startswith(”/”): return

```
await update.message.reply_text("🔍 Procesando...")
try:
    data = await extract_data(text)
    await process_extracted(update, ctx, data, text)
except Exception as e:
    log.error(e)
    await update.message.reply_text("❌ No entendí. Intenta: 'botox a María, $3500 en efectivo'")
```

async def process_extracted(update: Update, ctx: ContextTypes.DEFAULT_TYPE, data: dict, original: str):
“”“Show confirmation keyboard before saving.”””
tipo = data.get(“tipo”)
resumen = data.get(“resumen_confirmacion”, “¿Confirmar registro?”)
confianza = data.get(“confianza”, “media”)

```
emoji_conf = {"alta": "✅", "media": "🟡", "baja": "⚠️"}.get(confianza, "🟡")

# store pending in context
ctx.user_data["pending"] = {"data": data, "original": original}

keyboard = InlineKeyboardMarkup([
    [InlineKeyboardButton("✅ Confirmar", callback_data="confirm"),
     InlineKeyboardButton("❌ Cancelar", callback_data="cancel")],
    [InlineKeyboardButton("✏️ Corregir", callback_data="correct")]
])

if tipo == "venta":
    d = data["datos"]
    msg = (
        f"{emoji_conf} *Venta detectada*\n\n"
        f"👤 Cliente: {d.get('cliente') or 'Sin nombre'}\n"
        f"💉 Servicio: {d.get('servicio_nombre')}\n"
        f"💵 Precio: `{fmt_mxn(d.get('precio', 0))}`\n"
        f"💳 Método: {d.get('metodo_pago') or 'No especificado'}\n"
        f"✔ Pagado: {'Sí' if d.get('pagado') else 'No'}\n\n"
        f"_{resumen}_"
    )
elif tipo == "gasto":
    d = data["datos"]
    msg = (
        f"{emoji_conf} *Gasto detectado*\n\n"
        f"📂 Categoría: {d.get('categoria')}\n"
        f"📝 Descripción: {d.get('descripcion')}\n"
        f"💸 Monto: `{fmt_mxn(d.get('monto', 0))}`\n\n"
        f"_{resumen}_"
    )
elif tipo == "consulta":
    # answer the question directly
    answer = await answer_question(data["datos"].get("pregunta", ""), db)
    await update.message.reply_text(answer, parse_mode="Markdown")
    return
else:
    await update.message.reply_text("🤔 No reconocí si es venta, gasto o consulta. ¿Puedes ser más específica?")
    return

await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=keyboard)
```

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
query = update.callback_query
await query.answer()
action = query.data
pending = ctx.user_data.get(“pending”)

```
if action == "confirm" and pending:
    data = pending["data"]
    try:
        if data["tipo"] == "venta":
            db.add_sale(data["datos"])
            await query.edit_message_text("✅ *Venta registrada correctamente* 🎉", parse_mode="Markdown")
        elif data["tipo"] == "gasto":
            db.add_expense(data["datos"])
            await query.edit_message_text("✅ *Gasto registrado correctamente* 📌", parse_mode="Markdown")
        ctx.user_data.pop("pending", None)
    except Exception as e:
        log.error(e)
        await query.edit_message_text("❌ Error al guardar. Intenta de nuevo.")

elif action == "cancel":
    ctx.user_data.pop("pending", None)
    await query.edit_message_text("❌ Registro cancelado.")

elif action == "correct":
    await query.edit_message_text(
        "✏️ Escríbeme la corrección, por ejemplo:\n"
        "_'El precio fue 4000, no 3500'_\n"
        "_'Es gasto de renta, no insumos'_",
        parse_mode="Markdown"
    )
```

async def answer_question(question: str, db: SheetsDB) -> str:
“”“Answer business questions using current data.”””
stats = db.get_monthly_summary()
context = f”Datos del mes actual: {json.dumps(stats, ensure_ascii=False)}”

```
msg = claude.messages.create(
    model="claude-opus-4-5",
    max_tokens=300,
    messages=[{
        "role": "user",
        "content": f"{context}\n\nPregunta de la doctora: {question}\n\nResponde brevemente en español, máximo 3 líneas."
    }]
)
return f"🤖 {msg.content[0].text}"
```

def fmt_mxn(n) -> str:
return f”${int(n or 0):,}”

# ── main ──────────────────────────────────────────────────────────────────────

def main():
app = Application.builder().token(TELEGRAM_TOKEN).build()
app.add_handler(CommandHandler(“start”,   cmd_start))
app.add_handler(CommandHandler(“resumen”, cmd_resumen))
app.add_handler(CommandHandler(“top”,     cmd_top))
app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
app.add_handler(CallbackQueryHandler(handle_callback))
log.info(“🌿 Seleah Bot iniciado”)
app.run_polling(drop_pending_updates=True)

if **name** == “**main**”:
main()
