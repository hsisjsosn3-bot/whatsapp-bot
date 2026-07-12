"""
diagnose.py — is script ko 'Run Module' se chalao is hosting platform pe.
Yeh check karega ki Chrome/Chromium system pe kahin bhi maujood hai ya nahi,
aur environment ke baare mein basic info print karega.
"""

import os
import shutil
import subprocess
import sys
import platform

print("=" * 50)
print("ENVIRONMENT DIAGNOSTIC")
print("=" * 50)

print(f"\nPython: {sys.version}")
print(f"Platform: {platform.platform()}")
print(f"Current dir: {os.getcwd()}")
print(f"User: {os.environ.get('USER', 'unknown')}")
print(f"HOME: {os.environ.get('HOME', 'unknown')}")

print("\n--- Searching PATH for Chrome/Chromium ---")
candidates = [
    "google-chrome", "google-chrome-stable", "google-chrome-beta",
    "chromium", "chromium-browser", "chrome",
]
found_any = False
for name in candidates:
    path = shutil.which(name)
    if path:
        print(f"  ✅ FOUND: {name} -> {path}")
        found_any = True
    else:
        print(f"  ❌ not found: {name}")

print("\n--- Checking common install directories ---")
common_paths = [
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/opt/google/chrome/chrome",
    "/snap/bin/chromium",
    "/app/.chrome/chrome",
    "/app/chrome/chrome",
]
for p in common_paths:
    exists = os.path.isfile(p)
    print(f"  {'✅' if exists else '❌'} {p}")
    if exists:
        found_any = True

print("\n--- Checking if apt/dpkg is usable (system package install) ---")
for tool in ("apt-get", "apt", "dpkg", "sudo"):
    path = shutil.which(tool)
    print(f"  {'✅' if path else '❌'} {tool}: {path or 'not available'}")

print("\n--- Trying `apt-get install` (will likely fail without root) ---")
try:
    result = subprocess.run(
        ["apt-get", "--version"], capture_output=True, text=True, timeout=5
    )
    print(f"  apt-get --version exit code: {result.returncode}")
except Exception as e:
    print(f"  apt-get not runnable: {e}")

print("\n" + "=" * 50)
if found_any:
    print("RESULT: Chrome/Chromium ka kuch trace mila — upar wala exact")
    print("path CHROME_BIN environment variable mein daal sakte ho.")
else:
    print("RESULT: Chrome/Chromium kahin nahi mila is environment mein,")
    print("aur na hi ise install karne ka access hai. Selenium-based")
    print("WhatsApp automation is platform par kaam nahi karega — ek")
    print("aisi hosting chahiye jo Docker / custom build command / apt")
    print("access deti ho (e.g. Render.com Background Worker with")
    print("render-build.sh, Railway, a VPS, ya Docker-based host).")
print("=" * 50)
