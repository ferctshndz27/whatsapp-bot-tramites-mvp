"""Preservación del estado de la vista (static/js/estado-vista.js).

Lo que protegen estas pruebas es una regla de producto, no un detalle de
implementación: una acción del usuario —o una actualización automática— no
puede borrarle el trabajo. En particular, la auto-recarga del navegador
(<meta http-equiv="refresh">) NO se puede detener desde JS, así que con un
textarea en la misma página equivale a perder el mensaje a medio escribir.
"""

import pathlib

TEMPLATES = pathlib.Path("templates")
JS = pathlib.Path("static/js/estado-vista.js")


def _plantillas():
    return sorted(TEMPLATES.glob("*.html"))


def test_ninguna_vista_usa_la_recarga_incancelable_del_navegador():
    # El bug real que esto cierra: team_chat se recargaba cada 20 s y tiene un
    # textarea para redactar. Un asesor que tardara más perdía el texto.
    culpables = [p.name for p in _plantillas() if 'http-equiv="refresh"' in p.read_text()]
    assert culpables == [], f"vistas con auto-recarga incancelable: {culpables}"


def test_las_vistas_que_se_actualizan_solas_usan_la_recarga_controlada():
    con_meta = {p.name for p in _plantillas() if 'name="auto-recarga"' in p.read_text()}
    # team_chat.html salió de aquí a propósito: recargar la página entera cada
    # 20 s era el parpadeo visible del chat. Ahora static/js/team.js actualiza
    # el hilo y la lista de salas en su lugar, sin recargar.
    assert con_meta == {"inbox_list.html", "tickets_list.html"}


def test_todas_las_vistas_heredan_el_modulo_de_preservacion():
    # Va en base.html para que ninguna vista quede fuera por olvido.
    assert "estado-vista.js" in (TEMPLATES / "base.html").read_text()


def test_el_modulo_jamas_guarda_el_texto_de_los_campos():
    # Regla de privacidad: en esos campos se escriben mensajes de clientes. El
    # texto sin enviar se protege impidiendo la recarga, no copiándolo al
    # almacenamiento del navegador.
    fuente = JS.read_text()
    guardado = fuente.split("function capturar()")[1].split("return estado;")[0]
    assert ".value" in fuente  # sí lee valores (para saber si hay texto)
    for prohibido in ("estado.texto", "valor: el.value", "estado.valores"):
        assert prohibido not in guardado
    # Lo que sí viaja al almacén, explícito:
    assert {"y", "contenedores", "detalles", "foco", "controles"} == {
        linea.split(":")[0].strip()
        for linea in guardado.split("var estado = {")[1].split("};")[0].split(",")
        if ":" in linea
    }


def test_la_seleccion_solo_se_reimpone_tras_una_recarga_automatica():
    # Si el usuario mandó un formulario, manda el servidor: reimponer su
    # selección vieja pisaría el resultado de su propia acción.
    fuente = JS.read_text()
    assert "if (veniaDeAutoRecarga) {" in fuente


def test_la_preservacion_nunca_puede_tumbar_la_pagina():
    # sessionStorage revienta en modo privado estricto y se llena; ninguna de
    # las dos cosas puede dejar el panel inservible.
    fuente = JS.read_text()
    assert fuente.count("try {") >= 4
    assert "return; // Sin almacén" in fuente


def test_el_hilo_pegado_al_fondo_vuelve_al_fondo_NUEVO():
    # Regresión que introdujo la primera versión: restaurar el número viejo
    # dejaba el mensaje recién enviado fuera de vista, justo lo contrario de lo
    # que espera quien está conversando. Se guarda si estaba al fondo, no solo
    # dónde estaba.
    fuente = JS.read_text()
    assert "alFondo" in fuente
    assert "guardado.alFondo ? el.scrollHeight : guardado.top" in fuente


def test_los_hilos_de_chat_tienen_llave_para_preservarse():
    # Sin id no hay nada que preservar tras un POST que recarga la página.
    for nombre in ("inbox_chat.html", "team_chat.html"):
        assert 'id="chat"' in (TEMPLATES / nombre).read_text(), nombre


def test_el_lienzo_del_estudio_recuerda_el_nodo_abierto():
    # Los botones del Estudio recargan la página: sin esto, guardar un párrafo
    # devolvía al panel genérico y había que volver a buscar el nodo.
    agente = (TEMPLATES / "agente.html").read_text()
    assert 'EstadoVista.guardar(SELECCION' in agente
    assert "EstadoVista.leer(SELECCION)" in agente
    # Y el gancho existe del lado del módulo compartido.
    assert "window.EstadoVista" in JS.read_text()


def test_la_llave_ignora_los_parametros_efimeros_del_redirect():
    # '/catalog' y '/catalog?ok=1' son la misma vista: si no, no se restaura
    # nada justo después de una acción, que es cuando más importa.
    fuente = JS.read_text()
    assert "EFIMEROS" in fuente
    for p in ("ok", "err", "volver"):
        assert f'"{p}"' in fuente.split("var EFIMEROS")[1].split("]")[0]


# ---------------------------------------------------------------------------
# Estados de los nodos del Estudio (active / focus / selected)
# ---------------------------------------------------------------------------


def test_los_nodos_del_lienzo_son_alcanzables_por_teclado():
    # Sin esto la configuración del agente era inaccesible sin ratón: los nodos
    # son <g> de SVG, que no entran solos en el orden de tabulación.
    agente = (TEMPLATES / "agente.html").read_text()
    for pieza in ('setAttribute("tabindex", "0")', 'setAttribute("role", "button")',
                  'setAttribute("aria-label"'):
        assert pieza in agente, pieza
    # Enter y Espacio abren el nodo, y Espacio no desplaza la página.
    assert 'e.key !== "Enter"' in agente and "e.preventDefault()" in agente


def test_los_tres_estados_visuales_existen_y_en_ese_orden():
    # El orden importa: el ganador del probador se pinta DESPUÉS de la
    # selección, así conviven sin que ninguno tenga que saber del otro.
    agente = (TEMPLATES / "agente.html").read_text()
    posiciones = {
        estado: agente.index(sel)
        for estado, sel in {
            "hover": "#lienzo .joint-cell:hover rect",
            "selected": "#lienzo .joint-cell.est-sel rect",
            "focus": "#lienzo .joint-cell:focus-visible rect",
            "active": "#lienzo .joint-cell:active rect",
            "ganador": "#lienzo .joint-cell.est-ganador rect",
        }.items()
    }
    assert posiciones["selected"] < posiciones["ganador"]
    assert posiciones["focus"] < posiciones["ganador"]


def test_el_resaltado_del_probador_dejo_de_pisar_atributos():
    # Antes el probador escribía el borde por atributo del modelo y la
    # selección por CSS: dos sistemas peleando por la misma propiedad.
    agente = (TEMPLATES / "agente.html").read_text()
    assert 'classList.add("est-ganador")' in agente
    assert 'attr({ body: { stroke: "#d29922"' not in agente


def test_solo_un_nodo_seleccionado_y_anunciado():
    agente = (TEMPLATES / "agente.html").read_text()
    assert 'querySelectorAll("#lienzo .joint-cell.est-sel").forEach' in agente
    assert 'setAttribute("aria-current", "true")' in agente
