"""Pruebas del agente de cadena de suministro (caso normal, borde, permiso y evento)."""
from odoo import Command
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

    def test_ev018_alerta_stock_previsto_por_orden(self):
        almacen = self.po.picking_type_id.warehouse_id
        ubicacion = almacen.lot_stock_id
        producto = self.env["product.product"].create({
            "name": "[PRUEBA] Cemento EV-018",
            "is_storable": True,
            "list_price": 10,
        })
        otro = self.env["product.product"].create({
            "name": "[PRUEBA] Producto ajeno EV-018",
            "is_storable": True,
        })
        self.env["stock.quant"]._update_available_quantity(producto, ubicacion, 110)
        for candidato in producto | otro:
            self.env["stock.warehouse.orderpoint"].create({
                "product_id": candidato.id,
                "warehouse_id": almacen.id,
                "location_id": ubicacion.id,
                "product_min_qty": 100,
                "product_max_qty": 200,
                "trigger": "manual",
            })

        for cantidad, espera_alerta in [(10, False), (25, True)]:
            orden = self.env["sale.order"].create({
                "partner_id": self.proveedor.id,
                "warehouse_id": almacen.id,
                "user_id": self.ven.id,
                "order_line": [Command.create({"product_id": producto.id, "product_uom_qty": cantidad})],
            })
            notas = self.env["mail.message"].sudo()
            dominio = [
                ("model", "=", "sale.order"),
                ("res_id", "=", orden.id),
                ("subject", "=", "EV-018 · Alerta de reposición"),
            ]
            self.assertFalse(notas.search(dominio))
            orden.with_user(self.ven).action_confirm()
            self.assertEqual(orden.state, "sale")
            nota = notas.search(dominio)
            self.assertEqual(len(nota), int(espera_alerta))
            if nota:
                self.assertEqual(nota.subtype_id, self.env.ref("mail.mt_note"))
                self.assertIn("Previsto: 75", nota.body)
                self.assertIn("Mínimo: 100", nota.body)
                self.assertNotIn(otro.name, nota.body)
                self.env["migal.agente.suministro"].with_user(self.ven)._al_confirmarse_venta({
                    "orden": orden.name,
                })
                self.assertEqual(notas.search_count(dominio), 1)

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

    def test_permiso_por_rol(self):
        res = self._agente(self.ven).ejecutar_herramienta("listar_compras_pendientes", {})
        self.assertEqual(res["estado"], "sin_permiso")

    def test_chat_reconoce_frase(self):
        html, log = self._agente(self.cmp).procesar_mensaje("que compras pendientes hay", canal="prueba")
        self.assertIn("listar_compras_pendientes", log.tools_invoked)
        self.assertEqual(log.agente, "suministro")
