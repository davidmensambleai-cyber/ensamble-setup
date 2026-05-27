"""
Ensamble Setup Tool
Herramienta de configuración centralizada para equipos de Ensamble.
Corre en Windows y Mac. Requiere permisos de administrador.

Uso:
  Windows (como Administrador): python ensamble_setup.py
  Mac (con sudo):               sudo python3 ensamble_setup.py
"""

import os
import sys
import platform
import subprocess
import ctypes
import urllib.request
import tempfile

# ─────────────────────────────────────────────
# CONSTANTES
# ─────────────────────────────────────────────

OS = platform.system()  # "Windows" | "Darwin"

if OS == "Windows":
    try:
        ctypes.windll.kernel32.SetConsoleCP(65001)
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

NAS_LAN_IP       = "192.168.2.7"
NAS_HOST_ALIAS   = "nas"
NAS_EXTERNAL_URL = "nas.ensambleai.com"

SHARE_ENSAMBLE  = "Ensamble"
SHARE_ARCHIVO   = "ARCHIVO ENSAMBLE"

DRIVE_ENSAMBLE  = "Z:"
DRIVE_ARCHIVO   = "Y:"

TAILSCALE_WIN_URL = "https://pkgs.tailscale.com/stable/tailscale-setup-latest.exe"
TAILSCALE_MAC_URL = "https://pkgs.tailscale.com/stable/Tailscale-latest.pkg"
SYNODRIVE_WIN_URL = "https://global.download.synology.com/download/Tools/SynologyDriveClient/3.5.1-16120/Windows/x64/Synology%20Drive%20Client-3.5.1-16120.exe"
SYNODRIVE_MAC_URL = "https://global.download.synology.com/download/Tools/SynologyDriveClient/3.5.1-16120/Mac/Synology%20Drive%20Client-3.5.1-16120.dmg"

NAMING_GUIDE = {
    "win-dsk": "PC escritorio Windows  → ENS-win-dsk-01, ENS-win-dsk-02 ...",
    "win-lap": "Laptop Windows         → ENS-win-lap-01, ENS-win-lap-02 ...",
    "mac-dsk": "iMac / Mac mini        → ENS-mac-dsk-01, ENS-mac-dsk-02 ...",
    "mac-lap": "MacBook                → ENS-mac-lap-01, ENS-mac-lap-02 ...",
}


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
    """Input con validación opcional de opciones."""
    while True:
        val = input(f"\n  → {prompt}: ").strip()
        if not options or val in options:
            return val
        warn(f"Opción inválida. Válidas: {', '.join(options)}")

def confirm(prompt):
    return ask(f"{prompt} [s/n]", ["s", "n"]) == "s"

def run(cmd, check=True, capture=False):
    """Ejecuta un comando de shell."""
    kwargs = {"shell": True, "check": check}
    if capture:
        kwargs["capture_output"] = True
        kwargs["text"] = True
    return subprocess.run(cmd, **kwargs)

def is_admin():
    if OS == "Windows":
        try:
            return ctypes.windll.shell32.IsUserAnAdmin()
        except Exception:
            return False
    else:
        return os.geteuid() == 0

def require_admin():
    if not is_admin():
        err("Este script requiere permisos de administrador.")
        if OS == "Windows":
            info("Cierra y vuelve a abrir como Administrador (clic derecho → Ejecutar como administrador).")
        else:
            info("Ejecuta con: sudo python3 ensamble_setup.py")
        sys.exit(1)

def current_user():
    return os.environ.get("USERNAME") or os.environ.get("USER") or ""

def is_admin_user():
    """Detecta si el usuario actual es admin (nombre = nombre del PC) o Asociado."""
    user = current_user().lower()
    if user == "asociado":
        return False
    # En Windows el nombre del PC viene de COMPUTERNAME
    pc = os.environ.get("COMPUTERNAME", "").lower()
    return user == pc or user.startswith("ens-")

def download(url, dest_path):
    """Descarga un archivo con barra de progreso simple."""
    info(f"Descargando {os.path.basename(dest_path)} ...")
    urllib.request.urlretrieve(url, dest_path)
    ok(f"Descargado: {dest_path}")


# ─────────────────────────────────────────────
# SECCIÓN: NOMBRE DEL EQUIPO
# ─────────────────────────────────────────────

def seccion_nombre_pc():
    title("CONFIGURAR NOMBRE DEL EQUIPO")

    info("Convención de nombres Ensamble:")
    for key, desc in NAMING_GUIDE.items():
        info(f"  {desc}")

    if OS == "Windows":
        actual = run("hostname", capture=True).stdout.strip()
    else:
        actual = run("hostname", capture=True).stdout.strip()

    info(f"\n  Nombre actual: {actual}")

    if not confirm("¿Quieres cambiar el nombre?"):
        return

    nuevo = ask("Ingresa el nuevo nombre (ej: ENS-win-dsk-01)").upper()
    if not nuevo.startswith("ENS-"):
        if not confirm(f"El nombre '{nuevo}' no sigue la convención ENS-OS-tipo-NN. ¿Continuar igual?"):
            return

    if OS == "Windows":
        run(f'powershell -Command "Rename-Computer -NewName \'{nuevo}\' -Force"')
        ok(f"Nombre cambiado a {nuevo}. Reinicia el equipo para aplicar.")
    else:
        run(f"scutil --set ComputerName '{nuevo}'")
        run(f"scutil --set HostName '{nuevo}'")
        run(f"scutil --set LocalHostName '{nuevo}'")
        ok(f"Nombre cambiado a {nuevo}.")


# ─────────────────────────────────────────────
# SECCIÓN: NAS INTERNO (red local)
# ─────────────────────────────────────────────

def _hosts_tiene_nas():
    hosts = r"C:\Windows\System32\drivers\etc\hosts" if OS == "Windows" else "/etc/hosts"
    try:
        with open(hosts, "r") as f:
            return NAS_HOST_ALIAS in f.read()
    except Exception:
        return False

def _agregar_hosts():
    hosts = r"C:\Windows\System32\drivers\etc\hosts" if OS == "Windows" else "/etc/hosts"
    entrada = f"\n{NAS_LAN_IP}    {NAS_HOST_ALIAS}    # NAS Ensamble\n"
    try:
        with open(hosts, "a") as f:
            f.write(entrada)
        ok(f"Alias '{NAS_HOST_ALIAS}' → {NAS_LAN_IP} agregado al archivo hosts.")
    except PermissionError:
        err("Sin permisos para modificar el archivo hosts.")

def _montar_unidad_win(letra, share, usuario, password):
    unc = f"\\\\{NAS_LAN_IP}\\{share}"
    # Eliminar mapeo previo si existe
    run(f'net use {letra} /delete /y', check=False)
    cmd = f'net use {letra} "{unc}" /persistent:yes'
    if usuario:
        cmd += f' /user:"{usuario}" "{password}"'
    result = run(cmd, check=False)
    if result.returncode == 0:
        ok(f"{letra} → {unc}")
    else:
        err(f"No se pudo montar {letra}. Verifica que estés en la red local y que las credenciales sean correctas.")

def _montar_smb_mac(share, punto_montaje, usuario, password):
    os.makedirs(punto_montaje, exist_ok=True)
    unc = f"smb://{usuario}:{password}@{NAS_LAN_IP}/{share}"
    result = run(f"mount_smbfs '{unc}' '{punto_montaje}'", check=False)
    if result.returncode == 0:
        ok(f"Montado en {punto_montaje}")
    else:
        err(f"No se pudo montar {share}. Verifica credenciales y conexión de red.")

def seccion_nas_interno():
    title("NAS INTERNO — RED LOCAL")

    admin = is_admin_user()
    rol = "admin" if admin else "Asociado"
    info(f"Usuario detectado: {current_user()} → perfil {rol}")

    if not _hosts_tiene_nas():
        if confirm(f"¿Agregar alias '{NAS_HOST_ALIAS}' en hosts para no tener que escribir la IP?"):
            _agregar_hosts()
    else:
        ok(f"Alias '{NAS_HOST_ALIAS}' ya existe en hosts.")

    # Verificar conectividad
    info(f"Verificando conexión al NAS ({NAS_LAN_IP}) ...")
    r = run(f"ping -n 1 -w 1000 {NAS_LAN_IP}" if OS == "Windows" else f"ping -c 1 -W 1 {NAS_LAN_IP}", check=False)
    if r.returncode != 0:
        err("No hay conexión al NAS. ¿Estás en la red de la oficina?")
        return
    ok("NAS accesible.")

    # Credenciales NAS
    info("\n  Ingresa tus credenciales del NAS:")
    usuario_nas = ask("Usuario NAS")
    import getpass
    password_nas = getpass.getpass("  → Contraseña NAS: ")

    if OS == "Windows":
        _montar_unidad_win(DRIVE_ENSAMBLE, SHARE_ENSAMBLE, usuario_nas, password_nas)
        if admin:
            _montar_unidad_win(DRIVE_ARCHIVO, SHARE_ARCHIVO, usuario_nas, password_nas)
            info(f"Perfil admin: se montaron {DRIVE_ENSAMBLE} y {DRIVE_ARCHIVO}.")
        else:
            info(f"Perfil Asociado: se montó {DRIVE_ENSAMBLE}.")

    elif OS == "Darwin":
        _montar_smb_mac(SHARE_ENSAMBLE, f"/Volumes/{SHARE_ENSAMBLE}", usuario_nas, password_nas)
        if admin:
            _montar_smb_mac(SHARE_ARCHIVO, f"/Volumes/ARCHIVO_ENSAMBLE", usuario_nas, password_nas)
        info("Nota: en Mac los montajes no son persistentes. Para montaje automático al login, usa 'Finder → Conectar al servidor' y guarda en Login Items.")


# ─────────────────────────────────────────────
# SECCIÓN: NAS EXTERNO (Tailscale + Synology Drive)
# ─────────────────────────────────────────────

def _instalar_tailscale():
    info("Instalando Tailscale ...")
    tmp = tempfile.gettempdir()
    if OS == "Windows":
        dest = os.path.join(tmp, "tailscale_setup.exe")
        download(TAILSCALE_WIN_URL, dest)
        run(f'"{dest}" /quiet /norestart')
        ok("Tailscale instalado.")
        info("Iniciando sesión en Tailscale — se abrirá el browser.")
        info("Usa la cuenta Google de Ensamble que te asignaron.")
        run("tailscale login", check=False)
    elif OS == "Darwin":
        dest = os.path.join(tmp, "tailscale.pkg")
        download(TAILSCALE_MAC_URL, dest)
        run(f"installer -pkg '{dest}' -target /")
        ok("Tailscale instalado.")
        info("Iniciando sesión en Tailscale — se abrirá el browser.")
        info("Usa la cuenta Google de Ensamble que te asignaron.")
        run("/Applications/Tailscale.app/Contents/MacOS/Tailscale login", check=False)

def _tailscale_activo():
    r = run("tailscale status", check=False, capture=True)
    return r.returncode == 0 and "100." in r.stdout

def _instalar_synology_drive():
    info("Instalando Synology Drive Client ...")
    tmp = tempfile.gettempdir()
    if OS == "Windows":
        dest = os.path.join(tmp, "synodrive_setup.exe")
        download(SYNODRIVE_WIN_URL, dest)
        run(f'"{dest}" /S')
        ok("Synology Drive Client instalado.")
    elif OS == "Darwin":
        dest = os.path.join(tmp, "synodrive.dmg")
        download(SYNODRIVE_MAC_URL, dest)
        run(f"hdiutil attach '{dest}' -quiet")
        run("installer -pkg '/Volumes/Synology Drive Client/Synology Drive Client.pkg' -target /")
        run(f"hdiutil detach '/Volumes/Synology Drive Client' -quiet", check=False)
        ok("Synology Drive Client instalado.")

def seccion_nas_externo():
    title("NAS EXTERNO — ACCESO REMOTO")

    info("Este proceso instala Tailscale y Synology Drive Client.")
    info("Necesitarás la cuenta Google que te asignó Ensamble.")

    # ── Tailscale ──
    title("Paso 1 de 2 — Tailscale (VPN)")
    if _tailscale_activo():
        ok("Tailscale ya está instalado y conectado.")
    else:
        r = run("tailscale version", check=False, capture=True)
        if r.returncode == 0:
            info("Tailscale está instalado pero no conectado.")
            if confirm("¿Iniciar sesión ahora?"):
                info("Se abrirá el browser. Usa tu cuenta Google de Ensamble.")
                run("tailscale login", check=False)
        else:
            if confirm("¿Instalar Tailscale ahora?"):
                _instalar_tailscale()

    if not _tailscale_activo():
        warn("Tailscale no está activo. Completa el login antes de continuar con Synology Drive.")
        if not confirm("¿Continuar de todas formas?"):
            return

    # ── Synology Drive ──
    title("Paso 2 de 2 — Synology Drive Client")
    info("Synology Drive sincroniza las carpetas del NAS a este equipo.")
    info(f"Servidor a configurar: {NAS_EXTERNAL_URL}")
    info("Carpeta a sincronizar: Ensamble (modo On Demand)")

    if confirm("¿Instalar Synology Drive Client ahora?"):
        _instalar_synology_drive()

    info("\n  Una vez que abra Synology Drive Client:")
    info(f"  1. Servidor: {NAS_EXTERNAL_URL}")
    info(f"  2. Puerto:   5001  (HTTPS)")
    info(f"  3. Usuario y contraseña: los que te dio Ensamble")
    info(f"  4. Carpeta:  Ensamble → On Demand Sync")

    if OS == "Windows":
        run('start "" "C:\\Program Files\\SynologyDrive\\SynologyDrive.exe"', check=False)
    elif OS == "Darwin":
        run("open '/Applications/Synology Drive Client.app'", check=False)

    ok("Configuración externa completada.")


# ─────────────────────────────────────────────
# MENÚ PRINCIPAL
# ─────────────────────────────────────────────

MENU = {
    "1": ("Nombre del equipo",              seccion_nombre_pc),
    "2": ("NAS interno  (red local)",       seccion_nas_interno),
    "3": ("NAS externo  (Tailscale + Drive)",seccion_nas_externo),
    # Próximas secciones:
    # "4": ("Desinstalar aplicaciones",     seccion_desinstalar),
    # "5": ("Instalar programas Ensamble",  seccion_instalar),
    # "6": ("Perfil de energía",            seccion_energia),
    # "7": ("Crear usuario Asociado",       seccion_crear_asociado),
}

def main():
    require_admin()

    so_label = "Windows" if OS == "Windows" else "macOS"
    user = current_user()

    while True:
        title(f"ENSAMBLE SETUP TOOL  ·  {so_label}  ·  {user}")
        for key, (label, _) in MENU.items():
            print(f"  [{key}] {label}")
        print("  [0] Salir\n")

        opcion = ask("Selecciona una opción", list(MENU.keys()) + ["0"])

        if opcion == "0":
            info("Hasta luego.")
            break

        _, fn = MENU[opcion]
        try:
            fn()
        except KeyboardInterrupt:
            warn("Sección cancelada.")
        except Exception as e:
            err(f"Error inesperado: {e}")

        input("\n  Presiona Enter para volver al menú...")


if __name__ == "__main__":
    main()
