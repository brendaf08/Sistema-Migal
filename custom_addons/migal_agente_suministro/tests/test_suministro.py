"""Pruebas del agente de cadena de suministro (caso normal, borde, permiso y evento)."""
from datetime import timedelta

from odoo import Command, fields
from odoo.tests import TransactionCase, new_test_user, tagged


@tagged("post_install", "-at_install", "migal_agente", "migal_agente_suministro")
class TestAgenteSuministro(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["ir.config_parameter"].sudo().set_param("migal_agente.llm_enabled", "False")
        cls.cmp = new_test_user(cls.env, login="cmp_sum", groups="purchase.group_purchase_user,stock.group_stock_user")
        cls.ven = new_test_user(cls.env, login="ven_sum", groups="sales_team.group_sale_salesman")
        cls.proveedor = cls.env["res.partner"].create({"name": "Proveedor prueba suministro"})
        cls.producto = cls.env["product.product"].create({"name": "Producto prueba suministro", "is_storable": True,
                                                           "purchase_ok": True})
        cls.po = cls.env["purchase.order"].create({"partner_id": cls.proveedor.id, "order_line": [
            Command.create({"product_id": cls.producto.id, "product_qty": 10})]})
        cls.po.button_confirm()

    def _agente(self, user):
        return self.env["migal.agente"].with_user(user)

    def test_baja_rotacion_excluye_salida_pos(self):
        almacen = self.po.picking_type_id.warehouse_id
        ubicacion = almacen.lot_stock_id
        productos = self.env["product.product"].create([
            {"name": "[PRUEBA] Sin ventas", "is_storable": True},
            {"name": "[PRUEBA] Vendido por POS", "is_storable": True},
            {"name": "[PRUEBA] Sin existencias", "is_storable": True},
        ])
        sin_ventas, vendido_pos, sin_stock = productos
        quant = self.env["stock.quant"]
        quant._update_available_quantity(sin_ventas, ubicacion, 10)
        quant._update_available_quantity(vendido_pos, ubicacion, 10)

        caja = self.env["pos.config"].create({"name": "[PRUEBA] Caja baja rotación"})
        sesion = self.env["pos.session"].create({"config_id": caja.id})
        destino = self.env.ref("stock.stock_location_customers")
        picking = self.env["stock.picking"].create({
            "picking_type_id": almacen.out_type_id.id,
            "location_id": ubicacion.id,
            "location_dest_id": destino.id,
            "pos_session_id": sesion.id,
        })
        movimiento = self.env["stock.move"].create({
            "product_id": vendido_pos.id,
            "product_uom_qty": 2,
            "product_uom": vendido_pos.uom_id.id,
            "location_id": ubicacion.id,
            "location_dest_id": destino.id,
            "picking_id": picking.id,
        })
        picking.action_confirm()
        picking.action_assign()
        movimiento.quantity = 2
        movimiento.picked = True
        picking._action_done()
        self.assertEqual(movimiento.state, "done")
        self.assertEqual(vendido_pos.qty_available, 8)

        resultado = self._agente(self.ven).consultar("marketing", "consultar_baja_rotacion", {})
        self.assertTrue(resultado["ok"], resultado["mensaje"])
        self.assertFalse(resultado.get("simulado"))
        self.assertEqual(resultado["datos"]["dias"], 90)
        filas = {p["producto"]: p for p in resultado["datos"]["productos"]}
        self.assertEqual(filas[sin_ventas.display_name]["stock"], 10)
        self.assertEqual(filas[sin_ventas.display_name]["vendido"], 0)
        self.assertNotIn(vendido_pos.display_name, filas)
        self.assertNotIn(sin_stock.display_name, filas)

    def test_baja_rotacion_chat_con_ventana(self):
        html, log = self._agente(self.ven).procesar_mensaje("inventario sin ventas en 30 días", canal="prueba")
        self.assertIn("en 30 días", html)
        self.assertIn("consultar_baja_rotacion", log.tools_invoked)
        self.assertEqual(log.agente, "suministro")

    def test_baja_rotacion_restringe_rol(self):
        usuario = new_test_user(self.env, login="sin_rol_rotacion", groups="base.group_user")
        resultado = self._agente(usuario).ejecutar_herramienta("consultar_baja_rotacion", {})
        self.assertEqual(resultado["estado"], "sin_permiso")


    def test_registro_y_herramientas(self):
        self.assertIn("suministro", self.env["migal.agente"]._agentes())
        spec = self.env["migal.agente"]._spec("evaluar_cobertura_preliminar")
        self.assertEqual(spec["agente"], "suministro", "La cobertura pertenece a cadena de suministro")

    def test_compras_pendientes_y_recepcion(self):
        res = self._agente(self.cmp).ejecutar_herramienta("listar_compras_pendientes", {})
        self.assertTrue(res["ok"])
        self.assertIn(self.po.name, res["datos"]["por_recibir"])
        res = self._agente(self.cmp).ejecutar_herramienta("consultar_recepcion", {"orden_compra": self.po.name})
        self.assertEqual(res["estado"], "pendiente")

    def test_recepcion_parcial_emite_ev027(self):
        picking = self.po.picking_ids
        picking.move_ids.quantity = 6
        accion = picking.button_validate()
        if isinstance(accion, dict) and accion.get("res_model") == "stock.backorder.confirmation":
            self.env["stock.backorder.confirmation"].with_context(accion["context"]).create({}).process()
        res = self._agente(self.cmp).ejecutar_herramienta("consultar_recepcion", {"orden_compra": self.po.name})
        self.assertEqual(res["estado"], "parcial")
        self.assertEqual(res["datos"]["productos"][0]["pendiente"], 4)
        log = self.env["migal.agente.log"].sudo().search([("canal", "=", "evento"), ("input_prompt", "like", "EV-027")],
                                                          order="id desc", limit=1)
        self.assertTrue(log, "La recepción validada emite EV-027")

    def test_diagnostico_faltantes_separa_ubicaciones_y_fecha(self):
        almacen = self.po.picking_type_id.warehouse_id
        origen = almacen.lot_stock_id
        auxiliar = self.env["stock.location"].create({
            "name": "[PRUEBA] Auxiliar diagnóstico",
            "usage": "internal",
            "location_id": origen.id,
            "company_id": origen.company_id.id,
        })
        producto = self.env["product.product"].create({
            "name": "[PRUEBA] Bolsas diagnóstico <demo>",
            "is_storable": True,
            "purchase_ok": True,
        })
        quant = self.env["stock.quant"]
        quant._update_available_quantity(producto, origen, 40)
        quant._update_available_quantity(producto, auxiliar, 30)

        orden = self.env["sale.order"].create({
            "partner_id": self.proveedor.id,
            "warehouse_id": almacen.id,
            "user_id": self.ven.id,
            "order_line": [Command.create({"product_id": producto.id, "product_uom_qty": 100})],
        })
        llegada = fields.Datetime.now() + timedelta(days=2)
        compra = self.env["purchase.order"].create({
            "partner_id": self.proveedor.id,
            "picking_type_id": almacen.in_type_id.id,
            "order_line": [Command.create({
                "product_id": producto.id, "product_qty": 50, "date_planned": llegada,
            })],
        })
        compra.button_confirm()
        self.assertEqual(compra.state, "purchase")
        self.assertTrue(compra.picking_ids.move_ids)

        vendedor = self._agente(self.ven)
        fecha = fields.Date.context_today(vendedor) + timedelta(days=1)
        args = {"cotizacion": orden.name, "fecha_requerida": fields.Date.to_string(fecha)}
        resultado = vendedor.ejecutar_herramienta("diagnosticar_faltantes", args)
        self.assertTrue(resultado["ok"], resultado["mensaje"])
        linea = resultado["datos"]["detalle_faltantes"][0]
        self.assertEqual(linea["libre_origen"], 40)
        self.assertEqual(linea["recepcion_en_plazo"], 0)
        self.assertEqual(linea["saldo_sin_traslado"], 60)
        self.assertEqual(linea["traslados"], [{"ubicacion": auxiliar.complete_name, "cantidad": 30}])
        self.assertEqual(linea["saldo_si_traslado"], 30)

        args["fecha_requerida"] = fields.Date.to_string(fecha + timedelta(days=1))
        posterior = vendedor.ejecutar_herramienta("diagnosticar_faltantes", args)
        self.assertTrue(posterior["ok"], posterior["mensaje"])
        linea_posterior = posterior["datos"]["detalle_faltantes"][0]
        self.assertEqual(linea_posterior["recepcion_en_plazo"], 50)
        self.assertEqual(linea_posterior["saldo_sin_traslado"], 10)
        self.assertEqual(linea_posterior["traslados"][0]["cantidad"], 10)
        self.assertEqual(linea_posterior["saldo_si_traslado"], 0)

        html, log = vendedor.procesar_mensaje(f"diagnóstico de {orden.name} para {fecha}", canal="prueba")
        self.assertIn("Diagnóstico de faltantes", html)
        self.assertEqual(html.count("<th>"), 1)
        self.assertIn("Bolsas diagnóstico &lt;demo&gt;", html)
        self.assertIn("<strong>Traslado posible</strong> 30 desde", html)
        self.assertIn("<strong>Saldo si se traslada</strong> 30", html)
        self.assertIn("diagnosticar_faltantes", log.tools_invoked)
        self.assertEqual(log.agente, "suministro")

    def test_diagnostico_faltantes_restringe_rol(self):
        usuario = new_test_user(self.env, login="sin_rol_diagnostico", groups="base.group_user")
        resultado = self._agente(usuario).ejecutar_herramienta(
            "diagnosticar_faltantes", {"cotizacion": "S00001", "fecha_requerida": "2099-01-01"},
        )
        self.assertEqual(resultado["estado"], "sin_permiso")

    def test_reposicion_minimos_considera_recepcion_pendiente(self):
        almacen = self.po.picking_type_id.warehouse_id
        ubicacion = almacen.lot_stock_id
        producto = self.env["product.product"].create({
            "name": "[PRUEBA] Reposición mínima",
            "default_code": "PRUEBA-MIN-SUM",
            "is_storable": True,
            "purchase_ok": True,
        })
        self.env["stock.quant"]._update_available_quantity(producto, ubicacion, 40)
        self.env["stock.warehouse.orderpoint"].create({
            "product_id": producto.id,
            "warehouse_id": almacen.id,
            "location_id": ubicacion.id,
            "product_min_qty": 100,
            "product_max_qty": 200,
            "trigger": "manual",
        })

        agente = self._agente(self.cmp)
        args = {"producto": "prueba-min-sum"}
        resultado = agente.ejecutar_herramienta("sugerir_reposicion_minimos", args)
        self.assertTrue(resultado["ok"], resultado["mensaje"])
        sugerencias = resultado["datos"]["sugerencias"]
        self.assertEqual(len(sugerencias), 1)
        self.assertEqual(sugerencias[0]["previsto"], 40)
        self.assertEqual(sugerencias[0]["sugerido"], 160)

        html, log = agente.procesar_mensaje("reposición por mínimos de PRUEBA-MIN-SUM", canal="prueba")
        self.assertIn("Sugerencia de reposición", html)
        self.assertIn("sugerir_reposicion_minimos", log.tools_invoked)
        self.assertEqual(log.agente, "suministro")
        self.assertFalse(self.env["purchase.order.line"].search_count([("product_id", "=", producto.id)]))

        compra = self.env["purchase.order"].create({
            "partner_id": self.proveedor.id,
            "picking_type_id": almacen.in_type_id.id,
            "order_line": [Command.create({
                "product_id": producto.id,
                "product_qty": 80,
                "date_planned": fields.Datetime.now(),
            })],
        })
        compra.button_confirm()
        resultado = agente.ejecutar_herramienta("sugerir_reposicion_minimos", args)
        self.assertTrue(resultado["ok"], resultado["mensaje"])
        self.assertFalse(resultado["datos"]["sugerencias"])

    def test_reposicion_minimos_restringe_rol(self):
        resultado = self._agente(self.ven).ejecutar_herramienta("sugerir_reposicion_minimos", {})
        self.assertEqual(resultado["estado"], "sin_permiso")

    def test_permiso_por_rol(self):
        res = self._agente(self.ven).ejecutar_herramienta("listar_compras_pendientes", {})
        self.assertEqual(res["estado"], "sin_permiso")

    def test_chat_reconoce_frase(self):
        html, log = self._agente(self.cmp).procesar_mensaje("que compras pendientes hay", canal="prueba")
        self.assertIn("listar_compras_pendientes", log.tools_invoked)
        self.assertEqual(log.agente, "suministro")

    def test_disponibilidad_producto_por_ubicacion(self):
        almacen = self.po.picking_type_id.warehouse_id
        principal = almacen.lot_stock_id
        auxiliar = self.env["stock.location"].create({
            "name": "[PRUEBA] Auxiliar disponibilidad",
            "usage": "internal",
            "location_id": principal.id,
            "company_id": principal.company_id.id,
        })
        producto = self.env["product.product"].create({
            "name": "[PRUEBA] Disponibilidad suministro",
            "default_code": "PRUEBA-SUM-DISP-TEST",
            "is_storable": True,
        })
        quant = self.env["stock.quant"]
        quant._update_available_quantity(producto, principal, 60)
        movimiento = self.env["stock.move"].create({
            "product_id": producto.id,
            "product_uom_qty": 20,
            "product_uom": producto.uom_id.id,
            "location_id": principal.id,
            "location_dest_id": self.env.ref("stock.stock_location_customers").id,
            "picking_type_id": almacen.out_type_id.id,
            "company_id": almacen.company_id.id,
        })
        movimiento._action_confirm(merge=False)
        movimiento._action_assign()
        quant._update_available_quantity(producto, auxiliar, 30)

        resultado = self._agente(self.ven).ejecutar_herramienta(
            "consultar_disponibilidad_producto", {"producto": "prueba-sum-disp-test"},
        )
        self.assertTrue(resultado["ok"], resultado["mensaje"])
        filas = {fila[0]: fila[1:] for fila in resultado["datos"]["tabla"]["filas"]}
        self.assertEqual(filas, {
            principal.complete_name: ["60", "20", "40"],
            auxiliar.complete_name: ["30", "0", "30"],
        })

    def test_disponibilidad_producto_restringe_rol(self):
        usuario = new_test_user(self.env, login="sin_rol_disponibilidad", groups="base.group_user")
        resultado = self._agente(usuario).ejecutar_herramienta(
            "consultar_disponibilidad_producto", {"producto": self.producto.name},
        )
        self.assertEqual(resultado["estado"], "sin_permiso")

    def test_chat_disponibilidad_producto(self):
        html, log = self._agente(self.ven).procesar_mensaje(
            f"disponibilidad de {self.producto.name}", canal="prueba",
        )
        self.assertIn("Disponibilidad por ubicación", html)
        self.assertIn("consultar_disponibilidad_producto", log.tools_invoked)
        self.assertEqual(log.agente, "suministro")