"""Resolución de credenciales por oficina, con herencia al .env — SPEC §3.

INVARIANTE: una fila viva de officecredential que casa (office_id, provider,
clave) SIEMPRE gana. No hay atajos por tipo de llave. Las llaves marcadas
`compartida` en config.CREDENCIALES se resuelven igual que las demás; lo que
`compartida` decide es que el PANEL (PR 3) las muestre de solo lectura como
«heredada del global» (§5), no que este resolvedor las ignore.

REGLA QUE SOSTIENE TODO EL PR 2: con `officecredential` VACÍA —el estado de
producción hoy y el de toda la suite— cada lectura resuelve por
`getattr(settings, ...)` EN CADA LLAMADA. Nada se memoiza. Por eso las
`monkeypatch.setattr(settings, <credencial>, ...)` de la suite siguen mandando y
el override en caliente de agent_settings.py sigue ganando.

Vive en su propio módulo y no en app/config.py porque necesita `db.engine`, y
app/db.py ya hace `from app.config import settings`: ahí cerraría el ciclo
config -> db -> config. Nadie de config/db/models lo importa a él, así que el
grafo sigue acíclico (config.py y models.py no importan nada de `app.*`; el
invariante está escrito en models.py). Precedente: app/codigos.py.

No nombra `whatsapp_client` ni `twilio_client`: lo prohíbe el candado AST de
tests/test_messaging_port.py.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import select

# El MÓDULO, jamás `from app.db import engine`: conftest.py parcha el ATRIBUTO
# (`monkeypatch.setattr(db, "engine", engine)`) y db.get_session() funciona
# porque resuelve `engine` de los globals en cada llamada. Un import por valor
# ligaría el engine original y TODA la suite leería y escribiría el tramites.db
# real, en silencio. Forma correcta ya en el repo: rag.py.
import app.db as db
from app.config import CREDENCIALES, settings
from app.models import OfficeCredential

if TYPE_CHECKING:  # solo anotación: stripe_client.py declara que no conoce
    from sqlmodel import Session  # SQLModel, y sus firmas lo siguen cumpliendo

logger = logging.getLogger(__name__)

# ponytail: sin caché — un SELECT por lectura, 184 µs medidos. El repo ya
# aceptó exactamente este costo con el mismo razonamiento en messaging.py
# (`canal_de` es un SELECT por envío). El disparador para reconsiderarlo es
# concreto: miles de mensajes por minuto, o más de un worker de uvicorn. Y
# cuando llegue, la regla ya está probada: solo se puede cachear LA FILA (o su
# ausencia); el fallback `getattr(settings, ...)` se reevalúa SIEMPRE. Si se
# cachea el valor efectivo, las monkeypatch.setattr(settings, ...) de la suite
# se vuelven no-ops del segundo test en adelante.


class CredencialIlegible(RuntimeError):
    """Hay fila para esta llave y NO se puede descifrar.

    Nunca degrada a la herencia del .env: caer ahí significaría firmar o cobrar
    con el secreto de OTRA oficina (en stripe_webhook.py, aceptar como propio
    un evento de dinero de una cuenta ajena). Devolver "" tampoco sirve:
    webhook.py y stripe_webhook.py lo leen como «no configurado» y responderían
    403 a TODO el tráfico, logueando «firma inválida o ausente» — un
    diagnóstico FALSO que manda al operador a revisar Stripe en vez de
    CREDENTIALS_KEY. Levantar deja el log nombrando la causa real.

    Lo que la excepción consigue NO es igual en las dos rutas, y conviene no
    prometer de más:

    - FIRMA: sale hasta arriba y da 500, así que Meta y Stripe reintentan. En
      webhook.py porque `_valid_signature` se llama antes del try que envuelve
      cada mensaje; en stripe_webhook.py porque ahí no hay try ninguno. El
      reintento del proveedor conserva el evento hasta que se arregle la llave.
    - ENVÍO contestando a un entrante de Meta (receive_webhook ->
      _process_message -> ... -> whatsapp_client._send): NO da 500. El
      `except Exception` de receive_webhook la caza, la petición contesta 200 y
      ESE saliente se pierde sin reintento (nadie reintenta un 200). El cliente
      se queda sin la respuesta y el único rastro es el logger.exception.
      Levantar sigue siendo lo correcto —mandar el mensaje con el token de otra
      oficina es peor—, pero aquí compra diagnóstico, no entrega.

    Los demás salientes (dashboard, cobranza, el webhook de Twilio) tienen cada
    uno su propio manejo: no se afirma nada de ellos desde aquí.

    El mensaje lleva oficina/proveedor/clave y NUNCA el valor ni el cifrado.
    """

    def __init__(self, office_id: int, provider: str, clave: str, motivo: str) -> None:
        self.office_id, self.provider, self.clave = office_id, provider, clave
        super().__init__(
            f"credencial ilegible oficina={office_id} {provider}/{clave}: {motivo}"
        )


def credencial(clave: str, *, office_id: int, session: "Session | None" = None) -> str:
    """Valor efectivo de `clave` para `office_id`; hereda del .env si no hay fila.

    Devuelve SIEMPRE str ("" si no está configurada), nunca None:
    stripe_client.modo_real() es `"_live_" in <valor>` y _filtro_modo
    (cobranza_panel) lo invoca en CADA render de /cobros — un None sería
    TypeError y un 500 en la vista principal de cobranza.

    `session` es opcional y se pasa solo donde ya está en la mano (cobranza.py,
    dashboard.py). Obligatoria está prohibida por el docstring de
    stripe_client.py, por cobranza_panel.py (`_request` puede durar ~33 s y
    sostener una sesión ahí es el «database is locked» que ese docstring
    documenta) y por stripe_webhook.py, que lee antes de que exista sesión.
    """
    try:
        meta = CREDENCIALES[clave]
    except KeyError:
        # Un typo muere ruidoso en el call site. Un "" mudo es justo el
        # fail-closed que §3 advierte: descartar todo sin decir por qué.
        raise KeyError(
            f"{clave!r} no está en config.CREDENCIALES. El provider se DERIVA de "
            f"ahí a propósito: pasarlo a mano dejaría que un 'meta' para una "
            f"llave de stripe no encuentre fila y herede en silencio la equivocada."
        ) from None

    cif = _valor_cifrado(clave, meta.provider, office_id, session)
    if cif is not None:
        return _descifrar(cif, office_id, meta.provider, clave)
    return getattr(settings, meta.atributo, "")


def cifrar(valor: str) -> tuple[bytes, str]:
    """(valor_cif, pista). La pista son los ÚLTIMOS 4 caracteres y es lo único
    que el panel del PR 3 puede mostrar (§3.2); la regla vive en un solo lugar.

    Se entrega en el PR 2 aunque el panel sea el PR 3 por dos razones concretas:
    tests/test_dependencias.py ya nombra a AMBAS como alcance de este PR, y sin
    ella la prueba de round-trip tendría que fabricar el Fernet a mano y
    probaría su propio código en vez del del repo.
    """
    from cryptography.fernet import Fernet  # perezoso e INDENTADO: ver _descifrar

    if not settings.credentials_key:
        raise RuntimeError("CREDENTIALS_KEY no está definida: no se puede cifrar.")
    return Fernet(settings.credentials_key.encode()).encrypt(valor.encode()), valor[-4:]


# ---------------------------------------------------------------------------
# Internos
# ---------------------------------------------------------------------------


def _valor_cifrado(clave, provider, office_id, session) -> bytes | None:
    def _leer(s) -> bytes | None:
        fila = s.exec(
            select(OfficeCredential).where(
                OfficeCredential.office_id == office_id,
                OfficeCredential.provider == provider,
                OfficeCredential.clave == clave,
                OfficeCredential.habilitado == True,  # noqa: E712
            )
        ).first()
        return None if fila is None else fila.valor_cif

    try:
        if session is not None:
            return _leer(session)
        with db.get_session() as propia:
            return _leer(propia)
    except SQLAlchemyError as exc:
        # La tabla puede NO existir: una base anterior al PR 1 que todavía no
        # arrancó init_db() (el tramites.db local de este repo es exactamente
        # ese caso), o el cron antes de init_db. HOY ningún punto de lectura
        # puede fallar al leer configuración y este PR no puede estrenar esa
        # clase de fallo: sin fila conocida, se hereda.
        #
        # El except cubre SOLO el SELECT, nunca el descifrado: tabla ausente
        # significa «no hay filas» (heredar es correcto) y fila ilegible es otra
        # pregunta, que sigue siendo ruidosa.
        #
        # ponytail: cuando existan filas reales (PR 5), heredar tras un SELECT
        # roto es leer el secreto de otra oficina — esta rama tiene que fallar
        # cerrada en cuanto haya datos.
        # Sin exc_info: esta rama corre en la ruta de los DOS webhooks públicos
        # (webhook.py y stripe_webhook.py leen su secreto ANTES de validar la
        # firma), así que un traceback completo aquí lo dispara cualquiera desde
        # internet sin autenticarse — un amplificador de log de un POST por
        # línea. El diagnóstico SÍ va, pero acotado: `exc.orig` es el error del
        # DBAPI a secas («no such table: officecredential»), mientras que
        # str(exc) arrastra el SELECT completo con sus parámetros —cinco
        # renglones por petición— y el SQL no añade nada que no esté aquí
        # arriba. El corte a 200 evita que un driver charlatán reabra el mismo
        # agujero por otro lado.
        #
        # «ilegible» NO se usa aquí a propósito: en la spec nombra el estado
        # OPUESTO —CredencialIlegible, hay fila y no se descifra, que jamás
        # hereda—. Esto es lo contrario: no se pudo ni preguntar, y se hereda.
        # `is not None` y no `or`: un orig falsy (una excepción cuyo __bool__ o
        # __len__ da False) caería al str(exc) completo, justo el volcado que
        # este renglón existe para evitar. Un SQLAlchemyError sin `orig` —los
        # que no vienen del DBAPI— sí usa exc, que ahí ya es corto.
        orig = getattr(exc, "orig", None)
        motivo = orig if orig is not None else exc
        logger.warning(
            "No se pudo consultar officecredential (oficina=%s %s/%s): %s: %s; se hereda del .env",
            office_id, provider, clave, type(exc).__name__, str(motivo)[:200],
        )
        return None


def _descifrar(cif: bytes, office_id: int, provider: str, clave: str) -> str:
    # PEREZOSO e INDENTADO, obligatorio y no estilístico: conftest.py hace
    # `import app.db` en tiempo de COLECCIÓN, así que un import a nivel de
    # módulo en la cadena de arranque tumbaría la suite ENTERA en un venv sin
    # reinstalar (escenario real: el venv del repo tiene 45.0.7 por el pin, y el
    # Python del sistema tiene 42.0.8, la rama del CVE-2024-12797 que §3.1
    # descarta). tests/test_dependencias.py usa un regex anclado con re.M a
    # inicio de línea, así que INDENTADO pasa sin tocar esa prueba — que es lo
    # que anuncia su propio docstring.
    from cryptography.fernet import Fernet, InvalidToken

    if not settings.credentials_key:
        raise CredencialIlegible(office_id, provider, clave, "CREDENTIALS_KEY sin definir")
    try:
        return Fernet(settings.credentials_key.encode()).decrypt(cif).decode()
    except (InvalidToken, ValueError, TypeError) as exc:
        # ValueError cubre una CREDENTIALS_KEY malformada (base64/longitud).
        raise CredencialIlegible(office_id, provider, clave, "no se pudo descifrar") from exc
