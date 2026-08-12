"""
mount-nas.py — Conecta y configura la NAS Ensamble
Pregunta con qué módulo operar (red de la oficina / fuera de la oficina) y enruta las acciones.
Corre SIN admin por defecto. Solo pide admin para Configurar.
Windows + Mac.
"""

import os
import sys
import platform
import subprocess
import ctypes
import urllib.request
import urllib.error
import tempfile
import socket
import ssl
import sqlite3
import json
import re
import glob

# ─────────────────────────────────────────────
# CONSTANTES
# ─────────────────────────────────────────────

OS = platform.system()

# PyInstaller en Mac no incluye los certificados del sistema operativo.
# Sin esto, cualquier descarga HTTPS falla con SSLCertVerificationError.
if OS == "Darwin" and os.path.exists("/etc/ssl/cert.pem"):
    os.environ.setdefault("SSL_CERT_FILE", "/etc/ssl/cert.pem")

if OS == "Windows":
    try:
        ctypes.windll.kernel32.SetConsoleCP(65001)
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

NAS_LAN_IP        = "192.168.2.7"
NAS_TAILSCALE_IP  = "100.81.124.50"
NAS_HOST_ALIAS    = "nas_local"
NAS_EXTERNAL_URL  = "nas.ensambleai.com"
SHARE_ENSAMBLE    = "Ensamble"
SHARE_ARCHIVO     = "ARCHIVO ENSAMBLE"
DRIVE_ENSAMBLE    = "Z:"
DRIVE_ARCHIVO     = "Y:"
NAS_ADMIN_USERS   = {"admin", "davidm", "juanpablop", "simonf"}

DSM_HTTP_PORT     = 5000
DSM_HTTPS_PORT    = 5001
SYNODRIVE_PORT    = 6690

TAILSCALE_WIN_URL = "https://pkgs.tailscale.com/stable/tailscale-setup-latest.exe"
TAILSCALE_MAC_URL = "https://pkgs.tailscale.com/stable/Tailscale-latest.pkg"
SYNODRIVE_WIN_URL = (
    "https://global.download.synology.com/download/Tools/SynologyDriveClient"
    "/3.5.1-16120/Windows/x64/Synology%20Drive%20Client-3.5.1-16120.exe"
)
SYNODRIVE_MAC_URL = (
    "https://global.download.synology.com/download/Tools/SynologyDriveClient"
    "/3.5.1-16120/Mac/Synology%20Drive%20Client-3.5.1-16120.dmg"
)
SYNODRIVE_DOWNLOAD_PAGE = (
    "https://www.synology.com/en-global/support/download/SynologyDriveClient"
)

# Ruta de caché donde el launcher guarda este script (usada para auto-elevación)
_CACHE_PATH = os.path.join(os.path.expanduser("~"), ".mount_nas.py")

# Estados de capa de diagnóstico
VERDE, ROJO, AMBAR = "verde", "rojo", "ambar"


# ─────────────────────────────────────────────
# UTILIDADES
# ─────────────────────────────────────────────

def title(text):
    w = 54
    print("\n" + "═" * w)
    print(f"  {text}")
    print("═" * w)

def ok(msg):   print(f"  ✔  {msg}")
def warn(msg): print(f"  ⚠  {msg}")
def err(msg):  print(f"  ✖  {msg}")
def info(msg): print(f"     {msg}")

def ask(prompt, options=None, _max_intentos=10):
    intentos = 0
    while True:
        try:
            val = input(f"\n  → {prompt}: ").strip()
        except EOFError:
            # stdin cerrado (terminal muerta): cancelar en vez de girar/crashear.
            warn("Entrada cerrada (EOF). Cancelando.")
            raise KeyboardInterrupt
        if not options or val in options:
            return val
        intentos += 1
        if intentos >= _max_intentos:
            warn("Demasiados intentos inválidos. Cancelando.")
            raise KeyboardInterrupt
        warn(f"Opción inválida. Válidas: {', '.join(options)}")

def confirm(prompt):
    return ask(f"{prompt} [s/n]", ["s", "n"]) == "s"

def run(cmd, check=True, capture=False):
    kwargs = {"shell": True, "check": check}
    if capture:
        kwargs["capture_output"] = True
        kwargs["text"] = True
    return subprocess.run(cmd, **kwargs)

def password_input(prompt="Contraseña"):
    print(f"\n  → {prompt}: ", end='', flush=True)
    pwd = []
    if OS == "Windows":
        import msvcrt
        while True:
            ch = msvcrt.getwch()
            if ch in ('\r', '\n'):
                break
            if ch == '\x03':
                raise KeyboardInterrupt
            if ch in ('\x00', '\xe0'):
                msvcrt.getwch()
                continue
            if ch == '\x08':
                if pwd:
                    pwd.pop()
                    print('\b \b', end='', flush=True)
            else:
                pwd.append(ch)
                print('*', end='', flush=True)
    else:
        import termios, tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while True:
                ch = sys.stdin.read(1)
                if ch == '':
                    # EOF: stdin cerrado. read(1) devuelve '' indefinidamente —
                    # sin esta guarda, el bucle gira al 100% de CPU para siempre.
                    raise KeyboardInterrupt
                if ch in ('\r', '\n'):
                    break
                if ch == '\x03':
                    raise KeyboardInterrupt
                if ch == '\x7f':
                    if pwd:
                        pwd.pop()
                        print('\b \b', end='', flush=True)
                else:
                    pwd.append(ch)
                    print('*', end='', flush=True)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    print()
    return ''.join(pwd)

def is_admin():
    if OS == "Windows":
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    else:
        return os.geteuid() == 0

def download(url, dest_path):
    info(f"Descargando {os.path.basename(dest_path)} ...")
    urllib.request.urlretrieve(url, dest_path)
    ok("Descargado.")


# ─────────────────────────────────────────────
# SONDAS DE RED (stdlib, cross-platform)
# ─────────────────────────────────────────────

def _tcp_abierto(host, port, timeout=3.0):
    """True si se puede abrir un socket TCP al host:puerto."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False

def _which(name):
    """Busca un ejecutable en PATH usando solo os (sin shutil — evita imports nuevos)."""
    for d in os.environ.get("PATH", "").split(os.pathsep):
        cand = os.path.join(d, name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None

def _tailscale_cli():
    """
    Ruta del CLI de Tailscale. CLAVE en Mac: el CLI NO está en el PATH — la app lo
    trae dentro del bundle (/Applications/Tailscale.app/Contents/MacOS/Tailscale).
    Devuelve la ruta (string) o None.
    """
    p = _which("tailscale.exe" if OS == "Windows" else "tailscale")
    if p:
        return p
    if OS == "Windows":
        cands = [r"C:\Program Files\Tailscale\tailscale.exe",
                 r"C:\Program Files (x86)\Tailscale\tailscale.exe"]
    else:
        cands = ["/Applications/Tailscale.app/Contents/MacOS/Tailscale",
                 os.path.expanduser("~/Applications/Tailscale.app/Contents/MacOS/Tailscale"),
                 "/usr/local/bin/tailscale",
                 "/opt/homebrew/bin/tailscale"]
    for c in cands:
        if os.path.exists(c):
            return c
    return None

def _tailscale_peer_alcanzable():
    """Alcanzabilidad real del NAS por Tailscale: TCP al puerto DSM HTTPS. Verdad de terreno."""
    return _tcp_abierto(NAS_TAILSCALE_IP, DSM_HTTPS_PORT, timeout=4.0)

def _tailscale_instalado():
    if _tailscale_cli():
        return True
    # Mac: la app puede estar instalada aunque el CLI no resuelva por PATH
    if OS == "Darwin" and os.path.exists("/Applications/Tailscale.app"):
        return True
    return False

def _tailscale_activo():
    # Si el NAS Tailscale es alcanzable, el túnel está arriba — no depende del CLI.
    if _tailscale_peer_alcanzable():
        return True
    cli = _tailscale_cli()
    if not cli:
        return False
    r = run(f'"{cli}" status', check=False, capture=True)
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        return False
    if "Logged out" in out or "stopped" in out.lower():
        return False
    return "100." in out

def _tailscale_login():
    cli = _tailscale_cli()
    if not cli:
        warn("No encontré el CLI de Tailscale para iniciar sesión.")
        info("Abre la app de Tailscale e inicia sesión manualmente.")
        return
    run(f'"{cli}" login', check=False)

def _dns_resuelve(host, expected_ip):
    """(resuelve_a_esperado, conjunto_de_ips). Usa DNS público — no requiere Tailscale."""
    try:
        infos = socket.getaddrinfo(host, None)
        ips = {i[4][0] for i in infos}
        return (expected_ip in ips), ips
    except Exception:
        return False, set()

def _https_responde(host, timeout=6.0):
    """(respondio, codigo_o_error). Cualquier respuesta HTTP (incl. 401/403) = proxy vivo."""
    url = f"https://{host}/"
    ctx = ssl.create_default_context()
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return True, r.status
    except urllib.error.HTTPError as e:
        return True, e.code
    except Exception as e:
        return False, type(e).__name__

def _smb_montado():
    """True si el share Ensamble está montado en este equipo."""
    if OS == "Windows":
        return os.path.exists(DRIVE_ENSAMBLE + "\\")
    else:
        punto = f"/Volumes/{SHARE_ENSAMBLE}"
        try:
            return os.path.ismount(punto) or os.path.exists(punto)
        except Exception:
            return False

def _synodrive_instalado():
    if OS == "Windows":
        paths = [
            r"C:\Program Files\SynologyDrive\SynologyDrive.exe",
            r"C:\Program Files (x86)\Synology\SynologyDrive\bin\launcher.exe",
        ]
        return any(os.path.exists(p) for p in paths)
    else:
        return os.path.exists("/Applications/Synology Drive Client.app")


# ─────────────────────────────────────────────
# SYNODRIVE — lectura defensiva del servidor configurado
# ─────────────────────────────────────────────

def _synodrive_db_dirs():
    if OS == "Windows":
        base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "SynologyDrive", "data", "db")
        return [base]
    else:
        return [
            os.path.expanduser("~/Library/Application Support/SynologyDrive/data/db"),
            os.path.expanduser("~/Library/Application Support/SynologyDrive"),
        ]

_EXTENSIONES_ARCHIVO = ('.pdf', '.docx', '.doc', '.xlsx', '.xls', '.pptx', '.ppt',
                        '.jpg', '.jpeg', '.png', '.gif', '.zip', '.rar', '.txt',
                        '.csv', '.json', '.dwg', '.rvt', '.skp')

def _parece_servidor(val):
    """Heurística: ¿este string parece una dirección de servidor?"""
    if not isinstance(val, str):
        return False
    v = val.strip()
    if not v or len(v) > 120 or " " in v or "\\" in v or v.count("/") > 0:
        return False
    low = v.lower()
    if low.endswith(_EXTENSIONES_ARCHIVO):
        return False
    if "quickconnect.to" in low or low == "ensambleai":
        return True
    # hostname con TLD conocido o IP
    if re.fullmatch(r'[A-Za-z0-9.\-]+\.(com|to|me|net|org)', low):
        return True
    if re.fullmatch(r'\d{1,3}(\.\d{1,3}){3}', v):
        return True
    return False

def _synodrive_servidores_configurados():
    """
    Lee defensivamente las SQLite de SynoDrive y devuelve los strings que parecen
    direcciones de servidor. Robusto a cambios de esquema entre versiones:
    introspecciona sqlite_master y barre cada columna de texto. Nunca lanza.
    """
    candidatos = []
    archivos = []
    for d in _synodrive_db_dirs():
        if not d or not os.path.isdir(d):
            continue
        for patron in ("*.sqlite", "*.db"):
            archivos += glob.glob(os.path.join(d, "**", patron), recursive=True)
    for f in sorted(set(archivos)):
        try:
            con = sqlite3.connect(f"file:{f}?mode=ro", uri=True, timeout=2.0)
            cur = con.cursor()
            tablas = [r[0] for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            for t in tablas:
                try:
                    for row in cur.execute(f'SELECT * FROM "{t}"').fetchall():
                        for val in row:
                            if _parece_servidor(val):
                                candidatos.append(val.strip())
                except Exception:
                    continue
            con.close()
        except Exception:
            continue
    # dedup preservando orden
    vistos, unicos = set(), []
    for c in candidatos:
        k = c.lower()
        if k not in vistos:
            vistos.add(k)
            unicos.append(c)
    return unicos

def _clasificar_synodrive(candidatos, https_ok):
    """
    Devuelve (estado, detalle). Canal viejo (quickconnect/DDNS) → ROJO.
    Apunta a nas.ensambleai.com y responde → VERDE. Ilegible/desconocido → ÁMBAR.
    """
    blob = " ".join(candidatos).lower()
    apunta_bien = "ensambleai.com" in blob
    canal_viejo = ("quickconnect.to" in blob) or bool(re.search(r'ensambleai\.i\w*\d', blob))

    if canal_viejo and not apunta_bien:
        viejo = next((c for c in candidatos
                      if "quickconnect.to" in c.lower() or re.search(r'ensambleai\.i\w*\d', c.lower())),
                     candidatos[0] if candidatos else "?")
        return ROJO, f"Está conectado a una dirección antigua ({viejo}) — hay que reconectarlo"
    if apunta_bien:
        if https_ok:
            return VERDE, "Está bien conectado y funcionando"
        return AMBAR, "Está bien conectado, pero el NAS no responde ahora mismo (revisa que Tailscale esté activo)"
    if candidatos:
        return AMBAR, f"No reconozco a dónde está conectado ({candidatos[0]}) — revisar"
    return AMBAR, "No pude leer a dónde está conectado"


# ─────────────────────────────────────────────
# DIAGNÓSTICO POR CAPAS
# ─────────────────────────────────────────────

def _capa(nombre, estado, detalle, para_que, accion=None):
    return {"capa": nombre, "estado": estado, "detalle": detalle,
            "para_que": para_que, "accion": accion}

def elegir_modulo():
    """Pregunta con qué módulo se quiere operar. Sin auto-detección — el usuario elige siempre."""
    print()
    print("  [1] Montar la NAS estando en la red de la oficina")
    print("  [2] Montar la NAS estando en una red fuera de la oficina")
    opcion = ask("Selecciona una opción", ["1", "2"])
    return "oficina" if opcion == "1" else "fuera"

def preguntar_otro_modulo(actual):
    """Tras terminar un módulo, ofrece dejar listo también el otro — en lenguaje simple."""
    if actual == "oficina":
        ok("Ya dejamos lista la conexión para cuando estés en la oficina.")
        pregunta = ("¿Este computador alguna vez sale de la oficina (por ejemplo, es un "
                    "portátil que te llevas a la casa)? Si es así, puedo dejarlo listo "
                    "para que también funcione desde afuera.")
        otro = "fuera"
    else:
        ok("Ya dejamos lista la conexión para cuando estés fuera de la oficina.")
        pregunta = ("¿Este computador también se usa dentro de la oficina? "
                    "Si es así, puedo dejarlo listo para que también funcione ahí.")
        otro = "oficina"
    if confirm(pregunta):
        return otro
    return None

def diagnosticar(ubicacion):
    """Construye la lista de capas del módulo elegido — cada módulo ve solo lo suyo."""
    capas = []
    oficina = (ubicacion == "oficina")

    if oficina:
        # ── Carpeta compartida del NAS ────────────────────────────
        if _smb_montado():
            destino = DRIVE_ENSAMBLE if OS == "Windows" else f"/Volumes/{SHARE_ENSAMBLE}"
            capas.append(_capa("Carpeta compartida del NAS", VERDE, f"Ya está conectada en {destino}",
                               "así ves y guardas los archivos del NAS como una carpeta más de tu computador"))
        else:
            capas.append(_capa("Carpeta compartida del NAS", ROJO, "Todavía no está conectada",
                               "así ves y guardas los archivos del NAS como una carpeta más de tu computador",
                               "conectar"))

        # ── Navegador dentro de la oficina ─────────────────────────
        if _hosts_tiene_alias():
            capas.append(_capa("Navegador dentro de la oficina", VERDE,
                               f"Listo — puedes entrar escribiendo http://{NAS_HOST_ALIAS}:{DSM_HTTP_PORT} en el navegador",
                               "para entrar al NAS escribiendo su nombre en el navegador, sin memorizar números"))
        else:
            capas.append(_capa("Navegador dentro de la oficina", ROJO, "Todavía falta configurarlo",
                               "para entrar al NAS escribiendo su nombre en el navegador, sin memorizar números",
                               "configurar"))

    else:
        # ── Tailscale ────────────────────────────────────────────────
        ts_peer = _tailscale_peer_alcanzable()
        ts_inst = _tailscale_instalado()
        if ts_peer:
            capas.append(_capa("Tailscale", VERDE, "Ya está funcionando — el NAS responde",
                               "el programa que te deja entrar al NAS aunque no estés en la oficina"))
        elif not ts_inst:
            capas.append(_capa("Tailscale", ROJO, "Todavía no está instalado en este computador",
                               "el programa que te deja entrar al NAS aunque no estés en la oficina", "configurar"))
        elif not _tailscale_activo():
            capas.append(_capa("Tailscale", ROJO, "Está instalado pero falta iniciar sesión",
                               "el programa que te deja entrar al NAS aunque no estés en la oficina", "configurar"))
        else:
            capas.append(_capa("Tailscale", AMBAR, "Está encendido pero el NAS no responde todavía",
                               "el programa que te deja entrar al NAS aunque no estés en la oficina", None))

        # ── Dirección del NAS en internet ───────────────────────────
        dns_ok, ips = _dns_resuelve(NAS_EXTERNAL_URL, NAS_TAILSCALE_IP)
        if dns_ok:
            capas.append(_capa("Dirección del NAS en internet", VERDE, f"{NAS_EXTERNAL_URL} apunta bien",
                               "para que el navegador sepa a dónde ir cuando escribes el nombre del NAS"))
        elif ips:
            capas.append(_capa("Dirección del NAS en internet", AMBAR,
                               f"{NAS_EXTERNAL_URL} apunta a otro lugar ({', '.join(sorted(ips))}) — revisar",
                               "para que el navegador sepa a dónde ir cuando escribes el nombre del NAS"))
        else:
            capas.append(_capa("Dirección del NAS en internet", ROJO, f"{NAS_EXTERNAL_URL} no se pudo encontrar",
                               "para que el navegador sepa a dónde ir cuando escribes el nombre del NAS"))

        # ── Navegador desde cualquier lugar ─────────────────────────
        https_ok, codigo = _https_responde(NAS_EXTERNAL_URL)
        if https_ok:
            capas.append(_capa("Navegador desde cualquier lugar", VERDE,
                               f"Funciona — https://{NAS_EXTERNAL_URL} responde",
                               "para entrar al NAS por el navegador, con el candado verde de seguridad, estés donde estés"))
        else:
            capas.append(_capa("Navegador desde cualquier lugar", ROJO,
                               f"https://{NAS_EXTERNAL_URL} no responde todavía",
                               "para entrar al NAS por el navegador, con el candado verde de seguridad, estés donde estés",
                               None if ts_peer else "configurar"))

        # ── Synology Drive ────────────────────────────────────────
        if not _synodrive_instalado():
            capas.append(_capa("Synology Drive", ROJO, "Todavía no está instalado",
                               "el programa que copia automáticamente las carpetas del NAS a tu computador", "configurar"))
        else:
            candidatos = _synodrive_servidores_configurados()
            estado_sd, detalle_sd = _clasificar_synodrive(candidatos, https_ok)
            accion_sd = "reconectar_synodrive" if estado_sd == ROJO else None
            capas.append(_capa("Synology Drive", estado_sd, detalle_sd,
                               "el programa que copia automáticamente las carpetas del NAS a tu computador", accion_sd))

    return capas

def imprimir_tarjeta(capas):
    simbolos = {VERDE: "✔", ROJO: "✖", AMBAR: "⚠"}
    title("CÓMO ESTÁ TU CONEXIÓN AL NAS")
    for c in capas:
        s = simbolos.get(c["estado"], "·")
        print(f"  {s}  {c['capa']:<32} {c['detalle']}")
    print()


# ─────────────────────────────────────────────
# ROUTING AUTOMÁTICO
# ─────────────────────────────────────────────

def enrutar(capas, ubicacion):
    """
    Decide y propone acciones según el diagnóstico. Devuelve dict con lo ejecutado.
    Orden: configurar (requiere admin) → reconectar SynoDrive → conectar (carpeta compartida).
    Cada acción pide confirmación.
    """
    resultado = {"acciones": [], "nota": None}
    acciones = {c["accion"] for c in capas if c["accion"]}

    # Todo verde
    if all(c["estado"] == VERDE for c in capas):
        title("RESULTADO")
        ok("Todo está funcionando. No hay nada que hacer.")
        return resultado

    title("QUÉ HACER")
    fallas = [c for c in capas if c["estado"] != VERDE]
    info("Encontré esto para resolver:")
    for c in fallas:
        simbolo = "✖" if c["estado"] == ROJO else "⚠"
        print(f"     {simbolo} {c['capa']}: {c['detalle']}")

    # 1) Configurar (Tailscale / SynoDrive faltante / alias del navegador) — requiere admin
    if "configurar" in acciones:
        info("")
        info("Falta instalar o configurar algo (se necesita permiso de administrador).")
        if confirm("¿Hacerlo ahora?"):
            seccion_configurar(ubicacion)
            resultado["acciones"].append("configurar")
            if OS == "Windows":
                resultado["nota"] = ("La configuración se abrió en otra ventana (con permiso de administrador). "
                                     "Complétala ahí; este resumen refleja el estado de antes.")

    # 2) Reconectar SynoDrive (dirección antigua) — guía guiada, no se reescribe en silencio
    if "reconectar_synodrive" in acciones:
        info("")
        warn("Synology Drive está conectado a una dirección antigua.")
        info("Eso no se puede corregir solo — hay que hacerlo desde la app.")
        if confirm("¿Cerrar Synology Drive y mostrarte la guía para reconectarlo?"):
            reconectar_synodrive()
            resultado["acciones"].append("reconectar_synodrive")

    # 3) Conectar (carpeta compartida) — solo módulo oficina, con todo lo demás listo
    if "conectar" in acciones:
        info("")
        if confirm("¿Conectar ahora la carpeta compartida del NAS?"):
            seccion_conectar(ubicacion)
            resultado["acciones"].append("conectar")

    return resultado


# ─────────────────────────────────────────────
# RECONECTAR SYNOLOGY DRIVE (guía guiada)
# ─────────────────────────────────────────────

def _cerrar_synodrive():
    if OS == "Windows":
        run("taskkill /IM SynologyDrive.exe /F", check=False, capture=True)
        run("taskkill /IM cloud-drive-ui.exe /F", check=False, capture=True)
    else:
        run("osascript -e 'quit app \"Synology Drive Client\"'", check=False, capture=True)
        run("pkill -f 'Synology Drive'", check=False, capture=True)

def reconectar_synodrive():
    title("RECONECTAR SYNOLOGY DRIVE")
    info("Cerrando Synology Drive...")
    _cerrar_synodrive()
    ok("Listo, se cerró.")
    print()
    print("  Sigue estos pasos (la app puede verse un poco distinta según la versión):")
    print()
    print('  1. Abre el programa "Synology Drive Client".')
    print("  2. Si no se abre la ventana, búscalo en la barra de tareas (junto al reloj):")
    print("     clic en el logo de Synology Drive → clic en el engranaje ⚙ → \"Settings\".")
    print("  3. En configuración, clic en el ícono del SERVIDOR — el de ARRIBA, NO el de la carpeta.")
    print("  4. Al seleccionar el servidor se habilitan ARRIBA los botones \"Editar conexión\" y")
    print("     \"Delete\". Clic en \"Delete\". (No borra archivos, solo la conexión vieja.)")
    print("  5. Clic en \"+\" / \"Crear\". En dirección del servidor escribe EXACTAMENTE:")
    print(f"        {NAS_EXTERNAL_URL}")
    print("     ✔ marca \"Habilitar SSL / cifrado\". NO escribas puerto (con SSL usa el suyo solo).")
    print(f"     Si y solo si pidiera puerto: pegarlo inline → {NAS_EXTERNAL_URL}:{DSM_HTTPS_PORT}")
    print("     (no hay campo aparte). Usuario y contraseña del NAS.")
    print("     Si sale advertencia de cert → Continuar.")
    print(f"  6. Carpeta a sincronizar: \"{SHARE_ENSAMBLE}\" → modo \"On-Demand\". Acepta.")
    print()
    print("  ⚠ Requiere Tailscale ACTIVO (SynoDrive entra directo por Tailscale, no por navegador).")
    print()


# ─────────────────────────────────────────────
# [1] CONECTAR NAS  (lógica existente — reusada)
# ─────────────────────────────────────────────

def _montar_unidad_win(letra, share, usuario, password, host=NAS_HOST_ALIAS):
    unc = f"\\\\{host}\\{share}"
    run(f'net use {letra} /delete /y', check=False, capture=True)
    # cmdkey graba la credencial en el Administrador de credenciales de Windows de forma
    # persistente, independiente del ciclo de vida de "net use". Sin esto, /persistent:yes
    # solo marca la letra para reconectar al iniciar sesión, pero Windows puede no tener
    # la credencial disponible en ese momento y vuelve a pedirla — confirmado real
    # (usuario reportó que "recordar contraseña" no sobrevivía a un reinicio). Se borra y
    # se vuelve a agregar en cada corrida para no dejar una credencial vieja si cambió.
    run(f'cmdkey /delete:{host}', check=False, capture=True)
    run(f'cmdkey /add:{host} /user:"{usuario}" /pass:"{password}"', check=False, capture=True)
    cmd = f'net use {letra} "{unc}" /persistent:yes'
    result = run(cmd, check=False)
    if result.returncode == 0:
        ok(f"{letra} → {unc}")
        return True
    else:
        err(f"No se pudo montar {letra}.")
        info("Verifica tus credenciales y que estés en la red local.")
        return False

def _montar_smb_mac(share, punto_montaje, usuario, password):
    os.makedirs(punto_montaje, exist_ok=True)
    unc = f"smb://{usuario}:{password}@{NAS_HOST_ALIAS}/{share}"
    result = run(f"mount_smbfs '{unc}' '{punto_montaje}'", check=False)
    if result.returncode == 0:
        ok(f"Montado en {punto_montaje}")
        return True
    else:
        err(f"No se pudo montar {share}.")
        info("Verifica credenciales y conexión de red.")
        return False

def _conectar_lan():
    info("Ingresa tus credenciales del NAS:")
    usuario = ask("Usuario NAS")
    pwd = password_input("Contraseña NAS")
    es_admin = usuario.lower() in NAS_ADMIN_USERS

    if OS == "Windows":
        ok_z = _montar_unidad_win(DRIVE_ENSAMBLE, SHARE_ENSAMBLE, usuario, pwd)
        if es_admin:
            # Host distinto al de Ensamble: Windows bloquea 2 conexiones persistentes
            # al mismo servidor con credenciales guardadas ("no se permiten varias
            # conexiones... con más de un nombre de usuario"). IP directa evita el choque.
            ok_y = _montar_unidad_win(DRIVE_ARCHIVO, SHARE_ARCHIVO, usuario, pwd, host=NAS_LAN_IP)
            if ok_z and ok_y:
                ok(f"Unidades {DRIVE_ENSAMBLE} y {DRIVE_ARCHIVO} montadas.")
            elif ok_z:
                warn(f"{DRIVE_ENSAMBLE} montada. No se pudo montar {DRIVE_ARCHIVO}.")
        else:
            if ok_z:
                ok(f"Unidad {DRIVE_ENSAMBLE} montada.")

    elif OS == "Darwin":
        ok_e = _montar_smb_mac(SHARE_ENSAMBLE, f"/Volumes/{SHARE_ENSAMBLE}", usuario, pwd)
        if es_admin:
            ok_a = _montar_smb_mac(SHARE_ARCHIVO, "/Volumes/ARCHIVO_ENSAMBLE", usuario, pwd)
            if ok_e and ok_a:
                ok("Carpetas Ensamble y ARCHIVO ENSAMBLE montadas.")
            elif ok_e:
                warn("Ensamble montada. No se pudo montar ARCHIVO ENSAMBLE.")
        else:
            if ok_e:
                ok("Carpeta Ensamble montada.")
        info("")
        info("En Mac el montaje no persiste al reiniciar.")
        info("Para montaje automático: Finder → Conectar al servidor → guardar en Login Items.")

def _conectar_externo():
    info("Verificando la conexión remota (Tailscale)...")
    if not _tailscale_activo():
        warn("Tailscale no está activo o no está instalado.")
        info("Tailscale es el programa necesario para conectarte al NAS desde fuera de la oficina.")
        if confirm("¿Ir a Configurar equipo para instalar Tailscale?"):
            _elevar_para_configurar("fuera")
        return

    ok("Tailscale está activo.")

    if OS == "Windows":
        drive_paths = [
            r"C:\Program Files\SynologyDrive\SynologyDrive.exe",
            r"C:\Program Files (x86)\Synology\SynologyDrive\bin\launcher.exe",
        ]
        drive_exe = next((p for p in drive_paths if os.path.exists(p)), None)
        if drive_exe:
            ok("Synology Drive está instalado.")
            info("Abriendo Synology Drive...")
            run(f'start "" "{drive_exe}"', check=False)
            info("Synology Drive se encarga de mantener sincronizados los archivos con el NAS.")
        else:
            warn("Synology Drive no está instalado.")
            if confirm("¿Ir a Configurar equipo para instalarlo?"):
                _elevar_para_configurar("fuera")
    elif OS == "Darwin":
        drive_app = "/Applications/Synology Drive Client.app"
        if os.path.exists(drive_app):
            ok("Synology Drive está instalado.")
            run("open '/Applications/Synology Drive Client.app'", check=False)
            info("Synology Drive se encarga de mantener sincronizados los archivos con el NAS.")
        else:
            warn("Synology Drive no está instalado.")
            if confirm("¿Ir a Configurar equipo para instalarlo?"):
                _elevar_para_configurar("fuera")

def seccion_conectar(ubicacion):
    title("CONECTAR NAS")
    if ubicacion == "oficina":
        _conectar_lan()
    else:
        _conectar_externo()


# ─────────────────────────────────────────────
# [2] CONFIGURAR EQUIPO (requiere admin — lógica existente)
# ─────────────────────────────────────────────

def _get_script_path():
    """Ruta del script para auto-elevación. Funciona tanto directo como vía launcher."""
    try:
        p = os.path.abspath(__file__)
        if p.endswith(".py") and os.path.exists(p):
            return p
    except NameError:
        pass
    return _CACHE_PATH

def _elevar_para_configurar(ubicacion):
    script = _get_script_path()
    flag = "--oficina" if ubicacion == "oficina" else "--fuera"
    if OS == "Windows":
        # Abre nueva ventana elevada — la ventana actual sigue esperando Enter
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable,
            f'"{script}" --configure {flag}',
            None, 1
        )
    else:
        # Sincrónico en Mac — corre en la misma ventana con sudo
        subprocess.run(["/usr/bin/sudo", sys.executable, script, "--configure", flag])

def _hosts_tiene_alias():
    hosts = r"C:\Windows\System32\drivers\etc\hosts" if OS == "Windows" else "/etc/hosts"
    try:
        return NAS_HOST_ALIAS in open(hosts, encoding="utf-8").read()
    except Exception:
        return False

def _agregar_hosts():
    hosts = r"C:\Windows\System32\drivers\etc\hosts" if OS == "Windows" else "/etc/hosts"
    linea = f"\n{NAS_LAN_IP}    {NAS_HOST_ALIAS}    # NAS Ensamble\n"
    try:
        with open(hosts, "a", encoding="utf-8") as f:
            f.write(linea)
        ok(f"Listo — ahora puedes escribir '{NAS_HOST_ALIAS}' en el navegador para entrar al NAS.")
    except PermissionError:
        err("No se pudo hacer este cambio — faltan permisos de administrador.")

def _instalar_tailscale():
    tmp = tempfile.gettempdir()
    if OS == "Windows":
        dest = os.path.join(tmp, "tailscale_setup.exe")
        download(TAILSCALE_WIN_URL, dest)
        run(f'"{dest}" /quiet /norestart')
        ok("Tailscale quedó instalado.")
        info("Se abrirá el navegador para que inicies sesión.")
        info("Usa la cuenta Google de Ensamble que te asignaron.")
        _tailscale_login()
    elif OS == "Darwin":
        dest = os.path.join(tmp, "tailscale.pkg")
        download(TAILSCALE_MAC_URL, dest)
        run(f"installer -pkg '{dest}' -target /")
        ok("Tailscale quedó instalado.")
        _tailscale_login()

def _instalar_synology_drive():
    tmp = tempfile.gettempdir()
    descargado = False
    if OS == "Windows":
        dest = os.path.join(tmp, "synodrive_setup.exe")
        try:
            download(SYNODRIVE_WIN_URL, dest)
            run(f'"{dest}" /S')
            ok("Synology Drive instalado.")
            descargado = True
        except Exception:
            warn("No se pudo descargar automáticamente.")
            info("Abriendo el centro de descarga de Synology en el navegador...")
            run(f'start "" "{SYNODRIVE_DOWNLOAD_PAGE}"', check=False)
    elif OS == "Darwin":
        dest = os.path.join(tmp, "synodrive.dmg")
        try:
            download(SYNODRIVE_MAC_URL, dest)
            run(f"hdiutil attach '{dest}' -quiet")
            run("installer -pkg '/Volumes/Synology Drive Client/Synology Drive Client.pkg' -target /")
            run(f"hdiutil detach '/Volumes/Synology Drive Client' -quiet", check=False)
            ok("Synology Drive instalado.")
            descargado = True
        except Exception:
            warn("No se pudo descargar automáticamente.")
            info("Abriendo el centro de descarga de Synology en el navegador...")
            run(f"open '{SYNODRIVE_DOWNLOAD_PAGE}'", check=False)
    if not descargado:
        info("  → Descarga 'Synology Drive Client' e instálalo.")
        info("  → Cuando termine, vuelve a abrir este programa para configurarlo.")
        return
    info("Configura el servidor en Synology Drive:")
    info(f"  Dirección: {NAS_EXTERNAL_URL}  ·  marca SSL  ·  SIN puerto")
    info(f"  (si pidiera puerto: {NAS_EXTERNAL_URL}:{DSM_HTTPS_PORT})")
    info(f"  Carpeta: {SHARE_ENSAMBLE} → modo On-Demand. Requiere Tailscale activo.")

def _configurar_lan():
    title("CONFIGURAR EQUIPO — RED DE LA OFICINA")

    if _hosts_tiene_alias():
        ok(f"Ya puedes escribir '{NAS_HOST_ALIAS}' en el navegador para entrar al NAS.")
    else:
        info("Configurando el acceso al NAS por navegador...")
        _agregar_hosts()

    info("")
    ok("Configuración completada.")

def _configurar_externo():
    title("CONFIGURAR EQUIPO — ACCESO DESDE FUERA DE LA OFICINA")
    info("Este computador va a poder entrar al NAS aunque no esté en la oficina.")

    info("")
    if _tailscale_instalado():
        if _tailscale_activo():
            ok("Tailscale ya está instalado y funcionando.")
        else:
            ok("Tailscale ya está instalado pero falta iniciar sesión.")
            info("Se abrirá el navegador para que inicies sesión.")
            info("Usa la cuenta Google de Ensamble que te asignaron.")
            _tailscale_login()
    else:
        _instalar_tailscale()

    drive_ok = _synodrive_instalado()
    info("")
    if drive_ok:
        ok("Synology Drive ya está instalado.")
    else:
        _instalar_synology_drive()

    info("")
    ok("Configuración completada.")

def seccion_configurar(ubicacion):
    """Configura el equipo para el módulo elegido. Requiere administrador — se auto-eleva."""
    if not is_admin():
        info("Esta parte necesita permisos de administrador.")
        if OS == "Windows":
            info("Se abrirá una nueva ventana pidiendo permiso de administrador.")
            info("Complétala ahí y luego vuelve a esta ventana.")
        _elevar_para_configurar(ubicacion)
        return

    if ubicacion == "oficina":
        _configurar_lan()
    else:
        _configurar_externo()


# ─────────────────────────────────────────────
# RESUMEN FINAL EDUCATIVO (condicional)
# ─────────────────────────────────────────────

def resumen_final(ubicacion, capas, resultado):
    oficina = (ubicacion == "oficina")
    por_capa = {c["capa"]: c for c in capas}
    title("RESUMEN")

    info(f"Módulo: {'red de la oficina' if oficina else 'acceso fuera de la oficina'}.")

    # Qué quedó funcionando
    verdes = [c for c in capas if c["estado"] == VERDE]
    if verdes:
        info("")
        info("Qué quedó funcionando:")
        for c in verdes:
            print(f"     ✔ {c['capa']} — {c['para_que']}.")

    pendientes = [c for c in capas if c["estado"] != VERDE]
    if pendientes:
        info("")
        info("Qué quedó pendiente:")
        for c in pendientes:
            print(f"     • {c['capa']}: {c['detalle']}")

    # Qué se hizo hoy
    if resultado.get("acciones"):
        nombres = {"configurar": "Se instaló/configuró lo que faltaba",
                   "reconectar_synodrive": "Se mostró la guía para reconectar Synology Drive",
                   "conectar": "Se conectó la carpeta compartida"}
        info("")
        info("Qué se hizo hoy:")
        for a in resultado["acciones"]:
            print(f"     • {nombres.get(a, a)}.")
    if resultado.get("nota"):
        info("")
        warn(resultado["nota"])

    # Cómo entrar al NAS — solo la vía que aplica a este módulo
    info("")
    info("CÓMO ENTRAR AL NAS:")
    if oficina:
        destino = DRIVE_ENSAMBLE if OS == "Windows" else "/Volumes/Ensamble"
        print(f"     1. Carpeta compartida: {destino} — así ves los archivos del NAS como una")
        print(f"        carpeta más de tu computador. Lo más cómodo, dentro de la oficina.")
        print(f"     2. Por navegador, dentro de la oficina: http://{NAS_HOST_ALIAS}:{DSM_HTTP_PORT}")
        print(f"        Si escribes https en vez de http sale una advertencia de seguridad:")
        print(f"        es normal en la red local → \"Avanzado → Continuar\".")
    else:
        ts = por_capa.get("Tailscale")
        if ts and ts["estado"] == VERDE:
            print(f"     1. Por navegador, desde cualquier lugar (Tailscale activo):")
            print(f"        https://{NAS_EXTERNAL_URL}  (sin puerto, candado verde válido).")
        else:
            print(f"     1. Activa Tailscale y luego entra por el navegador a")
            print(f"        https://{NAS_EXTERNAL_URL}  (sin puerto, candado verde).")

    info("")
    info("Si algo deja de conectar, vuelve a abrir este programa:")
    info("se revisa solo y te dice qué hacer.")


# ─────────────────────────────────────────────
# FLUJOS DE ENTRADA
# ─────────────────────────────────────────────

def flujo_diagnostico(ubicacion):
    so_label = "Windows" if OS == "Windows" else "macOS"
    modulo_label = "RED DE LA OFICINA" if ubicacion == "oficina" else "ACCESO FUERA DE LA OFICINA"
    title(f"ENSAMBLE — NAS  ·  {so_label}  ·  {modulo_label}")
    info("Revisando tu conexión al NAS...")

    capas = diagnosticar(ubicacion)
    imprimir_tarjeta(capas)

    try:
        resultado = enrutar(capas, ubicacion)
    except KeyboardInterrupt:
        warn("Cancelado.")
        resultado = {"acciones": [], "nota": None}

    # Refresco best-effort para el resumen (en Mac refleja cambios; en Win, config va en otra ventana)
    try:
        capas = diagnosticar(ubicacion)
    except Exception:
        pass

    resumen_final(ubicacion, capas, resultado)

def menu_clasico():
    """Menú manual (fallback). Se invoca con --menu."""
    so_label = "Windows" if OS == "Windows" else "macOS"
    while True:
        title(f"ENSAMBLE — NAS  ·  {so_label}  (modo manual)")
        print("  [1] Conectar NAS")
        print("  [2] Configurar equipo  (necesita admin)")
        print("  [3] Revisar la conexión")
        print("  [0] Salir\n")
        opcion = ask("Selecciona una opción", ["1", "2", "3", "0"])
        if opcion == "0":
            info("Hasta luego.")
            break
        try:
            if opcion == "1":
                seccion_conectar(elegir_modulo())
            elif opcion == "2":
                seccion_configurar(elegir_modulo())
            elif opcion == "3":
                flujo_diagnostico(elegir_modulo())
        except KeyboardInterrupt:
            warn("Cancelado.")
        input("\n  Presiona Enter para volver al menú...")


def main():
    # Guarda de TTY: esta herramienta es 100% interactiva. Si stdin no es una
    # terminal real (proceso huérfano, lanzado sin consola, terminal cerrada),
    # salir limpio. Sin esto, los bucles de input() giran al 100% de CPU sobre
    # un stdin muerto — causa de los procesos zombie que recalentaban el equipo.
    try:
        es_tty = sys.stdin.isatty()
    except Exception:
        es_tty = False
    if not es_tty:
        sys.stderr.write(
            "mount-nas requiere una terminal interactiva. No hay TTY disponible — saliendo.\n"
        )
        sys.exit(0)

    # Modo configurar: re-lanzado con permisos de admin. El módulo viaja por línea de
    # comandos (--oficina / --fuera) para que la ventana elevada no vuelva a preguntar.
    if "--configure" in sys.argv:
        if "--oficina" in sys.argv:
            ubicacion = "oficina"
        elif "--fuera" in sys.argv:
            ubicacion = "fuera"
        else:
            ubicacion = elegir_modulo()
        seccion_configurar(ubicacion)
        input("\n  Presiona Enter para cerrar...")
        return

    # Modo manual (fallback)
    if "--menu" in sys.argv:
        menu_clasico()
        return

    # Por defecto: pregunta el módulo, lo resuelve, y ofrece dejar listo el otro
    try:
        modulo = elegir_modulo()
        flujo_diagnostico(modulo)
        otro = preguntar_otro_modulo(modulo)
        if otro:
            flujo_diagnostico(otro)
    except KeyboardInterrupt:
        warn("Cancelado.")
    input("\n  Presiona Enter para cerrar...")


if __name__ == "__main__":
    main()
