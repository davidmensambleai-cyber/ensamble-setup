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
import json
import hashlib
from datetime import datetime
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

LINE = '─' * 56

def ok(msg):   print(f"  ✔  {msg}")
def warn(msg): print(f"  ⚠  {msg}")
def err(msg):  print(f"  ✖  {msg}")
def info(msg): print(f"     {msg}")

def ask(prompt, options=None):
    """Input con validación opcional de opciones."""
    while True:
        try:
            val = input(f"\n  → {prompt}: ").strip()
        except EOFError:
            # stdin cerrado (terminal muerta): cancelar en vez de girar/crashear.
            # Misma guarda que mount-nas.py.
            warn("Entrada cerrada (EOF). Cancelando.")
            raise KeyboardInterrupt
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

def _quote_exe_path(cmd: str) -> str:
    """Los UninstallString del registro y las rutas de ejecutables propios vienen sin
    comillas — si la ruta tiene espacios (típico de "Program Files"), shell=True corta
    el comando en el primer espacio y falla con '"C:\\Program" no reconocido'. Bug real
    observado en producción (Carbon Insights, Autodesk Access, Autodesk AutoCAD 2025).
    Si ya viene entre comillas, se deja igual."""
    if cmd.startswith('"'):
        return cmd
    idx = cmd.lower().find('.exe')
    if idx == -1:
        return cmd
    exe_part = cmd[:idx + 4]
    if ' ' in exe_part:
        return f'"{exe_part}"{cmd[idx + 4:]}'
    return cmd


def run_logged(cmd, accion: str, timeout: int = None, codigos_ok=(0,)) -> bool:
    """Ejecuta cmd (str con shell, o list de argv) y reporta éxito/fallo con la razón
    real (stderr o stdout) en vez de asumir éxito o fallar en silencio. 3010 (MSI:
    reinicio pendiente) se trata como éxito informativo, no fallo."""
    kwargs = {}
    if isinstance(cmd, str):
        cmd = _quote_exe_path(cmd)
        if OS == "Windows":
            # cmd.exe no puede iniciar con cwd en una ruta UNC (ej. corriendo desde
            # \\nas_local\Ensamble\...) — sin esto cae a C:\Windows con un aviso y a
            # veces arrastra fallos de parseo aguas abajo. Bug real observado en
            # producción. Se fija un cwd local estable.
            kwargs['cwd'] = os.environ.get('SystemRoot', r'C:\Windows')
    try:
        result = subprocess.run(
            cmd, shell=isinstance(cmd, str), capture_output=True, text=True, timeout=timeout, **kwargs
        )
    except subprocess.TimeoutExpired:
        warn(f'{accion}: timeout.')
        return False
    except Exception as e:
        warn(f'{accion}: error al ejecutar — {e}')
        return False
    if result.returncode not in codigos_ok:
        motivo = (result.stderr or result.stdout or '').strip() or f'código de salida {result.returncode}'
        warn(f'{accion}: {motivo}')
        return False
    if result.returncode == 3010:
        info(f'{accion}: correcto (reinicio pendiente).')
    return True

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
                if ch == '':
                    # EOF: read(1) devuelve '' para siempre y el bucle giraría al 100% de CPU.
                    raise KeyboardInterrupt
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
    # Con BOM: PowerShell 5.1 lee un .ps1 sin BOM como ANSI y rompe tildes y eñes, también
    # las de una contraseña (misma razón que _ps_archivo).
    tmp_path.write_text(script_content, encoding="utf-8-sig")
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
    creada = run_logged(
        f'schtasks /create /tn "{task_name}" /tr "cmd.exe /c whoami" /sc once /st 23:59 '
        f'/ru "{nombre}" /rp "{password}" /f',
        f'Crear tarea temporal "{task_name}"',
    )
    del password
    if creada:
        run_logged(f'schtasks /run /tn "{task_name}"', f'Ejecutar tarea temporal "{task_name}"')
        time.sleep(5)
        run_logged(f'schtasks /delete /tn "{task_name}" /f', f'Eliminar tarea temporal "{task_name}"')
    else:
        warn("No se forzó la creación del perfil — la tarea temporal no se pudo crear.")

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
    confirmacion = input('  Escribe "si" para continuar: ').strip().lower()
    if confirmacion != "si":
        info("Cancelado.")
        return

    if not run_logged(f'powershell -Command "Remove-LocalUser -Name \'{objetivo}\'"', f'Eliminar cuenta "{objetivo}"'):
        return

    carpeta = Path(f"C:/Users/{objetivo}")
    carpeta_ok = True
    if carpeta.exists():
        try:
            shutil.rmtree(carpeta)
        except Exception as e:
            carpeta_ok = False
            warn(f"Cuenta eliminada, pero no se pudo eliminar la carpeta {carpeta}: {e}")
    if carpeta_ok:
        ok(f"Cuenta '{objetivo}' y su carpeta eliminadas.")
    else:
        ok(f"Cuenta '{objetivo}' eliminada (carpeta pendiente — ver warning arriba).")


def seccion_nombre_equipo():
    _submenu("NOMBRE DEL EQUIPO", {
        "1": ("Cambiar nombre del equipo + crear cuenta admin alineada", seccion_cambiar_nombre_y_cuenta),
        "2": ("Eliminar cuenta en desuso", seccion_eliminar_cuenta_desuso),
    })


# ─────────────────────────────────────────────
# SECCIÓN: DESINSTALACIÓN → BLOATWARE Y SERVICIOS
# Fuente de verdad: 03_Agents/Agentes/transversales/asesor-ti/references/
# config_parque_tecnologico.json → bloque "bloatware". Solo Windows.
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
                    removidos += 1
                elif run_logged(f'powershell -Command "Get-AppxPackage {pkg} | Remove-AppxPackage"', f'Remover {pkg}'):
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
            elif run_logged(f'"{onedrive_exe}" /uninstall', 'Desinstalar OneDrive'):
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
            sysmain_ok = run_logged(
                'powershell -Command "Set-Service SysMain -StartupType Disabled; '
                'Stop-Service SysMain -ErrorAction SilentlyContinue"',
                'Deshabilitar SysMain',
            )
            diagtrack_ok = run_logged(
                'powershell -Command "Set-Service DiagTrack -StartupType Disabled; '
                'Stop-Service DiagTrack -ErrorAction SilentlyContinue"',
                'Deshabilitar DiagTrack',
            )
            if sysmain_ok:
                ok("SysMain deshabilitado.")
            if diagtrack_ok:
                ok("DiagTrack deshabilitado.")

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
                r'C:\Program Files (x86)\Common Files\Adobe',  # faltaba el par x86
                r'C:\ProgramData\Adobe',
                # AppData por-usuario — verificado contra guía community Adobe CC
                # (photographylife.com). Resuelto por env var en scan_vendor_win.
                r'%LOCALAPPDATA%\Adobe',
                r'%APPDATA%\Adobe',
            ],
            # 'AdobeGCInvoker-1.0' (Run key) descartado: verificado en vivo contra una
            # instalación real de Creative Cloud 2025 (ENS-WIN-LAP-07) que esa entrada
            # de arranque no existe — el mecanismo parece obsoleto en versiones actuales.
            # CCXProcess.exe (Adobe Creative Cloud Experience) sí se confirmó corriendo
            # en esa misma verificación — es el proceso real a matar, no uno adivinado.
            'processes': [
                'Creative Cloud.exe', 'AdobeIPCBroker.exe', 'AdobeUpdateService.exe',
                'CCXProcess.exe',
            ],
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
            # "Autodesk Genuine Service" coincide con el keyword 'Autodesk' pero debe
            # desinstalarse AL FINAL, después de limpiar registro y carpetas — si corre
            # en la misma pasada puede dejar residuos que confunden al resto de la
            # limpieza (ver uninstall_vendor_win → found['registry_deferred']).
            'exclude_keywords': ['Genuine Service'],
            'path_globs': [
                r'C:\Program Files\Autodesk',
                r'C:\Program Files (x86)\Autodesk',
                r'C:\ProgramData\Autodesk',
                # Common Files\Autodesk Shared: donde vive AdskLicensingService.exe (el
                # chequeo de licencia) — faltaba en desinstalador_total/main.py original;
                # corregido aquí. No se propagó a ese script standalone (fuera de scope).
                r'C:\Program Files\Common Files\Autodesk Shared',
                r'C:\Program Files (x86)\Common Files\Autodesk Shared',
                # AppData por-usuario y licencias FLEXnet — verificado contra
                # documentación oficial Autodesk (Clean-uninstall.html). FLEXnet son
                # 3 archivos (uno oculto), resueltos por glob en scan_vendor_win.
                r'%LOCALAPPDATA%\Autodesk',
                r'%APPDATA%\Autodesk',
                r'C:\ProgramData\FLEXnet\adsk*.*',
            ],
            # Borrado directo de claves — más allá de las entradas Uninstall estándar.
            'registry_keys': [
                ('HKLM', r'SOFTWARE\Autodesk'),
                ('HKCU', r'SOFTWARE\Autodesk'),
            ],
            # Desinstaladores propios de Autodesk — correr ANTES de borrar carpetas
            # (no solo taskkill + rmtree). El script ya corre elevado (require_admin()
            # en main()), no hace falta re-elevar. Sin flags de silencio: no
            # confirmados en la documentación citada por la tarea — si el instalador
            # pide interacción, verificar los flags con Autodesk antes de dejarlo
            # desatendido.
            'custom_uninstallers': [
                r'C:\Program Files\Autodesk\AdODIS\V1\RemoveODIS.exe',
                r'C:\Program Files (x86)\Common Files\Autodesk Shared\AdskLicensing\uninstall.exe',
                r'C:\Program Files\Autodesk\AdskIdentityManager\uninstall.exe',
            ],
            # AdskLicensingService: por si el uninstall.exe de AdskLicensing no lo
            # desregistra. "Autodesk CER Service" y "Autodesk Access Service Host":
            # nombres reales confirmados en producción (Get-Service) — mantenían
            # cer.dll/cer.db/cer.log abiertos y bloqueaban el borrado de carpetas
            # aunque el proceso ya no apareciera en Get-Process.
            'services_to_delete': ['AdskLicensingService', 'Autodesk CER Service', 'Autodesk Access Service Host'],
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


# ─── Helpers de registro y rutas (Windows) ──────────────────────────

_HIVES = {'HKLM': winreg.HKEY_LOCAL_MACHINE, 'HKCU': winreg.HKEY_CURRENT_USER} if winreg else {}


def _resolve_win_path_glob(path_str: str) -> list:
    """Expande variables de entorno (%LOCALAPPDATA%, %APPDATA%, etc.) y, si el patrón
    trae wildcards, resuelve por glob. Sin wildcards es una comprobación de existencia
    directa — mismo comportamiento que antes para las rutas planas ya registradas."""
    expanded = os.path.expandvars(path_str)
    if any(ch in expanded for ch in '*?['):
        return [Path(p) for p in glob.glob(expanded)]
    p = Path(expanded)
    return [p] if p.exists() else []


def _registry_key_exists(hive_name: str, subkey: str) -> bool:
    hive = _HIVES.get(hive_name)
    if hive is None:
        return False
    try:
        with winreg.OpenKey(hive, subkey):
            return True
    except OSError:
        return False


def _registry_value_exists(hive_name: str, subkey: str, value_name: str) -> bool:
    hive = _HIVES.get(hive_name)
    if hive is None:
        return False
    try:
        with winreg.OpenKey(hive, subkey) as key:
            winreg.QueryValueEx(key, value_name)
        return True
    except OSError:
        return False


def _delete_registry_key_recursive(hive_name: str, subkey: str):
    """DeleteKey falla si la clave tiene subclaves — hay que vaciarla primero."""
    hive = _HIVES.get(hive_name)
    if hive is None:
        return
    try:
        with winreg.OpenKey(hive, subkey, 0, winreg.KEY_ALL_ACCESS) as key:
            while True:
                try:
                    sub = winreg.EnumKey(key, 0)
                except OSError:
                    break
                _delete_registry_key_recursive(hive_name, f'{subkey}\\{sub}')
        winreg.DeleteKey(hive, subkey)
    except OSError as e:
        warn(f'    No se pudo eliminar la clave {hive_name}\\{subkey}: {e}')


def _delete_registry_value(hive_name: str, subkey: str, value_name: str):
    hive = _HIVES.get(hive_name)
    if hive is None:
        return
    try:
        with winreg.OpenKey(hive, subkey, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, value_name)
    except OSError as e:
        warn(f'    No se pudo eliminar el valor {hive_name}\\{subkey}\\{value_name}: {e}')


def _service_exists(service_name: str) -> bool:
    # Comillas obligatorias — nombres de servicio de Autodesk vienen con espacios
    # ("Autodesk CER Service"), sin ellas sc.exe interpreta argumentos extra.
    result = run(f'sc query "{service_name}"', check=False, capture=True)
    return result.returncode == 0


def _run_uninstall_string(entry: dict) -> bool:
    """Ejecuta el UninstallString de una entrada de registro, agregando flags de
    silencio cuando faltan. Compartido por el pase normal y el diferido (Genuine Service)."""
    cmd = entry.get('uninstall')
    if not cmd:
        warn(f'    "{entry.get("name", "?")}" no tiene UninstallString registrada — no se puede desinstalar automáticamente.')
        return False
    if 'msiexec' in cmd.lower():
        if '/quiet' not in cmd.lower():
            cmd += ' /quiet /norestart'
    elif '/S' not in cmd and '/silent' not in cmd.lower() and '/quiet' not in cmd.lower():
        cmd += ' /S'
    return run_logged(cmd, f'Desinstalar "{entry["name"]}"', timeout=180, codigos_ok=(0, 3010))


def scan_vendor_win(vendor_id: str) -> dict:
    found: dict = {
        'registry': [],
        'registry_deferred': [],
        'paths': [],
        'registry_keys': [],
        'registry_run_keys': [],
        'custom_uninstallers': [],
        'services_to_delete': [],
    }
    profile = VENDORS[vendor_id]['win']

    if winreg:
        keywords = profile.get('keywords', [])
        exclude_keywords = profile.get('exclude_keywords', [])
        hives = [
            (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'),
            (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'),
            (winreg.HKEY_CURRENT_USER, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'),
        ]
        hive_names = {winreg.HKEY_LOCAL_MACHINE: 'HKLM', winreg.HKEY_CURRENT_USER: 'HKCU'}
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
                                        # hive/subkey_full guardados para poder borrar esta
                                        # entrada de Uninstall después, sin importar si el
                                        # UninstallString existe o funciona — antes esto no
                                        # se guardaba y las entradas fantasma (UninstallString
                                        # vacío, ej. "AutoCAD 2025 - English") sobrevivían para
                                        # siempre porque nunca se intentaba nada con ellas.
                                        entry = {
                                            'name': name,
                                            'uninstall': uninstall_str,
                                            'hive': hive_names[hive],
                                            'subkey_full': f'{key_path}\\{subkey_name}',
                                        }
                                        # Ej. "Autodesk Genuine Service" — desinstalar al
                                        # final (ver uninstall_vendor_win), no en esta pasada.
                                        target = (
                                            found['registry_deferred']
                                            if any(ex.lower() in name.lower() for ex in exclude_keywords)
                                            else found['registry']
                                        )
                                        if entry not in target:
                                            target.append(entry)
                                except OSError:
                                    pass
                        except OSError:
                            continue
            except OSError:
                continue

    for path_str in profile.get('path_globs', []):
        found['paths'].extend(_resolve_win_path_glob(path_str))

    for hive_name, subkey in profile.get('registry_keys', []):
        if _registry_key_exists(hive_name, subkey):
            found['registry_keys'].append({'hive': hive_name, 'subkey': subkey})

    for hive_name, subkey, value_name in profile.get('registry_run_keys', []):
        if _registry_value_exists(hive_name, subkey, value_name):
            found['registry_run_keys'].append({'hive': hive_name, 'subkey': subkey, 'value': value_name})

    for exe_path in profile.get('custom_uninstallers', []):
        p = Path(exe_path)
        if p.exists():
            found['custom_uninstallers'].append(p)

    for svc in profile.get('services_to_delete', []):
        if _service_exists(svc):
            found['services_to_delete'].append(svc)

    found['_total'] = sum(len(v) for k, v in found.items() if k != '_total')
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
            # bootout puede fallar con un código "no cargado" si el daemon ya no está
            # activo — no fabrico ese código exacto (no confirmado), así que se reporta
            # igual que cualquier otro fallo; es información veraz aunque sea benigna.
            run_logged(['launchctl', 'bootout', 'system', str(path)], f'Bootout daemon {path.name}')
            run_logged(['launchctl', 'unload', str(path)], f'Unload daemon {path.name}')

    for path in found.get('launch_agents', []):
        info(f'  Descargando agente: {path.name}')
        if not dry_run:
            run_logged(['launchctl', 'unload', str(path)], f'Unload agente {path.name}')

    all_paths = (
        found.get('apps', []) + found.get('dirs', [])
        + found.get('launch_agents', []) + found.get('launch_daemons', [])
    )
    for path in all_paths:
        info(f'  Eliminando: {path}')
        if not dry_run:
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                elif path.exists():
                    path.unlink()
            except Exception as e:
                warn(f'    No se pudo eliminar {path}: {e}')

    for pkg in found.get('packages', []):
        info(f'  Olvidando paquete: {pkg}')
        if not dry_run:
            run_logged(['pkgutil', '--forget', pkg], f'Olvidar paquete {pkg}')


def uninstall_vendor_win(vendor_id: str, found: dict, dry_run: bool):
    profile = VENDORS[vendor_id]['win']
    label = VENDORS[vendor_id]['label']

    info(f'Deteniendo procesos de {label}...')
    if not dry_run:
        kill_processes_win(profile.get('processes', []))

    for exe in found.get('custom_uninstallers', []):
        info(f'  Ejecutando desinstalador propio: {exe}')
        if not dry_run:
            run_logged(str(exe), f'Desinstalador propio {exe}', timeout=180, codigos_ok=(0, 3010))

    for svc in found.get('services_to_delete', []):
        info(f'  Deteniendo y eliminando servicio: {svc}')
        if not dry_run:
            # sc stop ANTES de sc delete — solo borrar el registro del servicio no
            # libera los archivos que ya tiene abiertos (bug real: cer.dll/cer.db/
            # cer.log seguían bloqueados y la carpeta Autodesk no se podía borrar en
            # la misma corrida). Si ya está detenido, sc stop falla con "no está
            # activo" — resultado esperado, no un fallo real de esta sección.
            run_logged(f'sc stop "{svc}"', f'Detener servicio {svc}')
            run_logged(f'sc delete "{svc}"', f'Eliminar servicio {svc}')

    for entry in found.get('registry', []):
        info(f'  Desinstalando: {entry["name"]}')
        if not dry_run:
            _run_uninstall_string(entry)
            # Se borra la entrada de Uninstall pase lo que pase con el comando de
            # arriba (existiera o no, tuviera éxito o no) — de lo contrario entradas
            # como "AutoCAD 2025 - English" (UninstallString vacío) quedan fantasma
            # en Programas y Características para siempre, verificado en un equipo real.
            _delete_registry_key_recursive(entry['hive'], entry['subkey_full'])

    for item in found.get('registry_run_keys', []):
        display = f'{item["hive"]}\\{item["subkey"]}\\{item["value"]}'
        info(f'  Eliminando entrada de arranque: {display}')
        if not dry_run:
            _delete_registry_value(item['hive'], item['subkey'], item['value'])

    for path in found.get('paths', []):
        info(f'  Eliminando: {path}')
        if not dry_run:
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except Exception as e:
                warn(f'    No se pudo eliminar {path}: {e}')

    for item in found.get('registry_keys', []):
        display = f'{item["hive"]}\\{item["subkey"]}'
        info(f'  Eliminando clave de registro: {display}')
        if not dry_run:
            _delete_registry_key_recursive(item['hive'], item['subkey'])

    # exclude_keywords (ej. "Genuine Service") se desinstala AL FINAL, después de
    # limpiar registro y carpetas — correrlo antes puede dejar residuos que
    # confunden al resto de la limpieza.
    for entry in found.get('registry_deferred', []):
        info(f'  Desinstalando (al final): {entry["name"]}')
        if not dry_run:
            _run_uninstall_string(entry)
            _delete_registry_key_recursive(entry['hive'], entry['subkey_full'])


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
        print('  Entrada inválida. Intenta de nuevo.')


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
    confirmacion = input('\n  Escribe "si" para continuar: ').strip().lower()
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
# Fuente de verdad (Windows): 03_Agents/Agentes/transversales/asesor-ti/references/
# config_parque_tecnologico.json → software.todos_los_equipos_ensamble. Todos los IDs
# verificados 2026-07-29 con `winget search` real en pc_13 — incluye la corrección de
# Python.Python.3 (deprecado) → Python.Python.3.13, y Synology/Claude (sí tienen ID).
# Además, en los dos SO:
#   · entorno de Python de 02_Python (setup_dev --solo-python), para que los scripts de los
#     agentes (ej. Word/PDF de los consultivos) corran en todo el equipo — decisión de David,
#     2026-09-28;
#   · VS Code solo si la NAS está montada con un usuario de VSCODE_USERS.
# Mac: LuLu + lulu-cli (versiones congeladas) y herramientas de documentos (pandoc, pango).
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
]
VSCODE_WINGET = ("Visual Studio Code", "Microsoft.VisualStudioCode")

# Usuarios NAS que trabajan el repo en VS Code. Mantener en sincronía con VSCODE_USERS de
# mount-nas.py: los dos scripts se descargan por separado y no comparten código.
VSCODE_USERS = {"davidm"}
NAS_HOST_ALIAS = "nas_local"

# VS Code anclado a la barra de tareas, abriendo directo el proyecto — ver
# asesor-ti/fixes/vscode-taskbar-abre-proyecto.md. Misma unidad de red (Z:) que monta
# mount-nas.py en todo equipo Windows conectado a la red local — ruta fija, no por equipo.
RUTA_PROYECTO_WIN = r"Z:\DTI_Tecnología, innovación y optimización\ensamble-platform"
# Un proceso elevado no ve las unidades mapeadas de la sesión normal: si Z: no aparece, se
# llega al mismo share por UNC con la credencial que guardó mount-nas (cmdkey).
RUTA_PROYECTO_UNC = rf"\\{NAS_HOST_ALIAS}\Ensamble\DTI_Tecnología, innovación y optimización\ensamble-platform"
RUTA_PROYECTO_MAC = Path("/Volumes/Ensamble/DTI_Tecnología, innovación y optimización/ensamble-platform")
VSCODE_LNK_PIN = os.path.join(
    os.environ.get("APPDATA", ""),
    "Microsoft", "Internet Explorer", "Quick Launch", "User Pinned", "TaskBar",
    "Visual Studio Code.lnk",
)

LULU_APP = Path("/Applications/LuLu.app")
LULU_CLI = Path("/usr/local/bin/lulu-cli")
LULU_DMG = "LuLu_4.5.1.dmg"
LULU_CLI_TGZ = "lulu-cli-v0.2.0-macos-universal.tar.gz"
# Versiones congeladas de LuLu y lulu-cli, con SHA256SUMS (decisión 2026-09-28: no se actualizan).
INSTALADORES_LULU_NAS = RUTA_PROYECTO_MAC / "04_Infraestructura" / "instaladores" / "lulu"
HERRAMIENTAS_DOCUMENTOS_MAC = ["pandoc", "pango"]   # Word y PDF de generar_word/pdf_legal


def _usuario_nas():
    """Usuario NAS con el que está montado el share Ensamble, en minúsculas, o None.

    Mac: sale de `mount` (//DavidM@192.168.2.7/Ensamble on /Volumes/Ensamble ...); macOS
    conserva las mayúsculas con que se escribió, por eso se compara en minúsculas.
    Windows: la credencial que mount-nas guarda con cmdkey para nas_local en cada montaje.
    Se busca el bloque de ese destino y el usuario en él, sin depender de las etiquetas
    ('User'/'Usuario'), que cambian con el idioma."""
    if IS_MAC:
        salida = run("mount", check=False, capture=True).stdout or ""
        for linea in salida.splitlines():
            if " on /Volumes/Ensamble (" in linea and linea.startswith("//") and "@" in linea:
                return linea[2:linea.index("@")].lower()
        return None
    salida = run("cmdkey /list", check=False, capture=True).stdout or ""
    bloque, dentro = [], False
    for linea in salida.splitlines():
        if f"target={NAS_HOST_ALIAS}".lower() in linea.lower():
            dentro = True
            continue
        if dentro:
            if not linea.strip():
                break
            bloque.append(linea)
    for linea in bloque:
        valor = linea.split(":", 1)[-1].strip().lower()
        if valor in VSCODE_USERS:
            return valor
    return None


def _es_usuario_vscode() -> bool:
    return _usuario_nas() in VSCODE_USERS


def _anclar_vscode_proyecto():
    """Edita el acceso directo YA anclado de VS Code para que abra directo el proyecto
    (Argumentos = ruta del proyecto). No puede anclar VS Code de cero de forma confiable —
    Windows no expone una API soportada para eso (mismo motivo por el que "Chrome
    predeterminado" quedó descartado en este script, más abajo). Si todavía no está
    anclado, solo da instrucciones."""
    if not os.path.exists(VSCODE_LNK_PIN):
        warn("VS Code todavía no está anclado a la barra de tareas.")
        info("Para que abra el proyecto directo, ancla el ícono una vez:")
        info('  1. Abre el menú Inicio y busca "Visual Studio Code".')
        info('  2. Clic derecho sobre el resultado → "Anclar a la barra de tareas".')
        info("  3. Vuelve a correr esta sección (Instalación → Software básico) —")
        info("     el script completa el segundo paso (cargar el proyecto) solo.")
        return

    ps_script = (
        f'$ws = New-Object -ComObject WScript.Shell; '
        f'$sc = $ws.CreateShortcut("{VSCODE_LNK_PIN}"); '
        f"$sc.Arguments = '\"{RUTA_PROYECTO_WIN}\"'; "
        f'$sc.Save()'
    )
    if run_logged(["powershell", "-NoProfile", "-Command", ps_script], "Anclar VS Code al proyecto"):
        ok("VS Code anclado ahora abre directo la carpeta del proyecto.")


def _winget_instalar(nombre, pkg_id):
    # winget list -e devuelve 0 si el paquete ya está instalado, distinto de 0 si no
    # (confirmado real en pc_13) — evita re-descargar instaladores de decenas/cientos
    # de MB en cada corrida para software que ya estaba.
    check = run(f'winget list --id {pkg_id} -e', check=False, capture=True)
    if check.returncode == 0:
        ok(f"{nombre} ya está instalado — omitido.")
        return
    info(f"\n  Instalando {nombre}...")
    result = run(
        f'winget install --id {pkg_id} -e --accept-package-agreements --accept-source-agreements',
        check=False,
    )
    if result.returncode == 0:
        ok(f"{nombre} instalado.")
    else:
        err(f"{nombre} — winget devolvió código {result.returncode} (ver el mensaje de winget arriba).")


# ─── Entorno de Python (los dos SO) ───

def _ruta_proyecto():
    if IS_MAC:
        return RUTA_PROYECTO_MAC if RUTA_PROYECTO_MAC.exists() else None
    for ruta in (RUTA_PROYECTO_WIN, RUTA_PROYECTO_UNC):
        if os.path.exists(ruta):
            return Path(ruta)
    return None


def _usuario_sesion_mac():
    """La persona de la sesión, no root: este script corre con sudo."""
    usuario = os.environ.get("SUDO_USER")
    if usuario and usuario != "root":
        return usuario
    try:
        import pwd
        uid = os.stat('/dev/console').st_uid
        return pwd.getpwuid(uid).pw_name if uid != 0 else None
    except (OSError, KeyError):
        return None


# sudo reemplaza PATH por uno seguro sin Homebrew; sin esto, setup-dev.sh no ve el uv ni el
# brew ya instalados y baja otro uv a ~/.local/bin.
PATH_MAC_USUARIO = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def _como_usuario_mac(usuario, cmd):
    return ["sudo", "-u", usuario, "-H", "env", f"PATH={PATH_MAC_USUARIO}", *cmd]


def _entorno_python():
    """Corre setup_dev --solo-python desde el NAS: uv, Python 3.13, UV_PROJECT_ENVIRONMENT
    persistida y `uv sync` de 02_Python. Debe correr como la persona de la sesión: la
    variable y el entorno viven en su perfil, no en el de root/administrador."""
    proyecto = _ruta_proyecto()
    if not proyecto:
        warn("No se encontró el proyecto en el NAS. Conecta el NAS (mount-nas) y vuelve a correr esta sección.")
        return
    carpeta = proyecto / "02_Python" / "scripts" / "setup_dev"
    info("Prepara el entorno de Python que usan los scripts de los agentes (Word, PDF, Excel...).")
    info("Descarga uv y las librerías la primera vez (unos minutos); después solo actualiza.")
    if not confirm("¿Preparar el entorno de Python?"):
        return
    if IS_MAC:
        usuario = _usuario_sesion_mac()
        if not usuario:
            err("No se pudo identificar el usuario de la sesión. Corre esta sección con la sesión abierta.")
            return
        cmd = _como_usuario_mac(usuario, ["/bin/sh", str(carpeta / "setup-dev.sh"), "--solo-python"])
    else:
        # En los equipos de oficina la cuenta diaria es la administradora (crear_cuenta_admin_
        # alineada): el proceso elevado es la misma persona y escribe en su HKCU.
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
               "-File", str(carpeta / "setup-dev.ps1"), "--solo-python"]
    if subprocess.run(cmd).returncode == 0:
        ok("Entorno de Python listo.")
    else:
        err("setup_dev terminó con errores (ver arriba).")


# ─── Mac: LuLu + lulu-cli ───

def _carpeta_instaladores_lulu():
    candidatos = []
    f = globals().get("__file__")
    if f:
        candidatos.append(Path(f).resolve().parents[1] / "instaladores" / "lulu")
    candidatos.append(INSTALADORES_LULU_NAS)
    return next((c for c in candidatos if (c / "SHA256SUMS").exists()), None)


def _verificar_instalador(carpeta: Path, nombre: str) -> bool:
    esperados = {}
    for linea in (carpeta / "SHA256SUMS").read_text().splitlines():
        partes = linea.split()
        if len(partes) == 2:
            esperados[partes[1]] = partes[0].lower()
    if not (carpeta / nombre).exists() or esperados.get(nombre) != _sha256(carpeta / nombre):
        err(f"{nombre}: falta o su hash no coincide con SHA256SUMS. No se instala.")
        return False
    return True


def _mac_instalar_lulu(carpeta: Path) -> bool:
    import plistlib
    if not _verificar_instalador(carpeta, LULU_DMG):
        return False
    r = subprocess.run(['hdiutil', 'attach', '-nobrowse', '-readonly', '-plist', str(carpeta / LULU_DMG)],
                       capture_output=True)
    if r.returncode != 0:
        err(f"No se pudo montar {LULU_DMG}: {r.stderr.decode(errors='replace').strip()}")
        return False
    entidades = plistlib.loads(r.stdout).get('system-entities', [])
    montaje = next((e['mount-point'] for e in entidades if 'mount-point' in e), None)
    try:
        apps = sorted(Path(montaje).glob('*.app')) if montaje else []
        if not apps:
            err("El DMG no trae ninguna .app en la raíz.")
            return False
        return run_logged(['ditto', str(apps[0]), str(Path('/Applications') / apps[0].name)],
                          f'Copiar {apps[0].name} a /Applications')
    finally:
        if montaje:
            subprocess.run(['hdiutil', 'detach', montaje, '-quiet'])


def _mac_instalar_lulu_cli(carpeta: Path) -> bool:
    import tarfile
    if not _verificar_instalador(carpeta, LULU_CLI_TGZ):
        return False
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(carpeta / LULU_CLI_TGZ) as tf:
            tf.extract(tf.getmember('lulu-cli'), tmp)
        LULU_CLI.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(tmp) / 'lulu-cli', LULU_CLI)
    os.chown(LULU_CLI, 0, 0)
    os.chmod(LULU_CLI, 0o755)
    ok(f"lulu-cli instalado en {LULU_CLI}.")
    return True


def _mac_lulu_extension_activa() -> bool:
    r = subprocess.run(['systemextensionsctl', 'list'], capture_output=True, text=True)
    return any('com.objective-see.lulu' in l and '[activated enabled]' in l for l in r.stdout.splitlines())


def _mac_lulu():
    """Instala desde el NAS lo que falte, verificando SHA256SUMS, y guía la configuración
    manual: macOS no deja automatizar la aprobación de una extensión de red."""
    info("LuLu filtra la salida a internet por programa (lo usa Instalación → 3 Aislador + Centinela).")
    carpeta = _carpeta_instaladores_lulu()
    for falta, nombre, instalar in ((not LULU_APP.exists(), LULU_DMG, _mac_instalar_lulu),
                                    (not LULU_CLI.exists(), LULU_CLI_TGZ, _mac_instalar_lulu_cli)):
        if not falta:
            continue
        warn(f"Falta {nombre.split('_')[0].split('-v')[0]}.")
        if not carpeta:
            err("No se encontró la carpeta de instaladores del NAS (04_Infraestructura/instaladores/lulu). ¿Está montado?")
            return
        if not confirm(f"¿Instalar {nombre} desde el NAS?") or not instalar(carpeta):
            return

    if _mac_lulu_extension_activa():
        ok("LuLu y lulu-cli listos, con la extensión de red activa.")
        return
    warn("La extensión de red de LuLu no está activa. Pasos manuales (una sola vez):")
    info("1. Abre LuLu desde Aplicaciones y sigue su asistente.")
    info("2. Aprueba la extensión en Configuración del Sistema → General →")
    info("   Ítems de inicio y extensiones → Extensiones de red.")
    info("3. En la configuración de LuLu: activa el modo pasivo permitiendo lo desconocido")
    info("   (así solo actúan las reglas de Ensamble) y desactiva la búsqueda de actualizaciones.")
    input("\n  Presiona Enter cuando hayas terminado...")
    if _mac_lulu_extension_activa():
        ok("LuLu y lulu-cli listos, con la extensión de red activa.")
    else:
        err("La extensión sigue inactiva: sin ella LuLu no filtra nada. Vuelve a esta sección cuando la apruebes.")


# ─── Mac: herramientas de documentos ───

def _brew_mac():
    return next((b for b in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew") if os.path.exists(b)), None)


def _mac_herramientas_documentos():
    """pandoc (Word) y pango (PDF directo) vía Homebrew. En Windows no hacen falta: pandoc
    viene dentro del paquete de Python y el PDF sale por Word."""
    brew, usuario = _brew_mac(), _usuario_sesion_mac()
    if not brew:
        warn("Homebrew no está instalado: sin él no se instalan pandoc ni pango (Word y PDF de los agentes).")
        info('Instálalo desde https://brew.sh (pega el comando en Terminal, SIN sudo) y vuelve a esta sección.')
        return
    if not usuario:
        err("No se pudo identificar el usuario de la sesión.")
        return
    faltan = [p for p in HERRAMIENTAS_DOCUMENTOS_MAC
              if subprocess.run(_como_usuario_mac(usuario, [brew, "list", "--versions", p]),
                                capture_output=True).returncode != 0]
    if not faltan:
        ok("pandoc y pango ya están instalados.")
        return
    if not confirm(f"¿Instalar {', '.join(faltan)} con Homebrew? (Word y PDF de los agentes)"):
        return
    # Homebrew se niega a correr como root: se instala a nombre de la persona de la sesión.
    if run_logged(_como_usuario_mac(usuario, [brew, "install", *faltan]), f"brew install {' '.join(faltan)}"):
        ok(f"{', '.join(faltan)} instalado(s).")


def seccion_software_basico():
    title("INSTALACIÓN · SOFTWARE BÁSICO")
    vscode = _es_usuario_vscode()

    if IS_MAC:
        print("\n  ─ LuLu + lulu-cli ─")
        _mac_lulu()
        print("\n  ─ Herramientas de documentos (pandoc, pango) ─")
        _mac_herramientas_documentos()
        print("\n  ─ Entorno de Python ─")
        _entorno_python()
        if vscode:
            # Consejo corregido 2026-09-11. Decia "abre el proyecto una vez y VS Code lo
            # recuerda" (window.restoreWindows) — eso es FIX-003 y FIX-007 demostro que NO
            # basta: si el share se desmonta (suspension) VS Code queda sobre una ruta rota
            # y no la recupera solo. En Mac eso lo resuelve mount-nas.py, no este script.
            print("\n  ─ VS Code ─")
            info("VS Code en Mac no se ancla al Dock con argumentos, pero NO hay que abrirlo a mano:")
            info("  correr `mount-nas` instala la apertura en el proyecto al iniciar sesión y la")
            info("  revalidación tras cada remontaje del NAS (solo para los usuarios de VSCODE_USERS).")
            info("  Confiar en window.restoreWindows no alcanza — ver FIX-007 y FIX-015.")
        ok("\nSección software básico completada.")
        return

    paquetes = SOFTWARE_BASICO_WINGET + ([VSCODE_WINGET] if vscode else [])
    info("Fuente: config_parque_tecnologico.json → software.todos_los_equipos_ensamble")
    info(f"\n  Se revisarán {len(paquetes)} paquete(s) via winget (se omite el que ya esté instalado):")
    for nombre, _ in paquetes:
        info(f"    - {nombre}")
    if not vscode:
        info("  (VS Code se omite: solo se instala si el NAS está montado con un usuario de VSCODE_USERS.)")

    if confirm(f"\n¿Instalar los {len(paquetes)} paquetes via winget?"):
        for nombre, pkg_id in paquetes:
            _winget_instalar(nombre, pkg_id)
        if vscode:
            print()
            _anclar_vscode_proyecto()

    print("\n  ─ Entorno de Python ─")
    info("Word sale con pandoc incluido en el entorno; el PDF de los agentes, por Word (--formato pdf).")
    _entorno_python()

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
# SECCIÓN: INSTALACIÓN → AISLADOR + CENTINELA
# Política de oficina: ningún programa profesional ni sus dependencias/licencias
# puede tener acceso a internet. Dos mitades:
#   · Aislar proveedores: bloquea por firewall cada ejecutable de las carpetas de los
#     proveedores elegidos (reutiliza VENDORS[...]['win'|'mac'], la misma fuente de verdad
#     de Programas profesionales). Esas carpetas quedan registradas como "zonas aprobadas".
#   · Centinela: baseline del equipo + detección lunes y viernes 13:30. Lo nuevo dentro de
#     una zona aprobada se bloquea provisionalmente; todo lo detectado sale en una pantalla
#     automática donde se elige qué queda bloqueado (lo demás pasa a la whitelist).
# Windows: firewall de Windows + tareas programadas en PowerShell. Aunque desde 2026-09-28
# todo equipo tiene Python (Software básico), ese entorno vive en el perfil de cada persona:
# una tarea que corre como SYSTEM no puede depender de él. Mac: LuLu + lulu-cli para la
# salida (se instalan en Software básico), socketfilterfw para la entrada, launchd.
# Documentación: README-aislar-internet.md.
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


REGLA_PREFIJO = "EnsambleAislar:"


def _ps(comando: str):
    """Ejecuta un comando de PowerShell y devuelve el CompletedProcess.

    Se usa PowerShell —y no la salida de texto de netsh— en todo chequeo que deba dar un
    sí/no. netsh traduce sus mensajes al idioma del Windows: el chequeo anterior buscaba la
    frase 'No rules match the specified criteria', que en un Windows en español nunca
    aparece, así que concluía siempre "la regla no existe" y duplicaba reglas en cada
    corrida. Los cmdlets devuelven objetos y booleanos, que no se traducen."""
    return run(f'powershell -NoProfile -NonInteractive -Command "{comando}"', check=False, capture=True)


def _rule_name(exe_path, direccion: str) -> str:
    """Un nombre distinto por dirección. Antes las reglas de entrada y salida compartían
    nombre: si una de las dos fallaba al crearse, la corrida siguiente veía el nombre puesto,
    daba el ejecutable por bloqueado y no reparaba nunca la que faltaba."""
    return f"{REGLA_PREFIJO} {exe_path} [{direccion}]"


def _reglas_existentes() -> set:
    """Todas las reglas EnsambleAislar ya presentes, en UNA sola consulta al firewall.

    Antes se lanzaba un netsh por ejecutable: sobre una suite CAD completa son miles de
    procesos y la corrida se iba a decenas de minutos. Devuelve un set de nombres."""
    result = _ps(
        f"Get-NetFirewallRule -DisplayName '{REGLA_PREFIJO}*' -ErrorAction SilentlyContinue"
        " | Select-Object -ExpandProperty DisplayName"
    )
    if result.returncode != 0:
        warn("No se pudo consultar las reglas ya existentes; se asumirá que no hay ninguna.")
        return set()
    return {linea.strip() for linea in result.stdout.splitlines() if linea.strip()}


def _perfiles_firewall_apagados() -> list:
    """Perfiles (Domain/Private/Public) con el firewall apagado.

    Reemplaza el chequeo viejo, que buscaba la subcadena 'ON' en toda la salida de netsh:
    bastaba con que UN perfil estuviera encendido para dar los tres por buenos, y además la
    palabra 'Configuración' de la salida en español contiene 'ON', con lo que el chequeo daba
    positivo siempre. Los nombres de perfil no se traducen y Enabled es un booleano."""
    result = _ps("Get-NetFirewallProfile | ForEach-Object { $_.Name + '=' + $_.Enabled }")
    if result.returncode != 0:
        warn("No se pudo consultar el estado del firewall; se continúa sin verificarlo.")
        return []
    apagados = []
    for linea in result.stdout.splitlines():
        if '=' in linea:
            nombre, _, estado = linea.strip().partition('=')
            if estado.strip().lower() not in ('true', '1'):
                apagados.append(nombre.strip())
    return apagados


def _bloquear_exe_firewall(exe_path, existentes: set, dry_run: bool = False) -> bool:
    """Crea las reglas de entrada y salida que le falten a exe_path. Devuelve True si creó
    alguna (o la crearía, en dry-run), False si las dos ya estaban.

    `existentes` es el set devuelto por _reglas_existentes() y se actualiza aquí mismo: así
    dos rutas repetidas dentro de la misma corrida no cuentan doble."""
    faltantes = [d for d in ('out', 'in') if _rule_name(exe_path, d) not in existentes]
    if not faltantes:
        return False
    for direccion in faltantes:
        nombre = _rule_name(exe_path, direccion)
        if not dry_run:
            etiqueta = 'salida' if direccion == 'out' else 'entrada'
            # Lista de argumentos, NO cadena: run_logged pasa toda cadena por
            # _quote_exe_path(), que al ver el primer '.exe' de la línea entrecomilla todo
            # lo que va antes. Sobre un UninstallString del registro eso es correcto; sobre
            # un comando de netsh produce una línea que empieza con comilla, y cmd.exe toma
            # 'netsh advfirewall firewall add rule name=...' como el nombre del programa a
            # ejecutar. Fallaba siempre y la sección reportaba "Bloqueado" sin haber creado
            # nada. Con una lista, run_logged no entrecomilla ni usa el shell.
            creada = run_logged(
                ['netsh', 'advfirewall', 'firewall', 'add', 'rule',
                 f'name={nombre}', f'dir={direccion}',
                 f'program={exe_path}', 'action=block'],
                f'Bloquear {etiqueta} {exe_path}',
            )
            if not creada:
                warn(f'    {exe_path}: falta la regla de {etiqueta}. Se reintenta en la próxima corrida.')
                continue
        existentes.add(nombre)
    return True


def _carpetas_a_bloquear(vendor_ids: list) -> list:
    carpetas = []
    for vid in vendor_ids:
        carpetas.extend(VENDORS[vid]['win'].get('path_globs', []))
    carpetas.extend(SHARED_LICENSING_PATHS)
    carpetas.extend(_carpetas_appdata_por_usuario(vendor_ids))
    return carpetas


# ─── Estado del centinela (compartido con los scripts de las tareas programadas) ───

CENTINELA_DIR = (Path(r"C:\ProgramData\EnsambleSetup\centinela") if IS_WIN
                 else Path("/Library/Application Support/EnsambleSetup/centinela"))
CENTINELA_PS1 = CENTINELA_DIR / "centinela.ps1"
CENTINELA_MAC_PY = CENTINELA_DIR / "centinela_mac.py"
TAREA_DETECTAR = "EnsambleCentinelaDetectar"
TAREA_REVISAR = "EnsambleCentinelaRevisar"
TAREA_VIEJA = "EnsambleReaplicarAislamiento"   # versión anterior; la vigilancia la retira
MAC_DAEMON = Path("/Library/LaunchDaemons/com.ensamble.centinela.detectar.plist")
MAC_AGENTE = Path("/Library/LaunchAgents/com.ensamble.centinela.revisar.plist")
SFW = "/usr/libexec/ApplicationFirewall/socketfilterfw"
# LULU_APP, LULU_CLI y la instalación de LuLu: sección Software básico.


def _ahora() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _cj_leer(nombre, defecto):
    ruta = CENTINELA_DIR / nombre
    if not ruta.exists():
        return defecto
    try:
        return json.loads(ruta.read_text(encoding="utf-8-sig"))
    except Exception as e:
        warn(f"No se pudo leer {ruta}: {e}")
        return defecto


def _cj_escribir(nombre, datos):
    CENTINELA_DIR.mkdir(parents=True, exist_ok=True)
    (CENTINELA_DIR / nombre).write_text(json.dumps(datos, indent=2, ensure_ascii=False), encoding="utf-8")


def _sha256(ruta) -> str:
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


def _whitelist_rutas() -> set:
    return {str(e.get("path", "")).lower() for e in _cj_leer("whitelist.json", [])}


def _registrar_zonas_win(carpetas):
    zonas = _cj_leer("zonas.json", [])
    vistas = {z.lower() for z in zonas}
    for c in carpetas:
        if c.lower() not in vistas:
            zonas.append(c)
            vistas.add(c.lower())
    _cj_escribir("zonas.json", zonas)


def _registrar_bloqueados_win(rutas, origen):
    bloq = _cj_leer("bloqueados.json", [])
    vistas = {b["path"].lower() for b in bloq}
    for r in rutas:
        if r.lower() not in vistas:
            bloq.append({"path": r, "origen": origen, "fecha": _ahora()})
            vistas.add(r.lower())
    _cj_escribir("bloqueados.json", bloq)


def _elegir_proveedores(pregunta: str) -> list:
    vendor_ids = list(VENDORS.keys())
    print(f'\n  {pregunta}')
    for i, vid in enumerate(vendor_ids, 1):
        print(f'    [{i}] {VENDORS[vid]["label"]}')
    print('  Números separados por coma (ej: 2,3) o "todos":')
    while True:
        choice = input('\n  Tu selección: ').strip().lower()
        if choice == 'todos':
            return vendor_ids
        try:
            indices = [int(x.strip()) for x in choice.split(',') if x.strip()]
            seleccion = [vendor_ids[i - 1] for i in indices if 1 <= i <= len(vendor_ids)]
            if seleccion:
                return seleccion
        except ValueError:
            pass
        print('  Entrada inválida. Intenta de nuevo.')


# ─── Windows ───

def _ps_archivo(script: str):
    """Como _run_ps_script(), pero con BOM: PowerShell 5.1 lee un .ps1 sin BOM como ANSI y
    rompe las tildes de rutas como C:\\Users\\Andrés."""
    tmp = Path(tempfile.gettempdir()) / f"_ensamble_centinela_{int(time.time() * 1000)}.ps1"
    tmp.write_text(script, encoding="utf-8-sig")
    try:
        return subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(tmp)],
                              capture_output=True, text=True, encoding='utf-8', errors='replace')
    finally:
        tmp.unlink(missing_ok=True)


def _centinela_preparar_win():
    CENTINELA_DIR.mkdir(parents=True, exist_ok=True)
    # Solo SYSTEM y Administradores escriben: un usuario estándar no puede meterse en la
    # whitelist ni editar el script que corre como SYSTEM. SIDs y no nombres: no se traducen.
    run_logged(['icacls', str(CENTINELA_DIR), '/inheritance:r', '/grant:r',
                '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F', '*S-1-5-32-545:(OI)(CI)RX'],
               'Restringir permisos de la carpeta del centinela')
    CENTINELA_PS1.write_text(CENTINELA_PS1_CODIGO, encoding="utf-8-sig")


def _centinela_ps(modo: str) -> bool:
    _centinela_preparar_win()
    r = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                        '-File', str(CENTINELA_PS1), '-Modo', modo])
    return r.returncode == 0


def _quitar_reglas_win(rutas) -> bool:
    """Borra las reglas de entrada y salida de cada ruta. Se trae todo el prefijo y se compara
    el nombre exacto: -DisplayName acepta comodines y '[out]' es un comodín de un carácter."""
    if not rutas:
        return True
    nombres = ",".join("'" + _rule_name(r, d).replace("'", "''") + "'" for r in rutas for d in ('out', 'in'))
    r = _ps_archivo(
        f"$n = @({nombres})\n"
        f"Get-NetFirewallRule -DisplayName '{REGLA_PREFIJO}*' -ErrorAction SilentlyContinue |"
        " Where-Object { $n -contains $_.DisplayName } | Remove-NetFirewallRule -ErrorAction Stop\n")
    if r.returncode != 0:
        warn(f"No se pudieron quitar las reglas: {(r.stderr or r.stdout).strip()}")
    return r.returncode == 0


def _whitelist_agregar_win(ruta: str):
    if not _quitar_reglas_win([ruta]):
        return
    wl = [e for e in _cj_leer("whitelist.json", []) if e["path"].lower() != ruta.lower()]
    wl.append({"path": ruta, "sha256": _sha256(ruta), "fecha": _ahora()})
    _cj_escribir("whitelist.json", wl)
    _cj_escribir("bloqueados.json", [b for b in _cj_leer("bloqueados.json", []) if b["path"].lower() != ruta.lower()])
    ok("Agregado a la whitelist y desbloqueado.")


def _aislar_win(dry_run: bool):
    apagados = _perfiles_firewall_apagados()
    if apagados:
        warn(f"\nEl firewall de Windows está apagado en: {', '.join(apagados)}.")
        if dry_run:
            info("[DRY-RUN] Se activaría en los 3 perfiles (Dominio/Privado/Público).")
        elif confirm("¿Activarlo ahora (Dominio/Privado/Público)?"):
            if run_logged('netsh advfirewall set allprofiles state on', 'Activar firewall (3 perfiles)'):
                ok("Firewall activado en los 3 perfiles.")
        else:
            warn("Sin el firewall activo, las reglas de bloqueo no tienen efecto. Continuando de todos modos...")

    seleccion = _elegir_proveedores('¿Qué proveedores aislar de internet?')
    carpetas = _carpetas_a_bloquear(seleccion)
    info(f"\nBuscando ejecutables en {len(carpetas)} carpeta(s) conocida(s)...")

    # Resuelto igual que scan_vendor_win (expandvars + glob) — path_globs puede traer
    # %LOCALAPPDATA%/%APPDATA% o wildcards (ej. FLEXnet de Autodesk); sin esto, esas
    # entradas se saltaban en silencio (Path() literal nunca las encontraba).
    encontrados, zonas = [], []
    for carpeta in carpetas:
        for p in _resolve_win_path_glob(carpeta):
            if p.is_dir():
                zonas.append(str(p))
                encontrados.extend(str(e) for e in p.rglob('*.exe'))
            elif p.suffix.lower() == '.exe':
                encontrados.append(str(p))
    encontrados = list(dict.fromkeys(encontrados))

    # Los alias de WindowsApps (winget, entre otros) no son el ejecutable real: una regla sobre
    # ellos no bloquea nada, y winget lo usan Instalación → 1 y Desinstalación de este script.
    alias = [e for e in encontrados if '\\windowsapps\\' in e.lower()]
    wl = _whitelist_rutas()
    en_wl = [e for e in encontrados if e.lower() in wl]
    encontrados = [e for e in encontrados if '\\windowsapps\\' not in e.lower() and e.lower() not in wl]
    if alias:
        info(f"{len(alias)} alias de WindowsApps omitido(s).")
    if en_wl:
        info(f"{len(en_wl)} ejecutable(s) omitido(s) por estar en la whitelist.")

    if not encontrados:
        ok("No se encontraron ejecutables en las carpetas de los proveedores seleccionados.")
        return

    info(f"{len(encontrados)} ejecutable(s) encontrado(s).")
    if not dry_run and not confirm(f"¿Bloquear entrada y salida de internet para los {len(encontrados)} ejecutable(s)?"):
        info("Cancelado.")
        return

    existentes = _reglas_existentes()
    if existentes:
        info(f"{len(existentes)} regla(s) de aislamiento ya presentes en el firewall.")

    nuevos, ya_existian = 0, 0
    for exe in encontrados:
        if _bloquear_exe_firewall(exe, existentes, dry_run=dry_run):
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

    # Solo se registran los que quedaron con sus dos reglas: la detección restaura las que se
    # borren y no vuelve a reportar como nuevo lo que ya está bloqueado.
    completos = [e for e in encontrados if all(_rule_name(e, d) in existentes for d in ('out', 'in'))]
    _registrar_bloqueados_win(completos, "proveedor")
    _registrar_zonas_win(zonas)
    if len(completos) < len(encontrados):
        warn(f"{len(encontrados) - len(completos)} ejecutable(s) quedaron sin sus dos reglas (ver motivos arriba).")
    info("Las carpetas de estos proveedores quedan como zonas aprobadas: lo nuevo que aparezca en ellas")
    info("se bloquea provisionalmente en cada detección. Actívala en la opción 4 de este menú.")


# ─── Mac ───

def _mac_helper(*args, capturar=False):
    """Escribe el helper del centinela (siempre, para que quede al día) y lo ejecuta."""
    CENTINELA_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CENTINELA_DIR, 0o755)
    CENTINELA_MAC_PY.write_text(CENTINELA_MAC_CODIGO, encoding="utf-8")
    os.chmod(CENTINELA_MAC_PY, 0o755)
    cmd = [_python_mac(), str(CENTINELA_MAC_PY), *args]
    if capturar:
        return subprocess.run(cmd, capture_output=True, text=True)
    return subprocess.run(cmd)


def _python_mac() -> str:
    """El Python que usarán launchd y el helper. /usr/bin/python3 (Command Line Tools) no cambia
    de ruta al actualizarse; uno de Homebrew sí, y dejaría las tareas apuntando a la nada."""
    if subprocess.run(['xcode-select', '-p'], capture_output=True).returncode == 0:
        return '/usr/bin/python3'
    return sys.executable


def _mac_prerrequisitos() -> bool:
    """Solo chequea: LuLu, lulu-cli y su extensión de red activa. La instalación y la
    configuración manual viven en Instalación → 1 Software básico; aquí no se instala nada."""
    faltan = [n for falta, n in ((not LULU_APP.exists(), "LuLu"), (not LULU_CLI.exists(), "lulu-cli")) if falta]
    if not faltan and not _mac_lulu_extension_activa():
        faltan.append("la extensión de red de LuLu activa")
    if faltan:
        err(f"Falta {', '.join(faltan)}.")
        info("Instálalo en Instalación → 1 Software básico y vuelve a esta sección.")
        return False
    return True


def _mac_firewall_entrante() -> bool:
    estado = subprocess.run([SFW, '--getglobalstate'], capture_output=True, text=True).stdout.lower()
    if 'enabled' in estado and 'disabled' not in estado:
        return True
    warn("El firewall de macOS (conexiones entrantes) está apagado: sin él, el bloqueo de entrada no tiene efecto.")
    if confirm("¿Activarlo?"):
        return run_logged([SFW, '--setglobalstate', 'on'], 'Activar firewall de macOS')
    return False


def _globs_mac_todos_los_usuarios(patrones) -> list:
    """VENDORS[...]['mac'] trae rutas bajo el HOME de quien corre el script (con sudo puede ser
    /var/root). Se replican para cada perfil de /Users."""
    home = str(HOME)
    usuarios = [u for u in Path('/Users').iterdir()
                if u.is_dir() and u.name not in ('Shared', 'Guest') and not u.name.startswith('.')]
    salida = []
    for pat in map(str, patrones):
        if pat.startswith(home + '/'):
            salida.extend(str(u) + pat[len(home):] for u in usuarios)
        else:
            salida.append(pat)
    return salida


def _aislar_mac(dry_run: bool):
    if not _mac_prerrequisitos():
        return
    if not dry_run:
        _mac_firewall_entrante()
    seleccion = _elegir_proveedores('¿Qué proveedores aislar de internet?')
    patrones = []
    for vid in seleccion:
        perfil = VENDORS[vid].get('mac', {})
        patrones += perfil.get('app_globs', []) + perfil.get('dir_globs', [])
    solicitud = CENTINELA_DIR / "_aislar_solicitud.json"
    CENTINELA_DIR.mkdir(parents=True, exist_ok=True)
    solicitud.write_text(json.dumps(_globs_mac_todos_los_usuarios(patrones)), encoding="utf-8")

    info("\nBuscando ejecutables (puede tardar)...")
    r = _mac_helper("aislar", str(solicitud), capturar=True)
    lineas = r.stdout.splitlines()
    total = next((int(l.split()[1]) for l in lineas if l.startswith('TOTAL ')), 0)
    for l in lineas:
        if not l.startswith('TOTAL '):
            print(l)
    if r.returncode != 0:
        err((r.stderr or '').strip() or "El escaneo falló.")
        return
    if not total:
        ok("No se encontraron ejecutables en las carpetas de los proveedores seleccionados.")
        return
    info(f"{total} ejecutable(s) encontrado(s).")
    if dry_run:
        ok("Simulación completa. Sin cambios realizados.")
        return
    if not confirm(f"¿Bloquear entrada y salida de internet para los {total} ejecutable(s)?"):
        info("Cancelado.")
        return
    info("LuLu se recarga al final: la red queda ~8 s sin filtrar.")
    if _mac_helper("aislar", str(solicitud), "--real").returncode == 0:
        info("Las carpetas de estos proveedores quedan como zonas aprobadas. Activa la vigilancia en la opción 4.")


def _uid_consola():
    try:
        uid = os.stat('/dev/console').st_uid
        return uid if uid != 0 else None
    except OSError:
        return None


def _vigilancia_mac():
    import plistlib
    if not _mac_prerrequisitos():
        return
    _mac_firewall_entrante()
    info("Cortando la red de la app de LuLu para que no se actualice sola...")
    _mac_helper("proteger-lulu")

    python, log = _python_mac(), str(CENTINELA_DIR / "launchd.log")
    daemon = {
        'Label': 'com.ensamble.centinela.detectar',
        'ProgramArguments': [python, str(CENTINELA_MAC_PY), 'detectar'],
        'StartCalendarInterval': [{'Weekday': 1, 'Hour': 13, 'Minute': 30},
                                  {'Weekday': 5, 'Hour': 13, 'Minute': 30}],
        'StandardOutPath': log, 'StandardErrorPath': log,
    }
    agente = {
        'Label': 'com.ensamble.centinela.revisar',
        'ProgramArguments': [python, str(CENTINELA_MAC_PY), 'revisar'],
        'RunAtLoad': True,
        'LimitLoadToSessionType': 'Aqua',
    }
    for ruta, datos in ((MAC_DAEMON, daemon), (MAC_AGENTE, agente)):
        ruta.write_bytes(plistlib.dumps(datos))
        os.chown(ruta, 0, 0)
        os.chmod(ruta, 0o644)

    subprocess.run(['launchctl', 'bootout', 'system/com.ensamble.centinela.detectar'], capture_output=True)
    ok_d = run_logged(['launchctl', 'bootstrap', 'system', str(MAC_DAEMON)], 'Cargar la detección (LaunchDaemon)')
    uid = _uid_consola()
    if uid:
        subprocess.run(['launchctl', 'bootout', f'gui/{uid}/com.ensamble.centinela.revisar'], capture_output=True)
        run_logged(['launchctl', 'bootstrap', f'gui/{uid}', str(MAC_AGENTE)], 'Cargar la pantalla (LaunchAgent)')
    if ok_d:
        ok("Vigilancia activa: lunes y viernes 13:30. La pantalla sale sola si hay algo que revisar.")
        if confirm("¿Correr la primera detección ahora?"):
            _mac_helper("detectar")


def _vigilancia_win():
    _centinela_preparar_win()
    ps1 = str(CENTINELA_PS1).replace("'", "''")
    r = _ps_archivo(f"""
$ErrorActionPreference = 'Stop'
$ps1 = '{ps1}'
# Nombres traducidos desde el SID: 'SYSTEM' y 'Administradores' cambian con el idioma.
$sys = (New-Object Security.Principal.SecurityIdentifier 'S-1-5-18').Translate([Security.Principal.NTAccount]).Value
$adm = (New-Object Security.Principal.SecurityIdentifier 'S-1-5-32-544').Translate([Security.Principal.NTAccount]).Value
$set = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 4)

$a1 = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $ps1 + '" -Modo detectar')
$t1 = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Friday -At '13:30'
$p1 = New-ScheduledTaskPrincipal -UserId $sys -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName '{TAREA_DETECTAR}' -Action $a1 -Trigger $t1 -Principal $p1 -Settings $set -Force | Out-Null

# Asignada al grupo, no a una persona: corre en la sesión de cualquier administrador conectado.
$a2 = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $ps1 + '" -Modo revisar')
$t2 = New-ScheduledTaskTrigger -AtLogOn
$p2 = New-ScheduledTaskPrincipal -GroupId $adm -RunLevel Highest
Register-ScheduledTask -TaskName '{TAREA_REVISAR}' -Action $a2 -Trigger $t2 -Principal $p2 -Settings $set -Force | Out-Null

Unregister-ScheduledTask -TaskName '{TAREA_VIEJA}' -Confirm:$false -ErrorAction SilentlyContinue
'OK'
""")
    if r.returncode != 0 or 'OK' not in r.stdout:
        err(f"No se pudieron crear las tareas: {(r.stderr or r.stdout).strip()}")
        return
    ok(f"Tareas '{TAREA_DETECTAR}' (lunes y viernes 13:30, SYSTEM) y '{TAREA_REVISAR}' creadas.")
    info(f"La tarea anterior '{TAREA_VIEJA}' se retiró: la detección ya restaura las reglas borradas.")
    if confirm("¿Correr la primera detección ahora? (la ventana sale al terminar si hay algo)"):
        _ps_archivo(f"Start-ScheduledTask -TaskName '{TAREA_DETECTAR}'")
        info("Detección en curso, en segundo plano.")


# ─── Opciones del submenú ───

def seccion_centinela_baseline():
    title("AISLADOR + CENTINELA · CREAR BASELINE")
    warn("El baseline es la foto de lo que se considera legítimo en este equipo. Tómalo con el")
    warn("equipo limpio: si ya hay algo malicioso instalado, quedará registrado como normal.")
    if IS_MAC and not _mac_prerrequisitos():
        return
    if _cj_leer("pendientes.json", []):
        warn("Hay cambios pendientes de revisar. Un baseline nuevo los daría por buenos sin que nadie los vea.")
        if not confirm("¿Crear el baseline de todos modos?"):
            return
    if (CENTINELA_DIR / "baseline.json").exists():
        if not confirm("Ya existe un baseline. ¿Reemplazarlo?"):
            return
    elif not confirm("¿Crear el baseline ahora? Puede tardar varios minutos."):
        return
    exito = _centinela_ps("baseline") if IS_WIN else _mac_helper("baseline").returncode == 0
    if exito:
        _cj_escribir("pendientes.json", [])
        ok("Baseline creado.")
    else:
        err("No se pudo crear el baseline (ver el motivo arriba).")


def seccion_aislar_software_profesional():
    title("AISLADOR + CENTINELA · AISLAR PROVEEDORES")
    warn("Política de oficina: ningún programa profesional ni sus dependencias pueden")
    warn("tener acceso a internet. Esto bloquea entrada y salida por firewall para cada")
    warn("ejecutable encontrado — no desinstala ni modifica nada del programa en sí.")

    dry_run = not confirm("\n¿Ejecutar en modo real? ('n' corre en modo dry-run / solo simulación)")
    if dry_run:
        info("[DRY-RUN] Se escaneará y reportará qué se bloquearía, sin tocar el firewall.")
    if IS_WIN:
        _aislar_win(dry_run)
    else:
        _aislar_mac(dry_run)


def seccion_centinela_whitelist():
    while True:
        title("AISLADOR + CENTINELA · WHITELIST")
        wl = _cj_leer("whitelist.json", [])
        if not wl:
            info("La whitelist está vacía.")
        for i, e in enumerate(wl, 1):
            print(f"  [{i}] {e['path']}   ({e.get('fecha', '')})")
        print("\n  [a] Agregar una ruta" + ("   [q] Quitar una entrada" if wl else "") + "   [0] Volver")
        op = ask("Opción", ["a", "0"] + (["q"] if wl else []))
        if op == "0":
            return
        if op == "a":
            ruta = input("\n  → Ruta completa del ejecutable: ").strip().strip('"')
            if not Path(ruta).is_file():
                err("No existe ese archivo.")
                continue
            if not confirm(f"¿Autorizar {ruta}? Se le quitan las reglas de bloqueo."):
                continue
            if IS_WIN:
                _whitelist_agregar_win(ruta)
            else:
                _mac_helper("whitelist-add", ruta)
        else:
            n = ask("Número a quitar", [str(i) for i in range(1, len(wl) + 1)])
            e = wl.pop(int(n) - 1)
            _cj_escribir("whitelist.json", wl)
            ok(f"Quitado de la whitelist: {e['path']}")
            if Path(e['path']).exists() and confirm("¿Bloquearlo ahora?"):
                if IS_WIN:
                    existentes = _reglas_existentes()
                    _bloquear_exe_firewall(e['path'], existentes)
                    if all(_rule_name(e['path'], d) in existentes for d in ('out', 'in')):
                        _registrar_bloqueados_win([e['path']], "centinela")
                        ok("Bloqueado.")
                else:
                    _mac_helper("bloquear", e['path'])


def seccion_centinela_vigilancia():
    title("AISLADOR + CENTINELA · VIGILANCIA AUTOMÁTICA")
    if not (CENTINELA_DIR / "baseline.json").exists():
        err("Primero crea el baseline (opción 1).")
        return
    info("Lunes y viernes a las 13:30 (si el equipo está apagado, al encenderlo en Windows):")
    info("  · restaura las reglas de bloqueo que alguien haya borrado;")
    info("  · compara contra el baseline; lo nuevo dentro de una zona aprobada queda bloqueado")
    info("    provisionalmente;")
    info("  · si hay algo, abre sola una ventana con la lista numerada: los números que elijas")
    info("    quedan bloqueados y lo demás pasa a la whitelist. Si la cierras sin responder,")
    info("    vuelve a salir al iniciar sesión o el siguiente lunes o viernes.")
    if IS_MAC:
        info("  · en Mac además avisa si LuLu dejó de filtrar, o si cambió LuLu o macOS.")
    if not confirm("¿Activar la vigilancia automática?"):
        return
    if IS_WIN:
        _vigilancia_win()
    else:
        _vigilancia_mac()


def seccion_centinela_rollback():
    title("AISLADOR + CENTINELA · ROLLBACK")
    warn("Quita TODAS las reglas de bloqueo del aislador y del centinela y apaga la vigilancia.")
    info("El baseline y la whitelist se conservan.")
    if not confirm("¿Continuar?"):
        return
    if IS_WIN:
        r = _ps_archivo(
            f"Get-NetFirewallRule -DisplayName '{REGLA_PREFIJO}*' -ErrorAction SilentlyContinue | Remove-NetFirewallRule\n"
            f"foreach ($t in @('{TAREA_DETECTAR}', '{TAREA_REVISAR}', '{TAREA_VIEJA}')) "
            "{ Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction SilentlyContinue }\n")
        if r.returncode != 0:
            err(f"El rollback falló: {(r.stderr or r.stdout).strip()}")
            return
        for nombre in ("bloqueados.json", "zonas.json", "pendientes.json"):
            _cj_escribir(nombre, [])
        _cj_escribir("estado.json", {})
    else:
        if _mac_helper("rollback").returncode != 0:
            err("El rollback de reglas falló (ver arriba). La vigilancia no se tocó.")
            return
        subprocess.run(['launchctl', 'bootout', 'system/com.ensamble.centinela.detectar'], capture_output=True)
        uid = _uid_consola()
        if uid:
            subprocess.run(['launchctl', 'bootout', f'gui/{uid}/com.ensamble.centinela.revisar'], capture_output=True)
        for ruta in (MAC_DAEMON, MAC_AGENTE):
            ruta.unlink(missing_ok=True)
        info("La regla que impedía que LuLu se actualice también se quitó.")
    ok("Rollback completo.")


def seccion_aislador_centinela():
    _submenu("INSTALACIÓN · AISLADOR + CENTINELA", {
        "1": ("Crear baseline (foto del equipo limpio)", seccion_centinela_baseline),
        "2": ("Aislar proveedores de internet", seccion_aislar_software_profesional),
        "3": ("Whitelist", seccion_centinela_whitelist),
        "4": ("Activar vigilancia automática (lunes y viernes 13:30)", seccion_centinela_vigilancia),
        "5": ("Rollback: quitar bloqueos y vigilancia", seccion_centinela_rollback),
    })


# ─── Scripts que corren las tareas programadas (se escriben a disco al usarse la sección) ───

CENTINELA_PS1_CODIGO = r'''# centinela.ps1 — generado por EnsambleSetup (Instalación → Aislador + Centinela).
# No editar a mano: se sobrescribe cada vez que se usa la sección.
#   -Modo baseline  foto del equipo (lo llama EnsambleSetup)
#   -Modo detectar  tarea EnsambleCentinelaDetectar, como SYSTEM, lunes y viernes 13:30
#   -Modo revisar   tarea EnsambleCentinelaRevisar, en la sesión de un administrador
#   -Modo pantalla  la ventana con la lista numerada (la abre 'revisar')
param([ValidateSet('baseline', 'detectar', 'revisar', 'pantalla')][string]$Modo = 'detectar')

$ErrorActionPreference = 'Continue'
$Base = 'C:\ProgramData\EnsambleSetup\centinela'
$Prefijo = 'EnsambleAislar:'
$TareaRevisar = 'EnsambleCentinelaRevisar'
# Ruido conocido que no se vigila: la plataforma de Defender cambia de carpeta en cada
# actualización, los alias de WindowsApps no son el ejecutable real, y Package Cache y Temp
# son instaladores de paso.
$Excluir = '\\Windows Defender|\\WindowsApps\\|\\Package Cache\\|\\AppData\\Local\\Temp\\|\\EnsambleSetup\\'
$Utf8 = New-Object System.Text.UTF8Encoding($false)

function Ahora { Get-Date -Format s }

function Log([string]$m) {
    try { [IO.File]::AppendAllText((Join-Path $Base 'centinela.log'), ('{0} [{1}] {2}' -f (Ahora), $Modo, $m) + "`r`n", $Utf8) } catch {}
}

# El ',' evita que PowerShell desenrolle un arreglo de un elemento (o vacío) al devolverlo.
function Leer([string]$nombre, $defecto) {
    $p = Join-Path $Base $nombre
    if (-not (Test-Path -LiteralPath $p)) { return ,$defecto }
    try {
        $t = [IO.File]::ReadAllText($p)
        if (-not $t.Trim()) { return ,$defecto }
        return ,(ConvertFrom-Json $t)
    } catch { Log "No se pudo leer ${nombre}: $_"; return ,$defecto }
}

function Escribir([string]$nombre, $datos) {
    $json = ConvertTo-Json -InputObject $datos -Depth 6
    if ($null -eq $json) { $json = '[]' }
    [IO.File]::WriteAllText((Join-Path $Base $nombre), $json, $Utf8)
}

function Alerta([string]$nivel, [string]$texto) { [pscustomobject]@{ nivel = $nivel; texto = $texto } }

function Guardar-Estado($alertas) {
    Escribir 'estado.json' ([pscustomobject]@{ fecha = (Ahora); alertas = @($alertas) })
}

# ─── Qué se vigila ───

function Perfiles {
    Get-ChildItem -LiteralPath 'C:\Users' -Directory -Force -ErrorAction SilentlyContinue |
        Where-Object { @('public', 'default', 'default user', 'all users') -notcontains $_.Name.ToLower() }
}

function Carpetas-Vigiladas {
    $c = @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:ProgramData)
    foreach ($u in Perfiles) {
        $c += Join-Path $u.FullName 'AppData\Local'
        $c += Join-Path $u.FullName 'AppData\Roaming'
    }
    $c | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique
}

# Hashtable ruta → FileInfo. Las claves de @{} no distinguen mayúsculas, igual que Windows.
function Escanear-Exes {
    $r = @{}
    foreach ($raiz in (Carpetas-Vigiladas)) {
        Get-ChildItem -LiteralPath $raiz -Recurse -File -Filter '*.exe' -Force -ErrorAction SilentlyContinue |
            ForEach-Object { if ($_.FullName -notmatch $Excluir) { $r[$_.FullName] = $_ } }
    }
    return $r
}

function Hash([string]$p) {
    try { return (Get-FileHash -LiteralPath $p -Algorithm SHA256 -ErrorAction Stop).Hash.ToLower() } catch { return '' }
}

function Firmado-Microsoft([string]$p) {
    try {
        $s = Get-AuthenticodeSignature -LiteralPath $p -ErrorAction Stop
        return ($s.Status -eq 'Valid' -and $s.SignerCertificate.Subject -match 'O=Microsoft Corporation')
    } catch { return $false }
}

function En-Zona([string]$p, $zonas) {
    foreach ($z in $zonas) {
        if ($p.StartsWith($z.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { return $true }
    }
    return $false
}

# Cada entrada es una línea de texto; si la línea cambia, es una entrada nueva.
function Persistencia {
    $s = New-Object System.Collections.Generic.List[string]
    $claves = @(
        'HKEY_LOCAL_MACHINE\Software\Microsoft\Windows\CurrentVersion\Run',
        'HKEY_LOCAL_MACHINE\Software\Microsoft\Windows\CurrentVersion\RunOnce',
        'HKEY_LOCAL_MACHINE\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run',
        'HKEY_LOCAL_MACHINE\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\RunOnce')
    # Solo los perfiles con sesión cargada aparecen en HKEY_USERS.
    foreach ($h in (Get-ChildItem -LiteralPath 'Registry::HKEY_USERS' -ErrorAction SilentlyContinue)) {
        if ($h.PSChildName -match '^S-1-5-21-[\d-]+$') {
            $claves += "HKEY_USERS\$($h.PSChildName)\Software\Microsoft\Windows\CurrentVersion\Run"
            $claves += "HKEY_USERS\$($h.PSChildName)\Software\Microsoft\Windows\CurrentVersion\RunOnce"
        }
    }
    foreach ($k in $claves) {
        $item = Get-ItemProperty -LiteralPath "Registry::$k" -ErrorAction SilentlyContinue
        if (-not $item) { continue }
        foreach ($p in $item.PSObject.Properties) {
            if ($p.Name -notmatch '^PS(Path|ParentPath|ChildName|Drive|Provider)$') { $s.Add("run|$k|$($p.Name)|$($p.Value)") }
        }
    }
    foreach ($t in (Get-ScheduledTask -ErrorAction SilentlyContinue)) {
        if ($t.TaskName -like 'Ensamble*') { continue }
        $acc = (@($t.Actions) | ForEach-Object { ('' + $_.Execute + ' ' + $_.Arguments).Trim() }) -join ' ; '
        # Las tareas propias de Windows cambian con cada actualización: se ignoran solo si apuntan
        # a C:\Windows o son handlers COM. Una tarea bajo \Microsoft\ que ejecute algo de otra
        # carpeta sí se reporta: es un disfraz clásico.
        if ($t.TaskPath -like '\Microsoft\*' -and ($acc -eq '' -or $acc -match '^"?(%windir%|%SystemRoot%|C:\\Windows)\\')) { continue }
        $s.Add("tarea|$($t.TaskPath)$($t.TaskName)|$acc")
    }
    foreach ($sv in (Get-CimInstance Win32_Service -ErrorAction SilentlyContinue)) {
        $pn = '' + $sv.PathName
        if ($pn -match '^"?(%SystemRoot%|%windir%|C:\\Windows)\\') { continue }
        $s.Add("servicio|$($sv.Name)|$pn")
    }
    $inicio = @('C:\ProgramData\Microsoft\Windows\Start Menu\Programs\StartUp')
    foreach ($u in Perfiles) { $inicio += Join-Path $u.FullName 'AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup' }
    foreach ($d in $inicio) {
        Get-ChildItem -LiteralPath $d -File -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -ne 'desktop.ini' } | ForEach-Object { $s.Add("inicio|$($_.FullName)") }
    }
    return ,[string[]]@($s | Where-Object { $_ -notmatch $Excluir })
}

function Exe-De([string]$firma) {
    $t = [Environment]::ExpandEnvironmentVariables($firma)
    if ($t -match '([A-Za-z]:\\[^"|*?<>]*?\.exe)') { return $Matches[1] }
    return ''
}

# ─── Baseline: ejecutables en TSV (ConvertFrom-Json de PowerShell 5.1 es lento con miles de
# ─── objetos), persistencia en baseline.json

function Cargar-BaseExes {
    $r = @{}
    $f = Join-Path $Base 'baseline_exes.tsv'
    if (Test-Path -LiteralPath $f) {
        foreach ($l in [IO.File]::ReadAllLines($f)) {
            $c = $l.Split("`t")
            if ($c.Count -eq 4) { $r[$c[0]] = [pscustomobject]@{ h = $c[1]; s = [int64]$c[2]; m = [int64]$c[3] } }
        }
    }
    return $r
}

function Guardar-BaseExes($r) {
    $lineas = foreach ($k in $r.Keys) { $v = $r[$k]; "$k`t$($v.h)`t$($v.s)`t$($v.m)" }
    [IO.File]::WriteAllLines((Join-Path $Base 'baseline_exes.tsv'), [string[]]@($lineas), $Utf8)
}

# ─── Firewall ───
# Get-NetFirewallRule -DisplayName acepta comodines, y '[out]' es un comodín de UN carácter:
# buscar el nombre exacto nunca encuentra la regla. Por eso se trae todo el prefijo y se compara.

function Reglas-Existentes {
    $h = @{}
    Get-NetFirewallRule -DisplayName "$Prefijo*" -ErrorAction SilentlyContinue | ForEach-Object { $h[$_.DisplayName] = $true }
    return $h
}

function Nombre-Regla([string]$p, [string]$dir) { "$Prefijo $p [$dir]" }

# Crea las reglas que falten. Devuelve cuántas creó, o -1 si alguna falló.
function Bloquear([string]$p, $existentes) {
    $creadas = 0
    foreach ($d in 'out', 'in') {
        $n = Nombre-Regla $p $d
        if ($existentes.ContainsKey($n)) { continue }
        $dir = if ($d -eq 'out') { 'Outbound' } else { 'Inbound' }
        try {
            New-NetFirewallRule -DisplayName $n -Direction $dir -Program $p -Action Block -Profile Any -ErrorAction Stop | Out-Null
            $existentes[$n] = $true
            $creadas++
        } catch { Log "No se pudo crear '$n': $_"; return -1 }
    }
    return $creadas
}

function Desbloquear($rutas) {
    $nombres = @{}
    foreach ($p in $rutas) { $nombres[(Nombre-Regla $p 'out')] = $true; $nombres[(Nombre-Regla $p 'in')] = $true }
    Get-NetFirewallRule -DisplayName "$Prefijo*" -ErrorAction SilentlyContinue |
        Where-Object { $nombres.ContainsKey($_.DisplayName) } | Remove-NetFirewallRule -ErrorAction SilentlyContinue
}

function Quitar-De($lista, [string]$p) {
    for ($j = $lista.Count - 1; $j -ge 0; $j--) { if ($lista[$j].path -eq $p) { $lista.RemoveAt($j) } }
}

# ─── Modos ───

function Modo-Baseline {
    Write-Host '  Buscando ejecutables en las carpetas vigiladas...'
    $exes = Escanear-Exes
    $total = $exes.Count
    $i = 0
    $r = @{}
    foreach ($f in $exes.Values) {
        $i++
        if ($i % 250 -eq 0) { Write-Host ('  {0}/{1} ejecutables con hash...' -f $i, $total) }
        $r[$f.FullName] = [pscustomobject]@{ h = (Hash $f.FullName); s = $f.Length; m = $f.LastWriteTimeUtc.Ticks }
    }
    Guardar-BaseExes $r
    Write-Host '  Registrando persistencia (inicio automático, tareas, servicios)...'
    $pers = @(Persistencia)
    Escribir 'baseline.json' ([pscustomobject]@{ creado = (Ahora); equipo = $env:COMPUTERNAME; exes = $total; persistencia = $pers })
    Log "Baseline: $total ejecutables, $($pers.Count) entradas de persistencia."
    Write-Host ('  {0} ejecutables y {1} entradas de persistencia registrados.' -f $total, $pers.Count)
}

function Modo-Detectar {
    $alertas = New-Object System.Collections.Generic.List[object]

    $apagados = @(Get-NetFirewallProfile -ErrorAction SilentlyContinue | Where-Object { -not $_.Enabled } | ForEach-Object { $_.Name })
    if ($apagados.Count) {
        $alertas.Add((Alerta 'grave' "El firewall de Windows está apagado en: $($apagados -join ', '). Mientras siga así, ningún bloqueo tiene efecto."))
    }

    # Reafirmación: lo que ya estaba bloqueado y alguien desbloqueó a mano vuelve a bloquearse.
    $existentes = Reglas-Existentes
    $bloq = @(Leer 'bloqueados.json' @())
    $bloqSet = @{}
    $restauradas = 0
    $fallidas = 0
    foreach ($b in $bloq) {
        $bloqSet[$b.path] = $true
        if (Test-Path -LiteralPath $b.path) {
            $n = Bloquear $b.path $existentes
            if ($n -gt 0) { $restauradas += $n } elseif ($n -lt 0) { $fallidas++ }
        }
    }
    if ($restauradas) { $alertas.Add((Alerta 'info' "Se restauraron $restauradas regla(s) de bloqueo que alguien había borrado.")) }
    if ($fallidas) { $alertas.Add((Alerta 'grave' "No se pudieron recrear las reglas de $fallidas ejecutable(s) bloqueado(s). Revisa $Base\centinela.log.")) }

    if (-not (Test-Path -LiteralPath (Join-Path $Base 'baseline_exes.tsv'))) {
        $alertas.Add((Alerta 'grave' 'No hay baseline: la detección de cambios no corre. Créalo en EnsambleSetup → Instalación → Aislador + Centinela.'))
        Guardar-Estado $alertas
        Start-ScheduledTask -TaskName $TareaRevisar -ErrorAction SilentlyContinue
        return
    }

    $bl = Leer 'baseline.json' $null
    $base = Cargar-BaseExes
    $wl = @{}
    foreach ($w in @(Leer 'whitelist.json' @())) { $wl[$w.path] = $true }
    $zonas = @(Leer 'zonas.json' @())
    $pend = New-Object System.Collections.Generic.List[object]
    foreach ($x in @(Leer 'pendientes.json' @())) { $pend.Add($x) }
    $ids = @{}
    foreach ($x in $pend) { $ids[$x.id] = $true }
    $nuevosBloq = New-Object System.Collections.Generic.List[object]
    $nuevos = 0

    foreach ($f in (Escanear-Exes).Values) {
        $p = $f.FullName
        if ($bloqSet.ContainsKey($p)) { continue }   # ya bloqueado: que cambie no importa
        $b = $base[$p]
        $tipo = $null
        $h = ''
        if ($null -eq $b) {
            if ($wl.ContainsKey($p)) { continue }
            $tipo = 'NUEVO'
        } elseif ($b.s -ne $f.Length -or $b.m -ne $f.LastWriteTimeUtc.Ticks) {
            $h = Hash $p
            if ($h -and $h -ne $b.h) { $tipo = 'MODIFICADO' }
        }
        if (-not $tipo) { continue }
        $id = "$tipo|$p"
        if ($ids.ContainsKey($id)) { continue }
        $enZona = En-Zona $p $zonas
        # Fuera de zona, lo firmado por Microsoft es Windows actualizándose: no se reporta.
        # Dentro de zona sí (Office es de Microsoft y está en la política de aislamiento).
        if (-not $enZona -and (Firmado-Microsoft $p)) { continue }
        if (-not $h) { $h = Hash $p }
        $prov = $false
        if ($enZona -and -not $wl.ContainsKey($p)) {
            if ((Bloquear $p $existentes) -ge 0) {
                $prov = $true
                $bloqSet[$p] = $true
                $nuevosBloq.Add([pscustomobject]@{ path = $p; origen = 'provisional'; fecha = (Ahora) })
            }
        }
        $pend.Add([pscustomobject]@{ id = $id; tipo = $tipo; path = $p; detalle = ''; sha256 = $h; en_zona = $enZona; provisional = $prov; fecha = (Ahora) })
        $ids[$id] = $true
        $nuevos++
    }

    $basePers = @{}
    if ($bl) { foreach ($x in @($bl.persistencia)) { if ($x) { $basePers[$x] = $true } } }
    foreach ($x in @(Persistencia)) {
        if ($basePers.ContainsKey($x)) { continue }
        $id = "PERSISTENCIA|$x"
        if ($ids.ContainsKey($id)) { continue }
        $pend.Add([pscustomobject]@{ id = $id; tipo = 'PERSISTENCIA'; path = (Exe-De $x); detalle = $x; sha256 = ''; en_zona = $false; provisional = $false; fecha = (Ahora) })
        $ids[$id] = $true
        $nuevos++
    }

    if ($nuevosBloq.Count) { Escribir 'bloqueados.json' (@($bloq) + @($nuevosBloq)) }
    Escribir 'pendientes.json' $pend
    Guardar-Estado $alertas
    Log "Detección: $nuevos nuevo(s), $($nuevosBloq.Count) bloqueado(s) provisionalmente, $($pend.Count) pendiente(s) en total."
    if ($pend.Count -or $alertas.Count) { Start-ScheduledTask -TaskName $TareaRevisar -ErrorAction SilentlyContinue }
}

# Corre oculto: si hay algo que mostrar abre la ventana; si no, termina sin que se note.
function Modo-Revisar {
    $pend = @(Leer 'pendientes.json' @())
    $est = Leer 'estado.json' $null
    $alertas = @()
    if ($est) { $alertas = @($est.alertas | Where-Object { $_ }) }
    if (-not $pend.Count -and -not $alertas.Count) { return }
    Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '-Modo', 'pantalla')
}

function Aplicar($pend, $elegidos) {
    $existentes = Reglas-Existentes
    $bloq = New-Object System.Collections.Generic.List[object]
    foreach ($b in @(Leer 'bloqueados.json' @())) { $bloq.Add($b) }
    $wl = New-Object System.Collections.Generic.List[object]
    foreach ($w in @(Leer 'whitelist.json' @())) { $wl.Add($w) }
    $bl = Leer 'baseline.json' $null
    $pers = New-Object System.Collections.Generic.List[string]
    if ($bl) { foreach ($x in @($bl.persistencia)) { if ($x) { $pers.Add($x) } } }
    $base = Cargar-BaseExes
    $desbloquear = New-Object System.Collections.Generic.List[string]
    $mostrados = @{}

    for ($i = 0; $i -lt $pend.Count; $i++) {
        $x = $pend[$i]
        $p = '' + $x.path
        $mostrados[$x.id] = $true
        if ($elegidos.ContainsKey($i)) {
            if ($p -and (Test-Path -LiteralPath $p)) {
                if ((Bloquear $p $existentes) -lt 0) { Write-Host "  No se pudo bloquear $p (ver centinela.log)" -ForegroundColor Red }
                Quitar-De $wl $p
                Quitar-De $bloq $p
                $bloq.Add([pscustomobject]@{ path = $p; origen = 'centinela'; fecha = (Ahora) })
            }
        } elseif ($p -and $x.tipo -ne 'PERSISTENCIA') {
            if ($x.provisional) { $desbloquear.Add($p) }
            Quitar-De $bloq $p
            Quitar-De $wl $p
            $wl.Add([pscustomobject]@{ path = $p; sha256 = $x.sha256; fecha = (Ahora) })
        }
        if ($x.tipo -eq 'PERSISTENCIA') {
            $pers.Add($x.detalle)
        } elseif ($p -and (Test-Path -LiteralPath $p)) {
            $f = Get-Item -LiteralPath $p -Force
            $h = if ($x.sha256) { $x.sha256 } else { Hash $p }
            $base[$p] = [pscustomobject]@{ h = $h; s = $f.Length; m = $f.LastWriteTimeUtc.Ticks }
        }
    }
    if ($desbloquear.Count) { Desbloquear $desbloquear }

    Escribir 'bloqueados.json' $bloq
    Escribir 'whitelist.json' $wl
    Guardar-BaseExes $base
    if ($bl) { $bl.persistencia = [string[]]$pers.ToArray(); Escribir 'baseline.json' $bl }
    # Si la detección corrió mientras la ventana estaba abierta, lo que agregó se conserva.
    $restantes = @(@(Leer 'pendientes.json' @()) | Where-Object { -not $mostrados.ContainsKey($_.id) })
    Escribir 'pendientes.json' $restantes
    Log ('Revisión: {0} bloqueado(s), {1} a whitelist.' -f $elegidos.Count, ($pend.Count - $elegidos.Count))
}

function Modo-Pantalla {
    $mutex = New-Object System.Threading.Mutex($false, 'Global\EnsambleCentinelaPantalla')
    if (-not $mutex.WaitOne(0)) { return }   # ya hay una ventana abierta
    try {
        $Host.UI.RawUI.WindowTitle = 'Ensamble · Centinela'
        Write-Host ''
        Write-Host '  ══════════════════════════════════════════════════════' -ForegroundColor Cyan
        Write-Host '   CENTINELA · Cambios en este equipo desde el baseline' -ForegroundColor Cyan
        Write-Host '  ══════════════════════════════════════════════════════' -ForegroundColor Cyan

        $est = Leer 'estado.json' $null
        $alertas = @()
        if ($est) { $alertas = @($est.alertas | Where-Object { $_ }) }
        foreach ($a in $alertas) {
            $color = if ($a.nivel -eq 'grave') { 'Red' } else { 'Yellow' }
            Write-Host ''
            Write-Host "  ⚠  $($a.texto)" -ForegroundColor $color
        }
        # Los avisos informativos se muestran una vez; los graves siguen hasta que se corrijan.
        if ($alertas.Count) {
            Escribir 'estado.json' ([pscustomobject]@{ fecha = $est.fecha; alertas = @($alertas | Where-Object { $_.nivel -eq 'grave' }) })
        }

        $pend = @(Leer 'pendientes.json' @())
        if (-not $pend.Count) {
            Write-Host ''
            Read-Host '  Presiona Enter para cerrar' | Out-Null
            return
        }

        Write-Host ''
        Write-Host "  Aparecieron $($pend.Count) cambio(s):"
        Write-Host ''
        for ($i = 0; $i -lt $pend.Count; $i++) {
            $x = $pend[$i]
            $marca = if ($x.provisional) { '  [bloqueado provisionalmente]' } elseif ($x.en_zona) { '  [zona aprobada]' } else { '' }
            $ruta = if ($x.path) { $x.path } else { '(sin ejecutable identificable: solo aviso)' }
            Write-Host ('  [{0,2}] {1,-12} {2}{3}' -f ($i + 1), $x.tipo, $ruta, $marca)
            if ($x.tipo -eq 'PERSISTENCIA') { Write-Host ('        {0}' -f $x.detalle) -ForegroundColor DarkGray }
        }
        Write-Host ''
        Write-Host '  Escribe los números que quedan BLOQUEADOS (ej: 1,3,4).'
        Write-Host '  Lo que no elijas pasa a la whitelist y recupera la red.'
        Write-Host "  'ninguno': no se bloquea nada.   Enter vacío: decidir después."

        while ($true) {
            $r = ('' + (Read-Host '  →')).Trim().ToLower()
            if (-not $r) {
                Write-Host '  Queda pendiente. La ventana vuelve a salir al iniciar sesión o el próximo lunes o viernes.'
                Start-Sleep -Seconds 4
                return
            }
            $elegidos = @{}
            $valido = $true
            if ($r -ne 'ninguno') {
                foreach ($t in $r.Split(',')) {
                    $n = 0
                    if ([int]::TryParse($t.Trim(), [ref]$n) -and $n -ge 1 -and $n -le $pend.Count) { $elegidos[$n - 1] = $true } else { $valido = $false }
                }
            }
            if (-not $valido) {
                Write-Host "  Entrada inválida: números de la lista separados por coma, o 'ninguno'." -ForegroundColor Yellow
                continue
            }
            $c = Read-Host ('  Se bloquean {0}; pasan a whitelist {1}. ¿Confirmas? [s/n]' -f $elegidos.Count, ($pend.Count - $elegidos.Count))
            if (('' + $c).Trim().ToLower() -eq 's') { break }
            Write-Host '  Escribe de nuevo los números, o Enter vacío para decidir después.'
        }

        Aplicar $pend $elegidos
        Write-Host ''
        Write-Host '  Listo.' -ForegroundColor Green
        Start-Sleep -Seconds 3
    } finally {
        $mutex.ReleaseMutex()
        $mutex.Dispose()
    }
}

if (-not (Test-Path -LiteralPath $Base)) { New-Item -ItemType Directory -Path $Base -Force | Out-Null }
switch ($Modo) {
    'baseline' { Modo-Baseline }
    'detectar' { Modo-Detectar }
    'revisar' { Modo-Revisar }
    'pantalla' { Modo-Pantalla }
}
'''

CENTINELA_MAC_CODIGO = r'''#!/usr/bin/env python3
"""Centinela para macOS, generado por EnsambleSetup (Instalación → Aislador + Centinela).
No editar a mano: se sobrescribe cada vez que se usa la sección.

Salida a la red: LuLu, vía lulu-cli. Entrada: firewall de aplicaciones de macOS (socketfilterfw).
Compatible con Python 3.9 (el de las Command Line Tools de Apple).

  baseline                       foto del equipo (root)
  aislar <patrones.json> [--real]  bloquea los ejecutables de las carpetas de proveedores (root)
  detectar                       LaunchDaemon, lunes y viernes 13:30 (root)
  revisar                        LaunchAgent, en la sesión del usuario: muestra la lista
  aplicar <huella> <números>     lo llama 'revisar' con contraseña de administrador (root)
  bloquear <ruta> | whitelist-add <ruta> | proteger-lulu | rollback   (root)
"""

import fcntl
import glob
import hashlib
import json
import os
import plistlib
import shlex
import struct
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path('/Library/Application Support/EnsambleSetup/centinela')
LULU_CLI = '/usr/local/bin/lulu-cli'
LULU_APP = '/Applications/LuLu.app'
LULU_EXE = LULU_APP + '/Contents/MacOS/LuLu'
SFW = '/usr/libexec/ApplicationFirewall/socketfilterfw'
LABEL_AGENTE = 'com.ensamble.centinela.revisar'
TITULO = 'Centinela · Ensamble'
# LuLu no se vigila como ejecutable nuevo: su cambio de versión lo reporta salud().
EXCLUIR = (str(BASE), '/Library/Application Support/Apple/', LULU_APP + '/')


def ahora():
    return datetime.now().strftime('%Y-%m-%dT%H:%M:%S')


def log(m):
    try:
        with open(BASE / 'centinela.log', 'a', encoding='utf-8') as f:
            f.write(f'{ahora()} {m}\n')
    except OSError:
        pass


def leer(nombre, defecto):
    p = BASE / nombre
    try:
        return json.loads(p.read_text(encoding='utf-8')) if p.exists() else defecto
    except Exception as e:
        log(f'No se pudo leer {nombre}: {e}')
        return defecto


def escribir(nombre, datos):
    BASE.mkdir(parents=True, exist_ok=True)
    tmp = BASE / (nombre + '.tmp')
    tmp.write_text(json.dumps(datos, indent=2, ensure_ascii=False), encoding='utf-8')
    os.chmod(tmp, 0o644)   # 'revisar' corre como usuario y necesita leer
    tmp.replace(BASE / nombre)


def sha256(p):
    h = hashlib.sha256()
    try:
        with open(p, 'rb') as f:
            for bloque in iter(lambda: f.read(1 << 20), b''):
                h.update(bloque)
        return h.hexdigest()
    except OSError:
        return ''


def alerta(nivel, texto):
    return {'nivel': nivel, 'texto': texto}


def guardar_estado(alertas):
    escribir('estado.json', {'fecha': ahora(), 'alertas': alertas})


def usuarios():
    try:
        return [u for u in Path('/Users').iterdir()
                if u.is_dir() and u.name not in ('Shared', 'Guest') and not u.name.startswith('.')]
    except OSError:
        return []


def vigiladas():
    c = ['/Applications', '/Library/Application Support', '/Library/PrivilegedHelperTools']
    for u in usuarios():
        c += [str(u / 'Applications'), str(u / 'Library/Application Support')]
    return [x for x in c if os.path.isdir(x)]


def en_zona(p, zonas):
    return any(p == z or p.startswith(z.rstrip('/') + '/') for z in zonas)


# ─── Ejecutables Mach-O ───

_THIN = {b'\xfe\xed\xfa\xce': '>', b'\xce\xfa\xed\xfe': '<', b'\xfe\xed\xfa\xcf': '>', b'\xcf\xfa\xed\xfe': '<'}
MH_EXECUTE = 2


def _tipo_thin(cab):
    end = _THIN.get(cab[:4])
    return struct.unpack(end + 'I', cab[12:16])[0] if end and len(cab) >= 16 else None


def es_ejecutable(p):
    """Mach-O de tipo MH_EXECUTE con bit de ejecución. Deja fuera dylibs, plugins y scripts:
    a una librería no se le puede poner una regla de firewall, se le pone al proceso que la carga."""
    try:
        st = os.lstat(p)
        if (st.st_mode & 0o170000) != 0o100000 or not (st.st_mode & 0o111):
            return False
        with open(p, 'rb') as f:
            cab = f.read(32)
            t = _tipo_thin(cab)
            if t is not None:
                return t == MH_EXECUTE
            if cab[:4] in (b'\xca\xfe\xba\xbe', b'\xca\xfe\xba\xbf') and len(cab) >= 24:
                n = struct.unpack('>I', cab[4:8])[0]
                if not 0 < n < 20:   # 0xcafebabe también es la firma de un .class de Java
                    return False
                off = struct.unpack('>I', cab[16:20])[0] if cab[3] == 0xbe else struct.unpack('>Q', cab[16:24])[0]
                f.seek(off)
                return _tipo_thin(f.read(16)) == MH_EXECUTE
    except (OSError, struct.error):
        pass
    return False


def escanear(raices):
    r = {}
    for raiz in raices:
        if os.path.isfile(raiz):
            if es_ejecutable(raiz):
                r[raiz] = os.stat(raiz)
            continue
        for d, subdirs, archivos in os.walk(raiz, onerror=lambda e: None):
            if d.startswith(EXCLUIR):
                subdirs[:] = []
                continue
            for a in archivos:
                p = os.path.join(d, a)
                if es_ejecutable(p):
                    r[p] = os.lstat(p)
    return r


def firma(p):
    """(tipo, clave de LuLu). Replica Process.generateKey de LuLu 4.5.1: firma de Apple o de la
    App Store → id de firma; Developer ID → 'id:autoridad hoja'; sin firma válida → la ruta."""
    r = subprocess.run(['codesign', '-dvv', p], capture_output=True, text=True)
    ident, auths = None, []
    for linea in r.stderr.splitlines():
        if linea.startswith('Identifier='):
            ident = linea.split('=', 1)[1].strip()
        elif linea.startswith('Authority='):
            auths.append(linea.split('=', 1)[1].strip())
    if r.returncode != 0 or not ident or not auths:
        return 'ninguna', p
    if auths[0] == 'Software Signing':
        return 'apple', ident
    if auths[0] == 'Apple Mac OS Application Signing':
        return 'appstore', ident
    if auths[0].startswith('Developer ID Application:'):
        return 'devid', f'{ident}:{auths[0]}'
    return 'otra', p


def persistencia():
    s = []
    dirs = ['/Library/LaunchAgents', '/Library/LaunchDaemons'] + [str(u / 'Library/LaunchAgents') for u in usuarios()]
    for d in dirs:
        try:
            nombres = sorted(os.listdir(d))
        except OSError:
            continue
        for n in nombres:
            if n.endswith('.plist') and not n.startswith('com.ensamble.centinela'):
                p = os.path.join(d, n)
                s.append(f'plist|{p}|{sha256(p)}')
    return s


def exe_de_plist(firma_txt):
    try:
        with open(firma_txt.split('|')[1], 'rb') as f:
            d = plistlib.load(f)
        prog = d.get('Program') or (d.get('ProgramArguments') or [''])[0]
        return prog if prog and os.path.isfile(prog) else ''
    except Exception:
        return ''


# ─── LuLu + socketfilterfw ───

def lulu(*args):
    return subprocess.run([LULU_CLI, *args], capture_output=True, text=True)


def claves_bloqueadas():
    """Claves con una regla 'Block * *' en LuLu, o None si lulu-cli no pudo leer las reglas."""
    if not os.path.isfile(LULU_CLI):
        return None
    r = lulu('list')
    if r.returncode != 0:
        return None
    s, actual = set(), None
    for linea in r.stdout.splitlines():
        if linea.startswith('[') and linea.endswith(']'):
            actual = linea[1:-1]
        elif actual and '| Block |' in linea and 'addr=* port=*' in linea:
            s.add(actual)
    return s


def bloquear(rutas, origen, existentes):
    """Bloquea salida (LuLu) y entrada (socketfilterfw). No recarga LuLu: quien llama hace un
    solo reload al final, porque cada reload deja la red ~8 s sin filtrar.

    Con firma válida se crean DOS reglas en LuLu, por id de firma y por ruta: si la firma
    dejara de validar (binario alterado), LuLu pasa a identificar el proceso por su ruta."""
    hechos = []
    for p in rutas:
        _, clave = firma(p)
        claves = [clave] if clave == p else [clave, p]
        todo_ok = True
        for k in claves:
            if k in existentes:
                continue
            if lulu('add', '--key', k, '--path', p, '--action', 'block', '--addr', '*', '--port', '*').returncode == 0:
                existentes.add(k)
            else:
                todo_ok = False
                log(f'lulu-cli add falló para {k}')
        subprocess.run([SFW, '--add', p], capture_output=True)
        subprocess.run([SFW, '--blockapp', p], capture_output=True)
        if todo_ok:
            hechos.append({'path': p, 'claves': claves, 'origen': origen, 'fecha': ahora()})
    return hechos


def desbloquear(items, restantes):
    """Quita los bloqueos de items. Una clave de LuLu que otro bloqueado siga usando (dos
    ejecutables con el mismo id de firma) no se toca."""
    en_uso = {k for b in restantes for k in b.get('claves', [])}
    for b in items:
        for k in b.get('claves', []):
            if k not in en_uso:
                lulu('delete-match', '--key', k, '--action', 'block', '--addr', '*', '--port', '*')
        subprocess.run([SFW, '--unblockapp', b['path']], capture_output=True)
        subprocess.run([SFW, '--remove', b['path']], capture_output=True)


def recargar():
    ok = lulu('reload').returncode == 0
    time.sleep(10)   # macOS reinicia la extensión en ~8 s
    return ok


def fusionar(bloq, hechos):
    rutas = {h['path'] for h in hechos}
    return [b for b in bloq if b['path'] not in rutas] + hechos


# ─── Salud de LuLu (decisión 2026-09-28: si LuLu se cae, se avisa; no se intenta arreglar) ───

def extension_activa():
    r = subprocess.run(['systemextensionsctl', 'list'], capture_output=True, text=True)
    return any('com.objective-see.lulu' in l and '[activated enabled]' in l for l in r.stdout.splitlines())


def version_app(app):
    try:
        with open(os.path.join(app, 'Contents/Info.plist'), 'rb') as f:
            return plistlib.load(f).get('CFBundleShortVersionString', '')
    except Exception:
        return ''


def version_macos():
    return subprocess.run(['sw_vers', '-productVersion'], capture_output=True, text=True).stdout.strip()


def registrar_versiones():
    escribir('versiones.json', {'lulu': version_app(LULU_APP), 'macos': version_macos()})


def salud(alertas):
    if not os.path.isdir(LULU_APP):
        alertas.append(alerta('grave', 'LuLu no está instalado: la salida a internet de este Mac no se está filtrando.'))
        return
    if not extension_activa():
        alertas.append(alerta('grave', 'La extensión de red de LuLu no está activa: LuLu no está filtrando nada. '
                                       'Suele pasar después de actualizar macOS.'))
    fw = subprocess.run([SFW, '--getglobalstate'], capture_output=True, text=True).stdout.lower()
    if 'disabled' in fw or 'enabled' not in fw:
        alertas.append(alerta('grave', 'El firewall de macOS está apagado: los bloqueos de conexiones entrantes no tienen efecto.'))

    reg = leer('versiones.json', {})
    v_lulu, v_mac = version_app(LULU_APP), version_macos()
    if reg.get('lulu') and reg['lulu'] != v_lulu:
        alertas.append(alerta('grave', f"LuLu cambió de versión ({reg['lulu']} → {v_lulu}). lulu-cli escribe el archivo "
                                       'interno de reglas de LuLu: verifica que siga funcionando.'))
    if reg.get('macos') and reg['macos'] != v_mac:
        alertas.append(alerta('grave', f"macOS cambió de versión ({reg['macos']} → {v_mac}). Verifica que LuLu siga filtrando."))
    registrar_versiones()

    existentes = claves_bloqueadas()
    if existentes is None:
        alertas.append(alerta('grave', 'lulu-cli no pudo leer las reglas de LuLu (¿falta, o cambió el formato con una '
                                       'actualización?). No se pueden crear ni restaurar bloqueos.'))
        return
    bloq = leer('bloqueados.json', [])
    faltan = [b for b in bloq if os.path.exists(b['path']) and any(k not in existentes for k in b.get('claves', []))]
    if faltan:
        bloquear([b['path'] for b in faltan], None, existentes)
        recargar()
        aun = claves_bloqueadas() or set()
        perdidas = [b for b in faltan if any(k not in aun for k in b.get('claves', []))]
        if perdidas:
            alertas.append(alerta('grave', f'No se pudieron restaurar las reglas de {len(perdidas)} ejecutable(s) en LuLu.'))
        else:
            alertas.append(alerta('info', f'Se restauraron las reglas de {len(faltan)} ejecutable(s) que ya no estaban en LuLu.'))


# ─── Comandos (root) ───

def cmd_baseline():
    print('  Buscando ejecutables en las carpetas vigiladas...')
    exes = escanear(vigiladas())
    datos = {}
    for i, (p, st) in enumerate(exes.items(), 1):
        if i % 250 == 0:
            print(f'  {i}/{len(exes)} ejecutables con hash...')
        datos[p] = {'h': sha256(p), 's': st.st_size, 'm': int(st.st_mtime)}
    print('  Registrando persistencia (LaunchAgents y LaunchDaemons)...')
    pers = persistencia()
    escribir('baseline_exes.json', datos)
    escribir('baseline.json', {'creado': ahora(), 'equipo': os.uname().nodename, 'exes': len(datos), 'persistencia': pers})
    log(f'Baseline: {len(datos)} ejecutables, {len(pers)} entradas de persistencia.')
    print(f'  {len(datos)} ejecutables y {len(pers)} entradas de persistencia registrados.')


def cmd_aislar(archivo, real):
    patrones = json.loads(Path(archivo).read_text(encoding='utf-8'))
    raices = sorted({p for pat in patrones for p in glob.glob(pat)})
    wl = {w['path'] for w in leer('whitelist.json', [])}
    exes = sorted(p for p in escanear(raices) if p not in wl)
    if not real:
        ya = {b['path'] for b in leer('bloqueados.json', [])}
        for p in exes:
            print(f'  Se bloquearía: {p}' + ('   (ya tenía regla)' if p in ya else ''))
        print(f'TOTAL {len(exes)}')
        return
    existentes = claves_bloqueadas()
    if existentes is None:
        sys.exit('  lulu-cli no pudo leer las reglas de LuLu. No se bloqueó nada.')
    print(f'  Bloqueando {len(exes)} ejecutable(s)...')
    hechos = bloquear(exes, 'proveedor', existentes)
    escribir('bloqueados.json', fusionar(leer('bloqueados.json', []), hechos))
    zonas = leer('zonas.json', [])
    escribir('zonas.json', sorted(set(zonas) | {r for r in raices if os.path.isdir(r)}))
    recargar()
    print(f'  {len(hechos)} de {len(exes)} ejecutable(s) quedaron bloqueados.')
    if len(hechos) < len(exes):
        print(f'  Los que faltan están en {BASE}/centinela.log. Se reintentan en la próxima detección.')


def cmd_bloquear(p):
    existentes = claves_bloqueadas()
    if existentes is None:
        sys.exit('  lulu-cli no pudo leer las reglas de LuLu.')
    hechos = bloquear([p], 'centinela', existentes)
    escribir('bloqueados.json', fusionar(leer('bloqueados.json', []), hechos))
    escribir('whitelist.json', [w for w in leer('whitelist.json', []) if w['path'] != p])
    recargar()
    print('  Bloqueado.' if hechos else '  No se pudo bloquear (ver centinela.log).')


def cmd_whitelist_add(p):
    bloq = leer('bloqueados.json', [])
    items = [b for b in bloq if b['path'] == p]
    restantes = [b for b in bloq if b['path'] != p]
    if not items:   # pudo haberse bloqueado fuera del registro: se quitan sus claves igual
        _, clave = firma(p)
        items = [{'path': p, 'claves': [clave] if clave == p else [clave, p]}]
    desbloquear(items, restantes)
    recargar()
    escribir('bloqueados.json', restantes)
    wl = [w for w in leer('whitelist.json', []) if w['path'] != p]
    wl.append({'path': p, 'sha256': sha256(p), 'fecha': ahora()})
    escribir('whitelist.json', wl)
    print('  Agregado a la whitelist.')


def cmd_proteger_lulu():
    """Regla que corta la red de la app de LuLu, para que no busque ni descargue versiones
    nuevas. El filtrado lo hace la extensión de red, no la app. Registra además las versiones
    de LuLu y macOS para avisar si cambian."""
    registrar_versiones()
    existentes = claves_bloqueadas()
    if existentes is None or not os.path.isfile(LULU_EXE):
        sys.exit('  No se pudo leer LuLu o lulu-cli.')
    hechos = bloquear([LULU_EXE], 'lulu-sin-actualizaciones', existentes)
    escribir('bloqueados.json', fusionar(leer('bloqueados.json', []), hechos))
    recargar()
    print('  LuLu queda sin acceso a internet (no se puede actualizar solo).' if hechos
          else '  No se pudo crear la regla (ver centinela.log).')


def cmd_rollback():
    bloq = leer('bloqueados.json', [])
    desbloquear(bloq, [])
    if bloq:
        recargar()
    for nombre, vacio in (('bloqueados.json', []), ('zonas.json', []), ('pendientes.json', []), ('estado.json', {})):
        escribir(nombre, vacio)
    print(f'  {len(bloq)} ejecutable(s) desbloqueados.')


def cmd_detectar():
    alertas = []
    salud(alertas)
    if not (BASE / 'baseline.json').exists():
        alertas.append(alerta('grave', 'No hay baseline: la detección de cambios no corre. '
                                       'Créalo en EnsambleSetup → Instalación → Aislador + Centinela.'))
        guardar_estado(alertas)
        avisar()
        return
    bl = leer('baseline.json', {})
    base = leer('baseline_exes.json', {})
    wl = {w['path'] for w in leer('whitelist.json', [])}
    zonas = leer('zonas.json', [])
    bloq = leer('bloqueados.json', [])
    bloq_set = {b['path'] for b in bloq}
    pend = leer('pendientes.json', [])
    ids = {x['id'] for x in pend}
    existentes = claves_bloqueadas() if extension_activa() else None
    nuevos_bloq, nuevos = [], 0

    for p, st in escanear(vigiladas()).items():
        if p in bloq_set:
            continue
        b, tipo, h = base.get(p), None, ''
        if b is None:
            if p in wl:
                continue
            tipo = 'NUEVO'
        elif b['s'] != st.st_size or b['m'] != int(st.st_mtime):
            h = sha256(p)
            if h and h != b['h']:
                tipo = 'MODIFICADO'
        if not tipo:
            continue
        id_ = f'{tipo}|{p}'
        if id_ in ids:
            continue
        zona = en_zona(p, zonas)
        # Fuera de zona, lo firmado por Apple es el sistema actualizándose: no se reporta.
        if not zona and firma(p)[0] == 'apple':
            continue
        h = h or sha256(p)
        prov = False
        if zona and p not in wl and existentes is not None:
            hechos = bloquear([p], 'provisional', existentes)
            if hechos:
                prov = True
                nuevos_bloq += hechos
                bloq_set.add(p)
        pend.append({'id': id_, 'tipo': tipo, 'path': p, 'detalle': '', 'sha256': h,
                     'en_zona': zona, 'provisional': prov, 'fecha': ahora()})
        ids.add(id_)
        nuevos += 1

    base_pers = set(bl.get('persistencia', []))
    for x in persistencia():
        id_ = 'PERSISTENCIA|' + x
        if x in base_pers or id_ in ids:
            continue
        pend.append({'id': id_, 'tipo': 'PERSISTENCIA', 'path': exe_de_plist(x), 'detalle': x.split('|')[1],
                     'firma': x, 'sha256': '', 'en_zona': False, 'provisional': False, 'fecha': ahora()})
        ids.add(id_)
        nuevos += 1

    if nuevos_bloq:
        escribir('bloqueados.json', fusionar(bloq, nuevos_bloq))
        recargar()
    escribir('pendientes.json', pend)
    guardar_estado(alertas)
    log(f'Detección: {nuevos} nuevo(s), {len(nuevos_bloq)} bloqueado(s) provisionalmente, {len(pend)} pendiente(s).')
    if pend or alertas:
        avisar()


def usuario_consola():
    try:
        uid = os.stat('/dev/console').st_uid
        return uid if uid != 0 else None
    except OSError:
        return None


def avisar():
    uid = usuario_consola()
    if uid:
        subprocess.run(['launchctl', 'kickstart', f'gui/{uid}/{LABEL_AGENTE}'], capture_output=True)


def cmd_aplicar(huella, lista):
    if sha256(BASE / 'pendientes.json') != huella:
        sys.exit('La lista cambió mientras la revisabas (corrió una detección). Vuelve a abrirse en el próximo aviso.')
    pend = leer('pendientes.json', [])
    elegidos = set() if lista == 'ninguno' else {int(n) - 1 for n in lista.split(',') if n.strip().isdigit()}
    bloq = leer('bloqueados.json', [])
    wl = leer('whitelist.json', [])
    bl = leer('baseline.json', {})
    base = leer('baseline_exes.json', {})
    existentes = claves_bloqueadas() or set()
    a_desbloquear = []

    for i, x in enumerate(pend):
        p = x.get('path') or ''
        if i in elegidos:
            if p and os.path.exists(p):
                bloq = fusionar(bloq, bloquear([p], 'centinela', existentes))
                wl = [w for w in wl if w['path'] != p]
        elif p and x['tipo'] != 'PERSISTENCIA':
            if x.get('provisional'):
                a_desbloquear += [b for b in bloq if b['path'] == p]
            bloq = [b for b in bloq if b['path'] != p]
            wl = [w for w in wl if w['path'] != p] + [{'path': p, 'sha256': x.get('sha256', ''), 'fecha': ahora()}]
        if x['tipo'] == 'PERSISTENCIA':
            bl.setdefault('persistencia', []).append(x.get('firma') or x.get('detalle'))
        elif p and os.path.exists(p):
            st = os.stat(p)
            base[p] = {'h': x.get('sha256') or sha256(p), 's': st.st_size, 'm': int(st.st_mtime)}

    if a_desbloquear:
        desbloquear(a_desbloquear, bloq)
    recargar()
    escribir('bloqueados.json', bloq)
    escribir('whitelist.json', wl)
    escribir('baseline.json', bl)
    escribir('baseline_exes.json', base)
    escribir('pendientes.json', [])
    est = leer('estado.json', {})
    escribir('estado.json', {'fecha': est.get('fecha', ahora()),
                             'alertas': [a for a in est.get('alertas', []) if a.get('nivel') == 'grave']})
    log(f'Revisión: {len(elegidos)} bloqueado(s), {len(pend) - len(elegidos)} a whitelist.')


# ─── Pantalla (corre como el usuario de la sesión) ───

def osa(script):
    return subprocess.run(['osascript', '-'], input=script, capture_output=True, text=True)


def as_texto(s):
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


def cmd_revisar():
    candado = open(f'/tmp/ensamble-centinela-{os.getuid()}.lock', 'w')
    try:
        fcntl.flock(candado, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return   # ya hay una ventana abierta

    est = leer('estado.json', {})
    alertas = [a for a in est.get('alertas', []) if a]
    pend = leer('pendientes.json', [])

    # Los avisos informativos se muestran una vez por detección; los graves, siempre.
    visto = Path.home() / 'Library/Application Support/EnsambleSetup/centinela_visto.txt'
    ya_visto = visto.exists() and visto.read_text().strip() == est.get('fecha', '')
    if ya_visto:
        alertas = [a for a in alertas if a.get('nivel') == 'grave']
    if alertas:
        grave = any(a.get('nivel') == 'grave' for a in alertas)
        osa(f'display alert {as_texto(TITULO)} message {as_texto(chr(10).join(a["texto"] for a in alertas))}'
            + (' as critical' if grave else ''))
        visto.parent.mkdir(parents=True, exist_ok=True)
        visto.write_text(est.get('fecha', ''))
    if not pend:
        return

    huella = sha256(BASE / 'pendientes.json')
    items = []
    for i, x in enumerate(pend, 1):
        marca = '  [bloqueado provisionalmente]' if x.get('provisional') else ('  [zona aprobada]' if x.get('en_zona') else '')
        items.append(f"{i}. {x['tipo']} · {x.get('path') or x.get('detalle')}{marca}")
    prompt = ('Elige lo que queda BLOQUEADO (Cmd+clic para varios). Lo que no elijas pasa a la whitelist y '
              'recupera la red. «Decidir después» deja todo pendiente.')
    script = (
        f'set sel to choose from list {{{", ".join(as_texto(s) for s in items)}}} with title {as_texto(TITULO)} '
        f'with prompt {as_texto(prompt)} OK button name "Aplicar" cancel button name "Decidir después" '
        'with multiple selections allowed and empty selection allowed\n'
        'if sel is false then return "CANCELAR"\n'
        'set salida to ""\n'
        'repeat with s in sel\n'
        '  set salida to salida & (text 1 thru ((offset of "." in s) - 1) of s) & ","\n'
        'end repeat\n'
        'return salida\n'
    )
    r = osa(script)
    salida = r.stdout.strip()
    if r.returncode != 0 or salida == 'CANCELAR':
        return
    elegidos = sorted({int(n) for n in salida.split(',') if n.strip().isdigit()})
    n_b = len(elegidos)
    r = osa(f'display dialog {as_texto(f"Se bloquean {n_b}; pasan a whitelist {len(pend) - n_b}. ¿Confirmas?")} '
            f'with title {as_texto(TITULO)} buttons {{"Cancelar", "Confirmar"}} default button "Confirmar"')
    if r.returncode != 0:
        return
    cmd = ' '.join(shlex.quote(a) for a in (sys.executable, str(Path(__file__).resolve()), 'aplicar', huella,
                                            ','.join(map(str, elegidos)) or 'ninguno'))
    r = osa(f'do shell script {as_texto(cmd)} with administrator privileges')
    if r.returncode != 0:
        motivo = (r.stderr or r.stdout).strip() or 'se canceló la contraseña'
        osa(f'display alert {as_texto(TITULO)} message {as_texto("No se aplicó: " + motivo + ". La lista sigue pendiente.")}')


if __name__ == '__main__':
    args = sys.argv[1:] or ['']
    cmd = args[0]
    if cmd == 'revisar':
        cmd_revisar()
        sys.exit(0)
    if os.geteuid() != 0:
        sys.exit('Este comando requiere root.')
    BASE.mkdir(parents=True, exist_ok=True)
    if cmd == 'baseline':
        cmd_baseline()
    elif cmd == 'aislar' and len(args) >= 2:
        cmd_aislar(args[1], '--real' in args)
    elif cmd == 'detectar':
        cmd_detectar()
    elif cmd == 'aplicar' and len(args) == 3:
        cmd_aplicar(args[1], args[2])
    elif cmd == 'bloquear' and len(args) == 2:
        cmd_bloquear(args[1])
    elif cmd == 'whitelist-add' and len(args) == 2:
        cmd_whitelist_add(args[1])
    elif cmd == 'proteger-lulu':
        cmd_proteger_lulu()
    elif cmd == 'rollback':
        cmd_rollback()
    else:
        sys.exit(__doc__)
'''



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
        elif run_logged(f"powercfg /setactive {ALTO_RENDIMIENTO_GUID}", "Activar plan 'Alto rendimiento'"):
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
        "3": ("Aislador + Centinela", seccion_aislador_centinela),
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
