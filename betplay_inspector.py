"""
capturar_kambi_v2.py
────────────────────
Fusión de betplay_inspector.py (login robusto) + captura de payload
Kambi para el submit de apuesta.

USO:
    pip install playwright
    playwright install chromium
    python capturar_kambi_v2.py

Flujo:
  1. Login automático con tus credenciales
  2. Browser queda abierto — navega a un partido y coloca una apuesta pequeña (500 COP)
  3. El script captura el POST a Kambi y guarda kambi_captura.json
  4. Se cierra solo al detectar el submit, o después de 10 minutos
"""

import asyncio
import json
from datetime import datetime
from playwright.async_api import async_playwright

# ── CREDENCIALES ──────────────────────────────────────────────────────
USUARIO  = "1095915302"
PASSWORD = "101106Cacv."
# ─────────────────────────────────────────────────────────────────────

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

KAMBI_HOST  = "kambicdn.com"
OUTPUT_FILE = "kambi_captura.json"

# Keywords para capturar también requests de saldo/apuestas de Betplay
KEYWORDS_BP = [
    "balance", "saldo", "wallet", "account",
    "bet", "apuesta", "ticket", "wager",
    "user", "profile", "session", "auth",
    "fund", "cash", "money", "reverse-proxy",
]
IGNORAR_EXT = (".js", ".css", ".png", ".jpg", ".svg",
               ".woff", ".woff2", ".ttf", ".ico", ".gif")

capturas_kambi = []   # Solo POSTs a Kambi
capturas_bp    = []   # Todo lo de Betplay (para el reporte general)
apuesta_detectada = False


async def main():
    global apuesta_detectada

    print("="*60)
    print("  🎯 Capturador Kambi v2 — BetSense")
    print("="*60)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            slow_mo=150,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                  "--start-maximized"]
        )
        context = await browser.new_context(
            user_agent=UA,
            viewport={"width": 1280, "height": 800},
            locale="es-CO",
            timezone_id="America/Bogota",
        )
        page = await context.new_page()

        # ── Interceptar requests ──────────────────────────────────
        async def on_request(request):
            url = request.url

            # Captura completa de Kambi (todos los métodos)
            if KAMBI_HOST in url:
                body_str  = request.post_data or ""
                body_json = None
                if body_str:
                    try:
                        body_json = json.loads(body_str)
                    except Exception:
                        body_json = body_str

                entrada = {
                    "ts":      datetime.utcnow().isoformat(),
                    "method":  request.method,
                    "url":     url,
                    "headers": dict(request.headers),
                    "body":    body_json,
                }
                capturas_kambi.append(entrada)

                if request.method == "POST":
                    print(f"\n🎯 POST KAMBI capturado:")
                    print(f"   URL: {url}")
                    if body_json:
                        print(f"   Body: {json.dumps(body_json, indent=2, ensure_ascii=False)[:600]}")
                    _guardar()

            # Captura general de Betplay
            url_lower = url.lower()
            if (any(kw in url_lower for kw in KEYWORDS_BP)
                    and not any(url.endswith(ext) for ext in IGNORAR_EXT)):
                capturas_bp.append({
                    "url":      url,
                    "method":   request.method,
                    "headers":  dict(request.headers),
                    "body":     request.post_data or "",
                    "response": None,
                })

        # ── Interceptar responses ─────────────────────────────────
        async def on_response(response):
            url = response.url

            # Respuestas de Kambi
            if KAMBI_HOST in url and response.status == 200:
                try:
                    ctype = response.headers.get("content-type", "")
                    if "json" in ctype:
                        body = await response.json()
                        # Guardar respuesta de coupon submit
                        if any(x in url for x in ["coupon", "bet", "punter"]):
                            for c in reversed(capturas_kambi):
                                if c["url"] == url and "response" not in c:
                                    c["response"] = body
                                    print(f"\n✅ Response Kambi: {url[-60:]}")
                                    print(f"   {json.dumps(body, ensure_ascii=False)[:400]}")
                                    _guardar()
                                    break
                except Exception:
                    pass

            # Respuestas de Betplay
            url_lower = url.lower()
            if (any(kw in url_lower for kw in KEYWORDS_BP)
                    and not any(url.endswith(ext) for ext in IGNORAR_EXT)):
                for entry in reversed(capturas_bp):
                    if entry["url"] == url and entry["response"] is None:
                        try:
                            body = await response.text()
                            try:
                                entry["response"] = json.loads(body)
                            except Exception:
                                entry["response"] = body[:500]
                            entry["status"]       = response.status
                            entry["resp_headers"] = dict(response.headers)
                        except Exception:
                            pass
                        break

        page.on("request",  on_request)
        page.on("response", on_response)

        # ── LOGIN ─────────────────────────────────────────────────
        print("\n🌐 Abriendo BetPlay...")
        await page.goto(
            "https://www.betplay.com.co/apuestas#login",
            timeout=60000,
            wait_until="domcontentloaded"
        )
        await page.wait_for_timeout(5000)

        print("🔑 Buscando campos de login...")
        selectores_user = [
            "input.inciosesion[placeholder='Usuario / Cédula']",
            "input[placeholder*='Usuario']",
            "input[placeholder*='Cédula']",
            "input[name='username']",
            "input[type='text']",
        ]
        sel_user = None
        for sel in selectores_user:
            try:
                await page.wait_for_selector(sel, timeout=5000, state="visible")
                sel_user = sel
                print(f"  ✅ Campo usuario encontrado: {sel}")
                break
            except Exception:
                continue

        if not sel_user:
            print("❌ No se encontró el campo de usuario.")
            await page.screenshot(path="debug_login.png", full_page=True)
            print("   Screenshot guardado: debug_login.png")
            print("\n⏳ Browser abierto 5 min para login manual...")
            await asyncio.sleep(300)
        else:
            await page.fill(sel_user, "")
            await page.type(sel_user, USUARIO, delay=80)
            await page.wait_for_timeout(400)

            await page.fill("input[type='password']", "")
            await page.type("input[type='password']", PASSWORD, delay=80)
            await page.wait_for_timeout(400)

            # Submit
            clicked = False
            for sel in [
                "button.btn-inicio",
                "button[type='submit']",
                "button:has-text('Ingresar')",
                "button:has-text('Iniciar')",
                "button:has-text('Entrar')",
            ]:
                try:
                    btn = await page.query_selector(sel)
                    if btn and await btn.is_visible():
                        await btn.click()
                        clicked = True
                        print(f"  ✅ Click submit: {sel}")
                        break
                except Exception:
                    pass

            if not clicked:
                await page.press("input[type='password']", "Enter")
                print("  ✅ Submit via Enter")

            print("\n⏳ Esperando post-login (10s)...")
            await page.wait_for_timeout(10000)

            # Verificar login exitoso
            contenido = await page.content()
            for err in ["contraseña incorrecta", "usuario incorrecto",
                        "datos incorrectos", "usuario o contraseña"]:
                if err.lower() in contenido.lower():
                    print("❌ Credenciales incorrectas.")
                    await browser.close()
                    return

            print("✅ Login aparentemente exitoso")

        # ── INSTRUCCIONES ─────────────────────────────────────────
        print("\n" + "="*60)
        print("  👆 AHORA:")
        print("  1. Navega a cualquier partido en el browser")
        print("  2. Agrega una selección al betslip")
        print("  3. Ingresa 500 COP y confirma la apuesta")
        print("  4. El payload se captura automáticamente")
        print(f"  Output: {OUTPUT_FILE}")
        print("="*60)
        print("\nEsperando... (timeout: 10 minutos)\n")

        # Esperar hasta 10 minutos
        for _ in range(120):
            await asyncio.sleep(5)
            posts_kambi = [c for c in capturas_kambi if c.get("method") == "POST"
                           and any(x in c["url"] for x in ["coupon", "bet"])]
            if posts_kambi:
                print("\n🎉 Apuesta capturada! Cerrando en 5s...")
                await asyncio.sleep(5)
                break

        # ── REPORTE FINAL ─────────────────────────────────────────
        await browser.close()
        _guardar()

        print("\n" + "="*60)
        print("  📊 RESUMEN")
        print("="*60)

        posts = [c for c in capturas_kambi if c.get("method") == "POST"]
        print(f"\n  POSTs a Kambi capturados: {len(posts)}")
        for p in posts:
            print(f"    → {p['url']}")

        print(f"\n  Requests Betplay capturados: {len(capturas_bp)}")

        # Guardar también reporte Betplay
        with open("betplay_api_report.json", "w", encoding="utf-8") as f:
            seen = set()
            unique = [e for e in capturas_bp
                      if e["url"] not in seen and not seen.add(e["url"])]
            json.dump(unique, f, ensure_ascii=False, indent=2)
        print(f"\n  💾 Reporte Betplay: betplay_api_report.json")
        print(f"  💾 Payload Kambi:   {OUTPUT_FILE}")
        print("\n✅ Listo.")


def _guardar():
    posts   = [c for c in capturas_kambi if c.get("method") == "POST"]
    resto   = [c for c in capturas_kambi if c.get("method") != "POST"]
    ordered = posts + resto
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(ordered, f, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    asyncio.run(main())