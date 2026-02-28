"""
ToonWorld4All Video Downloader
==============================
Downloads anime episodes from archive.toonworld4all.me

Features:
- Extracts all quality options (480p, 720p, 1080p)
- Supports multiple languages (Hindi, Tamil, Telugu, English, Japanese)
- Uses Playwright to intercept the m3u8 stream from the Watch Online player
- Downloads HLS streams using yt-dlp or ffmpeg

Usage:
  python toonworld_downloader.py <episode_url> [options]

Examples:
  python toonworld_downloader.py https://archive.toonworld4all.me/episode/jujutsu-kaisen-3x1
  python toonworld_downloader.py https://archive.toonworld4all.me/episode/jujutsu-kaisen-3x1 --lang en --output ./downloads
  python toonworld_downloader.py https://archive.toonworld4all.me/episode/jujutsu-kaisen-3x1 --list

Requirements:
  pip install playwright requests yt-dlp
  python -m playwright install chromium
"""

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import urllib.request
import urllib.parse

# -- Optional imports --
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    from playwright.async_api import async_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

try:
    import yt_dlp
    HAS_YTDLP = True
except ImportError:
    HAS_YTDLP = False


# -- Constants --
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

LANG_MAP = {
    "hi": "Hindi",
    "ta": "Tamil",
    "te": "Telugu",
    "en": "English",
    "ja": "Japanese",
    "ml": "Malayalam",
    "kn": "Kannada",
    "bn": "Bengali",
}


# -- Helper: HTTP fetch --
def fetch(url, referer="", follow_redirects=True):
    """Simple HTTP GET with browser headers."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    if referer:
        headers["Referer"] = referer

    if HAS_REQUESTS:
        r = requests.get(url, headers=headers, allow_redirects=follow_redirects, timeout=30)
        return r.content
    else:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()


# -- Step 1: Parse episode page --
def parse_episode_page(url):
    """
    Fetches the episode page and extracts __PROPS__ data which contains:
      - encodes: list of quality options with download links
      - streams: list of watch-online players with language info
    """
    print("\n[1/4] Fetching episode page: " + url)
    html = fetch(url).decode("utf-8", errors="replace")

    # Extract window.__PROPS__
    match = re.search(r"window\.__PROPS__\s*=\s*(\{.+)", html, re.DOTALL)
    if not match:
        raise RuntimeError("Could not find __PROPS__ in page. The site structure may have changed.")

    props_raw = match.group(1)
    # Balance braces to find the end of the JSON object
    depth, end = 0, 0
    for i, ch in enumerate(props_raw):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    props = json.loads(props_raw[:end])
    ep_data = props.get("data", {}).get("data", {})

    if not ep_data:
        raise RuntimeError("Episode data is empty. The URL may be incorrect.")

    return ep_data


# -- Step 2: Display available options --
def print_options(ep_data):
    """Print all available quality and language options."""
    meta = ep_data.get("metadata", {})
    print("\n" + "=" * 60)
    print("  Show   : " + str(meta.get("show", "Unknown")))
    print("  Season : " + str(meta.get("season", "?")) + "  Episode: " + str(meta.get("episode", "?")))
    print("=" * 60)

    print("\n[Watch Online / Streams]:")
    for i, stream in enumerate(ep_data.get("streams", [])):
        langs = [LANG_MAP.get(l["code"], l.get("large", l["code"])) for l in stream.get("languages", [])]
        print("  Stream " + str(i + 1) + ": " + ", ".join(langs))
        print("    URL: " + stream["play"][:80] + "...")

    print("\n[Download Options by Quality]:")
    for enc in ep_data.get("encodes", []):
        r = enc["readable"]
        print("\n  [" + r["codec"] + "] -- " + r["size"])
        for f in enc.get("files", []):
            print("    - " + f["host"].ljust(12) + " -> " + f["short"])


# -- Step 3: Intercept stream URL using Playwright --
async def intercept_stream_url(player_url, timeout=45):
    """
    Opens the JWPlayer page in a stealth headless browser, intercepts network
    responses, and returns HLS master playlist URLs.

    The player at pages.dev uses Cloudflare Workers to proxy HLS streams.
    The master playlist URL has content-type: application/vnd.apple.mpegurl
    and contains multi-language audio tracks.
    """
    if not HAS_PLAYWRIGHT:
        raise RuntimeError(
            "playwright is not installed. Run: pip install playwright && python -m playwright install chromium"
        )

    print("\n[2/4] Launching headless browser to intercept HLS streams...")
    print("      Player URL: " + player_url[:80] + "...")

    # master_url: the first m3u8 master playlist captured
    master_url = None
    all_m3u8_urls = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--disable-infobars",
            ],
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 720},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        # Mask automation indicators so Cloudflare passes the request
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )

        page = await context.new_page()

        # Capture responses with HLS content-type (master playlist & segments)
        async def on_response(response):
            nonlocal master_url
            url = response.url
            ct = response.headers.get("content-type", "")
            status = response.status

            is_hls = (
                "mpegurl" in ct.lower()
                or ".m3u8" in url
                or url.endswith("/m3u8")
            )
            if is_hls and status == 200:
                # Check if it's the master playlist (contains #EXT-X-MEDIA or #EXT-X-STREAM-INF)
                try:
                    text = await response.text()
                    if "#EXTM3U" in text:
                        if url not in all_m3u8_urls:
                            all_m3u8_urls.append(url)
                            # Prefer the master (multi-variant) playlist
                            if "#EXT-X-MEDIA" in text or "#EXT-X-STREAM-INF" in text:
                                if master_url is None:
                                    master_url = url
                                    print("      [+] Master HLS playlist: " + url[:100])
                            else:
                                print("      [+] Media playlist: " + url[:60] + "...")
                except Exception:
                    pass

        page.on("response", on_response)

        # Navigate to the player page - use domcontentloaded to not wait for all JS
        print("      Loading player page...")
        try:
            await page.goto(player_url, wait_until="domcontentloaded", timeout=timeout * 1000)
        except Exception as nav_err:
            print("      [note] Nav: " + str(nav_err)[:80])

        # Wait for JWPlayer to load and initialize
        print("      Waiting for player to initialize (10s)...")
        await asyncio.sleep(10)

        # Click the player to start playback (JWPlayer requires interaction)
        for selector in [".jwplayer", ".jw-media", "video", "body"]:
            try:
                await page.click(selector, timeout=3000)
                print("      Clicked: " + selector)
                await asyncio.sleep(6)
                break
            except Exception:
                pass

        # If still no master url, wait a bit more
        if master_url is None:
            print("      Waiting extra 8s for streams...")
            await asyncio.sleep(8)

        await browser.close()

    # Return master playlist first, then all others
    result = []
    if master_url:
        result.append(master_url)
    for url in all_m3u8_urls:
        if url != master_url:
            result.append(url)

    return result


# -- Step 4: Download with yt-dlp --
def download_stream(stream_url, output_path, title="video", lang=None, available_langs=None, resolution=None, progress_hook=None):
    """
    Downloads an HLS (m3u8) or direct video stream using yt-dlp.
    For multi-language HLS, yt-dlp's format selection can fail to merge audio.
    We download video and audio separately and merge them using ffmpeg.
    Supports downloading all languages into a single multi-audio MKV file.
    """
    if not HAS_YTDLP:
        raise RuntimeError("yt-dlp not installed. Run: pip install yt-dlp")

    import shutil
    if not shutil.which("ffmpeg"):
        print("      [FAIL] ffmpeg not found in PATH. It is required for merging audio/video.")
        return False

    print("\n[3/4] Downloading stream...")
    print("      URL : " + stream_url[:80] + "...")
    print("      Out : " + output_path)

    safe_title = re.sub(r'[<>:"/\\|?*]', "_", title)
    
    # If downloading all audio, use MKV container as it handles multi-audio better
    ext = ".mkv" if lang == "all" else ".mp4"
    final_out = os.path.join(output_path, safe_title + ext)
    temp_vid = os.path.join(output_path, safe_title + "_temp_vid.mp4")
    
    temp_files = [temp_vid]
    
    if lang == "all":
        langs_to_download = available_langs if available_langs else ["en"]
    else:
        langs_to_download = [lang] if lang else ["en"]
        
    audio_files = []
    for l in langs_to_download:
        aud_f = os.path.join(output_path, f"{safe_title}_temp_aud_{l}.m4a")
        audio_files.append((l, aud_f))
        temp_files.append(aud_f)

    # Clean previous temp files aggressively
    import glob
    for prefix in [f"{safe_title}_temp_vid", f"{safe_title}_temp_aud"]:
        for f in glob.glob(os.path.join(output_path, prefix + "*")):
            try: os.remove(f)
            except: pass
    if os.path.exists(final_out):
        try: os.remove(final_out)
        except: pass

    base_opts = {
        "quiet": False,
        "no_warnings": False,
        "http_headers": {
            "User-Agent": USER_AGENT,
            "Referer": "https://324fecbc-2d1d-4473-adb8-68632ec0f5eb.pages.dev/",
        },
        "fragment_retries": 10,
        "retries": 5,
        "concurrent_fragment_downloads": 5,
    }

    # 1. Download Video
    print("\n      --> Downloading Video track...")
    opts_v = base_opts.copy()
    if resolution:
        opts_v["format"] = f"bestvideo[height<={resolution}]/bestvideo/best"
    else:
        opts_v["format"] = "bestvideo/best"
    opts_v["outtmpl"] = temp_vid
    if progress_hook:
        opts_v["progress_hooks"] = [lambda d: progress_hook(d, "Video")]
    
    try:
        with yt_dlp.YoutubeDL(opts_v) as ydl:
            ydl.download([stream_url])
    except Exception as e:
        print("\n      [FAIL] Video download error: " + str(e))
        return _ffmpeg_fallback(stream_url, final_out)

    # 2. Download Audio Track(s)
    successfully_downloaded_audios = []
    for l, aud_f in audio_files:
        print(f"\n      --> Downloading Audio track ({l})...")
        opts_a = base_opts.copy()
        opts_a["format"] = f"bestaudio[language={l}]/bestaudio[format_id*={l}]/bestaudio"
        opts_a["outtmpl"] = aud_f
        if progress_hook:
            opts_a["progress_hooks"] = [lambda d, lang_code=l: progress_hook(d, f"Audio ({lang_code})")]

        try:
            with yt_dlp.YoutubeDL(opts_a) as ydl:
                ydl.download([stream_url])
            successfully_downloaded_audios.append((l, aud_f))
        except Exception as e:
            print(f"\n      [WARN] Audio download error for '{l}': " + str(e))
            
    if not successfully_downloaded_audios:
        print("      [WARN] No audio tracks downloaded successfully. Saving video only...")
        if os.path.exists(temp_vid):
            os.rename(temp_vid, final_out)
            return True
        return False

    # 3. Merge with ffmpeg
    print("\n      --> Merging Video and Audio...")
    cmd = ["ffmpeg", "-y", "-i", temp_vid]
    
    # Add input for each audio file
    for _, aud_f in successfully_downloaded_audios:
        cmd.extend(["-i", aud_f])
        
    cmd.extend(["-map", "0:v:0"]) # Map video from first input
    
    # Map each audio input
    for i, (l, _) in enumerate(successfully_downloaded_audios):
        cmd.extend(["-map", f"{i+1}:a:0"])
        
    cmd.extend(["-c:v", "copy", "-c:a", "copy"])
    
    # Add language metadata for audio streams
    for i, (l, _) in enumerate(successfully_downloaded_audios):
        # map code to standard ISO 639-2 lang codes if possible, usually MKV players recognize standard ones
        lang_mapped = {"en":"eng", "hi":"hin", "ta":"tam", "te":"tel", "ja":"jpn"}.get(l, l)
        name_mapped = LANG_MAP.get(l, l)
        cmd.extend([f"-metadata:s:a:{i}", f"language={lang_mapped}"])
        cmd.extend([f"-metadata:s:a:{i}", f"title={name_mapped}"])
        
        # Make the first downloaded track default
        disp = "default" if i == 0 else "0"
        cmd.extend([f"-disposition:a:{i}", disp])
        
    cmd.extend(["-loglevel", "warning", final_out])
    
    try:
        result = subprocess.run(cmd, capture_output=False)
        if result.returncode == 0 and os.path.exists(final_out):
            print("\n      [OK] Merge complete!")
            
            # Create individual language videos
            if lang == "all":
                print("\n      --> Generating individual language video files...")
                for l, aud_f in successfully_downloaded_audios:
                    lang_name = LANG_MAP.get(l, l)
                    lang_filename_suffix = lang_name.lower().replace(" ", "")
                    indiv_out = os.path.join(output_path, f"{safe_title}_{lang_filename_suffix}.mp4")
                    print(f"          - Merging {lang_name} track -> {os.path.basename(indiv_out)}")
                    
                    lang_code_iso = {"en":"eng", "hi":"hin", "ta":"tam", "te":"tel", "ja":"jpn"}.get(l, l)
                    indiv_cmd = [
                        "ffmpeg", "-y",
                        "-i", temp_vid,
                        "-i", aud_f,
                        "-c:v", "copy",
                        "-c:a", "copy",
                        "-metadata:s:a:0", f"language={lang_code_iso}",
                        "-metadata:s:a:0", f"title={lang_name}",
                        "-loglevel", "warning",
                        indiv_out
                    ]
                    try:
                        subprocess.run(indiv_cmd, capture_output=False)
                    except Exception as e:
                        print(f"          [FAIL] Failed to create {lang_name} file: {e}")

            # Aggressive cleanup of all temporary fragments and .ytdl files
            import glob
            for prefix in [f"{safe_title}_temp_vid", f"{safe_title}_temp_aud"]:
                for f in glob.glob(os.path.join(output_path, prefix + "*")):
                    try: os.remove(f)
                    except: pass
            return True
        else:
            print("\n      [FAIL] ffmpeg merge failed.")
            return False
    except Exception as e:
        print("\n      [FAIL] ffmpeg merge error: " + str(e))
        return False


def _ffmpeg_fallback(stream_url, output_file):
    print("      Trying ffmpeg fallback for entire stream...")
    cmd = [
        "ffmpeg", "-y",
        "-user_agent", USER_AGENT,
        "-referer", "https://324fecbc-2d1d-4473-adb8-68632ec0f5eb.pages.dev/",
        "-i", stream_url,
        "-c", "copy",
        output_file,
    ]
    result = subprocess.run(cmd, capture_output=False)
    if result.returncode == 0:
        print("      [OK] ffmpeg download complete: " + output_file)
        return True
    else:
        print("      [FAIL] ffmpeg failed with code " + str(result.returncode))
        return False


# -- Main flow --
async def main_async(args):
    # Parse the episode page
    ep_data = parse_episode_page(args.url)

    # Show available options only
    if args.list:
        print_options(ep_data)
        return

    meta = ep_data.get("metadata", {})
    show = meta.get("show", "Unknown").replace(" ", "_")
    season = meta.get("season", 0)
    episode = meta.get("episode", 0)
    base_title = show + "_S" + str(season).zfill(2) + "E" + str(episode).zfill(2)

    # Get the Watch Online stream
    streams = ep_data.get("streams", [])
    if not streams:
        print("\n[!] No Watch Online streams found for this episode.")
        print("  You can still download using the direct links (--list to view).")
        return

    # Select stream (currently only one stream player, but with multiple languages)
    stream_info = streams[0]
    player_url = stream_info["play"]
    langs_available = [l["code"] for l in stream_info.get("languages", [])]

    print("\n[INFO] Available languages: " + ", ".join(LANG_MAP.get(c, c) for c in langs_available))

    if args.lang and args.lang not in langs_available:
        print("\n[WARN] Language '" + args.lang + "' not available. Available: " + str(langs_available))
        print("   Proceeding with default (first language in stream).")

    # Intercept the actual m3u8 stream URL
    captured_urls = await intercept_stream_url(player_url, timeout=args.timeout)

    if not captured_urls:
        print("\n[!] Could not capture any stream URLs from the player.")
        print("  Possible reasons:")
        print("  - The page requires an ad-click before playing")
        print("  - The stream is DRM-protected")
        print("  - Try increasing --timeout")
        return

    print("\n[INFO] Captured " + str(len(captured_urls)) + " stream URL(s):")
    for i, u in enumerate(captured_urls):
        print("  [" + str(i) + "] " + u[:100])

    # Select which URL to download
    target_url = captured_urls[0]
    if len(captured_urls) > 1 and args.stream_index is not None:
        target_url = captured_urls[args.stream_index]

    # Set output directory
    output_dir = args.output or "."
    os.makedirs(output_dir, exist_ok=True)

    # Download
    use_lang = "all" if args.all_audio else args.lang
    if use_lang == "all":
        lang_suffix = "_multi"
    elif use_lang:
        lang_name = LANG_MAP.get(use_lang, use_lang)
        lang_suffix = "_" + lang_name.lower().replace(" ", "")
    else:
        lang_suffix = ""
        
    res_suffix = f"_{args.res}p" if args.res else ""
    title = base_title + res_suffix + lang_suffix
    success = download_stream(target_url, output_dir, title, lang=use_lang, available_langs=langs_available, resolution=args.res)

    if success:
        print("\n[DONE] Done! Check: " + output_dir)
    else:
        print("\n[FAIL] Download failed. Please check errors above.")


def main():
    parser = argparse.ArgumentParser(
        description="Download anime episodes from archive.toonworld4all.me",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # List all quality and language options:
  python toonworld_downloader.py https://archive.toonworld4all.me/episode/jujutsu-kaisen-3x1 --list

  # Download to current directory (Watch Online stream):
  python toonworld_downloader.py https://archive.toonworld4all.me/episode/jujutsu-kaisen-3x1

  # Download with language tag in filename:
  python toonworld_downloader.py https://archive.toonworld4all.me/episode/jujutsu-kaisen-3x1 --lang en

  # Download with all available languages as separate audio tracks (MKV):
  python toonworld_downloader.py https://archive.toonworld4all.me/episode/jujutsu-kaisen-3x1 --all-audio

  # Download to a specific folder:
  python toonworld_downloader.py https://archive.toonworld4all.me/episode/jujutsu-kaisen-3x1 -o ./downloads

  # Increase timeout if player loads slowly:
  python toonworld_downloader.py https://archive.toonworld4all.me/episode/jujutsu-kaisen-3x1 --timeout 60
        """
    )
    parser.add_argument("url", help="Episode URL, e.g. https://archive.toonworld4all.me/episode/jujutsu-kaisen-3x1")
    parser.add_argument("--list", "-l", action="store_true",
                        help="List all available quality/language options and exit")
    parser.add_argument("--lang", "-L", default=None,
                        help="Language code (e.g. en, hi). Use 'all' or --all-audio for multi-language.")
    parser.add_argument("--all-audio", action="store_true",
                        help="Download all available language tracks into a multi-audio MKV file")
    parser.add_argument("--res", type=int, default=None,
                        help="Maximum video resolution to download (e.g. 1080, 720, 480). Default: best available.")
    parser.add_argument("--output", "-o", default=".",
                        help="Output directory (default: current directory)")
    parser.add_argument("--timeout", "-t", type=int, default=45,
                        help="Browser timeout in seconds (default: 45)")
    parser.add_argument("--stream-index", "-s", type=int, default=None,
                        help="Index of captured stream URL to download if multiple m3u8 URLs are found")

    args = parser.parse_args()

    # Validate URL
    if "toonworld4all.me" not in args.url:
        print("[WARN] URL doesn't look like a toonworld4all.me link. Proceeding anyway...")

    # Check dependencies
    missing = []
    if not HAS_PLAYWRIGHT:
        missing.append("playwright  ->  pip install playwright && python -m playwright install chromium")
    if not HAS_YTDLP:
        missing.append("yt-dlp      ->  pip install yt-dlp")

    if missing and not args.list:
        print("[WARN] Missing dependencies:")
        for m in missing:
            print("   - " + m)
        if not HAS_PLAYWRIGHT:
            sys.exit(1)

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
