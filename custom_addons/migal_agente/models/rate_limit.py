import logging
from datetime import timedelta

import odoo.modules.module
from psycopg2 import errors as pg_errors
from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class MigalAgenteRateLimit(models.Model):
    """Contador persistente para limitar solicitudes a endpoints públicos.

    Compatible con multi-worker: usa PostgreSQL como almacén compartido y
    actualización atómica por clave/ventana.
    """

    _name = "migal.agente.rate.limit"
    _description = "Rate limit de endpoints públicos del agente"
    _rec_name = "key"

    key = fields.Char(required=True, index=True)
    window_start = fields.Datetime(required=True)
    count = fields.Integer(required=True, default=0)

    _key_uniq = models.Constraint("unique(key)", "La clave de rate limit debe ser única.")


    @api.model
    def check_and_hit(self, key, limit, window_seconds):
        """Registra un hit y devuelve (allowed, retry_after_seconds).

        Ventana fija por clave. Al expirar la ventana se reinicia el contador.
        Usa cursor independiente para persistir aunque la request termine en 404/rollback.
        En modo test (TestCursor) usa un cursor real para evitar rollback de savepoints.
        """
        if not key:
            return True, 0
        now = fields.Datetime.now()
        cutoff = now - timedelta(seconds=window_seconds)
        uid = self.env.uid or 1
        sql = """
            INSERT INTO migal_agente_rate_limit
                (key, window_start, count, create_uid, create_date, write_uid, write_date)
            VALUES (%s, %s, 1, %s, %s, %s, %s)
            ON CONFLICT (key) DO UPDATE SET
                count = CASE
                    WHEN migal_agente_rate_limit.window_start <= %s THEN 1
                    ELSE migal_agente_rate_limit.count + 1
                END,
                window_start = CASE
                    WHEN migal_agente_rate_limit.window_start <= %s THEN %s
                    ELSE migal_agente_rate_limit.window_start
                END,
                write_uid = EXCLUDED.write_uid,
                write_date = EXCLUDED.write_date
            RETURNING count, window_start
        """
        params = (key, now, uid, now, uid, now, cutoff, cutoff, now)
        # El contador persiste incluso si la solicitud termina en 404.
        # HttpCase parchea registry.cursor con savepoints. En ese caso se
        # usa un cursor real del pool para poder confirmar el incremento.
        registry = self.env.registry
        en_prueba = bool(getattr(odoo.modules.module, "current_test", False))
        nuevo_cursor = registry._db.cursor if en_prueba else registry.cursor
        for intento in range(3):
            try:
                with nuevo_cursor() as cr:
                    # El UPSERT actualiza una sola fila y cuenta sobre su
                    # última versión confirmada. Este nivel solo se aplica
                    # a la transacción independiente del contador.
                    cr.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                    cr.execute(sql, params)
                    count, window_start = cr.fetchone()
                break
            except (pg_errors.DeadlockDetected, pg_errors.SerializationFailure):
                if intento == 2:
                    raise
        if count > limit:
            elapsed = (now - window_start).total_seconds()
            retry_after = max(1, int(window_seconds - elapsed))
            _logger.warning("migal_agente rate limit exceeded: key=%s count=%s limit=%s", key, count, limit)
            return False, retry_after
        return True, 0

    @api.autovacuum
    def _gc_old_limits(self):
        """Limpia contadores antiguos para evitar crecimiento indefinido."""
        cutoff = fields.Datetime.now() - timedelta(days=1)
        self.sudo().search([("window_start", "<", cutoff)]).unlink()
