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
# Usuarios que trabajan el repo en VS Code. Solo a ellos se les instala la
# automatizacion de VS Code (apertura al login + revalidacion tras remontaje).
# Al resto del equipo no le sirve de nada y les mete agentes de arranque.
# Si un equipo cambia de cuenta NAS, agregar la cuenta nueva aqui el mismo dia:
# el bloque se desinstala solo y nadie se entera hasta abrir VS Code.
VSCODE_USERS      = {"davidm"}
RUTA_PROYECTO_WIN = r"Z:\DTI_Tecnología, innovación y optimización\ensamble-platform"
# Nombre del tailnet compartido de Ensamble (CurrentTailnet.Name en `tailscale status --json`).
# Es el mismo para todo el equipo aunque cada colaborador entre con su propia cuenta Google —
# verificado en vivo contra el NAS 2026-08-19 (todo el User map del JSON comparte este tailnet).
TAILSCALE_TAILNET_ESPERADO = "ensamble.dai@gmail.com"

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

def _tailscale_identidad_correcta():
    """
    True/False si se pudo verificar, None si no (sin CLI, JSON inesperado, etc. — nunca
    bloquea el diagnóstico por esto). Compara CurrentTailnet.Name, NO el correo individual
    logueado: gerentes comparten ensamble.dai@gmail.com pero cada colaborador entra con su
    propia cuenta Google — todos caen en el mismo tailnet compartido (verificado 2026-08-19).
    """
    cli = _tailscale_cli()
    if not cli:
        return None
    r = run(f'"{cli}" status --json', check=False, capture=True)
    if r.returncode != 0 or not r.stdout:
        return None
    try:
        data = json.loads(r.stdout)
        nombre = (data.get("CurrentTailnet") or {}).get("Name")
        if not nombre:
            return None
        return nombre == TAILSCALE_TAILNET_ESPERADO
    except Exception:
        return None

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

def _watchdog_mac_instalado():
    """True si el watchdog de reconexión del Mac está instalado Y cargado."""
    if OS != "Darwin":
        return False
    script = os.path.expanduser("~/.local/bin/nas-watchdog-mac.sh")
    plist = os.path.expanduser(f"~/Library/LaunchAgents/{WATCHDOG_MAC_LABEL}.plist")
    if not (os.path.exists(script) and os.path.exists(plist)):
        return False
    r = run(f"launchctl list {WATCHDOG_MAC_LABEL}", check=False, capture=True)
    return r.returncode == 0


def _keychain_tiene_nas():
    """True si el llavero ya guarda una contraseña SMB para el NAS.

    El watchdog monta SIN contraseña en el comando — la toma del llavero. Si no
    está guardada, bajo launchd macOS abriría un diálogo que nadie puede
    contestar y la reconexión falla en silencio. Por eso se diagnostica aparte.
    """
    if OS != "Darwin":
        return False
    for servidor in (NAS_LAN_IP, NAS_HOST_ALIAS):
        r = run(f"security find-internet-password -s '{servidor}'",
                check=False, capture=True)
        if r.returncode == 0:
            return True
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
        montada = _smb_montado()
        if montada:
            destino = DRIVE_ENSAMBLE if OS == "Windows" else f"/Volumes/{SHARE_ENSAMBLE}"
            capas.append(_capa("Carpeta compartida del NAS", VERDE, f"Ya está conectada en {destino}",
                               "así ves y guardas los archivos del NAS como una carpeta más de tu computador"))
        else:
            capas.append(_capa("Carpeta compartida del NAS", ROJO, "Todavía no está conectada",
                               "así ves y guardas los archivos del NAS como una carpeta más de tu computador",
                               "conectar"))

        # ── Credenciales guardadas + Reconexión automática (Windows, solo si ya montada) ──
        # Separadas de la capa de arriba a propósito: si la unidad ya está montada, esta
        # capa se ponía verde y escondía que cmdkey o la tarea de reconexión podían faltar
        # (bug real detectado 2026-08-19 — Z: en verde, sin garantía de las otras dos).
        if OS == "Windows" and montada:
            if _cmdkey_tiene_credencial(NAS_HOST_ALIAS):
                capas.append(_capa("Credenciales guardadas", VERDE,
                                   "Guardadas — no debería volver a pedir la contraseña",
                                   "para que Windows recuerde tu usuario y contraseña del NAS"))
            else:
                capas.append(_capa("Credenciales guardadas", ROJO, "Todavía no están guardadas",
                                   "para que Windows recuerde tu usuario y contraseña del NAS",
                                   "guardar_credenciales"))

            if _reconexion_startup_existe():
                capas.append(_capa("Reconexión automática", VERDE,
                                   "Configurada — reintenta al iniciar sesión y revisa cada 10 min el resto del día",
                                   "para que no tengas que volver a poner la contraseña si la red va lenta o se cae un momento"))
            else:
                capas.append(_capa("Reconexión automática", ROJO,
                                   "Todavía no está configurada",
                                   "para que no tengas que volver a poner la contraseña si la red va lenta o se cae un momento",
                                   "crear_tarea_reconexion"))

        # ── Reconexión automática + llavero (Mac) ──────────────────
        # Deliberadamente NO condicionado a `montada`: si el share ya está
        # montado a mano, estas dos capas son justo las que faltan y las que
        # nadie ve. Es el mismo criterio de las capas equivalentes de Windows
        # (FIX-006), que en Mac nunca se habían replicado — un Mac con el NAS
        # montado a mano salía "todo verde" y jamás ofrecía el watchdog
        # (incidente ENS-MAC-DSK-01, 2026-09-04).
        if OS == "Darwin":
            if _watchdog_mac_instalado():
                capas.append(_capa("Reconexión automática", VERDE,
                                   "Configurada — se reconecta al iniciar sesión y cada minuto",
                                   "para que el NAS vuelva solo al reiniciar, sin montarlo a mano"))
            else:
                capas.append(_capa("Reconexión automática", ROJO,
                                   "Todavía no está configurada",
                                   "para que el NAS vuelva solo al reiniciar, sin montarlo a mano",
                                   "instalar_watchdog_mac"))

            if _keychain_tiene_nas():
                capas.append(_capa("Contraseña en el llavero", VERDE,
                                   "Guardada — la reconexión automática puede usarla",
                                   "porque la reconexión automática saca la contraseña del llavero: no te la puede preguntar"))
            else:
                capas.append(_capa("Contraseña en el llavero", AMBAR,
                                   "No está guardada — conecta una vez desde Finder marcando \"Recordar esta contraseña\"",
                                   "porque la reconexión automática saca la contraseña del llavero: no te la puede preguntar"))

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
            # Cuenta correcta de Tailscale — separada de la capa de arriba: si el NAS
            # responde pero el equipo está en OTRO tailnet (cuenta personal, otra empresa),
            # el resto de capas puede verse verde por casualidad sin que esto sea real.
            identidad = _tailscale_identidad_correcta()
            if identidad is True:
                capas.append(_capa("Cuenta de Tailscale", VERDE, "Conectado a la red de Ensamble",
                                   "para confirmar que este equipo está en la red correcta y no en otra cuenta de Tailscale"))
            elif identidad is False:
                capas.append(_capa("Cuenta de Tailscale", ROJO,
                                   "Conectado a OTRA red de Tailscale — no es la de Ensamble",
                                   "para confirmar que este equipo está en la red correcta y no en otra cuenta de Tailscale",
                                   None))
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
    Decide y propone acciones según el diagnóstico. Devuelve dict con lo ejecutado. Orden:
    configurar (requiere admin) → reconectar SynoDrive → conectar (carpeta compartida) →
    guardar credenciales → crear tarea de reconexión. Cada acción pide confirmación —
    solo se ofrece lo que la capa correspondiente marcó como faltante, nunca de más.
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

    # 4) Guardar credenciales (Windows, unidad ya montada pero sin cmdkey) — no toca net use
    if "guardar_credenciales" in acciones:
        info("")
        if confirm("¿Guardar tus credenciales del NAS ahora?"):
            _guardar_credenciales_win()
            resultado["acciones"].append("guardar_credenciales")

    # 5) Configurar reconexión automática al iniciar sesión (Windows) — usa cmdkey
    if "crear_tarea_reconexion" in acciones:
        info("")
        if confirm("¿Configurar la reconexión automática al iniciar sesión?"):
            # El usuario decide si este equipo recibe la automatizacion de VS Code.
            _instalar_reconexion_startup_win(ask("Usuario NAS"))
            resultado["acciones"].append("crear_tarea_reconexion")

    # 6) Instalar la reconexión automática (Mac) — no requiere desmontar nada
    if "instalar_watchdog_mac" in acciones:
        info("")
        if confirm("¿Configurar la reconexión automática del NAS?"):
            usuario_wd = ask("Usuario NAS")
            if _instalar_watchdog_mac(usuario_wd, _shares_montados()):
                resultado["acciones"].append("instalar_watchdog_mac")

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

def _cmdkey_tiene_credencial(host):
    """True si ya hay una credencial guardada para ese host en el Administrador de
    credenciales de Windows."""
    r = run('cmdkey /list', check=False, capture=True)
    return host.lower() in (r.stdout or "").lower()

def _unidad_asignada(letra):
    """True si la letra tiene una asignación de red registrada (montada o no —
    'No disponible' cuenta como asignada, distinto de nunca haberse configurado)."""
    return run(f'net use {letra}', check=False, capture=True).returncode == 0

def _startup_folder():
    return os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                         "Start Menu", "Programs", "Startup")

def _reconexion_startup_existe():
    return os.path.exists(os.path.join(_startup_folder(), "EnsambleReconectarNAS.bat"))

def _lineas_reconexion(chequear):
    """
    Genera las líneas `net use` para las unidades actualmente asignadas (Z: siempre,
    Y: solo si corresponde a este usuario). Si chequear=True, cada línea se envuelve en
    "if not exist letra:" — para el bucle periódico, que no debe tocar una unidad que ya
    está conectada, solo reconectar la que de verdad se cayó. Si chequear=False,
    reconecta sin condición — para el reintento de arranque, donde vale la pena forzar
    el intento aunque el estado todavía no sea confiable tan temprano en el login.
    """
    incluye_z = _unidad_asignada(DRIVE_ENSAMBLE)
    incluye_y = _unidad_asignada(DRIVE_ARCHIVO)
    unc_z = f"\\\\{NAS_HOST_ALIAS}\\{SHARE_ENSAMBLE}"
    unc_y = f"\\\\{NAS_LAN_IP}\\{SHARE_ARCHIVO}"
    lineas = []
    for incluye, letra, unc in ((incluye_z, DRIVE_ENSAMBLE, unc_z), (incluye_y, DRIVE_ARCHIVO, unc_y)):
        if not incluye:
            continue
        cmd = f'net use {letra} "{unc}" /persistent:yes >nul 2>&1'
        lineas.append(f'if not exist {letra}\\ ({cmd})' if chequear else cmd)
    return lineas

def _instalar_reconexion_startup_win(usuario=None):
    """
    Windows reconecta unidades persistentes muy temprano en el arranque, antes de que la
    red/NetBIOS esté lista. Si esa primera reconexión silenciosa falla, la unidad queda
    "desconectada" y Windows pide credenciales aunque cmdkey ya las tenga guardadas —
    confirmado real (FIX-006, asesor-ti/fixes/reconexion-nas-boot-race-windows.md).

    Un solo script en la carpeta de Inicio (shell:startup) cubre dos cosas: (1) el
    reintento de arranque (a los 30s y 120s del login, sin condición — vale la pena
    forzar el intento tan temprano) y (2) un bucle periódico cada 10 minutos el resto de
    la sesión, que solo reconecta lo que esté caído (`if not exist`) — pedido explícito
    del usuario, para cubrir una caída a mitad del día que el reintento de arranque no
    alcanza a cubrir.

    Deliberadamente NO usa el Programador de tareas para el bucle periódico (se intentó
    primero, revertido 2026-08-19): aunque un `/sc minute` simple sí se logra crear sin
    el problema de sintaxis de `/ri`+`onlogon`, Windows Defender marcó el patrón
    `schtasks /create` + `.bat` en `AppData` + `cmd.exe` como `Trojan:Win32/Commando.A!ml`
    (falso positivo por ML, no por firma) en pruebas reales — es indistinguible para esa
    heurística de una técnica de persistencia real de malware, y se repetiría en cada PC
    de oficina que reciba este script por primera vez. Un bucle dentro de un script de
    Inicio no registra ninguna tarea nueva — mismo patrón (proceso vivo el resto de la
    sesión) que en Mac cubre `_instalar_watchdog_mac()` con un LaunchAgent, sin la firma
    que dispara la alerta.
    """
    # Solo quien trabaja el repo recibe la parte de VS Code (VSCODE_USERS).
    quiere_vscode = bool(usuario) and usuario.lower() in VSCODE_USERS
    proyecto = RUTA_PROYECTO_WIN
    log_vs = r'"%TEMP%\EnsambleVSCode.log"'
    # `code -r` reutiliza la ventana activa en vez de abrir una nueva: si VS Code
    # quedo apuntando a una ruta rota, la reengancha; si ya esta bien, no molesta.
    abrir_vscode = 'start "" /b cmd /c code -r "' + proyecto + '" >> ' + log_vs + ' 2>&1'

    lineas = ["@echo off", "timeout /t 30 /nobreak >nul"]
    for _ in range(2):
        lineas += _lineas_reconexion(chequear=False)
        lineas.append("timeout /t 90 /nobreak >nul")

    if quiere_vscode:
        # Equivalente Windows de `open-project` en Mac: abrir en el proyecto al
        # iniciar sesion, una vez que la unidad de verdad responde.
        # NAS_CAIDA=1 cuando la apertura inicial NO pudo hacerse (unidad todavia
        # no lista): asi el bucle la hace en cuanto la unidad aparezca, en vez de
        # esperar a una caida posterior que quiza nunca ocurra.
        lineas += [
            'if not exist "' + proyecto + '\\" goto sin_apertura',
            'echo %DATE% %TIME% apertura inicial >> ' + log_vs,
            abrir_vscode,
            "set NAS_CAIDA=0",
            "goto bucle",
            ":sin_apertura",
            "set NAS_CAIDA=1",
        ]

    lineas += [
        ":bucle",
        "timeout /t 600 /nobreak >nul",
    ]
    # Se marca ANTES de reconectar: despues de `net use` la unidad ya existe y
    # seria imposible saber que se habia caido.
    if quiere_vscode:
        lineas.append("if not exist " + DRIVE_ENSAMBLE + "\\ set NAS_CAIDA=1")

    lineas += _lineas_reconexion(chequear=True)

    if quiere_vscode:
        # Equivalente Windows de `revalidar-vscode.sh`: solo actua si la unidad se
        # cayo y volvio. Lineas planas, sin bloques entre parentesis, para no
        # depender de la expansion retardada de variables en un bucle `goto`.
        lineas += [
            "if not exist " + DRIVE_ENSAMBLE + "\\ goto bucle",
            'if "%NAS_CAIDA%"=="0" goto bucle',
            "set NAS_CAIDA=0",
            "echo %DATE% %TIME% unidad recuperada, revalidando VS Code >> " + log_vs,
            abrir_vscode,
        ]

    lineas.append("goto bucle")

    destino = os.path.join(_startup_folder(), "EnsambleReconectarNAS.bat")
    try:
        os.makedirs(_startup_folder(), exist_ok=True)
        with open(destino, "w", encoding="utf-8") as f:
            f.write("\r\n".join(lineas) + "\r\n")
        ok("Reconexión automática configurada (arranque + verificación cada 10 min).")
        if quiere_vscode:
            ok("VS Code abrirá en el proyecto al iniciar sesión y tras recuperar la unidad.")
        elif usuario:
            info(f"Usuario {usuario}: sin automatización de VS Code (solo para quien trabaja el repo).")
        return True
    except Exception:
        warn("No se pudo configurar la reconexión automática (no crítico).")
        return False

def _guardar_credenciales_win():
    """Acción standalone: solo pide usuario/contraseña y los guarda en cmdkey — no toca
    net use ni la conexión ya activa. Para cuando la unidad ya está montada pero sin
    credencial guardada (ej. se mapeó manualmente desde el Explorador)."""
    info("Vamos a guardar tu usuario y contraseña del NAS para que Windows no los vuelva a pedir.")
    usuario = ask("Usuario NAS")
    pwd = password_input("Contraseña NAS")
    run(f'cmdkey /delete:{NAS_HOST_ALIAS}', check=False, capture=True)
    run(f'cmdkey /add:{NAS_HOST_ALIAS} /user:"{usuario}" /pass:"{pwd}"', check=False, capture=True)
    if _unidad_asignada(DRIVE_ARCHIVO):
        run(f'cmdkey /delete:{NAS_LAN_IP}', check=False, capture=True)
        run(f'cmdkey /add:{NAS_LAN_IP} /user:"{usuario}" /pass:"{pwd}"', check=False, capture=True)
    ok("Credenciales guardadas.")

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
        _instalar_reconexion_startup_win(usuario)
        return True
    else:
        err(f"No se pudo montar {letra}.")
        info("Verifica tus credenciales y que estés en la red local.")
        return False

def _montar_smb_mac(share, punto_montaje, usuario, password):
    """Monta un share SMB del NAS en Mac vía AppleScript (`mount volume`).

    NO crear el mountpoint a mano: `/Volumes` es root:wheel, así que
    `os.makedirs()` revienta con PermissionError [Errno 13] para un usuario
    normal. Incidente real 2026-09-04 en ENS-MAC-DSK-01: el crash tumbaba el
    script entero antes de llegar a `_instalar_watchdog_mac()`, 10 líneas más
    abajo, y el equipo quedaba sin reconexión automática de forma permanente.
    `mount volume` delega en NetAuthAgent, que crea el punto de montaje con
    privilegios — exactamente lo que hace Finder.

    El AppleScript viaja por stdin, nunca por argv: así la contraseña no queda
    visible en `ps` para los demás usuarios del equipo (Bloque L).
    """
    def esc(s):
        return str(s).replace("\\", "\\\\").replace('"', '\\"')

    script = (
        f'mount volume "smb://{NAS_LAN_IP}/{esc(share)}" '
        f'as user name "{esc(usuario)}" with password "{esc(password)}"'
    )
    try:
        result = subprocess.run(["osascript", "-"], input=script, text=True,
                                capture_output=True, timeout=90)
    except Exception as e:
        err(f"No se pudo montar {share}: {e}")
        return False

    if result.returncode == 0 and os.path.ismount(punto_montaje):
        ok(f"Montado en {punto_montaje}")
        return True

    err(f"No se pudo montar {share}.")
    detalle = (result.stderr or "").strip()
    if detalle:
        info(detalle.splitlines()[0][:160])
    info("Verifica credenciales y conexión de red.")
    return False


# ─────────────────────────────────────────────
# Watchdog SMB para Mac en LA LAN DE OFICINA
# ─────────────────────────────────────────────
# macOS no persiste el montaje SMB al reiniciar (a diferencia de `net use
# /persistent` en Windows) y un laptop que duerme deja un mountpoint zombie.
# Este watchdog (LaunchAgent: al login + cada 60s) remonta al arranque y repara
# montajes muertos sin churn — equivalente Mac de `_instalar_reconexion_startup_win()`.
# NO tiene fallback Tailscale: SMB del NAS fuera de la LAN es una excepción de un
# solo equipo (whitelist en `smb-restrict.sh` del NAS), con su propio watchdog
# personal aparte en `04_Infraestructura/NAS/mac/`.
# Copia de referencia legible: `04_Infraestructura/superscript/nas-watchdog-mac.sh`
# (mantener en sincronía con esta constante).

WATCHDOG_MAC_LABEL = "com.ensamble.nas-watchdog"
OPEN_PROJECT_MAC_LABEL = "com.ensamble.open-project"
SLEEPWATCHER_LABEL = "com.ensamble.sleepwatcher"

OPEN_PROJECT_SH = r'''#!/bin/bash
# open-project.sh — abre VS Code en la carpeta del proyecto Ensamble una vez que
# el NAS esta montado. Generado por mount-nas.py; no editar a mano aqui.
#
# Existe porque VS Code, si arranca antes de que /Volumes/Ensamble este montado
# (tipico tras un reinicio: red aun levantando), no puede restaurar la carpeta
# del proyecto y abre una ventana vacia.
#
# Se espera por `mount` (syscall, sin TCC) y NO por lectura de archivo: un
# proceso de launchd no tiene consentimiento de privacidad para leer volumenes
# de red y no puede pedirlo (FIX-009). Se lanza VS Code con `open -a`
# (LaunchServices) — VS Code si tiene consentimiento y lee la carpeta sin
# problema una vez abierto.

PROJECT="/Volumes/Ensamble/DTI_Tecnología, innovación y optimización/ensamble-platform"
MOUNTPOINT="/Volumes/Ensamble"
LOG="/tmp/open-project.log"

log() { echo "$(date '+%F %T'): $1" >> "$LOG"; }

if [ -f "$LOG" ] && [ "$(stat -f%z "$LOG" 2>/dev/null || echo 0)" -gt 262144 ]; then
    tail -c 40000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

# Espera hasta 120s (40 x 3s) a que el share este montado.
for i in $(seq 1 40); do
    if /sbin/mount | grep -q " on ${MOUNTPOINT} ("; then
        log "NAS montado tras ~$((i*3))s -> abriendo VS Code"
        open -a "Visual Studio Code" "$PROJECT" >>"$LOG" 2>&1
        exit 0
    fi
    sleep 3
done

log "timeout: NAS no montado tras 120s -> no se abre VS Code"
exit 0
'''

SLEEP_UNMOUNT_SH = r'''#!/bin/bash
# ~/.sleep — lo ejecuta sleepwatcher JUSTO ANTES de que el Mac se duerma.
# Generado por mount-nas.py; no editar a mano aqui.
#
# Desmonta limpio los shares SMB del NAS. Dormir con SMB montado deja un
# mountpoint zombie: aparece montado para `mount` pero cuelga en cualquier
# lectura real. El watchdog no puede distinguirlo de uno sano (no puede leer
# bajo launchd por TCC) y por la REGLA DE ORO no lo desmonta, asi que el zombie
# sobrevive hasta que alguien lo desmonta a mano. Prevenirlo aqui es la unica
# defensa. ~/.wakeup vuelve a montar al despertar.
#
# FIX-013 (2026-09-09): este hook ya NO deja marcador para la revalidacion de
# VS Code. Dejo de depender de el porque su ejecucion puede terminar DESPUES
# del despertar (umount de SMB congelado durante el sleep), cuando el consumidor
# del marcador ya habria corrido. Ahora el watchdog sella cada montaje y
# revalidar-vscode.sh compara sellos. Aqui solo queda el desmontaje limpio.

LOG="/tmp/nas-sleepwake.log"
log() { echo "$(date '+%F %T') [sleep] $1" >> "$LOG"; }

if [ -f "$LOG" ] && [ "$(stat -f%z "$LOG" 2>/dev/null || echo 0)" -gt 262144 ]; then
    tail -c 40000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

desmontados=0
for share in __SHARES__; do
    mnt="/Volumes/$share"
    /sbin/mount | grep -q " on $mnt (" || continue
    if /sbin/umount "$mnt" 2>/dev/null || /usr/sbin/diskutil unmount force "$mnt" 2>/dev/null; then
        log "desmontado: $share"
        desmontados=$((desmontados + 1))
    else
        log "NO se pudo desmontar: $share (se dormira con el montaje vivo)"
    fi
done

log "durmiendo (shares desmontados: $desmontados)"
exit 0
'''

WAKEUP_REMOUNT_SH = r'''#!/bin/bash
# ~/.wakeup — lo ejecuta sleepwatcher al DESPERTAR el Mac (incluye dark wakes).
# Generado por mount-nas.py; no editar a mano aqui.
#
# Solo remonta. NO toca VS Code: en un dark wake la pantalla sigue apagada y el
# usuario no esta, asi que abrir una app ahi es robar foco a nadie. De eso se
# encarga revalidar-vscode.sh, que el watchdog invoca cada 60 s y que decide por
# tiempo de inactividad (HIDIdleTime), no por eventos (FIX-015).
#
# El watchdog ya espera a que el NAS acepte TCP/445 antes de montar, asi que
# aqui no hace falta adivinar cuanto tarda la red en volver.

LOG="/tmp/nas-sleepwake.log"
log() { echo "$(date '+%F %T') [wake] $1" >> "$LOG"; }

log "despertando -> lanzando watchdog"
/bin/bash "$HOME/.local/bin/nas-watchdog-mac.sh"

montados=$(/sbin/mount | grep -c "on /Volumes/.* (smbfs")
log "watchdog terminado (shares montados ahora: $montados)"
exit 0
'''

REVALIDAR_VSCODE_SH = r'''#!/bin/bash
# revalidar-vscode.sh — Reabre/enfoca la carpeta del proyecto en VS Code cuando
# el share del NAS se fue y volvio. Generado por mount-nas.py; no editar a mano.
#
# VS Code queda apuntando a una ruta que desaparecio (file watchers rotos,
# archivos que no cargan). El watchdog no arregla eso porque no es un problema
# de montaje.
#
# Lo invoca el watchdog del equipo en CADA corrida — cada 60 s bajo launchd.
# NO hay disparador de eventos: el watchdog pregunta por estado. Este texto es
# identico en las dos variantes de Mac (oficina por LAN, MacBook por Tailscale):
# los watchdogs difieren, la revalidacion no. Fuente unica: esta constante.
#
# -- FIX-015 (2026-09-11): por que ya NO lo dispara sleepwatcher -------------
# FIX-013 dejo esta revalidacion colgada de `sleepwatcher -W` (~/.displaywake).
# Ese disparador NO FUNCIONA. Evidencia medida en las dos maquinas:
#   MacBook de David  — 8 encendidos de pantalla en 14 h, 3 con sello de
#                       remontaje sin consumir, 0 ejecuciones del hook.
#   ENS-MAC-DSK-01    — unicas 2 lineas [display] del log son las corridas
#                       manuales de instalacion (2026-09-07 y 2026-09-09);
#                       el remontaje real de 2026-09-11 11:23:46 quedo
#                       2 h 20 min sin consumir.
# `-s` y `-w` si disparan: los ciclos de desmontaje/remontaje lo prueban. Solo
# `-W` esta muerto, aunque el man page diga que corre tambien al despertar.
#
# La causa de las 5 reincidencias no fue la logica sino el METODO DE
# VERIFICACION: FIX-012 y FIX-013 se validaron corriendo el script a mano. Eso
# prueba la logica, no que macOS invoque el hook.
#
# Regla que queda: un fix de automatizacion no esta verificado hasta que se lo
# observa dispararse SOLO, en un ciclo real.

PROJECT="/Volumes/Ensamble/DTI_Tecnología, innovación y optimización/ensamble-platform"
MOUNTPOINT="/Volumes/Ensamble"
STAMP="/tmp/nas-remount-stamp"        # lo sella el watchdog al montar
DONE="/tmp/nas-revalidado-stamp"      # ultima revalidacion ya aplicada
LOG="/tmp/nas-sleepwake.log"

# Presencia. Sin esto se abriria VS Code en los dark wakes de madrugada, que es
# lo que FIX-013 intentaba evitar con -W. Aca no se pregunta por la pantalla:
# ninguna API de display sirve en Apple Silicon (medido 2026-09-11 —
# IODisplayWrangler no existe, AppleCLCD2.CurrentPowerState y la asercion
# UserIsActive se quedan en 1 con la pantalla apagada). Se pregunta por lo que
# de verdad importa: si alguien toco el equipo hace poco.
IDLE_MAX=1800   # 30 min

log() { echo "$(date '+%F %T') [revalidar] $1" >> "$LOG"; }
solo_numero() { case "$1" in ''|*[!0-9]*) echo 0 ;; *) echo "$1" ;; esac; }

# Segundos desde la ultima actividad de teclado/mouse. ioreg no pasa por TCC,
# asi que es fiable bajo launchd (a diferencia de leer un archivo — FIX-009).
# Si no se puede leer se asume presencia: el fallo historico de este mecanismo
# fue no actuar nunca, no actuar de mas.
idle_segundos() {
    local ns
    ns=$(ioreg -c IOHIDSystem 2>/dev/null | awk -F' = ' '/HIDIdleTime/{print $2; exit}')
    case "$ns" in
        ''|*[!0-9]*) echo 0 ;;
        *) echo $((ns / 1000000000)) ;;
    esac
}

[ -f "$STAMP" ] || exit 0
cur=$(solo_numero "$(cat "$STAMP" 2>/dev/null)")
last=$(solo_numero "$(cat "$DONE" 2>/dev/null)")

# Nada se remonto desde la ultima revalidacion -> no se toca VS Code.
[ "$cur" -gt "$last" ] || exit 0

# El share tiene que estar montado AHORA. Si no, el sello queda pendiente y se
# reintenta al proximo tick. `mount` es syscall: fiable bajo launchd.
/sbin/mount | grep -q " on ${MOUNTPOINT} (" || exit 0

# Nadie en el equipo -> no se consume el sello, se revalida cuando vuelva.
idle=$(idle_segundos)
[ "$idle" -le "$IDLE_MAX" ] || exit 0

# `open -a` es idempotente: si VS Code ya tiene la carpeta abierta la enfoca y
# revalida; si quedo con la ruta rota, la reabre; si estaba cerrado, la abre.
# Con timeout porque esto corre dentro del lock del watchdog: un `open` colgado
# bloquearia los remontajes (leccion FIX-011, el osascript de una hora).
perl -e 'alarm 20; exec @ARGV' \
    /usr/bin/open -a "Visual Studio Code" "$PROJECT" >>"$LOG" 2>&1

echo "$cur" > "$DONE"
log "remontaje $(date -r "$cur" '+%F %T') (idle ${idle}s) -> VS Code revalidado"
exit 0
'''

WATCHDOG_MAC_SH = r'''#!/bin/bash
# nas-watchdog-mac.sh — Mantiene montados los shares SMB del NAS en un Mac DE OFICINA (LAN).
# Generado por mount-nas.py (modulo "red de la oficina"). NO editar a mano aqui:
# editar la constante WATCHDOG_MAC_SH en mount-nas.py y volver a conectar.
# Copia de referencia legible: 04_Infraestructura/superscript/nas-watchdog-mac.sh
#
# Corre al iniciar sesion y cada 60s (LaunchAgent com.ensamble.nas-watchdog).
#
# -- REGLA DE ORO: este script NUNCA desmonta nada ---------------------------
#   Share NO montado (y NAS alcanzable) -> lo monta.
#   Share montado                       -> no hace absolutamente nada.
#   NAS en ventana de apagado           -> silencio total.
#
# -- Por que no desmonta (FIX-009, 2026-09-04) ------------------------------
# Un proceso lanzado por launchd no tiene consentimiento de privacidad (TCC)
# para leer volumenes de red, y no puede pedirlo. La version anterior probaba
# "liveness" leyendo un archivo del share y desmontaba tras N fallos: bajo
# launchd esa lectura falla SIEMPRE -> desmontaba en bucle. `mount` es una
# llamada al sistema y NO pasa por TCC: detectar "no montado" si es fiable.
# Detectar "montado pero zombie" no lo es, asi que ese caso no se maneja aqui
# (lo previene el desmontaje limpio antes de dormir, ~/.sleep).
#
# -- Timeout y espera de red (heredado del Mac de David, 2026-09-07) --------
# Un `osascript mount volume` contra una red que no responde se cuelga con
# error AppleScript -5014 y retiene el lock, bloqueando todos los reintentos
# del ciclo de 60s (caso real: 1 hora colgado). Dos defensas:
#   A1) timeout duro alrededor del osascript (MOUNT_TIMEOUT).
#   A2) sondeo TCP a 445 antes de llamar a AppleScript (nas_alcanzable).
#
# Sin fallback Tailscale: montar el NAS por SMB fuera de la LAN es una
# excepcion reservada a un unico equipo (whitelist en smb-restrict.sh del NAS).

set -u

# -- Config (los marcadores los resuelve mount-nas.py al instalar) ----------
HOST_ALIAS="__HOST_ALIAS__"    # preferido: entrada de /etc/hosts (nas_local)
LAN_IP="__LAN_IP__"            # respaldo por IP directa
NAS_USER="__NAS_USER__"
SHARES=(__SHARES__)

PROJECT_SHARE="Ensamble"          # share que contiene el proyecto (el que le importa a VS Code)
STAMP_FILE="/tmp/nas-remount-stamp"  # sello de remontaje que consume revalidar-vscode.sh
REVALIDAR="$HOME/.local/bin/revalidar-vscode.sh"   # FIX-015

LOG="/tmp/nas-wd-lan.log"
LOCK_DIR="/tmp/nas-wd-lan.lock.d"
LOG_MAX_BYTES=1048576
MOUNT=/sbin/mount

MOUNT_TIMEOUT=45               # A1 - segundos maximos por intento de montaje
RED_INTENTOS=6                 # A2 - rondas de espera a que el NAS responda
RED_PAUSA=4                    # A2 - segundos entre rondas
NC_TIMEOUT=2                   # A2 - timeout de cada sondeo TCP

log() { echo "$(date '+%F %T'): $1" >> "$LOG"; }

rotar_logs() {
    local f size
    for f in "$LOG" /tmp/nas-wd-lan.out /tmp/nas-wd-lan.err; do
        [ -f "$f" ] || continue
        size=$(stat -f%z "$f" 2>/dev/null || echo 0)
        [ "$size" -gt "$LOG_MAX_BYTES" ] && { tail -c 200000 "$f" > "$f.tmp" 2>/dev/null && mv "$f.tmp" "$f"; }
    done
}

adquirir_lock() {
    if mkdir "$LOCK_DIR" 2>/dev/null; then echo $$ > "$LOCK_DIR/pid"; trap 'rm -rf "$LOCK_DIR"' EXIT; return 0; fi
    local oldpid; oldpid=$(cat "$LOCK_DIR/pid" 2>/dev/null || echo "")
    if [ -n "$oldpid" ] && kill -0 "$oldpid" 2>/dev/null; then return 1; fi
    rm -rf "$LOCK_DIR"
    if mkdir "$LOCK_DIR" 2>/dev/null; then echo $$ > "$LOCK_DIR/pid"; trap 'rm -rf "$LOCK_DIR"' EXIT; return 0; fi
    return 1
}

# Ventana de apagado programado del NAS (Power Schedule DSM):
#   Lunes a viernes: apagado 01:00, encendido 04:00
#   Sabado y domingo: apagado 23:00, encendido 04:00 del dia siguiente
en_ventana_apagado() {
    local dow hour
    dow=$(date '+%u'); hour=$(date '+%H'); hour=$((10#$hour))
    if [ "$dow" -ge 1 ] && [ "$dow" -le 5 ] && [ "$hour" -ge 1 ] && [ "$hour" -lt 4 ]; then return 0; fi
    if { [ "$dow" -eq 6 ] || [ "$dow" -eq 7 ]; } && [ "$hour" -ge 23 ]; then return 0; fi
    if { [ "$dow" -eq 7 ] || [ "$dow" -eq 1 ]; } && [ "$hour" -lt 4 ]; then return 0; fi
    return 1
}

# Unico chequeo de estado. `mount` es syscall: fiable desde launchd, sin TCC.
montado() { "$MOUNT" | grep -q " on /Volumes/$1 ("; }

# A2 - espera a que el NAS acepte SMB. Devuelve por stdout el destino alcanzable.
# El ALIAS va primero a proposito: la credencial del llavero queda atada al
# nombre de servidor con que se guardo, y el equipo se conecta por `nas_local`
# (protocolo-conexion-nas.md manda hostname local en la LAN). Montar por IP
# cuando el llavero solo tiene el alias dispara un dialogo de contrasena que
# nadie puede contestar bajo launchd.
nas_alcanzable() {
    local intento destino
    for intento in $(seq 1 "$RED_INTENTOS"); do
        for destino in "$HOST_ALIAS" "$LAN_IP"; do
            [ -n "$destino" ] || continue
            if nc -z -G "$NC_TIMEOUT" "$destino" 445 >/dev/null 2>&1; then
                echo "$destino"; return 0
            fi
        done
        sleep "$RED_PAUSA"
    done
    return 1
}

# A1 - montaje con timeout duro. Sin la alarma, un osascript colgado retiene el
# lock indefinidamente y bloquea todos los reintentos.
montar() {
    local share="$1" destino="$2"
    perl -e "alarm $MOUNT_TIMEOUT; exec @ARGV" \
        /usr/bin/osascript -e "mount volume \"smb://$NAS_USER@$destino/$share\"" >> "$LOG" 2>&1
    sleep 2
    if montado "$share"; then
        log "$share: montado via $destino"
        # Este script es el unico que SABE que hubo un remontaje. Sella la hora;
        # revalidar-vscode.sh la consume cuando el usuario este presente.
        [ "$share" = "$PROJECT_SHARE" ] && date +%s > "$STAMP_FILE"
    else
        log "$share: no se pudo montar via $destino (error o timeout ${MOUNT_TIMEOUT}s; reintenta al proximo ciclo)"
    fi
}

# ----------------------------- flujo ------------------------------
rotar_logs
en_ventana_apagado && exit 0
adquirir_lock || exit 0

# Hay algo pendiente de montar? Si no, no se toca la red.
pendiente=0
for share in "${SHARES[@]}"; do montado "$share" || pendiente=1; done

if [ "$pendiente" -eq 1 ]; then
    if DESTINO=$(nas_alcanzable); then
        for share in "${SHARES[@]}"; do
            montado "$share" && continue      # ya esta montado -> NO TOCAR
            log "$share: no montado -> montando via $DESTINO"
            montar "$share" "$DESTINO"
        done
    else
        log "NAS no responde en 445 tras ~$((RED_INTENTOS * (RED_PAUSA + NC_TIMEOUT * 2)))s -> no se intenta montar (reintenta al proximo ciclo)"
    fi
fi

__REVALIDAR_BLOQUE__
exit 0
'''


def _shares_montados():
    """Shares del NAS montados ahora mismo (por `mount`, sin TCC)."""
    if OS != "Darwin":
        return []
    r = run("/sbin/mount", check=False, capture=True)
    salida = r.stdout or ""
    return [s for s in (SHARE_ENSAMBLE, SHARE_ARCHIVO) if f" on /Volumes/{s} (" in salida]


def _escribir_ejecutable(ruta, contenido):
    try:
        with open(ruta, "w") as f:
            f.write(contenido)
        os.chmod(ruta, 0o755)
        return True
    except Exception:
        return False


def _plist_launchagent(label, script_path, run_at_load=True, interval=None,
                       out_log=None, err_log=None):
    """Genera un LaunchAgent. `LimitLoadToSessionType: Aqua` es obligatorio:
    `osascript mount volume` y `open -a` necesitan sesión gráfica."""
    partes = [
        '<?xml version="1.0" encoding="UTF-8"?>\n',
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n',
        '<plist version="1.0">\n<dict>\n',
        f'    <key>Label</key><string>{label}</string>\n',
        '    <key>ProgramArguments</key>\n',
        f'    <array><string>/bin/bash</string><string>{script_path}</string></array>\n',
    ]
    if run_at_load:
        partes.append('    <key>RunAtLoad</key><true/>\n')
    if interval:
        partes.append(f'    <key>StartInterval</key><integer>{interval}</integer>\n')
    partes.append('    <key>LimitLoadToSessionType</key><string>Aqua</string>\n')
    if out_log:
        partes.append(f'    <key>StandardOutPath</key><string>{out_log}</string>\n')
    if err_log:
        partes.append(f'    <key>StandardErrorPath</key><string>{err_log}</string>\n')
    partes.append('</dict>\n</plist>\n')
    return "".join(partes)


def _cargar_launchagent(plist_path):
    run(f"launchctl unload '{plist_path}'", check=False, capture=True)
    r = run(f"launchctl load '{plist_path}'", check=False, capture=True)
    return r.returncode == 0


def _desinstalar_vscode_mac(bin_dir, la_dir):
    """Retira la automatizacion de VS Code de un Mac cuyo usuario NAS no la necesita.

    Se llama cuando el equipo YA la tenia y cambio de usuario, o cuando una version
    vieja de este script la instalaba a todo el mundo. Devuelve lo que quito, para
    poder decirlo en pantalla en vez de hacerlo callado.
    """
    quitados = []
    plist = os.path.join(la_dir, f"{OPEN_PROJECT_MAC_LABEL}.plist")
    if os.path.exists(plist):
        run(f"launchctl unload '{plist}'", check=False, capture=True)
        try:
            os.remove(plist)
            quitados.append("LaunchAgent open-project")
        except Exception:
            pass
    for nombre in ("open-project.sh", "revalidar-vscode.sh"):
        ruta = os.path.join(bin_dir, nombre)
        if os.path.exists(ruta):
            try:
                os.remove(ruta)
                quitados.append(nombre)
            except Exception:
                pass
    # Resto de FIX-013 por si el equipo viene de una version anterior a FIX-015.
    dw = os.path.expanduser("~/.displaywake")
    if os.path.exists(dw):
        try:
            os.remove(dw)
            quitados.append("~/.displaywake")
        except Exception:
            pass
    return quitados


def _instalar_watchdog_mac(usuario, shares):
    """Instala la reconexión automática del NAS en un Mac de oficina.

    Tres piezas, todas idempotentes (se pueden reinstalar sin romper nada):

    1. **Watchdog** (`nas-watchdog-mac.sh` + LaunchAgent, al login y cada 60s):
       monta lo que falte, nunca desmonta.
    2. **open-project** (`open-project.sh` + LaunchAgent, al login): abre VS Code
       en el proyecto una vez que el NAS está montado.
    3. **Hooks de sleep/wake** (`~/.sleep` / `~/.wakeup`): desmontan limpio antes
       de dormir y remontan al despertar. **Requieren `sleepwatcher`** (Homebrew).
       Si no está instalado se omiten y se avisa — el resto funciona igual, solo
       que un mountpoint zombie tras una suspensión sobrevive hasta desmontarlo
       a mano (ver REGLA DE ORO en el watchdog).

    `shares`: lista de shares realmente montados. Sin shares -> no hace nada.
    """
    if OS != "Darwin" or not shares:
        return False

    bin_dir = os.path.expanduser("~/.local/bin")
    la_dir = os.path.expanduser("~/Library/LaunchAgents")
    os.makedirs(bin_dir, exist_ok=True)
    os.makedirs(la_dir, exist_ok=True)

    shares_bash = " ".join(f'"{s}"' for s in shares)

    # Solo quien trabaja el repo recibe la automatizacion de VS Code (VSCODE_USERS).
    # Al resto no le sirve y le deja agentes de arranque que nadie pidio.
    quiere_vscode = usuario.lower() in VSCODE_USERS

    if quiere_vscode:
        revalidar_bloque = (
            '# FIX-015 - corre SIEMPRE, incluso en los ticks donde no hubo nada que montar:\n'
            '# tras un remontaje exitoso el estado normal es "todo montado, sello pendiente".\n'
            '# El script decide solo si hay algo que hacer; aqui no se filtra nada.\n'
            'if [ -x "$REVALIDAR" ]; then\n'
            '    "$REVALIDAR"\n'
            'else\n'
            '    log "AVISO: falta $REVALIDAR -> VS Code no se revalidara tras un remontaje"\n'
            'fi'
        )
    else:
        # Sin la llamada, no solo sin el script: dejarla puesta escribiria el AVISO
        # cada 60 s en un equipo donde el script falta A PROPOSITO.
        revalidar_bloque = (
            '# Sin revalidacion de VS Code: el usuario NAS de este equipo no esta en\n'
            '# VSCODE_USERS (mount-nas.py). No es un olvido, es deliberado.'
        )

    # ── 1. Watchdog ───────────────────────────────────────────────
    wd_path = os.path.join(bin_dir, "nas-watchdog-mac.sh")
    wd = (WATCHDOG_MAC_SH
          .replace("__HOST_ALIAS__", NAS_HOST_ALIAS)
          .replace("__LAN_IP__", NAS_LAN_IP)
          .replace("__NAS_USER__", usuario)
          .replace("__SHARES__", shares_bash)
          .replace("__REVALIDAR_BLOQUE__", revalidar_bloque))
    if not _escribir_ejecutable(wd_path, wd):
        warn("No se pudo escribir el watchdog del Mac (no crítico).")
        return False

    wd_plist = os.path.join(la_dir, f"{WATCHDOG_MAC_LABEL}.plist")
    if not _escribir_ejecutable(wd_plist, _plist_launchagent(
            WATCHDOG_MAC_LABEL, wd_path, run_at_load=True, interval=60,
            out_log="/tmp/nas-wd-lan.out", err_log="/tmp/nas-wd-lan.err")):
        warn("No se pudo escribir el LaunchAgent del watchdog (no crítico).")
        return False
    _cargar_launchagent(wd_plist)
    ok("Reconexión automática configurada (al iniciar sesión y cada 60s).")

    # ── 2. VS Code: open-project + revalidacion (solo VSCODE_USERS) ──
    if quiere_vscode:
        op_path = os.path.join(bin_dir, "open-project.sh")
        if _escribir_ejecutable(op_path, OPEN_PROJECT_SH):
            op_plist = os.path.join(la_dir, f"{OPEN_PROJECT_MAC_LABEL}.plist")
            if _escribir_ejecutable(op_plist, _plist_launchagent(
                    OPEN_PROJECT_MAC_LABEL, op_path, run_at_load=True, interval=None,
                    out_log="/tmp/open-project.out", err_log="/tmp/open-project.err")):
                _cargar_launchagent(op_plist)
                ok("VS Code se abrirá en el proyecto al iniciar sesión.")
        # FIX-015: la revalidacion ya no es un hook de sleepwatcher (-W es un
        # disparador muerto). Vive junto al watchdog, que la invoca cada 60 s.
        _escribir_ejecutable(os.path.join(bin_dir, "revalidar-vscode.sh"), REVALIDAR_VSCODE_SH)
    else:
        quitados = _desinstalar_vscode_mac(bin_dir, la_dir)
        if quitados:
            ok("Automatización de VS Code retirada: " + ", ".join(quitados) + ".")
        info(f"Usuario {usuario}: sin automatización de VS Code (solo para quien trabaja el repo).")

    # ── 3. Hooks de sleep/wake (requieren sleepwatcher) ───────────
    sleep_path = os.path.expanduser("~/.sleep")
    wake_path = os.path.expanduser("~/.wakeup")
    _escribir_ejecutable(sleep_path, SLEEP_UNMOUNT_SH.replace("__SHARES__", shares_bash))
    _escribir_ejecutable(wake_path, WAKEUP_REMOUNT_SH)

    if _sleepwatcher_instalado():
        _cargar_sleepwatcher()
        ok("Desmontaje limpio antes de dormir y remontaje al despertar activados.")
    else:
        warn("Falta `sleepwatcher` — los hooks de suspensión quedaron escritos pero inactivos.")
        info("Para activarlos: brew install sleepwatcher, y vuelve a correr esto.")

    return True


def _sleepwatcher_bin():
    for p in ("/opt/homebrew/sbin/sleepwatcher", "/usr/local/sbin/sleepwatcher"):
        if os.path.exists(p):
            return p
    return None


def _sleepwatcher_instalado():
    return _sleepwatcher_bin() is not None


def _cargar_sleepwatcher():
    """LaunchAgent propio para sleepwatcher, apuntando a ~/.sleep y ~/.wakeup.
    No se reutiliza el plist de la fórmula de Homebrew: cambia de ruta según el
    prefijo (Intel vs Apple Silicon) y no siempre queda instalado en el usuario."""
    binario = _sleepwatcher_bin()
    if not binario:
        return False
    la_dir = os.path.expanduser("~/Library/LaunchAgents")
    plist_path = os.path.join(la_dir, f"{SLEEPWATCHER_LABEL}.plist")
    home = os.path.expanduser("~")
    plist = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        f'    <key>Label</key><string>{SLEEPWATCHER_LABEL}</string>\n'
        '    <key>ProgramArguments</key>\n'
        f'    <array><string>{binario}</string>'
        f'<string>-V</string>'
        f'<string>-s</string><string>{home}/.sleep</string>'
        # FIX-015: sin -W. Ese disparador esta muerto (0 ejecuciones medidas en
        # ambas maquinas); la revalidacion de VS Code la hace el watchdog.
        f'<string>-w</string><string>{home}/.wakeup</string></array>\n'
        '    <key>RunAtLoad</key><true/>\n'
        '    <key>KeepAlive</key><true/>\n'
        '    <key>LimitLoadToSessionType</key><string>Aqua</string>\n'
        '    <key>StandardOutPath</key><string>/tmp/sleepwatcher.out</string>\n'
        '    <key>StandardErrorPath</key><string>/tmp/sleepwatcher.err</string>\n'
        '</dict>\n</plist>\n'
    )
    if not _escribir_ejecutable(plist_path, plist):
        return False
    return _cargar_launchagent(plist_path)


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
        montados = []
        ok_e = _montar_smb_mac(SHARE_ENSAMBLE, f"/Volumes/{SHARE_ENSAMBLE}", usuario, pwd)
        if ok_e:
            montados.append(SHARE_ENSAMBLE)
        if es_admin:
            # Punto de montaje con el NOMBRE REAL del share (con espacio): lo crea
            # NetAuthAgent, y es el que el watchdog vigila con `mount`. Antes decía
            # "ARCHIVO_ENSAMBLE" (guion bajo) y nunca coincidía.
            ok_a = _montar_smb_mac(SHARE_ARCHIVO, f"/Volumes/{SHARE_ARCHIVO}", usuario, pwd)
            if ok_a:
                montados.append(SHARE_ARCHIVO)
            if ok_e and ok_a:
                ok("Carpetas Ensamble y ARCHIVO ENSAMBLE montadas.")
            elif ok_e:
                warn("Ensamble montada. No se pudo montar ARCHIVO ENSAMBLE.")
        else:
            if ok_e:
                ok("Carpeta Ensamble montada.")
        info("")
        if ok_e:
            # Se pasan los shares REALMENTE montados: si el usuario es admin y
            # ARCHIVO ENSAMBLE subió, el watchdog debe vigilar los dos. Antes se
            # pasaba siempre [SHARE_ENSAMBLE] y el segundo share quedaba huérfano.
            _instalar_watchdog_mac(usuario, montados)
            info("El montaje se reconecta solo al reiniciar y tras cada suspensión.")
        else:
            info("En Mac el montaje no persiste al reiniciar; vuelve a correr esto para reconectar.")

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
                   "conectar": "Se conectó la carpeta compartida",
                   "guardar_credenciales": "Se guardaron las credenciales del NAS",
                   "crear_tarea_reconexion": "Se configuró la reconexión automática (arranque + cada 10 min)"}
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
