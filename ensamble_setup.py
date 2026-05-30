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


# ─────────────────────────────────────────────
# SECCIÓN: NOMBRE DEL EQUIPO
# ─────────────────────────────────────────────

def seccion_nombre_pc():
    title("CONFIGURAR NOMBRE DEL EQUIPO")

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


# ─────────────────────────────────────────────
# MENÚ PRINCIPAL
# ─────────────────────────────────────────────

MENU = {
    "1": ("Nombre del equipo", seccion_nombre_pc),
    # Próximas secciones:
    # "2": ("Desinstalar aplicaciones",    seccion_desinstalar),
    # "3": ("Instalar programas Ensamble", seccion_instalar),
    # "4": ("Perfil de energía",           seccion_energia),
    # "5": ("Crear usuario Asociado",      seccion_crear_asociado),
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
