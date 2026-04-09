import os, json, logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
Application, CommandHandler, MessageHandler,
CallbackQueryHandler, ContextTypes, filters
)
import anthropic
from sheets import SheetsDB

logging.basicConfig(level=logging.INFO, format=”%(asctime)s %(levelname)s %(message)s”)
log = logging.getLogger(**name**)

TELEGRAM_TOKEN = os.environ[“TELEGRAM_TOKEN”]
ANTHROPIC_KEY  = os.environ[“ANTHROPIC_API_KEY”]
ALLOWED_USERS  = [int(x) for x in os.environ.get(“ALLOWED_USER_IDS”, “”).split(”,”) if x]

claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
db     = SheetsDB()

def auth(update: Update) -> bool:
uid = update.effective_user.id
if ALLOWED_USERS and uid not in ALLOWED_USERS:
return False
return True

def fmt_mxn(n) -> str:
return “$” + str(int(n or 0))

async def transcribe_voice(file_path: str) -> str:
from groq import Groq
client = Groq(api_key=os.environ[“GROQ_API_KEY”])
with open(file_path, “rb”) as f:
result = client.audio.transcriptions.create(
model=“whisper-large-v3-turbo”, file=f, language=“es”
)
return result.text

SYSTEM_EXTRACT = (
“Eres el asistente contable del consultorio Seleah de medicina estetica. “
“Extrae informacion de mensajes transcritos y responde SOLO con JSON valido sin markdown.\n\n”
“Servicios disponibles (usa estos IDs exactos):\n”
“botox, filler, laser, facial, prp, hidratacion, peeling, biorevitalizacion, consulta, otro\n\n”
“Formato de respuesta:\n”
“{\n”
’  “tipo”: “venta” | “gasto” | “consulta”,\n’
’  “datos”: {\n’
’    “cliente”: “nombre o null”,\n’
’    “servicio_id”: “id”,\n’
’    “servicio_nombre”: “nombre legible”,\n’
’    “precio”: numero,\n’
’    “metodo_pago”: “Efectivo” | “Tarjeta” | “Transferencia” | null,\n’
’    “pagado”: true | false,\n’
’    “notas”: “texto o null”,\n’
’    “categoria”: “Insumos”|“Renta”|“Nomina”|“Servicios”|“Marketing”|“Equipo”|“Otro”,\n’
’    “descripcion”: “texto”,\n’
’    “monto”: numero,\n’
’    “pregunta”: “texto”\n’
“  },\n”
’  “confianza”: “alta” | “media” | “baja”,\n’
’  “resumen_confirmacion”: “frase corta max 80 chars”\n’
“}”
)

async def extract_data(text: str) -> dict:
msg = claude.messages.create(
model=“claude-haiku-4-5-20251001”,
max_tokens=500,
system=SYSTEM_EXTRACT,
messages=[{“role”: “user”, “content”: text}]
)
raw = msg.content[0].text.strip()
if raw.startswith(”`"): parts = raw.split("`”)
raw = parts[1] if len(parts) > 1 else raw
if raw.startswith(“json”):
raw = raw[4:]
return json.loads(raw.strip())

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
if not auth(update):
return
name = update.effective_user.first_name
await update.message.reply_text(
“Hola Dra. “ + name + “! Soy tu asistente Seleah.\n\n”
“Puedes enviarme:\n”
“- Nota de voz para registrar ventas y gastos\n”
“- Texto si prefieres escribir\n\n”
“Ejemplos:\n”
“- ‘Le aplique botox a Ana, pago 3500 con tarjeta’\n”
“- ‘Gasto de insumos 2800 pesos’\n”
“- ‘Cuanto llevo de ingresos este mes?’\n\n”
“Comandos:\n”
“/resumen - Resumen del mes\n”
“/top - Top servicios del mes”
)

async def cmd_resumen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
if not auth(update):
return
await update.message.reply_text(“Calculando resumen…”)
try:
stats = db.get_monthly_summary()
text = (
“Resumen “ + stats[“mes”] + “\n\n”
“Ingresos:   “ + fmt_mxn(stats[“ingresos”]) + “\n”
“Costo svs:  “ + fmt_mxn(stats[“costos”]) + “\n”
“Gastos:     “ + fmt_mxn(stats[“gastos”]) + “\n”
“————————\n”
“Utilidad:   “ + fmt_mxn(stats[“utilidad”]) + “\n\n”
+ str(stats[“num_ventas”]) + “ servicios realizados”
)
await update.message.reply_text(text)
except Exception as e:
log.error(e)
await update.message.reply_text(“Error al obtener el resumen.”)

async def cmd_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
if not auth(update):
return
try:
top = db.get_top_services()
lines = [“Top Servicios del Mes\n”]
for i, s in enumerate(top[:5], 1):
lines.append(str(i) + “. “ + s[“nombre”] + “ - “ + fmt_mxn(s[“ingreso”]) + “ (” + str(s[“sesiones”]) + “ ses.)”)
await update.message.reply_text(”\n”.join(lines))
except Exception as e:
log.error(e)
await update.message.reply_text(“Error al obtener los servicios.”)

async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
if not auth(update):
return
await update.message.reply_text(“Escuchando…”)
voice = update.message.voice or update.message.audio
file  = await ctx.bot.get_file(voice.file_id)
path  = “/tmp/” + voice.file_id + “.ogg”
await file.download_to_drive(path)
try:
transcript = await transcribe_voice(path)
log.info(“Transcripcion: “ + transcript)
await update.message.reply_text(“Escuche: “ + transcript)
data = await extract_data(transcript)
await process_extracted(update, ctx, data)
except Exception as e:
log.error(“Error en voz: “ + str(e))
await update.message.reply_text(“No pude procesar el audio. Intentalo de nuevo o escribelo.”)
finally:
if os.path.exists(path):
os.remove(path)

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
if not auth(update):
return
text = update.message.text
if text.startswith(”/”):
return
await update.message.reply_text(“Procesando…”)
try:
data = await extract_data(text)
await process_extracted(update, ctx, data)
except Exception as e:
log.error(str(e))
await update.message.reply_text(“No entendi. Intenta: ‘botox a Maria, 3500 en efectivo’”)

async def process_extracted(update: Update, ctx: ContextTypes.DEFAULT_TYPE, data: dict):
tipo    = data.get(“tipo”)
resumen = data.get(“resumen_confirmacion”, “Confirmar registro?”)
conf    = data.get(“confianza”, “media”)
emoji   = {“alta”: “OK”, “media”: “Revisar”, “baja”: “Verificar”}.get(conf, “Revisar”)

```
ctx.user_data["pending"] = data

keyboard = InlineKeyboardMarkup([
    [InlineKeyboardButton("Confirmar", callback_data="confirm"),
     InlineKeyboardButton("Cancelar",  callback_data="cancel")],
    [InlineKeyboardButton("Corregir",  callback_data="correct")]
])

if tipo == "venta":
    d = data["datos"]
    msg = (
        emoji + " - Venta detectada\n\n"
        "Cliente:  " + str(d.get("cliente") or "Sin nombre") + "\n"
        "Servicio: " + str(d.get("servicio_nombre") or "") + "\n"
        "Precio:   " + fmt_mxn(d.get("precio", 0)) + "\n"
        "Metodo:   " + str(d.get("metodo_pago") or "No especificado") + "\n"
        "Pagado:   " + ("Si" if d.get("pagado") else "No") + "\n\n"
        + resumen
    )
elif tipo == "gasto":
    d = data["datos"]
    msg = (
        emoji + " - Gasto detectado\n\n"
        "Categoria:   " + str(d.get("categoria") or "") + "\n"
        "Descripcion: " + str(d.get("descripcion") or "") + "\n"
        "Monto:       " + fmt_mxn(d.get("monto", 0)) + "\n\n"
        + resumen
    )
elif tipo == "consulta":
    answer = await answer_question(data["datos"].get("pregunta", ""))
    await update.message.reply_text(answer)
    return
else:
    await update.message.reply_text("No identifique si es venta, gasto o consulta. Se mas especifica.")
    return

await update.message.reply_text(msg, reply_markup=keyboard)
```

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
query  = update.callback_query
await query.answer()
action  = query.data
pending = ctx.user_data.get(“pending”)

```
if action == "confirm" and pending:
    try:
        if pending["tipo"] == "venta":
            db.add_sale(pending["datos"])
            await query.edit_message_text("Venta registrada correctamente!")
        elif pending["tipo"] == "gasto":
            db.add_expense(pending["datos"])
            await query.edit_message_text("Gasto registrado correctamente!")
        ctx.user_data.pop("pending", None)
    except Exception as e:
        log.error(str(e))
        await query.edit_message_text("Error al guardar. Intenta de nuevo.")
elif action == "cancel":
    ctx.user_data.pop("pending", None)
    await query.edit_message_text("Registro cancelado.")
elif action == "correct":
    await query.edit_message_text(
        "Escribe la correccion, por ejemplo:\n"
        "'El precio fue 4000, no 3500'\n"
        "'Es gasto de renta, no insumos'"
    )
```

async def answer_question(question: str) -> str:
try:
stats   = db.get_monthly_summary()
context = “Datos del mes: “ + json.dumps(stats, ensure_ascii=False)
msg = claude.messages.create(
model=“claude-haiku-4-5-20251001”,
max_tokens=300,
messages=[{
“role”: “user”,
“content”: context + “\n\nPregunta: “ + question + “\n\nResponde brevemente en español, maximo 3 lineas.”
}]
)
return msg.content[0].text
except Exception as e:
return “No pude obtener los datos: “ + str(e)

def main():
app = Application.builder().token(TELEGRAM_TOKEN).build()
app.add_handler(CommandHandler(“start”,   cmd_start))
app.add_handler(CommandHandler(“resumen”, cmd_resumen))
app.add_handler(CommandHandler(“top”,     cmd_top))
app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
app.add_handler(CallbackQueryHandler(handle_callback))
log.info(“Seleah Bot iniciado”)
app.run_polling(drop_pending_updates=True)

if **name** == “**main**”:
main()
