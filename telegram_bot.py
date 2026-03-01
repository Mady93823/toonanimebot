import os
import re
import time
import json
import asyncio
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# Import downloader functions
from toonworld_downloader import parse_episode_page, intercept_stream_url, download_stream, LANG_MAP

# Load environment variables
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_ID = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH")

# Parse allowed admin IDs
allowed_admins_str = os.getenv("ALLOWED_ADMIN_IDS", "")
ALLOWED_ADMINS = []
if allowed_admins_str:
    try:
        ALLOWED_ADMINS = [int(x.strip()) for x in allowed_admins_str.split(",") if x.strip()]
    except ValueError:
        print("Warning: Could not parse ALLOWED_ADMIN_IDS. Ensure they are comma-separated integers.")

if not all([BOT_TOKEN, API_ID, API_HASH]):
    print("Error: TELEGRAM_BOT_TOKEN, TELEGRAM_API_ID, and TELEGRAM_API_HASH must be set in .env")
    exit(1)

# Initialize Pyrogram Client
app = Client(
    "toonworld_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    plugins=dict(root="plugins")
)

# In-memory session state storage to track user workflow choices
# Format: { chat_id: { "url": str, "res": int or None, "langs": list, "ep_data": dict } }
user_sessions = {}

def make_progress_bar(percent, width=15):
    filled = int(width * percent // 100)
    return "█" * filled + "▒" * (width - filled)

def is_authorized(user_id):
    # Check if user is a Master Admin (.env)
    if ALLOWED_ADMINS and user_id in ALLOWED_ADMINS:
        return True
        
    # Check if user is explicitly authorized via the /auth plugin command
    try:
        if os.path.exists("authorized_users.json"):
            with open("authorized_users.json", "r") as f:
                auth_users = json.load(f)
                if user_id in auth_users:
                    return True
    except Exception as e:
        print(f"Error reading authorized_users.json: {e}")
        
    # If no master admins are configured, default to strictly deny rather than open access
    if not ALLOWED_ADMINS:
        print("Warning: No ALLOWED_ADMIN_IDS set. The bot is locked until an admin is configured in .env.")
        
    return False


@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    if not is_authorized(message.from_user.id):
        return await message.reply("⛔ Unauthorized.")
    
    await message.reply(
        "👋 Welcome! I am your ToonWorld Anime Downloader bot.\n\n"
        "Send me a link to any episode (`archive.toonworld4all.me/...`) OR a main series page (`toonworld4all.me/...`) and I will download it for you.",
        quote=True
    )


# ---------------------------------------------------------
# STATE 0: SERIES PAGE PARSING
# ---------------------------------------------------------
@app.on_message(filters.text & filters.regex(r"https?://toonworld4all\.me/(?!tag|category|page|about)(.+)"))
async def handle_series_url(client, message):
    user_id = message.from_user.id
    if not is_authorized(user_id):
        return await message.reply("⛔ Unauthorized.")

    url = message.text.strip()
    processing_msg = await message.reply("🔍 Scraping Series Page for episodes...", quote=True)

    try:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, lambda: requests.get(url, timeout=15))
        response.raise_for_status()
    except Exception as e:
        return await processing_msg.edit_text(f"❌ Failed to parse series page: {e}")

    soup = BeautifulSoup(response.text, "html.parser")
    
    # Find all anchor tags pointing to the archive episode domain
    episode_links = []
    
    # We use a set to keep track of seen URLs to prevent duplicates
    seen_urls = set()
    
    # The actual Watch/Download links are often styled uniquely, 
    # but the most robust way is just scanning for the domain.
    for a in soup.find_all('a', href=True):
        href = a['href']
        if "archive.toonworld4all.me/episode/" in href:
            if href not in seen_urls:
                seen_urls.add(href)
                # Try to extract the episode designation from the URL (e.g. 3x1 or 12)
                # The URLs usually look like: /episode/jujutsu-kaisen-3x1 or /episode/show-name-12
                match = re.search(r'-(\d+x\d+|\d+)[^/]*$', href)
                if match:
                    ep_name = f"Ep {match.group(1)}"
                else:
                    # Fallback if no clean number found
                    clean_slug = href.split('/')[-1] if not href.endswith('/') else href.split('/')[-2]
                    ep_name = clean_slug[-10:] # get last 10 chars as fallback
                
                # Format strictly to keep callback data short
                # For safety, Pyrogram callback_data is limited to 64 bytes. 
                # We will store the URL in a session buffer based on its index.
                episode_links.append((ep_name, href))

    if not episode_links:
        return await processing_msg.edit_text("❌ Could not detect any direct 'archive' episode links on this page.")

    # Create session buffer for this user to hold the long URLs
    if user_id not in user_sessions:
        user_sessions[user_id] = {}
    
    # Always reset the episode buffer for a new scraped page
    user_sessions[user_id]["episode_buffer"] = {}

    
    # Build inline keyboard list
    keyboard_buttons = []
    row = []
    for idx, (ep_name, href) in enumerate(episode_links):
        ep_id = f"ep_{idx}"
        user_sessions[user_id]["episode_buffer"][ep_id] = href
        
        # Max label length is 15 chars so it looks good on mobile
        label = ep_name[:15]
        
        row.append(InlineKeyboardButton(f"🎬 {label}", callback_data=f"selectep_{ep_id}"))
        
        # 3 buttons per row looks better for short "Ep 1x1" labels
        if len(row) == 3:
            keyboard_buttons.append(row)
            row = []
    
    # Append any remaining buttons
    if row:
        keyboard_buttons.append(row)
        
    await processing_msg.edit_text(
        f"✅ Found **{len(episode_links)}** Episodes.\n\nPlease select which episode to download:",
        reply_markup=InlineKeyboardMarkup(keyboard_buttons)
    )


@app.on_callback_query(filters.regex(r"^selectep_"))
async def handle_episode_selection(client, callback_query):
    user_id = callback_query.from_user.id
    
    if user_id not in user_sessions or "episode_buffer" not in user_sessions[user_id]:
        return await callback_query.answer("Session expired. Please send the link again.", show_alert=True)

    # Reconstruct the ep_id matching the buffer key
    callback_id = callback_query.data.split("_")[1] # e.g. "0"
    buffer_key = f"ep_{callback_id}" # "ep_0"
    
    url = user_sessions[user_id]["episode_buffer"].get(buffer_key)
    
    if not url:
        return await callback_query.answer("Could not resolve episode link. State lost.", show_alert=True)
        
    await callback_query.message.edit_text(f"🚀 Triggering standard download for:\n`{url}`")
    
    # Synthetically call the normal handle_url function
    # Create a mock message object
    class MockMessage:
        def __init__(self, uid, url_text, orig_msg):
            self.from_user = type('User', (), {'id': uid})()
            self.text = url_text
            self.chat = orig_msg.chat
            self.id = orig_msg.id
            self.orig_msg = orig_msg
            
        async def reply(self, text, quote=False):
            return await self.orig_msg.reply(text, quote=quote)
            
    mock_msg = MockMessage(user_id, url, callback_query.message)
    await handle_archive_url(client, mock_msg)


# ---------------------------------------------------------
# STATE 1: ARCHIVE EPISODE RESOLUTION
# ---------------------------------------------------------
@app.on_message(filters.text & filters.regex(r"https?://archive\.toonworld4all\.me/episode/"))
async def handle_archive_url(client, message):
    user_id = message.from_user.id
    if not is_authorized(user_id):
        return await message.reply("⛔ Unauthorized.")

    url = message.text.strip()
    processing_msg = await message.reply("🔍 Analyzing episode page...", quote=True)

    try:
        # Run blocking HTTP request in executor
        loop = asyncio.get_running_loop()
        ep_data = await loop.run_in_executor(None, parse_episode_page, url)
    except Exception as e:
        return await processing_msg.edit_text(f"❌ Failed to parse page: {e}")

    # Ensure streams exist
    streams = ep_data.get("streams", [])
    if not streams:
        return await processing_msg.edit_text("❌ No 'Watch Online' streams found for this episode.")

    # Save initial state
    user_sessions[user_id] = {
        "url": url,
        "ep_data": ep_data,
        "res": None,
        "lang": None
    }

    # Step 1: Ask for resolution
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🖥 1080p", callback_data="res_1080"),
         InlineKeyboardButton("📱 720p", callback_data="res_720")],
        [InlineKeyboardButton("📺 Best Available", callback_data="res_best")]
    ])
    
    meta = ep_data.get("metadata", {})
    title = f"{meta.get('show', 'Unknown')} - S{str(meta.get('season', 0)).zfill(2)}E{str(meta.get('episode', 0)).zfill(2)}"
    
    await processing_msg.edit_text(
        f"✅ Found: **{title}**\n\n"
        "Please select the maximum video resolution you want to download:",
        reply_markup=keyboard
    )


@app.on_callback_query(filters.regex(r"^res_"))
async def handle_res_selection(client, callback_query):
    user_id = callback_query.from_user.id
    
    if user_id not in user_sessions:
        return await callback_query.answer("Session expired. Please send the link again.", show_alert=True)

    # Parse resolution selection
    res_choice = callback_query.data.split("_")[1]
    res_value = None if res_choice == "best" else int(res_choice)
    user_sessions[user_id]["res"] = res_value

    # Extract available languages from stream info
    ep_data = user_sessions[user_id]["ep_data"]
    streams = ep_data.get("streams", [])
    stream_info = streams[0]
    langs_available = [l["code"] for l in stream_info.get("languages", [])]

    # Step 2: Ask for language
    buttons = []
    # Add multiple language option
    buttons.append([InlineKeyboardButton("🌍 Multi-Audio (All Languages)", callback_data="lang_all")])
    
    # Add individual language options
    row = []
    for l in langs_available:
        lang_name = LANG_MAP.get(l, l.title())
        row.append(InlineKeyboardButton(lang_name, callback_data=f"lang_{l}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    keyboard = InlineKeyboardMarkup(buttons)
    
    await callback_query.message.edit_text(
        f"Resolution selected: **{res_choice if res_choice != 'best' else 'Highest'}**\n\n"
        "Now, select the audio language track to download:",
        reply_markup=keyboard
    )
    await callback_query.answer()


@app.on_callback_query(filters.regex(r"^lang_"))
async def handle_lang_selection(client, callback_query):
    user_id = callback_query.from_user.id
    
    if user_id not in user_sessions:
        return await callback_query.answer("Session expired. Please send the link again.", show_alert=True)

    # Parse language selection
    lang_choice = callback_query.data.split("_")[1]
    
    session = user_sessions[user_id]
    session["lang"] = lang_choice
    
    # Step 3: Ask for upload type
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎥 Send as Video", callback_data="upload_video")],
        [InlineKeyboardButton("📄 Send as Document", callback_data="upload_document")]
    ])
    
    await callback_query.message.edit_text(
        f"Language selected: **{lang_choice if lang_choice != 'all' else 'Multi-Audio'}**\n\n"
        "How should I upload the final file to Telegram?",
        reply_markup=keyboard
    )
    await callback_query.answer()


@app.on_callback_query(filters.regex(r"^upload_"))
async def handle_upload_selection(client, callback_query):
    user_id = callback_query.from_user.id
    
    if user_id not in user_sessions:
        return await callback_query.answer("Session expired. Please send the link again.", show_alert=True)

    upload_type = callback_query.data.split("_")[1] # 'video' or 'document'
    session = user_sessions[user_id]
    session["upload_type"] = upload_type
    
    await callback_query.answer("Starting download process...")
    await process_download(client, callback_query.message, session)
    
    # Cleanup session
    del user_sessions[user_id]


async def process_download(client, message, session):
    status_msg = await message.edit_text("⏳ `Intercepting stream URLs...`")
    
    url = session["url"]
    ep_data = session["ep_data"]
    res = session["res"]
    lang = session["lang"]
    upload_type = session.get("upload_type", "document")
    
    # Get Stream metadata
    meta = ep_data.get("metadata", {})
    show = meta.get("show", "Unknown").replace(" ", "_")
    season = meta.get("season", 0)
    episode = meta.get("episode", 0)
    base_title = f"{show}_S{str(season).zfill(2)}E{str(episode).zfill(2)}"

    streams = ep_data.get("streams", [])
    stream_info = streams[0]
    player_url = stream_info["play"]
    langs_available = [l["code"] for l in stream_info.get("languages", [])]

    # 1. Intercept M3U8 Stream URL via Playwright
    # (Since intercept_stream_url uses playwright's async api, we can await it directly)
    try:
        captured_urls = await intercept_stream_url(player_url, timeout=45)
    except Exception as e:
        return await status_msg.edit_text(f"❌ Failed to intercept stream: {e}")

    if not captured_urls:
        return await status_msg.edit_text("❌ Could not capture the HLS stream URL from the player. It may be DRM protected.")

    target_url = captured_urls[0]
    output_dir = "./bot_downloads"
    os.makedirs(output_dir, exist_ok=True)

    # Prepare title
    lang_suffix = "_multi" if lang == "all" else f"_{LANG_MAP.get(lang, lang).lower().replace(' ', '')}"
    res_suffix = f"_{res}p" if res else ""
    title = base_title + res_suffix + lang_suffix

    await status_msg.edit_text("⬇️ `Downloading and merging video/audio tracks...`\n*(Please wait, this may take several minutes)*")

    session["dl_progress_str"] = "⬇️ `Starting download...`"
    session["dl_finished"] = False

    def dl_progress_hook(d, track_type):
        if d['status'] == 'downloading':
            frag_idx = d.get('fragment_index')
            frag_count = d.get('fragment_count')
            
            if frag_count and frag_idx:
                percent = (frag_idx / frag_count) * 100
                bar = make_progress_bar(percent)
                session["dl_progress_str"] = f"⬇️ **Downloading {track_type}**\n`{bar}` {percent:.1f}%\nFragments: {frag_idx}/{frag_count}"
            else:
                downloaded = d.get('downloaded_bytes', 0)
                total = d.get('total_bytes') or d.get('total_bytes_estimate')
                if total:
                    percent = (downloaded / total) * 100
                    bar = make_progress_bar(percent)
                    session["dl_progress_str"] = f"⬇️ **Downloading {track_type}**\n`{bar}` {percent:.1f}%\nBytes: {downloaded/(1024*1024):.1f} MB / {total/(1024*1024):.1f} MB"
                else:
                    session["dl_progress_str"] = f"⬇️ **Downloading {track_type}**\nDownloaded: {downloaded/(1024*1024):.1f} MB"
        elif d['status'] == 'finished':
            session["dl_progress_str"] = f"✅ **{track_type} downloaded. Pending Merge...**"

    async def update_dl_msg():
        last_str = ""
        while not session.get("dl_finished"):
            current_str = session.get("dl_progress_str", "")
            if current_str and current_str != last_str:
                try:
                    await status_msg.edit_text(current_str)
                    last_str = current_str
                except Exception:
                    pass
            await asyncio.sleep(5)

    updater_task = asyncio.create_task(update_dl_msg())

    # 2. Download via yt-dlp & ffmpeg
    # (download_stream is synchronous and runs ffmpeg blockingly, meaning we must run it in an executor)
    loop = asyncio.get_running_loop()
    success = await loop.run_in_executor(
        None, 
        lambda: download_stream(
            stream_url=target_url, 
            output_path=output_dir, 
            title=title, 
            lang=lang, 
            available_langs=langs_available, 
            resolution=res,
            progress_hook=dl_progress_hook
        )
    )

    session["dl_finished"] = True
    await updater_task

    if not success:
        return await status_msg.edit_text("❌ Download or merge process failed. Check server console logs.")

    await status_msg.edit_text("📤 `Upload starting...`")

    # 3. Locate final file(s) to upload
    # If "all", we have a master MKV and several MP4s. 
    # To avoid spamming, we will just upload the individual MP4s for each language if they exist.
    # If the user wants the multi-audio MKV, we upload that too.
    
    files_to_upload = []
    
    if lang == "all":
        # Search for the individual requested languages
        for l in langs_available:
            lang_name = LANG_MAP.get(l, l)
            lang_fn_suffix = lang_name.lower().replace(" ", "")
            single_file = os.path.join(output_dir, f"{base_title}{res_suffix}_{lang_fn_suffix}.mp4")
            if os.path.exists(single_file):
                files_to_upload.append(single_file)
        
        # Also append the master MKV
        master_mkv = os.path.join(output_dir, f"{title}.mkv")
        if os.path.exists(master_mkv):
            files_to_upload.append(master_mkv)
            
    else:
        # Just the one file requested
        single_file = os.path.join(output_dir, f"{title}.mp4")
        if os.path.exists(single_file):
            files_to_upload.append(single_file)

    if not files_to_upload:
        return await status_msg.edit_text("❌ Could not find downloaded file on disk.")

    # 4. Upload Files
    for file_path in files_to_upload:
        filename = os.path.basename(file_path)
        
        last_update_time = time.time()
        
        # Progress callback for upload
        async def progress(current, total):
            nonlocal last_update_time
            now = time.time()
            if now - last_update_time >= 5 or current == total:
                percent = (current / total) * 100 if total else 0
                bar = make_progress_bar(percent)
                try:
                    await status_msg.edit_text(
                        f"📤 **Uploading {filename}**\n`{bar}` {percent:.1f}%\n{current/(1024*1024):.1f} MB / {total/(1024*1024):.1f} MB"
                    )
                    last_update_time = now
                except Exception:
                    pass

        try:
            if upload_type == "video":
                await client.send_video(
                    chat_id=message.chat.id,
                    video=file_path,
                    caption=f"🎥 **{filename}**",
                    progress=progress,
                    supports_streaming=True
                )
            else:
                await client.send_document(
                    chat_id=message.chat.id,
                    document=file_path,
                    caption=f"🎥 **{filename}**",
                    progress=progress
                )
        except Exception as e:
            await message.reply(f"❌ Failed to upload {filename}: {e}")
        finally:
            # Clean up local file after uploading
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except:
                    pass

    await status_msg.edit_text("✅ **All processes completed successfully!**")


if __name__ == "__main__":
    print("Starting ToonWorld Bot...")
    app.run()
