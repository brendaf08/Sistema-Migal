"""Agente de cadena de suministro. Responsable: Alejandro Flores.

Punto de partida real (ya funciona): cobertura, compras pendientes y estado de recepción.
Por hacer (ficha producto/agentes/suministro.md): consultar_baja_rotacion (la usa marketing; hoy responde el
simulador), sugerir reposición con las reglas de stock mínimo, borrador de orden de compra, comparar
proveedores, despachos pendientes y recepción con OCR de la guía.
"""
import logging
import re

from markupsafe import Markup

from odoo import models
from odoo.tools import format_date
from odoo.tools.float_utils import float_compare

from odoo.addons.migal_agente.agent.contracts import DATE, ORDER_REF

_logger = logging.getLogger(__name__)

OC_REF = {"type": "string", "description": "Referencia de la orden de compra, p. ej. P00020.",
          "pattern": r"^[A-Za-z]{1,4}\d{3,}$", "maxLength": 20}


class AgenteSuministro(models.AbstractModel):
    _name = "migal.agente.suministro"
    _inherit = "migal.agente.especialista"
    _description = "Agente MIGAL · Cadena de suministro"
    _agente_codigo = "suministro"
    _agente_nombre = "Cadena de suministro"

    # ------------------------------------------------------------------
    # Contratos
    # ------------------------------------------------------------------
    def _herramientas(self):
        return {
            "evaluar_cobertura_preliminar": {
                "description": (
                    "Revisa por producto y cantidad si una cotización vigente puede cubrirse con el stock "
                    "disponible en tienda y las reposiciones previstas hasta la fecha solicitada. Aplica la "
                    "regla habitual para cementos, agregados, fierros y ladrillos. No reserva stock, no crea "
                    "SOV ni compras y no garantiza entregas."),
                "input_schema": {"type": "object", "properties": {"cotizacion": ORDER_REF, "fecha_requerida": DATE},
                                 "required": ["cotizacion"], "additionalProperties": False},
                "roles": ["VEN", "ALM", "CMP", "GER"],
                "escritura": "Ninguna (solo bitácora).",
                "reglas": ["RN-011", "RN-015"],
                "metodo": "evaluar_cobertura_preliminar",
            },
            "listar_compras_pendientes": {
                "description": "Órdenes de compra por aprobar, por recibir y atrasadas.",
                "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
                "roles": ["CMP", "ALM", "GER"],
                "escritura": "Ninguna (solo bitácora).",
                "reglas": ["RN-012"],
                "metodo": "listar_compras_pendientes",
            },
            "consultar_recepcion": {
                "description": "Cantidades pedidas, recibidas y pendientes de una orden de compra.",
                "input_schema": {"type": "object", "properties": {"orden_compra": OC_REF},
                                 "required": ["orden_compra"], "additionalProperties": False},
                "roles": ["CMP", "ALM", "FCO", "GER"],
                "escritura": "Ninguna (solo bitácora).",
                "reglas": ["RN-013"],
                "metodo": "consultar_recepcion",
            },
        }

    def _eventos_escuchados(self):
        return {"EV-018": "_al_confirmarse_venta"}

    def _reconocer(self, texto):
        m = re.search(r"\b(recepcion|recibido|llego)\b.*\b(p\d{5})\b", texto)
        if m:
            return "consultar_recepcion", {"orden_compra": m.group(2).upper()}
        if re.search(r"\b(compras? pendientes?|ordenes? de compra|que falta recibir|por recibir|oc pendientes?)\b", texto):
            return "listar_compras_pendientes", {}
        return None

    def _acciones_widget(self, record):
        if record._name == "purchase.order" and record.state == "purchase":
            return [{"id": "consultar_recepcion", "label": "Estado de recepción", "icon": "fa-truck",
                     "descripcion": "Pedido, recibido y pendiente por producto", "principal": True}]
        return []

    def _argumentos_widget(self, accion, record, params):
        if accion == "consultar_recepcion":
            return {"orden_compra": record.name}
        return None

    # ------------------------------------------------------------------
    # Herramientas
    # ------------------------------------------------------------------
    def evaluar_cobertura_preliminar(self, cotizacion, fecha_requerida=None):
        return self.env["migal.agente.tool.cobertura"].evaluar(cotizacion, fecha_requerida)

    def listar_compras_pendientes(self):
        hoy = self._hoy()
        PO = self.env["purchase.order"]
        por_aprobar = PO.search([("state", "in", ("draft", "sent"))], order="date_order")
        por_recibir = PO.search([("state", "=", "purchase"), ("receipt_status", "in", ("pending", "partial"))],
                                order="date_planned")
        atrasadas = por_recibir.filtered(lambda o: o.date_planned and o.date_planned.date() < hoy)
        filas = []
        for po in por_aprobar:
            filas.append([po.name, po.partner_id.display_name, "Por aprobar (GER)", "-", "S/ {:,.2f}".format(po.amount_total)])
        for po in por_recibir:
            estado = "Atrasada" if po in atrasadas else ("Parcial" if po.receipt_status == "partial" else "Por recibir")
            filas.append([po.name, po.partner_id.display_name, estado,
                          format_date(self.env, po.date_planned) if po.date_planned else "-",
                          "S/ {:,.2f}".format(po.amount_total)])
        mensaje = "%s por aprobar, %s por recibir (%s atrasadas)." % (len(por_aprobar), len(por_recibir), len(atrasadas))
        avisos = ["Confirmar una orden de compra lo autoriza Gerencia (RN-012)."] if por_aprobar else []
        if atrasadas:
            avisos.append("Hay recepciones atrasadas: incumplimiento de abastecimiento (EV-024).")
        return self._resultado(True, "ok", "Compras pendientes", mensaje, reglas=["RN-012"], avisos=avisos, datos={
            "por_aprobar": por_aprobar.mapped("name"), "por_recibir": por_recibir.mapped("name"),
            "atrasadas": atrasadas.mapped("name"),
            "tabla": {"columnas": ["Orden", "Proveedor", "Estado", "Fecha prevista", "Total"], "filas": filas}})

    def consultar_recepcion(self, orden_compra):
        po = self.env["purchase.order"].search([("name", "=", orden_compra.upper())], limit=1)
        if not po:
            return self._resultado(False, "error", "Estado de recepción",
                                   "No encontré la orden de compra %s." % orden_compra)
        productos = []
        for linea in po.order_line.filtered(lambda l: l.product_id and not l.display_type):
            productos.append({"producto": linea.product_id.display_name, "pedido": linea.product_qty,
                              "recibido": linea.qty_received,
                              "pendiente": max(linea.product_qty - linea.qty_received, 0.0)})
        pendiente = sum(p["pendiente"] for p in productos)
        recibido = sum(p["recibido"] for p in productos)
        estado = "completa" if not pendiente else ("parcial" if recibido else "pendiente")
        texto = {"completa": "Recepción completa.", "parcial": "Recepción parcial: faltan %g unidades." % pendiente,
                 "pendiente": "Aún no se recibe nada."}[estado]
        return self._resultado(True, estado, "Recepción de %s" % po.name, "%s · %s" % (po.partner_id.display_name, texto),
                               res=po, reglas=["RN-013"], datos={
                                   "orden_compra": po.name, "estado_recepcion": estado, "productos": productos,
                                   "tabla": {"columnas": ["Producto", "Pedido", "Recibido", "Pendiente"],
                                             "filas": [["%s" % p["producto"], "{:g}".format(p["pedido"]),
                                                        "{:g}".format(p["recibido"]), "{:g}".format(p["pendiente"])]
                                                       for p in productos]}})

    # ------------------------------------------------------------------
    # Eventos
    # ------------------------------------------------------------------
    def _al_confirmarse_venta(self, datos):
        """EV-018. Deja una nota interna si la salida deja stock previsto bajo el mínimo."""
        referencia = datos.get("orden")
        if not referencia:
            return

        orden = self.env["sale.order"].search([
            ("name", "=", referencia),
            ("company_id", "in", self.env.companies.ids),
            ("state", "=", "sale"),
        ], limit=2)
        if len(orden) != 1 or not orden.warehouse_id.lot_stock_id:
            return

        orden = orden.with_company(orden.company_id)
        asunto = "EV-018 · Alerta de reposición"
        if self.env["mail.message"].search_count([
            ("model", "=", orden._name),
            ("res_id", "=", orden.id),
            ("subject", "=", asunto),
        ], limit=1):
            return

        movimientos = orden.order_line.move_ids.filtered(
            lambda m: m.state != "cancel" and m.location_id.usage == "internal"
        )
        if not movimientos:
            return

        reglas = self.env["stock.warehouse.orderpoint"].with_company(orden.company_id).search([
            ("company_id", "=", orden.company_id.id),
            ("location_id", "child_of", orden.warehouse_id.lot_stock_id.id),
            ("location_id", "parent_of", movimientos.location_id.ids),
            ("product_id", "in", movimientos.product_id.ids),
            ("trigger", "=", "manual"),
        ])
        origenes_por_producto = {}
        for movimiento in movimientos:
            origenes_por_producto.setdefault(movimiento.product_id.id, set()).update(
                int(ubicacion) for ubicacion in movimiento.location_id.parent_path.split("/") if ubicacion
            )
        reglas = reglas.filtered(
            lambda regla: regla.location_id.id in origenes_por_producto[regla.product_id.id]
        )
        filas = []
        for regla in reglas:
            previsto = regla.qty_forecast
            minimo = regla.product_min_qty
            if float_compare(previsto, minimo, precision_rounding=regla.product_uom.rounding) >= 0:
                continue
            filas.append(Markup("<li><strong>%s</strong> · %s · Previsto: %g %s · Mínimo: %g %s</li>") % (
                regla.product_id.display_name, regla.location_id.complete_name,
                previsto, regla.product_uom.name, minimo, regla.product_uom.name,
            ))

        if filas:
            cuerpo = Markup(
                "<p><strong>EV-018 · Alerta de reposición</strong></p>"
                "<p>Productos de esta venta con stock previsto bajo el mínimo. "
                "Compras o Almacén pueden revisar la reposición.</p><ul>%s</ul>"
            ) % Markup("").join(filas)
            orden.message_post(body=cuerpo, subject=asunto, subtype_xmlid="mail.mt_note")
