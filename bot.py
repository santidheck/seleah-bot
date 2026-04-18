import os, json, logging, re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
import anthropic
from sheets import SheetsDB

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"]
ALLOWED_USERS  = [int(x) for x in os.environ.get("ALLOWED_USER_IDS","").split(",") if x]

claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
db     = SheetsDB()

# Correcciones foneticas de marcas que Whisper malescribe
CORRECCIONES = {
    "vienox": "Bienox", "bienocks": "Bienox", "vienoks": "Bienox",
    "daisport": "Dysport", "disport": "Dysport", "dysport": "Dysport",
    "seomeen": "Xeomin", "xeomeen": "Xeomin", "seomín": "Xeomin",
    "hidrafacial": "Hydrafacial", "hydrafacial": "Hydrafacial",
    "encimas": "enzimas", "enzimas": "enzimas",
    "empapada": "en papada", "en papada": "en papada",
}

def corregir_texto(texto):
    t = texto.lower()
    for error, correcto in CORRECCIONES.items():
        t = re.sub(r"\b"+error+r"\b", correcto, t, flags=re.IGNORECASE)
    return t


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
    with open(file_path,"rb") as f:
        result = client.audio.transcriptions.create(
            model="whisper-large-v3-turbo", file=f, language="es"
        )
    return result.text


async def extract_data(text: str) -> dict:
    prompt = """Eres el asistente del consultorio de medicina estetica Seleah.
Analiza el mensaje y extrae la informacion. Responde UNICAMENTE con JSON valido sin markdown.

REGLAS CRITICAS:
1. Nombres: si dice "a Sofia" el cliente es "Sofia" (quita la preposicion "a")
2. Marcas EXACTAS: Bienox, Dysport, Botox, Xeomin (nunca Vienox u otras)
3. Hydrafacial siempre con H
4. Enzimas lipoliticas son SERVICIO valido (papada, abdomen, brazos, cuello)
5. Creditos de equipo (Ultraformer, Hydrafacial maquina) son GASTOS categoria Credito
6. Impuestos SAT son GASTOS categoria Impuestos
7. Si menciona "con factura" o "factura" → factura: true

SERVICIOS (IDs exactos):
botox, filler, facial, laser, prp, nctf, enzimas, hilos, biorevitalizacion, peeling, consulta, otro

METODOS: Efectivo, Tarjeta, Transferencia

CATEGORIAS GASTO: Insumos, Renta, Nomina, Servicios, Marketing, Equipo, Credito, Impuestos, Otro

VENTA:
{"tipo":"venta","datos":{"cliente":"nombre","servicio_id":"id","servicio_nombre":"nombre","precio":0,"metodo_pago":"metodo","msi":0,"pagado":true,"factura":false,"notas":null},"resumen_confirmacion":"resumen"}

GASTO:
{"tipo":"gasto","datos":{"categoria":"cat","descripcion":"desc","monto":0},"resumen_confirmacion":"resumen"}

PREGUNTA:
{"tipo":"consulta","datos":{"pregunta":"texto"},"resumen_confirmacion":""}

Mensaje: """ + text + """

Solo JSON."""

    msg = claude.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=500,
        messages=[{"role":"user","content":prompt}]
    )
    raw = msg.content[0].text.strip()
    log.info("Claude: " + raw[:150])
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"): raw = raw[4:]
    raw = raw.strip()
    start = raw.find("{"); end = raw.rfind("}")+1
    if start>=0 and end>start: raw = raw[start:end]
    return json.loads(raw)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    name = update.effective_user.first_name
    await update.message.reply_text(
        "Hola Dra. "+name+"! Soy tu asistente Seleah.\n\n"
        "Manda nota de voz o escribe:\n"
        "- Botox Bienox a Ana, 3500 con tarjeta\n"
        "- Hydrafacial Deluxe a Sofia, 3250 efectivo\n"
        "- Enzimas en papada a Karen, 3500 efectivo\n"
        "- Gasto renta 18000\n"
        "- Pago impuestos SAT 9200\n\n"
        "/resumen - Resumen del mes\n"
        "/top - Top servicios\n"
        "/clientes - Top clientes\n"
        "/recordatorios - Botox a recordar este mes\n"
        "/retencion - Pacientes en riesgo de perderse\n"
        "/facturas - Facturas pendientes del mes"
    )


async def cmd_resumen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    await update.message.reply_text("Calculando...")
    try:
        s = db.get_monthly_summary()
        await update.message.reply_text(
            "Resumen "+s["mes"]+"\n\n"
            "Ingresos:  "+fmt_mxn(s["ingresos"])+"\n"
            "Costos:    "+fmt_mxn(s["costos"])+"\n"
            "Gastos:    "+fmt_mxn(s["gastos"])+"\n"
            "-------------------\n"
            "Utilidad:  "+fmt_mxn(s["utilidad"])+"\n\n"
            +str(s["num_ventas"])+" servicios | "
            +str(s["pendientes"])+" pendientes de pago"
        )
    except Exception as e:
        log.error(str(e))
        await update.message.reply_text("Error: "+str(e))


async def cmd_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    try:
        top = db.get_top_services()
        lines = ["Top Servicios del Mes\n"]
        for i,s in enumerate(top[:5],1):
            lines.append(str(i)+". "+s["nombre"]+" - "+fmt_mxn(s["ingreso"])+" ("+str(s["sesiones"])+" ses.)")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text("Error: "+str(e))


async def cmd_clientes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    try:
        top = db.get_top_clients()
        lines = ["Top Clientes\n"]
        for i,c in enumerate(top,1):
            lines.append(
                str(i)+". "+str(c.get("Nombre",""))+"\n"
                "   Total: "+fmt_mxn(c.get("Total_Historico",0))
                +" | "+str(c.get("Visitas",0))+" visitas"
                +" | Ticket: "+fmt_mxn(c.get("Ticket_Promedio",0))
            )
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text("Error: "+str(e))


async def cmd_recordatorios(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    try:
        recs = db.get_recordatorios_mes()
        if not recs:
            await update.message.reply_text("Sin recordatorios de Botox pendientes para este mes.")
            return
        lines = ["Recordatorios Botox — Este Mes\n"]
        for r in recs:
            lines.append(
                "- "+str(r.get("Cliente",""))+"\n"
                "  Ultimo Botox: "+str(r.get("Ultimo_Botox",""))+"\n"
                "  Tel: "+str(r.get("Telefono","Sin telefono"))
            )
        lines.append("\nEscribe el nombre para marcarlo como recordado.")
        ctx.user_data["modo"] = "marcar_recordatorio"
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text("Error: "+str(e))


async def cmd_retencion(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    try:
        todos = db.get_top_clients(limit=100)
        from datetime import datetime, timedelta
        hoy = datetime.now()
        activos = riesgo = perdidos = 0
        en_riesgo = []
        for c in todos:
            ultima = str(c.get("Ultima_Visita",""))
            if not ultima: continue
            try:
                dt = datetime.strptime(ultima[:10], "%Y-%m-%d")
                dias = (hoy - dt).days
                if dias <= 90:
                    activos += 1
                elif dias <= 150:
                    riesgo += 1
                    en_riesgo.append((c.get("Nombre",""), dias, c.get("Servicios_Top","")))
                else:
                    perdidos += 1
            except: continue

        lines = [
            "Retencion de Clientes\n",
            "Activos (0-90 dias):    "+str(activos),
            "En riesgo (91-150):     "+str(riesgo),
            "Perdidos (+150 dias):   "+str(perdidos),
        ]
        if en_riesgo:
            lines.append("\nEn riesgo este mes:")
            for nombre, dias, svcs in en_riesgo[:8]:
                lines.append("- "+nombre+" ("+str(dias)+" dias) — "+str(svcs)[:30])
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text("Error: "+str(e))


async def cmd_facturas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    try:
        from datetime import datetime
        mes = datetime.now().strftime("%Y-%m")
        ventas = db.sh.worksheet("Ventas").get_all_records()
        con_factura = [v for v in ventas
                      if str(v.get("Fecha","")).startswith(mes)
                      and str(v.get("Notas","")).lower().find("factura") >= 0]
        if not con_factura:
            await update.message.reply_text("Sin facturas pendientes este mes.")
            return
        total = sum(float(v.get("Precio",0)) for v in con_factura)
        lines = ["Facturas Pendientes — "+mes+"\n"]
        for v in con_factura:
            lines.append(
                "- "+str(v.get("Cliente",""))+" — "
                +str(v.get("Servicio_Nombre",""))+" — "
                +fmt_mxn(v.get("Precio",0))
            )
        lines.append("\nTotal a facturar: "+fmt_mxn(total))
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text("Error: "+str(e))


async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    await update.message.reply_text("Escuchando...")
    voice = update.message.voice or update.message.audio
    file  = await ctx.bot.get_file(voice.file_id)
    path  = "/tmp/"+voice.file_id+".ogg"
    await file.download_to_drive(path)
    try:
        transcript = await transcribe_voice(path)
        transcript_corregido = corregir_texto(transcript)
        log.info("Original: "+transcript)
        log.info("Corregido: "+transcript_corregido)
        await update.message.reply_text("Escuche: "+transcript)
        data = await extract_data(transcript_corregido)
        await process_extracted(update, ctx, data)
    except Exception as e:
        log.error("Error voz: "+str(e))
        await update.message.reply_text("No pude procesar. Intentalo de nuevo o escribelo.")
    finally:
        import os as _os
        if _os.path.exists(path): _os.remove(path)


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    text = update.message.text
    if text.startswith("/"): return

    # Modo marcar recordatorio
    if ctx.user_data.get("modo") == "marcar_recordatorio":
        ok = db.marcar_recordatorio_enviado(text.strip())
        ctx.user_data.pop("modo", None)
        if ok:
            await update.message.reply_text("Listo! "+text.strip()+" marcado como recordado.")
        else:
            await update.message.reply_text("No encontre ese paciente en recordatorios.")
        return

    # Cancelar ultimo registro
    if text.strip().lower() in ["cancelar","cancel","deshacer"]:
        last = ctx.user_data.get("last_saved")
        if last:
            await update.message.reply_text(
                "El ultimo registro fue:\n"+last+"\n\n"
                "Ve al Sheet y eliminalo manualmente.\n"
                "Proximamente podre hacerlo automatico."
            )
        else:
            await update.message.reply_text("No tengo registro de la ultima operacion en esta sesion.")
        return

    await update.message.reply_text("Procesando...")
    try:
        texto_corregido = corregir_texto(text)
        data = await extract_data(texto_corregido)
        await process_extracted(update, ctx, data)
    except Exception as e:
        log.error("Error texto: "+str(e))
        await update.message.reply_text("No entendi. Intenta de nuevo.")


async def process_extracted(update: Update, ctx: ContextTypes.DEFAULT_TYPE, data: dict):
    tipo    = data.get("tipo")
    resumen = data.get("resumen_confirmacion","Confirmar?")
    ctx.user_data["pending"] = data

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Confirmar", callback_data="confirm"),
         InlineKeyboardButton("Cancelar",  callback_data="cancel")],
        [InlineKeyboardButton("Corregir",  callback_data="correct")]
    ])

    if tipo == "venta":
        d = data["datos"]
        msi_txt = (" | "+str(d.get("msi"))+" MSI") if d.get("msi") and d.get("msi")>0 else ""
        fac_txt = " | FACTURA" if d.get("factura") else ""
        msg = (
            "VENTA DETECTADA\n\n"
            "Cliente:  "+str(d.get("cliente") or "Sin nombre")+"\n"
            "Servicio: "+str(d.get("servicio_nombre") or "")+"\n"
            "Precio:   "+fmt_mxn(d.get("precio",0))+"\n"
            "Metodo:   "+str(d.get("metodo_pago") or "No especificado")+msi_txt+fac_txt+"\n"
            "Pagado:   "+("Si" if d.get("pagado") else "No")+"\n"
        )
        if d.get("notas"):
            msg += "Notas:    "+str(d.get("notas"))+"\n"
        msg += "\n"+resumen
        await update.message.reply_text(msg, reply_markup=keyboard)

    elif tipo == "gasto":
        d = data["datos"]
        msg = (
            "GASTO DETECTADO\n\n"
            "Categoria:   "+str(d.get("categoria") or "")+"\n"
            "Descripcion: "+str(d.get("descripcion") or "")+"\n"
            "Monto:       "+fmt_mxn(d.get("monto",0))+"\n\n"
            +resumen
        )
        await update.message.reply_text(msg, reply_markup=keyboard)

    elif tipo == "consulta":
        pregunta = data["datos"].get("pregunta","")
        try:
            s = db.get_monthly_summary()
            context = (
                "Mes: "+s["mes"]+" | "
                "Ingresos: "+fmt_mxn(s["ingresos"])+" | "
                "Gastos: "+fmt_mxn(s["gastos"])+" | "
                "Utilidad: "+fmt_mxn(s["utilidad"])+" | "
                "Ventas: "+str(s["num_ventas"])
            )
            msg = claude.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=200,
                messages=[{"role":"user","content":
                    context+"\n\nPregunta: "+pregunta+
                    "\n\nResponde en maximo 2 lineas en espanol con numeros concretos."}]
            )
            await update.message.reply_text(msg.content[0].text)
        except Exception as e:
            await update.message.reply_text("Error: "+str(e))
    else:
        await update.message.reply_text(
            "No identifique si es venta, gasto o consulta.\n"
            "Ejemplo:\n"
            "- Botox Bienox a Ana, 3500 con tarjeta\n"
            "- Gasto renta 18000\n"
            "- Cuanto llevo este mes?"
        )


async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    action  = query.data
    pending = ctx.user_data.get("pending")

    if action == "confirm" and pending:
        try:
            if pending["tipo"] == "venta":
                sale_id = db.add_sale(pending["datos"])
                resumen = (
                    pending["datos"].get("servicio_nombre","")+" - "
                    +str(pending["datos"].get("cliente",""))+" - "
                    +fmt_mxn(pending["datos"].get("precio",0))
                )
                ctx.user_data["last_saved"] = resumen
                msg = "Venta registrada! ID: "+sale_id
                if pending["datos"].get("servicio_id") == "botox":
                    msg += "\nRecordatorio de retoque agendado en 4 meses."
                await query.edit_message_text(msg)
            elif pending["tipo"] == "gasto":
                exp_id = db.add_expense(pending["datos"])
                resumen = (
                    pending["datos"].get("categoria","")+" - "
                    +fmt_mxn(pending["datos"].get("monto",0))
                )
                ctx.user_data["last_saved"] = resumen
                await query.edit_message_text("Gasto registrado! ID: "+exp_id)
            ctx.user_data.pop("pending", None)
        except Exception as e:
            log.error(str(e))
            await query.edit_message_text("Error al guardar: "+str(e))

    elif action == "cancel":
        ctx.user_data.pop("pending", None)
        await query.edit_message_text("Cancelado. No se registro nada.")

    elif action == "correct":
        await query.edit_message_text(
            "Escribe la correccion, por ejemplo:\n"
            "El precio fue 4000 no 3500\n"
            "El cliente es Sofia no Asofia"
        )


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",         cmd_start))
    app.add_handler(CommandHandler("resumen",       cmd_resumen))
    app.add_handler(CommandHandler("top",           cmd_top))
    app.add_handler(CommandHandler("clientes",      cmd_clientes))
    app.add_handler(CommandHandler("recordatorios", cmd_recordatorios))
    app.add_handler(CommandHandler("retencion",     cmd_retencion))
    app.add_handler(CommandHandler("facturas",      cmd_facturas))
    app.add_handler(MessageHandler(filters.VOICE|filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT&~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))
    log.info("Seleah Bot iniciado")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
