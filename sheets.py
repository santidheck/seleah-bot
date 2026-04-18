import os, json, tempfile
from datetime import datetime, timedelta
from typing import Optional
import gspread
from oauth2client.service_account import ServiceAccountCredentials

VENTAS_COLS        = ["ID","Fecha","Cliente","Servicio_ID","Servicio_Nombre","Precio","Costo","Margen","Metodo_Pago","Pagado","Notas","Fuente"]
GASTOS_COLS        = ["ID","Fecha","Categoria","Descripcion","Monto","Fuente"]
CLIENTES_COLS      = ["ID","Nombre","Primera_Visita","Ultima_Visita","Visitas","Total_Historico","Ticket_Promedio","Servicios_Top"]
RECORDATORIOS_COLS = ["Cliente","Telefono","Ultimo_Botox","Fecha_Recordatorio","Mes_Recordatorio","Estado"]

COSTOS = {
    "botox":900,"filler":1800,"laser":400,"facial":350,
    "prp":1200,"hidratacion":600,"peeling":280,
    "biorevitalizacion":950,"consulta":0,"otro":0,
}

BOTOX_IDS = {"botox"}

def uid8():
    import random, string
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=8))

def today_str():
    return datetime.now().strftime("%Y-%m-%d")

def month_str():
    return datetime.now().strftime("%Y-%m")

def get_credentials():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_raw = os.environ.get("GOOGLE_CREDENTIALS_JSON","").strip().strip("'\"")
    if creds_raw:
        creds_dict = json.loads(creds_raw)
        tmp = tempfile.NamedTemporaryFile(mode="w",suffix=".json",delete=False)
        json.dump(creds_dict,tmp); tmp.close()
        return ServiceAccountCredentials.from_json_keyfile_name(tmp.name,scope)
    return ServiceAccountCredentials.from_json_keyfile_name(
        os.environ.get("GOOGLE_CREDENTIALS_FILE","credentials.json"),scope)


class SheetsDB:
    def __init__(self):
        creds   = get_credentials()
        self.gc = gspread.authorize(creds)
        self.sh = self.gc.open_by_key(os.environ["SPREADSHEET_ID"])
        self._ensure_sheets()

    def _get_or_create(self, name):
        try:
            return self.sh.worksheet(name)
        except gspread.WorksheetNotFound:
            return self.sh.add_worksheet(title=name,rows=2000,cols=20)

    def _ensure_sheets(self):
        for name, cols in [
            ("Ventas",          VENTAS_COLS),
            ("Gastos",          GASTOS_COLS),
            ("Clientes",        CLIENTES_COLS),
            ("Recordatorios",   RECORDATORIOS_COLS),
        ]:
            ws = self._get_or_create(name)
            if not ws.row_values(1):
                ws.update("A1",[cols])

    # ── ventas ────────────────────────────────────────────────────────────────
    def add_sale(self, d: dict) -> str:
        ws      = self.sh.worksheet("Ventas")
        sale_id = uid8()
        costo   = COSTOS.get(d.get("servicio_id","otro"),0)
        precio  = float(d.get("precio",0))
        fecha   = today_str()
        row = [
            sale_id, fecha,
            d.get("cliente") or "Sin nombre",
            d.get("servicio_id","otro"),
            d.get("servicio_nombre","Servicio"),
            precio, costo, round(precio-costo,2),
            d.get("metodo_pago") or "No especificado",
            "Pagado" if d.get("pagado",True) else "Pendiente",
            d.get("notas") or "",
            "Telegram",
        ]
        ws.append_row(row,value_input_option="USER_ENTERED")
        self._update_client(d.get("cliente"),precio,fecha,d.get("servicio_nombre",""))
        if d.get("servicio_id","") in BOTOX_IDS:
            self._update_recordatorio(d.get("cliente"),fecha)
        return sale_id

    # ── gastos ────────────────────────────────────────────────────────────────
    def add_expense(self, d: dict) -> str:
        ws = self.sh.worksheet("Gastos")
        ws.append_row([
            uid8(), today_str(),
            d.get("categoria","Otro"),
            d.get("descripcion",""),
            float(d.get("monto",0)),
            "Telegram",
        ],value_input_option="USER_ENTERED")
        return uid8()

    # ── clientes ──────────────────────────────────────────────────────────────
    def _update_client(self, name: Optional[str], amount: float, fecha: str, servicio: str):
        if not name or name=="Sin nombre":
            return
        ws      = self.sh.worksheet("Clientes")
        records = ws.get_all_records()
        existing = next((r for r in records if str(r.get("Nombre","")).lower()==name.lower()),None)
        if existing:
            idx     = records.index(existing)+2
            visitas = int(existing.get("Visitas",0))+1
            total   = float(existing.get("Total_Historico",0))+amount
            ticket  = round(total/visitas,2)
            svcs    = [s.strip() for s in str(existing.get("Servicios_Top","")).split(",") if s.strip()]
            if servicio and servicio not in svcs:
                svcs.append(servicio)
            ws.update(f"D{idx}:H{idx}",[[fecha,visitas,total,ticket,", ".join(svcs)]])
        else:
            ws.append_row([uid8(),name,fecha,fecha,1,amount,amount,servicio],
                          value_input_option="USER_ENTERED")

    # ── recordatorios botox ───────────────────────────────────────────────────
    def _update_recordatorio(self, name: Optional[str], fecha_botox: str):
        if not name or name=="Sin nombre":
            return
        ws      = self.sh.worksheet("Recordatorios")
        records = ws.get_all_records()

        # Calcular fecha de recordatorio = fecha_botox + 4 meses
        dt_botox = datetime.strptime(fecha_botox,"%Y-%m-%d")
        dt_rec   = dt_botox + timedelta(days=120)
        fecha_rec = dt_rec.strftime("%Y-%m-%d")
        mes_rec   = dt_rec.strftime("%Y-%m")

        existing = next((r for r in records if str(r.get("Cliente","")).lower()==name.lower()),None)
        if existing:
            idx = records.index(existing)+2
            # Solo actualizar si la nueva fecha de botox es mas reciente
            prev_botox = str(existing.get("Ultimo_Botox",""))
            if fecha_botox > prev_botox:
                ws.update(f"C{idx}:F{idx}",[[fecha_botox,fecha_rec,mes_rec,"Pendiente"]])
        else:
            # Buscar telefono en Clientes
            cli_records = self.sh.worksheet("Clientes").get_all_records()
            cli = next((r for r in cli_records if str(r.get("Nombre","")).lower()==name.lower()),None)
            tel = str(cli.get("Telefono","")) if cli else ""
            ws.append_row([name,tel,fecha_botox,fecha_rec,mes_rec,"Pendiente"],
                          value_input_option="USER_ENTERED")

    def marcar_recordatorio_enviado(self, nombre: str):
        ws      = self.sh.worksheet("Recordatorios")
        records = ws.get_all_records()
        existing = next((r for r in records if str(r.get("Cliente","")).lower()==nombre.lower()),None)
        if existing:
            idx = records.index(existing)+2
            ws.update_cell(idx,6,"Enviado")
            return True
        return False

    def get_recordatorios_mes(self, year_month: str = None) -> list:
        ym      = year_month or month_str()
        records = self.sh.worksheet("Recordatorios").get_all_records()
        return [r for r in records
                if str(r.get("Mes_Recordatorio","")).startswith(ym)
                and r.get("Estado","") != "Enviado"]

    def limpiar_recordatorios_anteriores(self):
        ym      = month_str()
        ws      = self.sh.worksheet("Recordatorios")
        records = ws.get_all_records()
        for i, r in enumerate(records):
            mes = str(r.get("Mes_Recordatorio",""))
            if mes < ym and r.get("Estado","") == "Enviado":
                row_idx = i+2
                ws.delete_rows(row_idx)

    # ── resumen mensual ───────────────────────────────────────────────────────
    def get_monthly_summary(self, year_month: str = None) -> dict:
        ym      = year_month or month_str()
        ventas  = self.sh.worksheet("Ventas").get_all_records()
        gastos  = self.sh.worksheet("Gastos").get_all_records()
        mv      = [v for v in ventas if str(v.get("Fecha","")).startswith(ym)]
        mg      = [g for g in gastos  if str(g.get("Fecha","")).startswith(ym)]
        pagadas = [v for v in mv if v.get("Pagado")=="Pagado"]
        ingresos = sum(float(v.get("Precio",0)) for v in pagadas)
        costos   = sum(float(v.get("Costo",0))  for v in pagadas)
        gastos_t = sum(float(g.get("Monto",0))  for g in mg)
        return {
            "mes":        ym,
            "ingresos":   ingresos,
            "costos":     costos,
            "gastos":     gastos_t,
            "utilidad":   ingresos-costos-gastos_t,
            "num_ventas": len(mv),
            "num_gastos": len(mg),
            "pendientes": len([v for v in mv if v.get("Pagado")=="Pendiente"]),
        }

    def get_top_services(self, year_month: str = None) -> list:
        ym  = year_month or month_str()
        mes = [v for v in self.sh.worksheet("Ventas").get_all_records()
               if str(v.get("Fecha","")).startswith(ym) and v.get("Pagado")=="Pagado"]
        svc = {}
        for v in mes:
            n = v.get("Servicio_Nombre","Otro")
            if n not in svc:
                svc[n] = {"nombre":n,"sesiones":0,"ingreso":0,"margen":0}
            svc[n]["sesiones"] += 1
            svc[n]["ingreso"]  += float(v.get("Precio",0))
            svc[n]["margen"]   += float(v.get("Margen",0))
        return sorted(svc.values(),key=lambda x:x["ingreso"],reverse=True)

    def get_top_clients(self, limit: int = 5) -> list:
        records = self.sh.worksheet("Clientes").get_all_records()
        return sorted(
            [r for r in records if r.get("Nombre")],
            key=lambda x: float(x.get("Total_Historico",0)),
            reverse=True
        )[:limit]

    def get_pending_payments(self) -> list:
        return [v for v in self.sh.worksheet("Ventas").get_all_records()
                if v.get("Pagado")=="Pendiente"]
