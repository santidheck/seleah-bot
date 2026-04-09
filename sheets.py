"""
sheets.py — Capa de acceso a Google Sheets para Seleah.

Hoja 1: Ventas   | Hoja 2: Gastos | Hoja 3: Clientes | Hoja 4: Servicios
"""

import os, json
from datetime import datetime
from typing import Optional

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ── Columnas de cada hoja ────────────────────────────────────────────────────

VENTAS_COLS = [
    "ID", "Fecha", "Cliente", "Servicio_ID", "Servicio_Nombre",
    "Precio", "Costo", "Margen", "Metodo_Pago", "Pagado", "Notas", "Fuente"
]

GASTOS_COLS = [
    "ID", "Fecha", "Categoria", "Descripcion", "Monto", "Fuente"
]

CLIENTES_COLS = [
    "ID", "Nombre", "Telefono", "Email",
    "Primera_Visita", "Visitas", "Total_Historico"
]

# Costos por servicio (actualizables en el catálogo)
COSTOS = {
    "botox": 900,
    "filler": 1800,
    "laser": 400,
    "facial": 350,
    "prp": 1200,
    "hidratacion": 600,
    "peeling": 280,
    "biorevitalizacion": 950,
    "consulta": 0,
    "otro": 0,
}


def uid8():
    import random, string
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=8))


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def month_str():
    return datetime.now().strftime("%Y-%m")


class SheetsDB:

    def __init__(self):
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]

        # Credentials from env var (JSON string) or file path
        creds_raw = os.environ.get("GOOGLE_CREDENTIALS_JSON")

        if creds_raw:
            import tempfile
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False
            )
            tmp.write(creds_raw)
            tmp.close()
            creds = ServiceAccountCredentials.from_json_keyfile_name(
                tmp.name, scope
            )
        else:
            creds = ServiceAccountCredentials.from_json_keyfile_name(
                os.environ.get("GOOGLE_CREDENTIALS_FILE", "credentials.json"),
                scope
            )

        self.gc = gspread.authorize(creds)
        self.sh = self.gc.open_by_key(os.environ["SPREADSHEET_ID"])
        self._ensure_sheets()

    # ── setup ────────────────────────────────────────────────────────────────

    def _get_or_create(self, name: str):
        try:
            return self.sh.worksheet(name)
        except gspread.WorksheetNotFound:
            return self.sh.add_worksheet(title=name, rows=1000, cols=20)

    def _ensure_sheets(self):
        """Create sheets and headers if they don't exist."""
        sheets_config = {
            "Ventas": VENTAS_COLS,
            "Gastos": GASTOS_COLS,
            "Clientes": CLIENTES_COLS,
        }

        for name, cols in sheets_config.items():
            ws = self._get_or_create(name)
            existing = ws.row_values(1)
            if not existing:
                ws.update("A1", [cols])
                ws.format("A1:Z1", {
                    "textFormat": {"bold": True},
                    "backgroundColor": {"red": 0.12, "green": 0.08, "blue": 0.05}
                })

    # ── ventas ───────────────────────────────────────────────────────────────

    def add_sale(self, d: dict) -> str:
        ws = self.sh.worksheet("Ventas")

        sale_id = uid8()
        costo = COSTOS.get(d.get("servicio_id", "otro"), 0)
        precio = float(d.get("precio", 0))

        row = [
            sale_id,
            d.get("fecha") or today_str(),
            d.get("cliente") or "Sin nombre",
            d.get("servicio_id", "otro"),
            d.get("servicio_nombre", "Servicio"),
            precio,
            costo,
            precio - costo,
            d.get("metodo_pago") or "No especificado",
            "Pagado" if d.get("pagado", True) else "Pendiente",
            d.get("notas") or "",
            "Telegram",
        ]

        ws.append_row(row, value_input_option="USER_ENTERED")
        self._update_client(d.get("cliente"), precio)
        return sale_id

    # ── gastos ───────────────────────────────────────────────────────────────

    def add_expense(self, d: dict) -> str:
        ws = self.sh.worksheet("Gastos")

        exp_id = uid8()
        row = [
            exp_id,
            d.get("fecha") or today_str(),
            d.get("categoria", "Otro"),
            d.get("descripcion", ""),
            float(d.get("monto", 0)),
            "Telegram",
        ]

        ws.append_row(row, value_input_option="USER_ENTERED")
        return exp_id

    # ── clientes ─────────────────────────────────────────────────────────────

    def _update_client(self, name: Optional[str], amount: float):
        if not name or name == "Sin nombre":
            return

        ws = self.sh.worksheet("Clientes")
        records = ws.get_all_records()

        existing = next(
            (r for r in records if r["Nombre"].lower() == name.lower()),
            None
        )

        if existing:
            row_idx = records.index(existing) + 2  # +2 header
            ws.update_cell(row_idx, 6, int(existing["Visitas"]) + 1)
            ws.update_cell(
                row_idx, 7,
                float(existing["Total_Historico"]) + amount
            )
        else:
            ws.append_row([
                uid8(), name, "", "", today_str(), 1, amount
            ], value_input_option="USER_ENTERED")

    # ── queries ──────────────────────────────────────────────────────────────

    def get_monthly_summary(self, year_month: str = None) -> dict:
        ym = year_month or month_str()

        ventas_ws = self.sh.worksheet("Ventas")
        gastos_ws = self.sh.worksheet("Gastos")

        ventas = ventas_ws.get_all_records()
        gastos = gastos_ws.get_all_records()

        mes_ventas = [
            v for v in ventas
            if str(v.get("Fecha", "")).startswith(ym)
        ]
        mes_gastos = [
            g for g in gastos
            if str(g.get("Fecha", "")).startswith(ym)
        ]

        pagadas = [v for v in mes_ventas if v.get("Pagado") == "Pagado"]

        ingresos = sum(float(v.get("Precio", 0)) for v in pagadas)
        costos = sum(float(v.get("Costo", 0)) for v in pagadas)
        gastos_t = sum(float(g.get("Monto", 0)) for g in mes_gastos)

        return {
            "mes": ym,
            "ingresos": ingresos,
            "costos": costos,
            "gastos": gastos_t,
            "utilidad": ingresos - costos - gastos_t,
            "num_ventas": len(mes_ventas),
            "num_gastos": len(mes_gastos),
            "pendientes": len([
                v for v in mes_ventas if v.get("Pagado") == "Pendiente"
            ]),
        }

    def get_top_services(self, year_month: str = None) -> list:
        ym = year_month or month_str()
        ws = self.sh.worksheet("Ventas")
        ventas = ws.get_all_records()

        mes = [
            v for v in ventas
            if str(v.get("Fecha", "")).startswith(ym)
            and v.get("Pagado") == "Pagado"
        ]

        svc_map = {}
        for v in mes:
            sid = v.get("Servicio_Nombre", "Otro")
            if sid not in svc_map:
                svc_map[sid] = {
                    "nombre": sid,
                    "sesiones": 0,
                    "ingreso": 0,
                    "margen": 0
                }
            svc_map[sid]["sesiones"] += 1
            svc_map[sid]["ingreso"] += float(v.get("Precio", 0))
            svc_map[sid]["margen"] += float(v.get("Margen", 0))

        return sorted(
            svc_map.values(),
            key=lambda x: x["ingreso"],
            reverse=True
        )

    def get_pending_payments(self) -> list:
        ws = self.sh.worksheet("Ventas")
        ventas = ws.get_all_records()
        return [v for v in ventas if v.get("Pagado") == "Pendiente"]
