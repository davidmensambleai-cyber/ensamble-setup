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
import glob
import shutil
import time
import tempfile
from pathlib import Path

# ─────────────────────────────────────────────
# CONSTANTES
# ─────────────────────────────────────────────

OS = platform.system()  # "Windows" | "Darwin"
IS_WIN = OS == "Windows"
IS_MAC = OS == "Darwin"

if IS_WIN:
    try:
        import winreg
    except ImportError:
        winreg = None
else:
    winreg = None

if OS == "Windows":
    try:
        ctypes.windll.kernel32.SetConsoleCP(65001)
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

HOME = Path.home()

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

def masked_input(prompt: str) -> str:
    """Input de contraseña que muestra * por carácter. Nunca usar getpass() — no da feedback visual.
    Implementación canónica: 04_Infraestructura/NAS/contenedores/ansible/add_pc.py"""
    print(prompt, end='', flush=True)
    chars = []
    if IS_WIN:
        import msvcrt
        while True:
            ch = msvcrt.getwch()
            if ch in ('\r', '\n'):
                print(); break
            if ch == '\x08' and chars:
                chars.pop(); print('\b \b', end='', flush=True)
            elif ch == '\x03':
                raise KeyboardInterrupt
            elif ch not in ('\x00', '\xe0'):
                chars.append(ch); print('*', end='', flush=True)
    else:
        import termios, tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while True:
                ch = sys.stdin.read(1)
                if ch in ('\r', '\n'):
                    print(); break
                if ch == '\x7f' and chars:
                    chars.pop(); print('\b \b', end='', flush=True)
                elif ch == '\x03':
                    raise KeyboardInterrupt
                else:
                    chars.append(ch); print('*', end='', flush=True)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return ''.join(chars)


def _run_ps_script(script_content: str):
    """Escribe un .ps1 temporal y lo ejecuta — evita -Command gigantes y reduce
    (no elimina) la exposición de secretos frente a pasar todo inline en el cmdline."""
    tmp_path = Path(tempfile.gettempdir()) / f"_ensamble_setup_{int(time.time())}.ps1"
    tmp_path.write_text(script_content, encoding="utf-8")
    try:
        return run(f'powershell -ExecutionPolicy Bypass -File "{tmp_path}"', check=False, capture=True)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


# ─────────────────────────────────────────────
# SECCIÓN: NOMBRE DEL EQUIPO
# Submenú: cambiar nombre (+crear cuenta admin alineada) / eliminar cuenta en desuso
# ─────────────────────────────────────────────

# Administrator/Guest se TRADUCEN por idioma de Windows (en español: Administrador/
# Invitado — confirmado real en pc_13); DefaultAccount/WDAGUtilityAccount no se traducen.
# Los RIDs (últimos dígitos del SID) son fijos e independientes del idioma en toda
# instalación de Windows — se usan para excluir estas cuentas en vez del nombre.
BUILTIN_RIDS = {"500", "501", "503", "504"}  # Administrator, Guest, DefaultAccount, WDAGUtilityAccount


def _validar_nombre_cuenta(nombre: str) -> bool:
    if len(nombre) > 20:
        err(f"'{nombre}' supera los 20 caracteres — límite de nombre de cuenta en Windows.")
        return False
    invalidos = set('"/\\[]:|<>+=;,?*@')
    if any(c in invalidos for c in nombre):
        err(f"'{nombre}' contiene caracteres no permitidos en un nombre de cuenta Windows.")
        return False
    return True


def crear_cuenta_admin_alineada(nombre: str):
    title(f"CREAR CUENTA ADMIN · {nombre}")

    if not IS_WIN:
        warn("Creación de cuenta admin alineada es solo Windows.")
        return

    if not _validar_nombre_cuenta(nombre):
        return

    info("Se creará una cuenta de administrador local con este nombre. La carpeta")
    info(f"de perfil (C:\\Users\\{nombre}) se genera sola en el primer uso — este")
    info("script la fuerza sin necesitar un login manual.")

    dry_run = not confirm("\n¿Ejecutar en modo real? ('n' corre en modo dry-run / solo simulación, sin pedir contraseña)")

    check_existente = run(
        f'powershell -Command "(Get-LocalUser -Name \'{nombre}\' -ErrorAction SilentlyContinue).Name"',
        check=False, capture=True,
    )
    ya_existe = bool(check_existente.stdout and check_existente.stdout.strip())
    if ya_existe:
        warn(f"Ya existe una cuenta local llamada '{nombre}' (probablemente de un intento anterior).")
        info("Se completarán solo los pasos que falten: contraseña, grupo de administradores y perfil.")
        if not confirm("¿Continuar?"):
            return

    if dry_run:
        info("\n[DRY-RUN] No se pide contraseña ni se ejecuta nada — solo se describe el plan:")
        if ya_existe:
            info(f"  1. (Se omite — '{nombre}' ya existe) Se usaría Set-LocalUser en vez de New-LocalUser")
        else:
            info(f"  1. New-LocalUser -Name '{nombre}' (con la contraseña que se pediría en modo real)")
        info(f"  2. Agregar '{nombre}' al grupo de administradores si no está ya (por SID, no por nombre — ver nota abajo)")
        info(f"  3. Forzar creación de C:\\Users\\{nombre} vía tarea programada temporal (schtasks)")
        ok("Simulación completa. Sin cambios realizados.")
        return

    password = masked_input(f"\n  Contraseña para la cuenta '{nombre}': ")
    password_confirm = masked_input("  Confirma la contraseña: ")
    if password != password_confirm:
        err("Las contraseñas no coinciden. Cancelado.")
        del password, password_confirm
        return
    del password_confirm

    if '"' in password or '%' in password:
        err('La contraseña no puede contener comillas dobles (") ni el signo (%) — rompe la sintaxis del comando usado para crear la cuenta. Elige otra.')
        del password
        return

    password_escaped = password.replace("'", "''")
    # El grupo local "Administrators" está TRADUCIDO por idioma de Windows (en español
    # es "Administradores") — Add-LocalGroupMember -Group 'Administrators' falla en
    # cualquier Windows en español con GroupNotFoundException. El SID del grupo
    # integrado de administradores (S-1-5-32-544) es universal, independiente del
    # idioma — confirmado real en pc_13 (Get-LocalGroup -SID 'S-1-5-32-544' → "Administradores").
    # Get-LocalGroupMember devuelve el nombre como "<equipo>\<usuario>" — se compara por
    # sufijo, no por igualdad directa (confirmado real en pc_13).
    if ya_existe:
        # Idempotente: cubre el caso de una corrida anterior que falló a medias (ej. la
        # cuenta se creó pero no se pudo agregar al grupo por el bug de localización).
        # Set-LocalUser -PasswordNeverExpires necesita $true/$false explícito (a
        # diferencia de New-LocalUser, donde es un switch) — confirmado real en pc_13.
        ps_cuenta = (
            f"$sec = ConvertTo-SecureString '{password_escaped}' -AsPlainText -Force\n"
            f"Set-LocalUser -Name '{nombre}' -Password $sec -PasswordNeverExpires $true "
            f"-AccountNeverExpires -ErrorAction Stop\n"
            f"$yaAdmin = Get-LocalGroupMember -SID 'S-1-5-32-544' -ErrorAction SilentlyContinue | "
            f'Where-Object {{ $_.Name -like "*\\{nombre}" }}\n'
            f"if (-not $yaAdmin) {{ Add-LocalGroupMember -SID 'S-1-5-32-544' -Member '{nombre}' -ErrorAction Stop }}\n"
        )
    else:
        ps_cuenta = (
            f"$sec = ConvertTo-SecureString '{password_escaped}' -AsPlainText -Force\n"
            f"New-LocalUser -Name '{nombre}' -Password $sec -PasswordNeverExpires -AccountNeverExpires -ErrorAction Stop\n"
            f"Add-LocalGroupMember -SID 'S-1-5-32-544' -Member '{nombre}' -ErrorAction Stop\n"
        )
    result = _run_ps_script(ps_cuenta)
    if result.returncode != 0:
        verbo_error = "completar" if ya_existe else "crear"
        err(f"No se pudo {verbo_error} la cuenta o agregarla al grupo de administradores: {result.stderr.strip()}")
        del password, password_escaped
        return
    verbo_ok = "actualizada" if ya_existe else "creada"
    ok(f"Cuenta '{nombre}' {verbo_ok} y en el grupo de administradores.")
    del password_escaped

    info("Forzando creación del perfil de usuario (tarea programada temporal)...")
    task_name = "EnsambleSetupInitProfile"
    run(
        f'schtasks /create /tn "{task_name}" /tr "cmd.exe /c whoami" /sc once /st 23:59 '
        f'/ru "{nombre}" /rp "{password}" /f',
        check=False, capture=True,
    )
    del password
    run(f'schtasks /run /tn "{task_name}"', check=False, capture=True)
    time.sleep(5)
    run(f'schtasks /delete /tn "{task_name}" /f', check=False, capture=True)

    perfil = Path(f"C:/Users/{nombre}")
    if perfil.exists():
        ok(f"Perfil creado correctamente: {perfil}")
    else:
        warn(f"No se detectó {perfil} todavía. Puede tardar unos segundos más — verifica manualmente.")

    print(f"\n{LINE}")
    ok("Cuenta admin alineada creada.")
    info("Próximos pasos:")
    info(f"  1. Cierra sesión y entra con la cuenta '{nombre}'.")
    info("  2. Configura un PIN de inicio de sesión: Configuración → Cuentas →")
    info("     Opciones de inicio de sesión → PIN de Windows Hello.")
    info("     (No se puede hacer desde el script — Windows Hello requiere sesión")
    info("     interactiva de esa cuenta para crear el PIN.)")
    info("  3. Verifica que todo funcione (NAS, Drive, accesos).")
    info("  4. Vuelve a correr el script → Nombre del equipo → Eliminar cuenta en desuso,")
    info("     para borrar la cuenta anterior.")
    print(LINE)


def seccion_cambiar_nombre_y_cuenta():
    title("CAMBIAR NOMBRE DEL EQUIPO")

    info("Convención de nombres Ensamble:")
    for key, desc in NAMING_GUIDE.items():
        info(f"  {desc}")

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
        return

    if confirm("\n¿Crear cuenta admin alineada con el nuevo nombre?"):
        crear_cuenta_admin_alineada(nuevo)
    else:
        info("Puedes crear la cuenta admin alineada después, desde este mismo submenú.")


def seccion_eliminar_cuenta_desuso():
    title("ELIMINAR CUENTA EN DESUSO")

    if not IS_WIN:
        warn("Esta sección es solo para Windows.")
        return

    hostname = run("hostname", capture=True).stdout.strip()
    usuario_actual = current_user()

    info(f"Cuenta de nombre actual: {hostname}")
    info("Escaneando cuentas locales habilitadas...")

    ps_scan = (
        "Get-LocalUser | Where-Object { $_.Enabled -eq $true } | ForEach-Object {\n"
        '    "$($_.Name)|$($_.SID)"\n'
        "}\n"
    )
    result = _run_ps_script(ps_scan)
    cuentas = []
    for linea in result.stdout.splitlines():
        linea = linea.strip()
        if not linea or '|' not in linea:
            continue
        nombre_cuenta, sid = linea.rsplit('|', 1)
        cuentas.append((nombre_cuenta.strip(), sid.strip().rsplit('-', 1)[-1]))

    excluidas_nombre = {hostname.lower(), "asociado"}
    candidatas = [
        nombre for nombre, rid in cuentas
        if nombre.lower() not in excluidas_nombre and rid not in BUILTIN_RIDS
    ]

    if not candidatas:
        ok("No se detectaron cuentas en desuso.")
        return

    print("\n  Cuentas detectadas fuera de la convención (ni cuenta de nombre ni Asociado):")
    for i, c in enumerate(candidatas, 1):
        marca = "  ← sesión activa, no se puede eliminar" if c.lower() == usuario_actual.lower() else ""
        print(f"    [{i}] {c}{marca}")

    seleccion = ask("¿Cuál eliminar? (número, o 'cancelar')")
    if seleccion.lower() == "cancelar":
        info("Cancelado.")
        return
    try:
        idx = int(seleccion)
        objetivo = candidatas[idx - 1]
    except (ValueError, IndexError):
        err("Selección inválida.")
        return

    if objetivo.lower() == usuario_actual.lower():
        err(f"No puedes eliminar la cuenta con la que estás conectado ahora mismo ('{objetivo}').")
        info("Cierra sesión y entra con la cuenta de nombre actual antes de eliminarla.")
        return

    warn(f"\nEsto eliminará la cuenta '{objetivo}' Y su carpeta C:\\Users\\{objetivo} — es IRREVERSIBLE.")
    confirmacion = input('  Escribí "si" para continuar: ').strip().lower()
    if confirmacion != "si":
        info("Cancelado.")
        return

    run(f'powershell -Command "Remove-LocalUser -Name \'{objetivo}\'"', check=False)
    carpeta = Path(f"C:/Users/{objetivo}")
    if carpeta.exists():
        try:
            shutil.rmtree(carpeta, ignore_errors=True)
        except Exception as e:
            warn(f"No se pudo eliminar la carpeta: {e}")
    ok(f"Cuenta '{objetivo}' y su carpeta eliminadas.")


def seccion_nombre_equipo():
    _submenu("NOMBRE DEL EQUIPO", {
        "1": ("Cambiar nombre del equipo + crear cuenta admin alineada", seccion_cambiar_nombre_y_cuenta),
        "2": ("Eliminar cuenta en desuso", seccion_eliminar_cuenta_desuso),
    })


# ─────────────────────────────────────────────
# SECCIÓN: DESINSTALACIÓN → BLOATWARE Y SERVICIOS
# Fuente de verdad: 03_Agents/Data base/capa-c/.../DTI_Tecnología y configuración de
# equipos/config_parque_tecnologico.json → bloque "bloatware". Solo Windows.
# ─────────────────────────────────────────────

APPX_BLOATWARE = [
    "Microsoft.XboxApp",
    "Microsoft.XboxGameOverlay",
    "Microsoft.XboxGamingOverlay",
    "Microsoft.XboxIdentityProvider",
    "Microsoft.GamingApp",
    "Microsoft.MicrosoftSolitaireCollection",
    "Microsoft.BingNews",
    "Microsoft.BingWeather",
    "Microsoft.GetHelp",
    "Microsoft.Getstarted",
    "Microsoft.MixedReality.Portal",
    "Microsoft.People",
    "Microsoft.SkypeApp",
    "Microsoft.ZuneMusic",
    "Microsoft.ZuneVideo",
    "Microsoft.549981C3F5F10",
    "Microsoft.WindowsFeedbackHub",
    "Microsoft.OneDriveSync",
    "MicrosoftTeams",
    "Microsoft.MicrosoftEdge.Stable",
    # Agregados 2026-07-29 — verificados en vivo contra pc_13 (Get-AppxPackage real)
    "Microsoft.OutlookForWindows",
    "MSTeams",
    "Microsoft.Copilot",
]


def seccion_bloatware_servicios():
    title("DESINSTALACIÓN · BLOATWARE Y SERVICIOS")

    if not IS_WIN:
        warn("Esta sección es solo para Windows. No aplica en Mac.")
        return

    info("Estrategia (config_parque_tecnologico.json → bloatware):")
    info("  1. Restore point")
    info("  2. Win11Debloat (script de terceros — github.com/Raphire/Win11Debloat)")
    info("  3. Remove-AppxPackage complementario")
    info("  4. Desinstalar OneDrive y Dropbox (no son Appx)")
    info("  5. Deshabilitar servicios SysMain y DiagTrack")

    dry_run = not confirm("\n¿Ejecutar en modo real? ('n' corre en modo dry-run / solo simulación)")
    if dry_run:
        info("[DRY-RUN] No se hará ningún cambio — solo se muestra qué se haría.")

    if confirm("\n¿Crear restore point antes de continuar?"):
        if dry_run:
            info("[DRY-RUN] Se crearía un restore point 'Antes Win11Debloat'.")
        else:
            run(
                'powershell -Command "Checkpoint-Computer -Description \'Antes Win11Debloat\' '
                '-RestorePointType MODIFY_SETTINGS"',
                check=False,
            )
            ok("Restore point solicitado (Windows limita la frecuencia — puede reusar uno reciente).")

    # Win11Debloat descarga su propio script desde internet EN ESTE EQUIPO cuando el
    # usuario confirma este paso — no es un acceso a internet del agente, es una acción
    # manual del técnico ejecutando la herramienta.
    if confirm("¿Ejecutar Win11Debloat? (descarga el script desde internet en ESTE equipo)"):
        if dry_run:
            info("[DRY-RUN] Se abriría Win11Debloat (omitido en simulación — su menú es interactivo y queda fuera de nuestro control).")
        else:
            info("Abriendo Win11Debloat...")
            # URL correcta confirmada 2026-07-29 contra github.com/Raphire/Win11Debloat
            # (el dominio "win11debloat.raphi.re" usado antes estaba mal — devolvía una
            # página HTML en vez del script, causando errores de parseo en PowerShell).
            result = run(
                'powershell -Command "Set-ExecutionPolicy Unrestricted -Scope Process; '
                "& ([scriptblock]::Create((irm 'https://debloat.raphi.re/')))\"",
                check=False,
            )
            if result.returncode == 0:
                ok("Win11Debloat ejecutado (revisa su propio menú interactivo).")
            else:
                err(f"Win11Debloat terminó con errores (código {result.returncode}). Revisa el mensaje de arriba.")

    if confirm("¿Remover apps residuales via Remove-AppxPackage?"):
        info(f"Procesando {len(APPX_BLOATWARE)} paquete(s)...")
        removidos, no_encontrados = 0, 0
        for pkg in APPX_BLOATWARE:
            check_result = run(
                f'powershell -Command "(Get-AppxPackage -Name {pkg}).Name"',
                check=False, capture=True,
            )
            instalado = bool(check_result.stdout and check_result.stdout.strip())
            if instalado:
                if dry_run:
                    info(f"  [DRY-RUN] Se removería: {pkg}")
                else:
                    run(f'powershell -Command "Get-AppxPackage {pkg} | Remove-AppxPackage"', check=False)
                    info(f"  Removido: {pkg}")
                removidos += 1
            else:
                no_encontrados += 1
        verbo = "se removerían" if dry_run else "removido(s)"
        ok(f"{removidos} paquete(s) {verbo}, {no_encontrados} no encontrado(s) o ya ausente(s).")

    if confirm("¿Desinstalar OneDrive y Dropbox? (no son paquetes Appx, se manejan aparte)"):
        # OneDrive no es Appx — se instala vía OneDriveSetup.exe. Probar primero la ruta
        # por-usuario (la más común), luego la ruta por-máquina como fallback.
        onedrive_candidatos = [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "OneDrive" / "OneDriveSetup.exe",
            Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / "SysWOW64" / "OneDriveSetup.exe",
        ]
        onedrive_exe = next((p for p in onedrive_candidatos if p.exists()), None)
        if onedrive_exe:
            if dry_run:
                info(f"  [DRY-RUN] Se ejecutaría: \"{onedrive_exe}\" /uninstall")
            else:
                run(f'"{onedrive_exe}" /uninstall', check=False)
                info("  OneDrive desinstalado.")
        else:
            info("  OneDrive no está instalado (no se encontró OneDriveSetup.exe).")

        # Dropbox tampoco es Appx ni encaja en VENDORS (no es Adobe/Autodesk/Graphisoft/
        # SketchUp) — se desinstala vía winget. ID confirmado en vivo: Dropbox.Dropbox.
        if dry_run:
            info("  [DRY-RUN] Se ejecutaría: winget uninstall --id Dropbox.Dropbox -e --silent")
        else:
            result = run("winget uninstall --id Dropbox.Dropbox -e --silent", check=False)
            if result.returncode == 0:
                info("  Dropbox desinstalado.")
            else:
                info("  Dropbox no estaba instalado (o winget no lo encontró).")

    if confirm("¿Deshabilitar servicios SysMain y DiagTrack?"):
        if dry_run:
            info("[DRY-RUN] Se deshabilitarían los servicios SysMain y DiagTrack.")
        else:
            run(
                'powershell -Command "Set-Service SysMain -StartupType Disabled; '
                'Stop-Service SysMain -ErrorAction SilentlyContinue"',
                check=False,
            )
            run(
                'powershell -Command "Set-Service DiagTrack -StartupType Disabled; '
                'Stop-Service DiagTrack -ErrorAction SilentlyContinue"',
                check=False,
            )
            ok("SysMain y DiagTrack deshabilitados.")

    if dry_run:
        ok("Sección bloatware y servicios: simulación completa. Sin cambios realizados.")
    else:
        ok("Sección bloatware y servicios completada.")


# ─────────────────────────────────────────────
# SECCIÓN: DESINSTALACIÓN → PROGRAMAS PROFESIONALES
# Portado de 02_Python/scripts/desinstalador_total/main.py (registrado, estado Activo).
# Duplicación intencional: este archivo debe quedar autocontenido — se distribuye como
# EnsambleSetup.exe descargado de GitHub en runtime, sin acceso garantizado a 02_Python/.
# El script standalone desinstalador_total se mantiene sin cambios para uso ad-hoc.
# ─────────────────────────────────────────────

VENDORS: dict = {
    'microsoft': {
        'label': 'Microsoft Office / 365',
        'mac': {
            'app_globs': [
                '/Applications/Microsoft Word.app',
                '/Applications/Microsoft Excel.app',
                '/Applications/Microsoft PowerPoint.app',
                '/Applications/Microsoft Outlook.app',
                '/Applications/Microsoft OneNote.app',
                '/Applications/Microsoft Teams.app',
                '/Applications/Microsoft Teams (work or school).app',
                '/Applications/Microsoft Remote Desktop.app',
                '/Applications/Microsoft To Do.app',
                '/Applications/Microsoft AutoUpdate.app',
                '/Applications/OneDrive.app',
            ],
            'dir_globs': [
                f'{HOME}/Library/Group Containers/UBF8T346G9.*',
                f'{HOME}/Library/Containers/com.microsoft.*',
                f'{HOME}/Library/Application Support/Microsoft',
                f'{HOME}/Library/Caches/com.microsoft.*',
                f'{HOME}/Library/Preferences/com.microsoft.*',
                f'{HOME}/Library/Saved Application State/com.microsoft.*',
                '/Library/Application Support/Microsoft',
                '/Library/Preferences/com.microsoft.*',
            ],
            'launch_agent_globs': [f'{HOME}/Library/LaunchAgents/com.microsoft.*'],
            'launch_daemon_globs': ['/Library/LaunchDaemons/com.microsoft.*'],
            'pkg_prefixes': ['com.microsoft.'],
            'processes': [
                'Microsoft Word', 'Microsoft Excel', 'Microsoft PowerPoint',
                'Microsoft Outlook', 'Microsoft OneNote', 'Microsoft Teams', 'OneDrive',
            ],
        },
        'win': {
            'keywords': ['Microsoft 365', 'Microsoft Office', 'Microsoft Teams', 'OneDrive'],
            'path_globs': [
                r'C:\Program Files\Microsoft Office',
                r'C:\Program Files (x86)\Microsoft Office',
                r'C:\Program Files\Microsoft OneDrive',
                r'C:\Program Files\Microsoft Teams',
                r'C:\Program Files\Common Files\Microsoft Shared',
            ],
            'processes': [
                'WINWORD.EXE', 'EXCEL.EXE', 'POWERPNT.EXE', 'OUTLOOK.EXE',
                'ONENOTE.EXE', 'Teams.exe', 'OneDrive.exe',
            ],
        },
    },

    'adobe': {
        'label': 'Adobe Creative Cloud',
        'mac': {
            'app_globs': [
                '/Applications/Adobe*',
                '/Applications/Utilities/Adobe*',
            ],
            'dir_globs': [
                f'{HOME}/Library/Application Support/Adobe',
                f'{HOME}/Library/Caches/Adobe',
                f'{HOME}/Library/Caches/com.adobe.*',
                f'{HOME}/Library/Preferences/com.adobe.*',
                f'{HOME}/Library/Logs/Adobe',
                f'{HOME}/Library/Containers/com.adobe.*',
                '/Library/Application Support/Adobe',
                '/Library/Logs/Adobe',
                '/Library/PrivilegedHelperTools/com.adobe.*',
            ],
            'launch_agent_globs': [
                f'{HOME}/Library/LaunchAgents/com.adobe.*',
                '/Library/LaunchAgents/com.adobe.*',
            ],
            'launch_daemon_globs': ['/Library/LaunchDaemons/com.adobe.*'],
            'pkg_prefixes': ['com.adobe.'],
            'processes': ['Creative Cloud', 'Adobe', 'AdobeIPCBroker', 'ACCFinderSync'],
        },
        'win': {
            'keywords': ['Adobe'],
            'path_globs': [
                r'C:\Program Files\Adobe',
                r'C:\Program Files (x86)\Adobe',
                r'C:\Program Files\Common Files\Adobe',
                r'C:\ProgramData\Adobe',
            ],
            'processes': ['Creative Cloud.exe', 'AdobeIPCBroker.exe', 'AdobeUpdateService.exe'],
        },
    },

    'autodesk': {
        'label': 'Autodesk',
        'mac': {
            'app_globs': [
                '/Applications/Autodesk',
                '/Applications/Autodesk*',
                '/Applications/AutoCAD*',
            ],
            'dir_globs': [
                f'{HOME}/Library/Application Support/Autodesk',
                f'{HOME}/Library/Caches/com.autodesk.*',
                f'{HOME}/Library/Preferences/com.autodesk.*',
                '/Library/Application Support/Autodesk',
            ],
            'launch_agent_globs': [f'{HOME}/Library/LaunchAgents/com.autodesk.*'],
            'launch_daemon_globs': ['/Library/LaunchDaemons/com.autodesk.*'],
            'pkg_prefixes': ['com.autodesk.'],
            'processes': ['Autodesk', 'AutoCAD', 'AdskLicensing'],
        },
        'win': {
            'keywords': ['Autodesk', 'AutoCAD', 'Revit', 'Maya', '3ds Max', 'Navisworks'],
            'path_globs': [
                r'C:\Program Files\Autodesk',
                r'C:\Program Files (x86)\Autodesk',
                r'C:\ProgramData\Autodesk',
                # Common Files\Autodesk Shared: donde vive AdskLicensingService.exe (el
                # chequeo de licencia) — faltaba en desinstalador_total/main.py original;
                # corregido aquí. No se propagó a ese script standalone (fuera de scope).
                r'C:\Program Files\Common Files\Autodesk Shared',
                r'C:\Program Files (x86)\Common Files\Autodesk Shared',
            ],
            'processes': ['acad.exe', 'AdskLicensingService.exe', 'AdAppMgrSvc.exe', 'revit.exe'],
        },
    },

    'graphisoft': {
        'label': 'Graphisoft (Archicad)',
        'mac': {
            'app_globs': [
                '/Applications/GRAPHISOFT',
                '/Applications/Archicad*',
                '/Applications/Graphisoft*',
            ],
            'dir_globs': [
                f'{HOME}/Library/Application Support/GRAPHISOFT',
                f'{HOME}/Library/Application Support/Graphisoft',
                f'{HOME}/Library/Preferences/com.graphisoft.*',
                f'{HOME}/Library/Caches/com.graphisoft.*',
                '/Library/Application Support/GRAPHISOFT',
            ],
            'launch_agent_globs': [f'{HOME}/Library/LaunchAgents/com.graphisoft.*'],
            'launch_daemon_globs': ['/Library/LaunchDaemons/com.graphisoft.*'],
            'pkg_prefixes': ['com.graphisoft.'],
            'processes': ['Archicad', 'GRAPHISOFT', 'ArchiCAD'],
        },
        'win': {
            'keywords': ['GRAPHISOFT', 'Archicad', 'ArchiCAD'],
            'path_globs': [
                r'C:\Program Files\GRAPHISOFT',
                r'C:\Program Files (x86)\GRAPHISOFT',
                r'C:\ProgramData\GRAPHISOFT',
            ],
            'processes': ['archicad.exe', 'ARCHICAD.exe', 'ArchiCAD.exe'],
        },
    },

    'sketchup': {
        'label': 'SketchUp',
        'mac': {
            'app_globs': [
                '/Applications/SketchUp*',
                '/Applications/Trimble SketchUp*',
            ],
            'dir_globs': [
                f'{HOME}/Library/Application Support/SketchUp*',
                f'{HOME}/Library/Application Support/Google SketchUp*',
                f'{HOME}/Library/Caches/com.sketchup.*',
                f'{HOME}/Library/Caches/com.trimble.*',
                f'{HOME}/Library/Preferences/com.sketchup.*',
                f'{HOME}/Library/Preferences/com.trimble.*',
            ],
            'launch_agent_globs': [
                f'{HOME}/Library/LaunchAgents/com.sketchup.*',
                f'{HOME}/Library/LaunchAgents/com.trimble.*',
            ],
            'launch_daemon_globs': [
                '/Library/LaunchDaemons/com.sketchup.*',
                '/Library/LaunchDaemons/com.trimble.*',
            ],
            'pkg_prefixes': ['com.sketchup.', 'com.trimble.sketchup.'],
            'processes': ['SketchUp'],
        },
        'win': {
            'keywords': ['SketchUp', 'Trimble SketchUp'],
            'path_globs': [
                r'C:\Program Files\SketchUp',
                r'C:\Program Files (x86)\SketchUp',
                r'C:\Program Files\Trimble\SketchUp',
                r'C:\ProgramData\SketchUp',
            ],
            'processes': ['SketchUp.exe'],
        },
    },
}


def scan_vendor_mac(vendor_id: str) -> dict:
    profile = VENDORS[vendor_id]['mac']
    found: dict = {'apps': [], 'dirs': [], 'launch_agents': [], 'launch_daemons': [], 'packages': []}

    for pattern in profile.get('app_globs', []):
        found['apps'].extend(Path(p) for p in glob.glob(pattern) if Path(p).exists())

    for pattern in profile.get('dir_globs', []):
        found['dirs'].extend(Path(p) for p in glob.glob(str(pattern)) if Path(p).exists())

    for pattern in profile.get('launch_agent_globs', []):
        found['launch_agents'].extend(Path(p) for p in glob.glob(str(pattern)) if Path(p).exists())

    for pattern in profile.get('launch_daemon_globs', []):
        found['launch_daemons'].extend(Path(p) for p in glob.glob(str(pattern)) if Path(p).exists())

    result = subprocess.run(['pkgutil', '--pkgs'], capture_output=True, text=True)
    all_pkgs = [p.strip() for p in result.stdout.strip().split('\n') if p.strip()]
    for pkg in all_pkgs:
        for prefix in profile.get('pkg_prefixes', []):
            if pkg.startswith(prefix) and pkg not in found['packages']:
                found['packages'].append(pkg)

    found['_total'] = sum(len(v) for k, v in found.items() if k != '_total')
    return found


def scan_vendor_win(vendor_id: str) -> dict:
    found: dict = {'registry': [], 'paths': []}
    profile = VENDORS[vendor_id]['win']

    if winreg:
        keywords = profile.get('keywords', [])
        hives = [
            (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'),
            (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'),
            (winreg.HKEY_CURRENT_USER, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'),
        ]
        for hive, key_path in hives:
            try:
                with winreg.OpenKey(hive, key_path) as key:
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            subkey_name = winreg.EnumKey(key, i)
                            with winreg.OpenKey(key, subkey_name) as subkey:
                                try:
                                    name = winreg.QueryValueEx(subkey, 'DisplayName')[0]
                                    if any(kw.lower() in name.lower() for kw in keywords):
                                        try:
                                            uninstall_str = winreg.QueryValueEx(subkey, 'UninstallString')[0]
                                        except OSError:
                                            uninstall_str = ''
                                        entry = {'name': name, 'uninstall': uninstall_str}
                                        if entry not in found['registry']:
                                            found['registry'].append(entry)
                                except OSError:
                                    pass
                        except OSError:
                            continue
            except OSError:
                continue

    for path_str in profile.get('path_globs', []):
        p = Path(path_str)
        if p.exists():
            found['paths'].append(p)

    found['_total'] = len(found['registry']) + len(found['paths'])
    return found


def kill_processes_mac(processes: list):
    for proc in processes:
        subprocess.run(['pkill', '-f', proc], capture_output=True)
    time.sleep(1)


def kill_processes_win(processes: list):
    for proc in processes:
        subprocess.run(['taskkill', '/F', '/IM', proc], capture_output=True)
    time.sleep(1)


def uninstall_vendor_mac(vendor_id: str, found: dict, dry_run: bool):
    profile = VENDORS[vendor_id]['mac']
    label = VENDORS[vendor_id]['label']

    info(f'Deteniendo procesos de {label}...')
    if not dry_run:
        kill_processes_mac(profile.get('processes', []))

    for path in found.get('launch_daemons', []):
        info(f'  Descargando daemon: {path.name}')
        if not dry_run:
            subprocess.run(['launchctl', 'bootout', 'system', str(path)], capture_output=True)
            subprocess.run(['launchctl', 'unload', str(path)], capture_output=True)

    for path in found.get('launch_agents', []):
        info(f'  Descargando agente: {path.name}')
        if not dry_run:
            subprocess.run(['launchctl', 'unload', str(path)], capture_output=True)

    all_paths = (
        found.get('apps', []) + found.get('dirs', [])
        + found.get('launch_agents', []) + found.get('launch_daemons', [])
    )
    for path in all_paths:
        info(f'  Eliminando: {path}')
        if not dry_run:
            try:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)
            except Exception as e:
                warn(f'    No se pudo eliminar {path}: {e}')

    for pkg in found.get('packages', []):
        info(f'  Olvidando paquete: {pkg}')
        if not dry_run:
            subprocess.run(['pkgutil', '--forget', pkg], capture_output=True)


def uninstall_vendor_win(vendor_id: str, found: dict, dry_run: bool):
    profile = VENDORS[vendor_id]['win']
    label = VENDORS[vendor_id]['label']

    info(f'Deteniendo procesos de {label}...')
    if not dry_run:
        kill_processes_win(profile.get('processes', []))

    for entry in found.get('registry', []):
        info(f'  Desinstalando: {entry["name"]}')
        if not dry_run and entry['uninstall']:
            cmd = entry['uninstall']
            if 'msiexec' in cmd.lower():
                if '/quiet' not in cmd.lower():
                    cmd += ' /quiet /norestart'
            elif '/S' not in cmd and '/silent' not in cmd.lower() and '/quiet' not in cmd.lower():
                cmd += ' /S'
            try:
                subprocess.run(cmd, shell=True, timeout=180, capture_output=True)
            except subprocess.TimeoutExpired:
                warn(f'    Timeout desinstalando: {entry["name"]}')
            except Exception as e:
                warn(f'    Error: {e}')

    for path in found.get('paths', []):
        info(f'  Eliminando carpeta: {path}')
        if not dry_run:
            try:
                shutil.rmtree(path, ignore_errors=True)
            except Exception as e:
                warn(f'    No se pudo eliminar {path}: {e}')


LINE = '─' * 56


def show_results_profesionales(scan_results: dict):
    print(f'\n{LINE}')
    for i, (vid, result) in enumerate(scan_results.items(), 1):
        label = VENDORS[vid]['label']
        total = result['_total']
        icon = '✓' if total > 0 else '○'
        print(f'  [{i}] {icon}  {label:<34} {total} elemento(s)')
    print(LINE)


def select_vendors(scan_results: dict) -> list:
    all_ids = list(scan_results.keys())
    with_items = [vid for vid in all_ids if scan_results[vid]['_total'] > 0]

    if not with_items:
        print('\n  No se encontraron programas de ningún proveedor.')
        return []

    print('\n  ¿Qué desinstalar?')
    print('  Números separados por coma (ej: 1,3) o "todos":')

    while True:
        choice = input('\n  Tu selección: ').strip().lower()
        if choice == 'todos':
            return with_items
        try:
            indices = [int(x.strip()) for x in choice.split(',') if x.strip()]
            selected, errors = [], False
            for idx in indices:
                if 1 <= idx <= len(all_ids):
                    vid = all_ids[idx - 1]
                    if scan_results[vid]['_total'] == 0:
                        print(f'  ⚠  [{idx}] No hay nada instalado de ese proveedor.')
                        errors = True
                    elif vid not in selected:
                        selected.append(vid)
                else:
                    print(f'  ⚠  [{idx}] Número fuera de rango.')
                    errors = True
            if selected and not errors:
                return selected
            if selected and errors:
                cont = input('  ¿Continuar con los válidos? (s/n): ').strip().lower()
                if cont == 's':
                    return selected
        except ValueError:
            pass
        print('  Entrada inválida. Intentá de nuevo.')


def confirm_uninstall_profesionales(selected: list, scan_results: dict) -> bool:
    print(f'\n{LINE}')
    print('  ESTO SE ELIMINARÁ:')
    print(LINE)
    for vid in selected:
        r = scan_results[vid]
        print(f'\n  {VENDORS[vid]["label"]}:')
        for cat, items in r.items():
            if cat == '_total' or not items:
                continue
            print(f'    {cat}: {len(items)} elemento(s)')
            for item in list(items)[:3]:
                print(f'      • {item}')
            if len(items) > 3:
                print(f'      ... y {len(items) - 3} más')
    print(f'\n{LINE}')
    print('  ⚠  ESTA ACCIÓN ES IRREVERSIBLE')
    confirmacion = input('\n  Escribí "si" para continuar: ').strip().lower()
    return confirmacion == 'si'


def seccion_programas_profesionales():
    title("DESINSTALACIÓN · PROGRAMAS PROFESIONALES")
    warn("Elimina apps, configuración, licencias locales y rastros del sistema. Es irreversible.")

    dry_run = not confirm("¿Ejecutar en modo real? ('n' corre en modo dry-run / solo simulación)")

    print(f'\n  Escaneando{" (dry-run)" if dry_run else ""}...\n')
    scan_results = {}
    for vid in VENDORS:
        print(f'    {VENDORS[vid]["label"]}...', end='  ', flush=True)
        scan_results[vid] = scan_vendor_mac(vid) if IS_MAC else scan_vendor_win(vid)
        print(f'{scan_results[vid]["_total"]} elemento(s)')

    show_results_profesionales(scan_results)

    selected = select_vendors(scan_results)
    if not selected:
        info('Nada seleccionado.')
        return

    if not dry_run and not confirm_uninstall_profesionales(selected, scan_results):
        warn('Cancelado.')
        return

    print(f'\n{LINE}')
    print(f'  {"[DRY-RUN] Simulando..." if dry_run else "Desinstalando..."}')
    print(LINE)

    for vid in selected:
        info(f'\n─ {VENDORS[vid]["label"]} ─')
        if IS_MAC:
            uninstall_vendor_mac(vid, scan_results[vid], dry_run)
        else:
            uninstall_vendor_win(vid, scan_results[vid], dry_run)
        ok(f'{VENDORS[vid]["label"]} {"(simulado)" if dry_run else "eliminado"}')

    print(f'\n{LINE}')
    if dry_run:
        ok('Simulación completa. Sin cambios realizados.')
    else:
        ok('Desinstalación completa.')
        info('Reinicia el equipo para limpiar lo que queda en memoria.')


# ─────────────────────────────────────────────
# SECCIÓN: INSTALACIÓN → SOFTWARE BÁSICO
# Fuente de verdad: config_parque_tecnologico.json → software.todos_los_equipos_ensamble
# Solo Windows (winget). Todos los IDs verificados 2026-07-29 con `winget search` real
# en pc_13 — incluye la corrección de Python.Python.3 (deprecado, ya no existe en el
# catálogo) → Python.Python.3.13, y Synology/Claude (sí tienen ID, no había que dejarlos
# manuales como antes).
# ─────────────────────────────────────────────

SOFTWARE_BASICO_WINGET = [
    ("Google Chrome", "Google.Chrome"),
    ("Python 3.13", "Python.Python.3.13"),
    ("Git", "Git.Git"),
    ("Google Drive", "Google.GoogleDrive"),
    ("Tailscale", "Tailscale.Tailscale"),
    ("Synology Drive Client", "Synology.DriveClient"),
    ("Claude Desktop", "Anthropic.Claude"),
    ("WinDirStat", "WinDirStat.WinDirStat"),
    ("Visual Studio Code", "Microsoft.VisualStudioCode"),
]


def seccion_software_basico():
    title("INSTALACIÓN · SOFTWARE BÁSICO")

    if not IS_WIN:
        warn("Esta sección usa winget (Windows). En Mac, instalar manualmente vía Homebrew — fuera de este alcance por ahora.")
        return

    info("Fuente: config_parque_tecnologico.json → software.todos_los_equipos_ensamble")
    info(f"\n  Se revisarán {len(SOFTWARE_BASICO_WINGET)} paquete(s) via winget (se omite el que ya esté instalado):")
    for nombre, _ in SOFTWARE_BASICO_WINGET:
        info(f"    - {nombre}")

    if not confirm(f"\n¿Instalar los {len(SOFTWARE_BASICO_WINGET)} paquetes via winget?"):
        return

    for nombre, pkg_id in SOFTWARE_BASICO_WINGET:
        # winget list -e devuelve 0 si el paquete ya está instalado, distinto de 0 si no
        # (confirmado real en pc_13) — evita re-descargar instaladores de decenas/cientos
        # de MB en cada corrida para software que ya estaba.
        check = run(f'winget list --id {pkg_id} -e', check=False, capture=True)
        if check.returncode == 0:
            ok(f"{nombre} ya está instalado — omitido.")
            continue

        info(f"\n  Instalando {nombre}...")
        result = run(
            f'winget install --id {pkg_id} -e --accept-package-agreements --accept-source-agreements',
            check=False,
        )
        if result.returncode == 0:
            ok(f"{nombre} instalado.")
        else:
            err(f"{nombre} — winget devolvió código {result.returncode}. Revisa manualmente.")

    ok("\nSección software básico completada.")


# ─────────────────────────────────────────────
# SECCIÓN: INSTALACIÓN → SOFTWARE PROFESIONAL (placeholder)
# ─────────────────────────────────────────────

def seccion_software_profesional():
    title("INSTALACIÓN · SOFTWARE PROFESIONAL")
    warn("Pendiente — requiere definir versiones exactas de AutoCAD, Revit, ArchiCAD, InDesign,")
    warn("Photoshop, Lightroom, Illustrator, Acrobat Pro y SketchUp con el equipo BIM.")
    info("Ver config_parque_tecnologico.json → pendientes.")


# ─────────────────────────────────────────────
# SECCIÓN: INSTALACIÓN → AISLAR SOFTWARE PROFESIONAL DE INTERNET
# Política de oficina: ningún programa profesional ni sus dependencias/licencias
# puede tener acceso a internet. Reutiliza VENDORS['<id>']['win']['path_globs']
# (la misma fuente de verdad ya validada para Programas profesionales) — bloquea
# en vez de borrar. Solo Windows (netsh advfirewall).
# ─────────────────────────────────────────────

SHARED_LICENSING_PATHS = [
    # Runtimes de licenciamiento de terceros compartidos entre varios CAD —
    # no viven bajo la carpeta de ningún proveedor individual. Pendiente
    # confirmar con el equipo BIM cuáles aplican según el software instalado.
    r'C:\Program Files\CodeMeter',
    r'C:\Program Files (x86)\CodeMeter',
    r'C:\Program Files (x86)\Common Files\SafeNet Sentinel',
    r'C:\Program Files (x86)\Common Files\Aladdin Shared',
]

# Subcarpetas por-usuario (%AppData%/%LocalAppData%) donde updaters/helpers de cada
# proveedor suelen instalar componentes que NO viven en Program Files/ProgramData.
APPDATA_VENDOR_SUBFOLDERS = {
    'microsoft': ['Microsoft'],
    'adobe': ['Adobe'],
    'autodesk': ['Autodesk'],
    'graphisoft': ['GRAPHISOFT', 'Graphisoft'],
    'sketchup': ['SketchUp', 'Trimble'],
}

USERS_PROFILE_EXCLUIR = {'public', 'default', 'default user', 'all users'}


def _carpetas_appdata_por_usuario(vendor_ids: list) -> list:
    """Recorre C:\\Users\\* (todos los perfiles, no solo el actual) buscando las
    subcarpetas AppData\\Local y AppData\\Roaming de cada proveedor seleccionado."""
    carpetas = []
    users_root = Path('C:/Users')
    if not users_root.exists():
        return carpetas
    try:
        perfiles = [p for p in users_root.iterdir() if p.is_dir()]
    except Exception:
        return carpetas
    for user_dir in perfiles:
        if user_dir.name.lower() in USERS_PROFILE_EXCLUIR:
            continue
        for vid in vendor_ids:
            for sub in APPDATA_VENDOR_SUBFOLDERS.get(vid, []):
                carpetas.append(str(user_dir / 'AppData' / 'Local' / sub))
                carpetas.append(str(user_dir / 'AppData' / 'Roaming' / sub))
    return carpetas


def _rule_exists(rule_name: str) -> bool:
    result = run(f'netsh advfirewall firewall show rule name="{rule_name}"', check=False, capture=True)
    return 'No rules match the specified criteria' not in result.stdout


def _bloquear_exe_firewall(exe_path, dry_run: bool = False) -> bool:
    """Agrega reglas de entrada/salida bloqueando exe_path. Devuelve True si las agregó
    (o las agregaría, en dry-run) — False si ya existían (evita duplicar en corridas repetidas)."""
    rule_name = f"EnsambleAislar: {exe_path}"
    if _rule_exists(rule_name):
        return False
    if not dry_run:
        run(f'netsh advfirewall firewall add rule name="{rule_name}" dir=out program="{exe_path}" action=block', check=False)
        run(f'netsh advfirewall firewall add rule name="{rule_name}" dir=in program="{exe_path}" action=block', check=False)
    return True


def _carpetas_a_bloquear(vendor_ids: list) -> list:
    carpetas = []
    for vid in vendor_ids:
        carpetas.extend(VENDORS[vid]['win'].get('path_globs', []))
    carpetas.extend(SHARED_LICENSING_PATHS)
    carpetas.extend(_carpetas_appdata_por_usuario(vendor_ids))
    return carpetas


# ─── Reafirmación periódica (restaura reglas borradas — ej. por Asociado, que ───
# ─── ahora es admin y puede desactivar el firewall o borrar reglas a mano)    ───

REAPLICAR_DIR = Path(r"C:\ProgramData\EnsambleSetup")
REAPLICAR_SCRIPT = REAPLICAR_DIR / "reaplicar_aislamiento.ps1"
REAPLICAR_TASK = "EnsambleReaplicarAislamiento"


def _generar_ps_reaplicar(exe_paths: list) -> str:
    lineas = ",\n    ".join(f'"{p}"' for p in exe_paths)
    return (
        "# Generado por Ensamble Setup Tool — reaplica bloqueo de firewall si falta.\n"
        "# No agrega ejecutables nuevos: solo restaura reglas borradas para esta lista.\n"
        "$ejecutables = @(\n"
        f"    {lineas}\n"
        ")\n"
        "foreach ($exe in $ejecutables) {\n"
        "    if (-not (Test-Path $exe)) { continue }\n"
        '    $ruleName = "EnsambleAislar: $exe"\n'
        '    $existe = netsh advfirewall firewall show rule name="$ruleName" | Select-String "No rules match"\n'
        "    if ($existe) {\n"
        '        netsh advfirewall firewall add rule name="$ruleName" dir=out program="$exe" action=block | Out-Null\n'
        '        netsh advfirewall firewall add rule name="$ruleName" dir=in program="$exe" action=block | Out-Null\n'
        "    }\n"
        "}\n"
    )


def _instalar_tarea_reaplicar(exe_paths: list) -> bool:
    REAPLICAR_DIR.mkdir(parents=True, exist_ok=True)
    REAPLICAR_SCRIPT.write_text(_generar_ps_reaplicar(exe_paths), encoding="utf-8")

    run(f'schtasks /delete /tn "{REAPLICAR_TASK}" /f', check=False, capture=True)
    result = run(
        f'schtasks /create /tn "{REAPLICAR_TASK}" '
        f'/tr "powershell -ExecutionPolicy Bypass -File \\"{REAPLICAR_SCRIPT}\\"" '
        f'/sc daily /st 09:00 /ru SYSTEM /rl HIGHEST /f',
        check=False, capture=True,
    )
    return result.returncode == 0


def seccion_aislar_software_profesional():
    title("INSTALACIÓN · AISLAR SOFTWARE PROFESIONAL DE INTERNET")

    if not IS_WIN:
        warn("Esta sección usa el firewall de Windows (netsh). No aplica en Mac.")
        return

    warn("Política de oficina: ningún programa profesional ni sus dependencias pueden")
    warn("tener acceso a internet. Esto bloquea entrada y salida por firewall para cada")
    warn(".exe encontrado — no desinstala ni modifica nada del programa en sí.")

    dry_run = not confirm("\n¿Ejecutar en modo real? ('n' corre en modo dry-run / solo simulación)")
    if dry_run:
        info("[DRY-RUN] Se escaneará y reportará qué se bloquearía, sin tocar el firewall ni crear tareas.")

    estado = run('netsh advfirewall show allprofiles state', capture=True, check=False).stdout
    if 'ON' not in estado.upper():
        warn("\nEl firewall de Windows no está activo en los 3 perfiles.")
        if dry_run:
            info("[DRY-RUN] Se activaría en los 3 perfiles (Dominio/Privado/Público).")
        elif confirm("¿Activarlo ahora (Dominio/Privado/Público)?"):
            run('netsh advfirewall set allprofiles state on', check=False)
            ok("Firewall activado en los 3 perfiles.")
        else:
            warn("Sin el firewall activo, las reglas de bloqueo no tienen efecto. Continuando de todos modos...")

    vendor_ids = list(VENDORS.keys())
    print('\n  ¿Qué proveedores aislar de internet?')
    for i, vid in enumerate(vendor_ids, 1):
        print(f'    [{i}] {VENDORS[vid]["label"]}')
    print('  Números separados por coma (ej: 2,3) o "todos":')

    seleccion = []
    while True:
        choice = input('\n  Tu selección: ').strip().lower()
        if choice == 'todos':
            seleccion = vendor_ids
            break
        try:
            indices = [int(x.strip()) for x in choice.split(',') if x.strip()]
            seleccion = [vendor_ids[i - 1] for i in indices if 1 <= i <= len(vendor_ids)]
            if seleccion:
                break
        except ValueError:
            pass
        print('  Entrada inválida. Intentá de nuevo.')

    carpetas = _carpetas_a_bloquear(seleccion)
    info(f"\nBuscando ejecutables en {len(carpetas)} carpeta(s) conocida(s)...")

    encontrados = []
    for carpeta in carpetas:
        p = Path(carpeta)
        if p.exists():
            encontrados.extend(p.rglob('*.exe'))

    if not encontrados:
        ok("No se encontraron ejecutables en las carpetas de los proveedores seleccionados.")
        return

    info(f"{len(encontrados)} ejecutable(s) encontrado(s).")
    if not dry_run and not confirm(f"¿Bloquear entrada y salida de internet para los {len(encontrados)} ejecutable(s)?"):
        info("Cancelado.")
        return

    nuevos, ya_existian = 0, 0
    for exe in encontrados:
        if _bloquear_exe_firewall(exe, dry_run=dry_run):
            verbo_exe = "Se bloquearía" if dry_run else "Bloqueado"
            info(f"  {verbo_exe}: {exe}")
            nuevos += 1
        else:
            ya_existian += 1

    verbo_resumen = "se bloquearían" if dry_run else "bloqueado(s) nuevo(s)"
    ok(f"\n{nuevos} ejecutable(s) {verbo_resumen}, {ya_existian} ya tenían regla (sin duplicar).")

    if dry_run:
        ok("Simulación completa. Sin cambios realizados.")
        return

    warn("Si el software se actualiza y agrega ejecutables nuevos o cambia de carpeta,")
    warn("vuelve a correr esta sección para cubrir los nuevos.")

    print(f"\n{LINE}")
    info("Como Asociado también es cuenta admin, puede desactivar el firewall o borrar")
    info("estas reglas manualmente. Esto no lo impide, pero puede restaurarlas solo.")
    if confirm("¿Programar reafirmación diaria (09:00, corre como SYSTEM)?"):
        exe_paths = [str(e) for e in encontrados]
        if _instalar_tarea_reaplicar(exe_paths):
            ok(f"Tarea '{REAPLICAR_TASK}' creada — corre diario a las 09:00 como SYSTEM.")
            info(f"Script: {REAPLICAR_SCRIPT}")
        else:
            err("No se pudo crear la tarea programada. Revisa permisos y vuelve a intentar.")


# ─────────────────────────────────────────────
# SECCIÓN: CONFIGURACIÓN INICIAL
# Ajustes de sistema que no encajan en instalar/desinstalar. Solo Windows.
# ─────────────────────────────────────────────

ALTO_RENDIMIENTO_GUID = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"  # GUID fijo de Windows, no localizado


def seccion_configuracion_inicial():
    title("CONFIGURACIÓN INICIAL")

    if not IS_WIN:
        warn("Esta sección es solo para Windows. No aplica en Mac.")
        return

    if confirm("¿Activar el plan de energía 'Alto rendimiento'?"):
        estado = run("powercfg /list", check=False, capture=True).stdout or ""
        linea_guid = next((l for l in estado.splitlines() if ALTO_RENDIMIENTO_GUID in l), "")
        if "*" in linea_guid:
            ok("Ya estaba activo — sin cambios.")
        else:
            run(f"powercfg /setactive {ALTO_RENDIMIENTO_GUID}", check=False)
            ok("Plan 'Alto rendimiento' activado.")

    ok("Sección configuración inicial completada.")


# ─────────────────────────────────────────────
# SUBMENÚS
# ─────────────────────────────────────────────

def _submenu(nombre_titulo, opciones):
    while True:
        title(nombre_titulo)
        for key, (label, _) in opciones.items():
            print(f"  [{key}] {label}")
        print("  [0] Volver\n")

        opcion = ask("Selecciona una opción", list(opciones.keys()) + ["0"])
        if opcion == "0":
            return

        _, fn = opciones[opcion]
        try:
            fn()
        except KeyboardInterrupt:
            warn("Sección cancelada.")
        except Exception as e:
            err(f"Error inesperado: {e}")

        input("\n  Presiona Enter para volver al submenú...")


def seccion_desinstalacion():
    _submenu("DESINSTALACIÓN", {
        "1": ("Bloatware y servicios", seccion_bloatware_servicios),
        "2": ("Programas profesionales", seccion_programas_profesionales),
    })


def seccion_instalacion():
    _submenu("INSTALACIÓN", {
        "1": ("Software básico", seccion_software_basico),
        "2": ("Software profesional", seccion_software_profesional),
        "3": ("Aislar software profesional de internet", seccion_aislar_software_profesional),
    })


# ─────────────────────────────────────────────
# MENÚ PRINCIPAL
# ─────────────────────────────────────────────

MENU = {
    "1": ("Nombre del equipo", seccion_nombre_equipo),
    "2": ("Desinstalación", seccion_desinstalacion),
    "3": ("Instalación", seccion_instalacion),
    "4": ("Configuración inicial", seccion_configuracion_inicial),
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
