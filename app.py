import os
import re
import traceback
import asyncio
import tempfile
import aiohttp
import aiofiles
from slack_bolt.async_app import AsyncApp
from aiohttp import web
from slack_bolt.adapter.aiohttp import to_bolt_request, to_aiohttp_response
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

app = AsyncApp(
    token=os.environ.get("SLACK_BOT_TOKEN"),
    signing_secret=os.environ.get("SLACK_SIGNING_SECRET")
)

async def process_threads_link(url, channel_id, thread_ts):
    print(f"   [BACKGROUND TASK] Connecting to existing Edge session for URL: {url}")
    
    browser = None
    page = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp("http://localhost:9222")
            context = browser.contexts[0]
            page = await context.new_page()
            
            await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            print(f"   [BACKGROUND TASK] Page landed on: '{await page.title()}'")

            main_region_selector = 'div[role="region"]'
            print(f"   [BACKGROUND TASK] Waiting for the main post region ('{main_region_selector}')...")
            await page.wait_for_selector(main_region_selector, timeout=20000)
            
            # Una vez que la región principal existe, esperamos a que aparezca un video O una imagen dentro de ella.
            # Esto soluciona la condición de carrera de forma más robusta, sin depender de la estructura interna de divs.
            media_selector = f'{main_region_selector} video, {main_region_selector} picture'
            print(f"   [BACKGROUND TASK] Waiting for media to appear ('{media_selector}')...")
            try:
                # Esperamos hasta 15 segundos por si la red es lenta para cargar el video/imagen.
                await page.wait_for_selector(media_selector, timeout=15000)
                print("   [BACKGROUND TASK] Media found! Extracting page content...")
            except Exception: # Playwright's TimeoutError
                print("   [BACKGROUND TASK] Timed out waiting for media. Proceeding without it.")
                # No es necesario hacer nada, el código posterior manejará el caso sin medios.
                pass

            content = await page.content()
            soup = BeautifulSoup(content, 'html.parser')
            
            image_items = []
            video_urls = []
            main_region = soup.find('div', attrs={'role': 'region'})
            post_container = None
            if main_region:
                # Buscamos el contenedor específico del post dentro de la región principal.
                # El "True" indica que solo nos importa que el atributo exista.
                post_container = main_region.find('div', attrs={'data-interactive-id': True})
            
            if post_container:
                print("   [BACKGROUND TASK] Searching for media within the post container...")
                # Los videos en Threads a menudo usan un tag <source> dentro del tag <video>.
                for video_tag in post_container.find_all('video'):
                    print("   [BACKGROUND TASK] Video tag found!")
                    video_url = None
                    # Intentar encontrar la URL en el tag <source> anidado.
                    source_tag = video_tag.find('source')
                    if source_tag and source_tag.get('src'):
                        print("   [BACKGROUND TASK] Inner Source tag found! ")
                        video_url = source_tag.get('src')
                    # Si no, como respaldo, buscar en el propio tag <video>.
                    elif video_tag.get('src'):
                        print("   [BACKGROUND TASK] src atttributee found!")
                        video_url = video_tag.get('src')
                    
                    if video_url:
                        print(f"   [BACKGROUND TASK] Found video URL: {video_url}")
                        video_urls.append(video_url)
                
                for picture_tag in post_container.find_all('picture'):
                    img_tag = picture_tag.find('img')
                    if not img_tag: continue
                    is_profile_pic = img_tag.get('height') == '36' and img_tag.get('width') == '36'
                    if (img_src := img_tag.get('src')) and not is_profile_pic:
                        alt_text = img_tag.get('alt', 'Imagen de Threads')
                        image_items.append({'url': img_src, 'alt': alt_text})
            
            media_found = bool(image_items or video_urls)

            if media_found:
                # Post images first, using Block Kit for a clean look
                if image_items:
                    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": f"He encontrado {len(image_items)} imagen(es) en el enlace:"}}]
                    for item in image_items:
                        blocks.append({"type": "image", "image_url": item['url'], "alt_text": item['alt']})
                    await app.client.chat_postMessage(
                        channel=channel_id, thread_ts=thread_ts,
                        blocks=blocks, text="Imágenes de Threads"
                    )
                
                # Download videos and upload them as files
                if video_urls:
                    await app.client.chat_postMessage(
                        channel=channel_id, thread_ts=thread_ts,
                        text=f"He encontrado {len(video_urls)} video(s):"
                    )
                    for video_url in video_urls:
                        # Usar un gestor de contexto para la sesión y el archivo temporal
                        async with aiohttp.ClientSession() as session:
                            async with session.get(video_url) as resp:
                                if resp.status == 200:
                                    # Crear un archivo temporal, obtener su nombre y cerrarlo inmediatamente.
                                    # Esto evita el PermissionError en Windows.
                                    tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
                                    tmp_file_path = tmp_file.name
                                    tmp_file.close()

                                    try:
                                        # Escribir el contenido del video en el archivo temporal.
                                        async with aiofiles.open(tmp_file_path, 'wb') as f:
                                            await f.write(await resp.read())
                                        print(f"   [BACKGROUND TASK] Video descargado en: {tmp_file_path}")

                                        # Subir el archivo a Slack.
                                        await app.client.files_upload_v2(
                                            channel=channel_id, thread_ts=thread_ts,
                                            file=tmp_file_path, initial_comment="Video de Threads:"
                                        )
                                    finally:
                                        os.remove(tmp_file_path) # Asegurarse de borrar el archivo temporal.
                print(f"✅ [BACKGROUND TASK] Success! Posted {len(image_items)} images and {len(video_urls)} videos to Slack.")
            else:
                await app.client.chat_postMessage(
                    channel=channel_id, thread_ts=thread_ts,
                    text="No he podido encontrar videos o imágenes en este enlace de Threads."
                )
                print("   [BACKGROUND TASK] No media found. Posted feedback message.")

    except Exception:
        print("\n❌ [BACKGROUND TASK ERROR] An error occurred:")
        traceback.print_exc()
        await app.client.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts,
            text="Lo siento, hubo un error al procesar ese enlace. Asegúrate de que Edge esté corriendo en modo de depuración."
        )
    finally:
        if page:
            await page.close()

# --- MANEJADORES DE EVENTOS Y SERVIDOR (sin cambios) ---
@app.event("app_mention")
async def handle_app_mention(body, say):
    print("\n✅ [EVENT RECEIVED] 'app_mention' event triggered. Acknowledging and starting background task.")
    text = body['event']['text']
    pattern = r"<?(https?://www\.threads\.com/[^>|\s]+)>?"
    match = re.search(pattern, text)
    if match:
        url = match.group(1)
        channel_id = body['event']['channel']
        thread_ts = body['event']['ts']
        asyncio.create_task(process_threads_link(url, channel_id, thread_ts))
    else:
        await say("¡Hola! Mencióname junto a un enlace de Threads para extraer los videos o imágenes.")

@app.event("reaction_added")
async def handle_reaction(body, say):
    event = body['event']
    match = None
    if event['reaction'] == 'eyes':
        print("\n✅ [EVENT RECEIVED] 'reaction_added' (eyes) event triggered. Acknowledging and starting background task.")
        channel_id = event['item']['channel']
        message_ts = event['item']['ts']
        history = await app.client.conversations_history(channel=channel_id, latest=message_ts, oldest=message_ts, inclusive=True)
        message_text = history['messages'][0]['text'] if history['messages'] else ''
        pattern = r"<?(https?://www\.threads\.com/[^>|\s]+)>?"
        match = re.search(pattern, message_text)
    if match:
        url = match.group(1)
        asyncio.create_task(process_threads_link(url, channel_id, message_ts))

async def slack_events_handler(request: web.Request):
    bolt_request = await to_bolt_request(request)
    bolt_response = await app.async_dispatch(bolt_request)
    return await to_aiohttp_response(bolt_response)

async def main():
    aio_app = web.Application()
    aio_app.router.add_post("/slack/events", slack_events_handler)
    port = int(os.environ.get("PORT", 3000))
    print(f"🚀 Starting AIOHTTP server on port {port}...")
    runner = web.AppRunner(aio_app)
    await runner.setup()
    site = web.TCPSite(runner, 'localhost', port)
    await site.start()
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())