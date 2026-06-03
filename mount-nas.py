"""
mount-nas.py — Conecta y configura la NAS Ensamble
Arranca con un DIAGNÓSTICO automático por capas y enruta las acciones.
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

def ask(prompt, options=None):
    while True:
        val = input(f"\n  → {prompt}: ").strip()
        if not options or val in options:
            return val
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

def detectar_red():
    """Devuelve 'lan' si el NAS local responde por ping, 'external' si no."""
    ping = (f"ping -n 1 -w 1000 {NAS_LAN_IP}" if OS == "Windows"
            else f"ping -c 1 -W 1 {NAS_LAN_IP}")
    r = run(ping, check=False, capture=True)
    return "lan" if r.returncode == 0 else "external"

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

def _parece_servidor(val):
    """Heurística: ¿este string parece una dirección de servidor?"""
    if not isinstance(val, str):
        return False
    v = val.strip()
    if not v or len(v) > 120 or " " in v or "\\" in v or v.count("/") > 0:
        return False
    low = v.lower()
    if "quickconnect.to" in low or "ensambleai" in low:
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
        return ROJO, f"Apunta a un canal viejo: {viejo}"
    if apunta_bien:
        if https_ok:
            return VERDE, "Apunta a nas.ensambleai.com y responde"
        return AMBAR, "Apunta a nas.ensambleai.com pero el servidor no responde (¿Tailscale apagado?)"
    if candidatos:
        return AMBAR, f"Servidor no reconocido: {candidatos[0]}"
    return AMBAR, "No se pudo leer el servidor configurado"


# ─────────────────────────────────────────────
# DIAGNÓSTICO POR CAPAS
# ─────────────────────────────────────────────

def _capa(nombre, estado, detalle, para_que, accion=None):
    return {"capa": nombre, "estado": estado, "detalle": detalle,
            "para_que": para_que, "accion": accion}

def detectar_ubicacion():
    """Auto-detecta oficina/fuera y deja que el usuario confirme o corrija."""
    info("Detectando tu ubicación...")
    red = detectar_red()
    propuesta = "oficina" if red == "lan" else "fuera"
    otra = "fuera" if propuesta == "oficina" else "oficina"
    if propuesta == "oficina":
        ok(f"El NAS responde en la red local ({NAS_LAN_IP}) → parece que estás en la OFICINA.")
    else:
        info(f"El NAS no responde en la red local → parece que estás FUERA de la oficina.")
    if confirm(f"¿Es correcto que estás en la {propuesta}?"):
        return propuesta
    info(f"Entendido — usaré modo: {otra}.")
    return otra

def diagnosticar(ubicacion):
    """Construye la lista de capas con estado verde/rojo/ámbar y acción sugerida."""
    capas = []
    oficina = (ubicacion == "oficina")

    # ── Tailscale ──────────────────────────────────────────────
    # Verdad de terreno: si el NAS Tailscale responde, el túnel está arriba.
    ts_peer = _tailscale_peer_alcanzable()
    ts_inst = _tailscale_instalado()
    if ts_peer:
        capas.append(_capa("Tailscale", VERDE, "Activo — NAS alcanzable (100.81.124.50)",
                           "VPN para entrar al NAS desde cualquier lugar"))
    elif not ts_inst:
        capas.append(_capa("Tailscale", ROJO, "No está instalado",
                           "VPN para entrar al NAS desde cualquier lugar", "configurar"))
    elif not _tailscale_activo():
        capas.append(_capa("Tailscale", ROJO, "Instalado pero sin sesión activa",
                           "VPN para entrar al NAS desde cualquier lugar", "configurar"))
    else:
        capas.append(_capa("Tailscale", AMBAR, "Activo pero el NAS no responde aún",
                           "VPN para entrar al NAS desde cualquier lugar", None))

    # ── DNS ────────────────────────────────────────────────────
    dns_ok, ips = _dns_resuelve(NAS_EXTERNAL_URL, NAS_TAILSCALE_IP)
    if dns_ok:
        capas.append(_capa("DNS", VERDE, f"{NAS_EXTERNAL_URL} → {NAS_TAILSCALE_IP}",
                           "Traduce el nombre del NAS a su dirección"))
    elif ips:
        capas.append(_capa("DNS", AMBAR, f"{NAS_EXTERNAL_URL} resuelve a {', '.join(sorted(ips))}",
                           "Traduce el nombre del NAS a su dirección"))
    else:
        capas.append(_capa("DNS", ROJO, f"{NAS_EXTERNAL_URL} no resuelve",
                           "Traduce el nombre del NAS a su dirección"))

    # ── Reverse proxy (DSM por HTTPS) ──────────────────────────
    https_ok, codigo = _https_responde(NAS_EXTERNAL_URL)
    if https_ok:
        capas.append(_capa("Acceso web (HTTPS)", VERDE,
                           f"DSM responde en https://{NAS_EXTERNAL_URL} (HTTP {codigo})",
                           "Entrar al NAS por navegador con candado verde"))
    else:
        capas.append(_capa("Acceso web (HTTPS)", ROJO,
                           f"https://{NAS_EXTERNAL_URL} no responde ({codigo})",
                           "Entrar al NAS por navegador con candado verde",
                           None if ts_peer else "configurar"))

    # ── SMB (solo oficina) ─────────────────────────────────────
    if oficina:
        if _smb_montado():
            destino = DRIVE_ENSAMBLE if OS == "Windows" else f"/Volumes/{SHARE_ENSAMBLE}"
            capas.append(_capa("Carpeta de red (SMB)", VERDE, f"Montada en {destino}",
                               "Trabajar archivos del NAS como una carpeta local"))
        else:
            capas.append(_capa("Carpeta de red (SMB)", ROJO, "No está montada",
                               "Trabajar archivos del NAS como una carpeta local", "conectar"))

    # ── Synology Drive ─────────────────────────────────────────
    if not _synodrive_instalado():
        capas.append(_capa("Synology Drive", ROJO, "No está instalado",
                           "Sincronizar carpetas del NAS al equipo", "configurar"))
    else:
        candidatos = _synodrive_servidores_configurados()
        estado_sd, detalle_sd = _clasificar_synodrive(candidatos, https_ok)
        accion_sd = "reconectar_synodrive" if estado_sd == ROJO else None
        capas.append(_capa("Synology Drive", estado_sd, detalle_sd,
                           "Sincronizar carpetas del NAS al equipo", accion_sd))

    # ── Acceso web LAN / hosts (solo oficina) ──────────────────
    if oficina:
        if _hosts_tiene_alias():
            capas.append(_capa("Alias LAN (hosts)", VERDE,
                               f"'{NAS_HOST_ALIAS}' presente → http://{NAS_HOST_ALIAS}:{DSM_HTTP_PORT}",
                               "Entrar al NAS por navegador dentro de la oficina"))
        else:
            capas.append(_capa("Alias LAN (hosts)", ROJO,
                               f"Falta '{NAS_HOST_ALIAS}' en el archivo hosts",
                               "Entrar al NAS por navegador dentro de la oficina", "configurar"))

    return capas

def imprimir_tarjeta(capas):
    simbolos = {VERDE: "✔", ROJO: "✖", AMBAR: "⚠"}
    title("DIAGNÓSTICO DE CONEXIÓN AL NAS")
    for c in capas:
        s = simbolos.get(c["estado"], "·")
        print(f"  {s}  {c['capa']:<22} {c['detalle']}")
    print()


# ─────────────────────────────────────────────
# ROUTING AUTOMÁTICO
# ─────────────────────────────────────────────

def enrutar(capas, ubicacion):
    """
    Decide y propone acciones según el diagnóstico. Devuelve dict con lo ejecutado.
    Orden: configurar (infra/admin) → reconectar SynoDrive → conectar (montar SMB).
    Cada acción pide confirmación.
    """
    resultado = {"acciones": [], "nota": None}
    acciones = {c["accion"] for c in capas if c["accion"]}

    # Todo verde
    if all(c["estado"] == VERDE for c in capas):
        title("RESULTADO")
        ok("Conexión ya funcionando. No hay nada que hacer.")
        return resultado

    title("QUÉ HACER")
    fallas = [c for c in capas if c["estado"] != VERDE]
    info("Detecté lo siguiente para resolver:")
    for c in fallas:
        simbolo = "✖" if c["estado"] == ROJO else "⚠"
        print(f"     {simbolo} {c['capa']}: {c['detalle']}")

    # 1) Configurar (Tailscale / SynoDrive faltante / hosts) — requiere admin
    if "configurar" in acciones:
        info("")
        info("Falta instalar o configurar componentes (requiere administrador).")
        if confirm("¿Ejecutar CONFIGURAR ahora?"):
            seccion_configurar()
            resultado["acciones"].append("configurar")
            if OS == "Windows":
                resultado["nota"] = ("La configuración se abrió en otra ventana (admin). "
                                     "Complétala allí; este resumen refleja el estado previo.")

    # 2) Reconectar SynoDrive (canal viejo) — guía guiada, no se reescribe en silencio
    if "reconectar_synodrive" in acciones:
        info("")
        warn("Synology Drive apunta a un canal viejo (QuickConnect/DDNS).")
        info("No se puede reescribir su configuración en silencio (Synology no lo soporta).")
        if confirm("¿Cerrar el cliente y mostrar la guía para reconectarlo?"):
            reconectar_synodrive()
            resultado["acciones"].append("reconectar_synodrive")

    # 3) Conectar (montar SMB) — solo oficina, infra OK
    if "conectar" in acciones:
        info("")
        if confirm("¿Montar ahora la carpeta de red del NAS (CONECTAR)?"):
            seccion_conectar()
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
    info("Cerrando el cliente de Synology Drive...")
    _cerrar_synodrive()
    ok("Cliente cerrado.")
    print()
    print("  Sigue estos pasos (la app puede variar un poco según su versión):")
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

def _montar_unidad_win(letra, share, usuario, password):
    unc = f"\\\\{NAS_HOST_ALIAS}\\{share}"
    run(f'net use {letra} /delete /y', check=False, capture=True)
    cmd = f'net use {letra} "{unc}" /persistent:yes /user:"{usuario}" "{password}"'
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
    ok(f"Red local detectada ({NAS_LAN_IP}).")
    info("")
    info("Ingresa tus credenciales del NAS:")
    usuario = ask("Usuario NAS")
    pwd = password_input("Contraseña NAS")
    es_admin = usuario.lower() in NAS_ADMIN_USERS

    if OS == "Windows":
        ok_z = _montar_unidad_win(DRIVE_ENSAMBLE, SHARE_ENSAMBLE, usuario, pwd)
        if es_admin:
            ok_y = _montar_unidad_win(DRIVE_ARCHIVO, SHARE_ARCHIVO, usuario, pwd)
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
    info("Sin acceso a red local. Verificando acceso remoto...")
    if not _tailscale_activo():
        warn("Tailscale no está activo o no está instalado.")
        info("Tailscale es necesario para conectarte al NAS de forma remota.")
        if confirm("¿Ir a Configurar equipo para instalar Tailscale?"):
            _elevar_para_configurar()
        return

    ok("Tailscale activo.")

    if OS == "Windows":
        drive_paths = [
            r"C:\Program Files\SynologyDrive\SynologyDrive.exe",
            r"C:\Program Files (x86)\Synology\SynologyDrive\bin\launcher.exe",
        ]
        drive_exe = next((p for p in drive_paths if os.path.exists(p)), None)
        if drive_exe:
            ok("Synology Drive instalado.")
            info("Abriendo Synology Drive...")
            run(f'start "" "{drive_exe}"', check=False)
            info("Synology Drive maneja la sincronización con el NAS automáticamente.")
        else:
            warn("Synology Drive no está instalado.")
            if confirm("¿Ir a Configurar equipo para instalarlo?"):
                _elevar_para_configurar()
    elif OS == "Darwin":
        drive_app = "/Applications/Synology Drive Client.app"
        if os.path.exists(drive_app):
            ok("Synology Drive instalado.")
            run("open '/Applications/Synology Drive Client.app'", check=False)
            info("Synology Drive maneja la sincronización con el NAS automáticamente.")
        else:
            warn("Synology Drive no está instalado.")
            if confirm("¿Ir a Configurar equipo para instalarlo?"):
                _elevar_para_configurar()

def seccion_conectar():
    title("CONECTAR NAS")
    red = detectar_red()
    if red == "lan":
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

def _elevar_para_configurar():
    script = _get_script_path()
    if OS == "Windows":
        # Abre nueva ventana elevada — la ventana actual sigue esperando Enter
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable,
            f'"{script}" --configure',
            None, 1
        )
    else:
        # Sincrónico en Mac — corre en la misma ventana con sudo
        subprocess.run(["/usr/bin/sudo", sys.executable, script, "--configure"])

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
        ok(f"Alias '{NAS_HOST_ALIAS}' → {NAS_LAN_IP} agregado al archivo hosts.")
    except PermissionError:
        err("Sin permisos para modificar el archivo hosts.")

def _instalar_tailscale():
    tmp = tempfile.gettempdir()
    if OS == "Windows":
        dest = os.path.join(tmp, "tailscale_setup.exe")
        download(TAILSCALE_WIN_URL, dest)
        run(f'"{dest}" /quiet /norestart')
        ok("Tailscale instalado.")
        info("Se abrirá el browser para iniciar sesión.")
        info("Usa la cuenta Google de Ensamble que te asignaron.")
        _tailscale_login()
    elif OS == "Darwin":
        dest = os.path.join(tmp, "tailscale.pkg")
        download(TAILSCALE_MAC_URL, dest)
        run(f"installer -pkg '{dest}' -target /")
        ok("Tailscale instalado.")
        _tailscale_login()

def _instalar_synology_drive():
    tmp = tempfile.gettempdir()
    if OS == "Windows":
        dest = os.path.join(tmp, "synodrive_setup.exe")
        download(SYNODRIVE_WIN_URL, dest)
        run(f'"{dest}" /S')
        ok("Synology Drive instalado.")
    elif OS == "Darwin":
        dest = os.path.join(tmp, "synodrive.dmg")
        download(SYNODRIVE_MAC_URL, dest)
        run(f"hdiutil attach '{dest}' -quiet")
        run("installer -pkg '/Volumes/Synology Drive Client/Synology Drive Client.pkg' -target /")
        run(f"hdiutil detach '/Volumes/Synology Drive Client' -quiet", check=False)
        ok("Synology Drive instalado.")
    info("Configura el servidor en Synology Drive:")
    info(f"  Dirección: {NAS_EXTERNAL_URL}  ·  marca SSL  ·  SIN puerto")
    info(f"  (si pidiera puerto: {NAS_EXTERNAL_URL}:{DSM_HTTPS_PORT})")
    info(f"  Carpeta: {SHARE_ENSAMBLE} → modo On-Demand. Requiere Tailscale activo.")

def _configurar_lan():
    title("CONFIGURAR EQUIPO — RED LOCAL")

    # Alias en hosts — siempre (para acceso web por nombre)
    if _hosts_tiene_alias():
        ok(f"Alias '{NAS_HOST_ALIAS}' ya existe en hosts.")
    else:
        info("Configurando alias del NAS en archivo hosts...")
        _agregar_hosts()

    # Tailscale — opcional (laptops que también salen de la oficina)
    info("")
    if _tailscale_instalado():
        ok("Tailscale ya está instalado.")
    elif confirm("¿Este equipo también se usa fuera de la oficina? → instalar Tailscale"):
        _instalar_tailscale()

    # Synology Drive — opcional
    drive_ok = _synodrive_instalado()
    info("")
    if drive_ok:
        ok("Synology Drive ya está instalado.")
    elif confirm("¿Instalar Synology Drive Client? (sincronización / acceso remoto)"):
        _instalar_synology_drive()

    info("")
    ok("Configuración completada.")

def _configurar_externo():
    title("CONFIGURAR EQUIPO — ACCESO REMOTO")
    info("Este equipo accede al NAS desde fuera de la oficina.")

    # Tailscale — siempre
    info("")
    if _tailscale_instalado():
        if _tailscale_activo():
            ok("Tailscale instalado y activo.")
        else:
            ok("Tailscale instalado pero sin sesión.")
            info("Iniciando sesión — se abrirá el browser.")
            info("Usa la cuenta Google de Ensamble que te asignaron.")
            _tailscale_login()
    else:
        _instalar_tailscale()

    # Synology Drive — siempre
    drive_ok = _synodrive_instalado()
    info("")
    if drive_ok:
        ok("Synology Drive ya está instalado.")
    else:
        _instalar_synology_drive()

    # Alias en hosts — opcional (solo útil si a veces van a la oficina)
    info("")
    if _hosts_tiene_alias():
        ok(f"Alias '{NAS_HOST_ALIAS}' ya existe en hosts.")
    elif confirm("¿Agregar alias 'nas_local' en hosts? (solo necesario si a veces vas a la oficina)"):
        _agregar_hosts()

    info("")
    ok("Configuración completada.")

def seccion_configurar():
    """Detecta red y configura el equipo según contexto. Requiere admin — se auto-eleva."""
    if not is_admin():
        info("Esta sección requiere permisos de administrador.")
        if OS == "Windows":
            info("Se abrirá una nueva ventana con permisos de administrador.")
            info("Completa la configuración allí y luego regresa a esta ventana.")
        _elevar_para_configurar()
        return

    red = detectar_red()
    if red == "lan":
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

    info(f"Estás en: {'la oficina' if oficina else 'fuera de la oficina'}.")

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
        nombres = {"configurar": "Configuración de componentes (Tailscale / Synology Drive / alias)",
                   "reconectar_synodrive": "Guía para reconectar Synology Drive",
                   "conectar": "Montaje de la carpeta de red"}
        info("")
        info("Qué se hizo hoy:")
        for a in resultado["acciones"]:
            print(f"     • {nombres.get(a, a)}.")
    if resultado.get("nota"):
        info("")
        warn(resultado["nota"])

    # Cómo entrar al NAS — solo las vías que aplican
    info("")
    info("CÓMO ENTRAR AL NAS:")
    n = 1
    if oficina:
        destino = f"{DRIVE_ENSAMBLE} (unidad de red)" if OS == "Windows" else "/Volumes/Ensamble"
        print(f"     {n}. Carpeta de red: {destino} — lo más cómodo, dentro de la oficina.")
        n += 1
        print(f"     {n}. Navegador en oficina: http://{NAS_HOST_ALIAS}:{DSM_HTTP_PORT}  (REQUIERE el puerto)")
        print(f"        Con https://{NAS_HOST_ALIAS}:{DSM_HTTPS_PORT} sale una advertencia de certificado:")
        print(f"        es normal en la red local → \"Avanzado → Continuar\".")
        n += 1
    ts = por_capa.get("Tailscale")
    if ts and ts["estado"] == VERDE:
        print(f"     {n}. Navegador desde cualquier lugar (Tailscale activo):")
        print(f"        https://{NAS_EXTERNAL_URL}  (sin puerto, candado verde válido).")
    else:
        print(f"     {n}. Desde fuera de la oficina: activa Tailscale y entra a")
        print(f"        https://{NAS_EXTERNAL_URL}  (sin puerto, candado verde).")

    info("")
    info("Si algo deja de conectar, vuelve a abrir este programa:")
    info("se diagnostica solo y te dice qué hacer.")


# ─────────────────────────────────────────────
# FLUJOS DE ENTRADA
# ─────────────────────────────────────────────

def flujo_diagnostico():
    so_label = "Windows" if OS == "Windows" else "macOS"
    title(f"ENSAMBLE — NAS  ·  {so_label}")
    info("Diagnóstico automático de tu conexión al NAS.")

    ubicacion = detectar_ubicacion()
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
        print("  [3] Diagnóstico automático")
        print("  [0] Salir\n")
        opcion = ask("Selecciona una opción", ["1", "2", "3", "0"])
        if opcion == "0":
            info("Hasta luego.")
            break
        try:
            if opcion == "1":
                seccion_conectar()
            elif opcion == "2":
                seccion_configurar()
            elif opcion == "3":
                flujo_diagnostico()
        except KeyboardInterrupt:
            warn("Cancelado.")
        input("\n  Presiona Enter para volver al menú...")


def main():
    # Modo configurar: re-lanzado con permisos de admin
    if "--configure" in sys.argv:
        seccion_configurar()
        input("\n  Presiona Enter para cerrar...")
        return

    # Modo manual (fallback)
    if "--menu" in sys.argv:
        menu_clasico()
        return

    # Por defecto: diagnóstico automático
    try:
        flujo_diagnostico()
    except KeyboardInterrupt:
        warn("Cancelado.")
    input("\n  Presiona Enter para cerrar...")


if __name__ == "__main__":
    main()
