"""
mount-nas.py — Conecta y configura la NAS Ensamble
Corre SIN admin por defecto. Solo pide admin para [2] Configurar.
Windows + Mac.
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
# DETECCIÓN DE RED
# ─────────────────────────────────────────────

def detectar_red():
    """Devuelve 'lan' si el NAS local responde por ping, 'external' si no."""
    info("Detectando red...")
    ping = (f"ping -n 1 -w 1000 {NAS_LAN_IP}" if OS == "Windows"
            else f"ping -c 1 -W 1 {NAS_LAN_IP}")
    r = run(ping, check=False, capture=True)
    return "lan" if r.returncode == 0 else "external"

def _tailscale_activo():
    r = run("tailscale status", check=False, capture=True)
    return r.returncode == 0 and "100." in r.stdout


# ─────────────────────────────────────────────
# [1] CONECTAR NAS
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
        if confirm("¿Ir a [2] Configurar equipo para instalar Tailscale?"):
            _elevar_para_configurar()
        return

    ok("Tailscale activo.")

    if OS == "Windows":
        drive_exe = r"C:\Program Files\SynologyDrive\SynologyDrive.exe"
        if os.path.exists(drive_exe):
            ok("Synology Drive instalado.")
            info("Abriendo Synology Drive...")
            run(f'start "" "{drive_exe}"', check=False)
            info("Synology Drive maneja la sincronización con el NAS automáticamente.")
        else:
            warn("Synology Drive no está instalado.")
            if confirm("¿Ir a [2] Configurar equipo para instalarlo?"):
                _elevar_para_configurar()
    elif OS == "Darwin":
        drive_app = "/Applications/Synology Drive Client.app"
        if os.path.exists(drive_app):
            ok("Synology Drive instalado.")
            run("open '/Applications/Synology Drive Client.app'", check=False)
            info("Synology Drive maneja la sincronización con el NAS automáticamente.")
        else:
            warn("Synology Drive no está instalado.")
            if confirm("¿Ir a [2] Configurar equipo para instalarlo?"):
                _elevar_para_configurar()

def seccion_conectar():
    title("CONECTAR NAS")
    red = detectar_red()
    if red == "lan":
        _conectar_lan()
    else:
        _conectar_externo()


# ─────────────────────────────────────────────
# [2] CONFIGURAR EQUIPO (requiere admin)
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
        run("tailscale login", check=False)
    elif OS == "Darwin":
        dest = os.path.join(tmp, "tailscale.pkg")
        download(TAILSCALE_MAC_URL, dest)
        run(f"installer -pkg '{dest}' -target /")
        ok("Tailscale instalado.")
        run("/Applications/Tailscale.app/Contents/MacOS/Tailscale login", check=False)

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
    info(f"Configura el servidor en Synology Drive:")
    info(f"  Servidor: {NAS_EXTERNAL_URL}  |  Puerto: 5001 (HTTPS)")
    info(f"  Carpeta: {SHARE_ENSAMBLE} → modo On Demand Sync")

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
    ts_ok = run("tailscale version", check=False, capture=True).returncode == 0
    if ts_ok:
        ok("Tailscale ya está instalado.")
    elif confirm("¿Este equipo también se usa fuera de la oficina? → instalar Tailscale"):
        _instalar_tailscale()

    # Synology Drive — opcional
    drive_ok = (
        os.path.exists(r"C:\Program Files\SynologyDrive\SynologyDrive.exe") if OS == "Windows"
        else os.path.exists("/Applications/Synology Drive Client.app")
    )
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
    ts_ok = run("tailscale version", check=False, capture=True).returncode == 0
    if ts_ok:
        if _tailscale_activo():
            ok("Tailscale instalado y activo.")
        else:
            ok("Tailscale instalado pero sin sesión.")
            info("Iniciando sesión — se abrirá el browser.")
            info("Usa la cuenta Google de Ensamble que te asignaron.")
            run("tailscale login", check=False)
    else:
        _instalar_tailscale()

    # Synology Drive — siempre
    drive_ok = (
        os.path.exists(r"C:\Program Files\SynologyDrive\SynologyDrive.exe") if OS == "Windows"
        else os.path.exists("/Applications/Synology Drive Client.app")
    )
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
# MENÚ PRINCIPAL
# ─────────────────────────────────────────────

def main():
    # Modo configurar: re-lanzado con permisos de admin
    if "--configure" in sys.argv:
        seccion_configurar()
        input("\n  Presiona Enter para cerrar...")
        return

    so_label = "Windows" if OS == "Windows" else "macOS"

    while True:
        title(f"ENSAMBLE — NAS  ·  {so_label}")
        print("  [1] Conectar NAS")
        print("  [2] Configurar equipo  (solo 1 vez por PC — necesita admin)")
        print("  [0] Salir\n")

        opcion = ask("Selecciona una opción", ["1", "2", "0"])

        if opcion == "0":
            info("Hasta luego.")
            break

        try:
            if opcion == "1":
                seccion_conectar()
            elif opcion == "2":
                seccion_configurar()
        except KeyboardInterrupt:
            warn("Cancelado.")

        input("\n  Presiona Enter para volver al menú...")


if __name__ == "__main__":
    main()
