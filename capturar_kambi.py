"""
capturar_kambi.py
─────────────────
Abre BetPlay en un browser visible, intercepta todas las llamadas
a Kambi y guarda en kambi_captura.json los requests POST (apuestas).

USO:
    pip install playwright
    playwright install chromium
    python capturar_kambi.py

Luego:
  1. Inicia sesión manualmente en el browser que se abre
  2. Navega a un partido y coloca una apuesta pequeña (500 COP)
  3. El script guarda el payload automáticamente y cierra
"""

import asyncio
import json
import os
from datetime import datetime
from playwright.async_api import async_playwright

OUTPUT_FILE = "kambi_captura.json"
KAMBI_HOST  = "kambicdn.com"

capturas = []


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--start-maximized"]
        )
        context = await browser.new_context(
            viewport=None,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )

        # ── Interceptar requests ──────────────────────────────────
        async def on_request(request):
            if KAMBI_HOST in request.url:
                try:
                    body_bytes = request.post_data_buffer
                    body_str   = request.post_data or ""
                    body_json  = None
                    if body_str:
                        try:
                            body_json = json.loads(body_str)
                        except Exception:
                            body_json = body_str

                    entrada = {
                        "ts":      datetime.utcnow().isoformat(),
                        "method":  request.method,
                        "url":     request.url,
                        "headers": dict(request.headers),
                        "body":    body_json,
                    }
                    capturas.append(entrada)

                    if request.method == "POST":
                        print(f"\n🎯 POST capturado: {request.url}")
                        print(json.dumps(body_json, indent=2, ensure_ascii=False)[:800])
                        _guardar()

                except Exception as e:
                    print(f"Error capturando request: {e}")

        # ── Interceptar responses (para ver tokens en respuestas) ─
        async def on_response(response):
            if KAMBI_HOST in response.url and response.status == 200:
                try:
                    ctype = response.headers.get("content-type", "")
                    if "json" in ctype:
                        body = await response.json()
                        # Guardar respuesta de login de Kambi (tiene el token)
                        if "punter/login" in response.url or "token" in str(body)[:200]:
                            capturas.append({
                                "ts":       datetime.utcnow().isoformat(),
                                "tipo":     "RESPONSE",
                                "url":      response.url,
                                "status":   response.status,
                                "body":     body,
                            })
                            print(f"\n✅ RESPONSE capturada: {response.url}")
                            _guardar()
                except Exception:
                    pass

        page = await context.new_page()
        page.on("request",  on_request)
        page.on("response", on_response)

        print("="*60)
        print("  Browser abierto — BetPlay cargando...")
        print("  1. Inicia sesión")
        print("  2. Coloca una apuesta pequeña (500 COP)")
        print("  3. El payload se guarda automáticamente")
        print(f"  Output: {OUTPUT_FILE}")
        print("="*60)

        await page.goto("https://betplay.com.co", wait_until="domcontentloaded")

        # Esperar hasta que el usuario coloque la apuesta (máx 10 min)
        print("\nEsperando apuesta... (timeout: 10 min)\n")
        await asyncio.sleep(600)

        await browser.close()
        _guardar()
        print(f"\n✅ Captura finalizada. Revisa {OUTPUT_FILE}")


def _guardar():
    # Filtrar solo los POST a Kambi (los más relevantes arriba)
    posts   = [c for c in capturas if c.get("method") == "POST"]
    resto   = [c for c in capturas if c.get("method") != "POST"]
    ordered = posts + resto

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(ordered, f, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    asyncio.run(main())