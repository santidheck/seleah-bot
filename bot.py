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

    # Intenta aislar el objeto JSON si trae texto extra
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start >= 0 and end > start:
        raw = raw[start:end]

    return json.loads(raw)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update):
        return

    name = update.effective_user.first_name
    await update.message.reply_text(
        "Hola Dra. " + name + "! Soy tu asistente Seleah.\n\n"
        "Manda una nota de voz o escribe:\n"
        "- 'Botox a Ana Garcia, 3500 con tarjeta'\n"
        "- 'Gasto insumos 2800 pesos'\n"
        "- 'Cuanto llevo este mes?'\n\n"
        "/resumen - Resumen del mes\n"
        "/top - Top servicios"
    )


async def cmd_resumen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update):
        return

    await update.message.reply_text("Calculando...")
    try:
        s = db.get_monthly_summary()
        await update.message.reply_text(
            "Resumen " + s["mes"] + "\n\n"
            "Ingresos:  " + fmt_mxn(s["ingresos"]) + "\n"
            "Costos:    " + fmt_mxn(s["costos"]) + "\n"
            "Gastos:    " + fmt_mxn(s["gastos"]) + "\n"
            "----------------\n"
            "Utilidad:  " + fmt_mxn(s["utilidad"]) + "\n\n"
            + str(s["num_ventas"]) + " servicios | "
            + str(s["pendientes"]) + " pendientes de pago"
        )
    except Exception as e:
        log.error(str(e))
        await update.message.reply_text("Error: " + str(e))


async def cmd_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update):
        return

    try:
        top = db.get_top_services()
        lines = ["Top Servicios del Mes\n"]
        for i, s in enumerate(top[:5], 1):
            lines.append(str(i) + ". " + s["nombre"] + " - " + fmt_mxn(s["ingreso"]))
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        log.error(str(e))
        await update.message.reply_text("Error: " + str(e))


async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update):
        return

    await update.message.reply_text("Escuchando...")
    voice = update.message.voice or update.message.audio
    file = await ctx.bot.get_file(voice.file_id)
    path = "/tmp/" + voice.file_id + ".ogg"

    await file.download_to_drive(path)

    try:
        transcript = await transcribe_voice(path)
        log.info("Transcripcion: " + transcript)
        await update.message.reply_text("Escuche: " + transcript)

        data = await extract_data(transcript)
        await process_extracted(update, ctx, data)

    except Exception as e:
        log.error("Error voz: " + str(e))
        await update.message.reply_text("No pude procesar. Intentalo de nuevo o escribelo.")
    finally:
        if os.path.exists(path):
            os.remove(path)


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update):
        return

    text = update.message.text
    if text.startswith("/"):
        return

    await update.message.reply_text("Procesando...")
    try:
        data = await extract_data(text)
        await process_extracted(update, ctx, data)
    except Exception as e:
        log.error("Error texto: " + str(e))
        await update.message.reply_text("No entendi el mensaje. Intenta de nuevo.")


async def process_extracted(update: Update, ctx: ContextTypes.DEFAULT_TYPE, data: dict):
    tipo = data.get("tipo")
    resumen = data.get("resumen_confirmacion", "Confirmar?")
    ctx.user_data["pending"] = data

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Confirmar", callback_data="confirm"),
         InlineKeyboardButton("Cancelar", callback_data="cancel")],
        [InlineKeyboardButton("Corregir", callback_data="correct")]
    ])

    if tipo == "venta":
        d = data.get("datos", {})
        msg = (
            "VENTA DETECTADA\n\n"
            "Cliente:  " + str(d.get("cliente") or "Sin nombre") + "\n"
            "Servicio: " + str(d.get("servicio_nombre") or "") + "\n"
            "Precio:   " + fmt_mxn(d.get("precio", 0)) + "\n"
            "Metodo:   " + str(d.get("metodo_pago") or "No especificado") + "\n"
            "Pagado:   " + ("Si" if d.get("pagado") else "No") + "\n\n"
            + resumen
        )
        await update.message.reply_text(msg, reply_markup=keyboard)

    elif tipo == "gasto":
        d = data.get("datos", {})
        msg = (
            "GASTO DETECTADO\n\n"
            "Categoria:   " + str(d.get("categoria") or "") + "\n"
            "Descripcion: " + str(d.get("descripcion") or "") + "\n"
            "Monto:       " + fmt_mxn(d.get("monto", 0)) + "\n\n"
            + resumen
        )
        await update.message.reply_text(msg, reply_markup=keyboard)

    elif tipo == "consulta":
        pregunta = (data.get("datos") or {}).get("pregunta", "")
        try:
            s = db.get_monthly_summary()
            context = (
                "Mes: " + s["mes"]
                + " | Ingresos: " + fmt_mxn(s["ingresos"])
                + " | Gastos: " + fmt_mxn(s["gastos"])
                + " | Utilidad: " + fmt_mxn(s["utilidad"])
                + " | Ventas: " + str(s["num_ventas"])
            )
            msg = claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=[{
                    "role": "user",
                    "content": context + "\n\nPregunta: " + pregunta + "\n\nResponde en maximo 2 lineas en espanol."
                }]
            )
            await update.message.reply_text(msg.content[0].text)
        except Exception as e:
            await update.message.reply_text("No pude obtener los datos: " + str(e))

    else:
        await update.message.reply_text("No identifique si es venta, gasto o consulta. Se mas especifica.")


async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action = query.data
    pending = ctx.user_data.get("pending")

    if action == "confirm" and pending:
        try:
            if pending["tipo"] == "venta":
                sale_id = db.add_sale(pending["datos"])
                await query.edit_message_text("Venta registrada! ID: " + sale_id)
            elif pending["tipo"] == "gasto":
                exp_id = db.add_expense(pending["datos"])
                await query.edit_message_text("Gasto registrado! ID: " + exp_id)
            ctx.user_data.pop("pending", None)
        except Exception as e:
            log.error(str(e))
            await query.edit_message_text("Error al guardar: " + str(e))

    elif action == "cancel":
        ctx.user_data.pop("pending", None)
        await query.edit_message_text("Cancelado.")

    elif action == "correct":
        await query.edit_message_text("Escribe la correccion:")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("resumen", cmd_resumen))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))

    log.info("Seleah Bot iniciado")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
