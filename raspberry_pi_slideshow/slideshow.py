#!/usr/bin/env python3

import os
import sys
import json
import time
import hashlib
import threading
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import requests
import pygame


# ============================================================
# Configuration
# ============================================================

API_URL = os.environ.get("API_URL")
DEVICE_ID = os.environ.get("DEVICE_ID")
DEVICE_KEY = os.environ.get("DEVICE_KEY")

IMAGE_FOLDER = Path(
    os.environ.get(
        "IMAGE_FOLDER",
        "/home/pi/image_receiver/images"
    )
)

MANIFEST_FILE = Path(
    os.environ.get(
        "MANIFEST_FILE",
        "/home/pi/image_receiver/manifest.json"
    )
)

SLIDE_DELAY = int(
    os.environ.get("SLIDE_DELAY", "5")
)

DEFAULT_POLL_INTERVAL = int(
    os.environ.get("DEFAULT_POLL_INTERVAL", "60")
)


# ============================================================
# Validation
# ============================================================

if not API_URL:
    print("ERROR: API_URL is not configured")
    sys.exit(1)

if not DEVICE_ID:
    print("ERROR: DEVICE_ID is not configured")
    sys.exit(1)

if not DEVICE_KEY:
    print("ERROR: DEVICE_KEY is not configured")
    sys.exit(1)


IMAGE_FOLDER.mkdir(parents=True, exist_ok=True)


# ============================================================
# Globals
# ============================================================

playlist_lock = threading.Lock()

playlist = []

stop_event = threading.Event()

session = requests.Session()

session.headers.update({
    "X-Device-Id": DEVICE_ID,
    "X-Device-Key": DEVICE_KEY,
    "User-Agent": "RaspberryPi-DigitalSignage/1.0"
})


# ============================================================
# Utility functions
# ============================================================

def calculate_md5(file_path):
    """
    Calculate MD5 without loading the entire image into memory.
    """

    md5 = hashlib.md5()

    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)

            if not chunk:
                break

            md5.update(chunk)

    return md5.hexdigest()


def load_manifest():
    """
    Load local manifest.
    """

    if not MANIFEST_FILE.exists():
        return {}

    try:
        with open(MANIFEST_FILE, "r") as f:
            data = json.load(f)

        if isinstance(data, dict):
            return data

    except Exception as e:
        print(f"Manifest read failed: {e}")

    return {}


def save_manifest(manifest):
    """
    Save manifest atomically.
    """

    temp_file = MANIFEST_FILE.with_suffix(".tmp")

    try:
        with open(temp_file, "w") as f:
            json.dump(
                manifest,
                f,
                indent=2
            )

        os.replace(temp_file, MANIFEST_FILE)

    except Exception as e:
        print(f"Manifest save failed: {e}")

        try:
            temp_file.unlink(missing_ok=True)
        except Exception:
            pass


def get_file_extension(url):
    """
    Determine image extension from URL.
    """

    try:
        path = urlparse(url).path
        extension = Path(path).suffix.lower()

        if extension in [
            ".jpg",
            ".jpeg",
            ".png",
            ".webp"
        ]:
            return extension

    except Exception:
        pass

    return ".jpg"


# ============================================================
# API
# ============================================================

def fetch_feed():
    """
    Fetch device feed from backend.
    """

    try:
        response = session.get(
            API_URL,
            timeout=(5, 15)
        )

        response.raise_for_status()

        data = response.json()

        if not data.get("status"):
            print(
                "API returned unsuccessful status:",
                data.get("message")
            )
            return None, DEFAULT_POLL_INTERVAL

        feed_data = data.get("data", {})

        posters = feed_data.get("posters", [])

        poll_interval = feed_data.get(
            "pollIntervalSeconds",
            DEFAULT_POLL_INTERVAL
        )

        try:
            poll_interval = int(poll_interval)
        except Exception:
            poll_interval = DEFAULT_POLL_INTERVAL

        if poll_interval < 5:
            poll_interval = 5

        return posters, poll_interval

    except requests.RequestException as e:
        print(f"API request failed: {e}")

    except ValueError as e:
        print(f"Invalid JSON response: {e}")

    except Exception as e:
        print(f"Feed error: {e}")

    return None, DEFAULT_POLL_INTERVAL


# ============================================================
# Image download
# ============================================================

def download_image(url, destination):
    """
    Download image to destination.
    """

    temp_file = None

    try:
        with session.get(
            url,
            stream=True,
            timeout=(5, 30)
        ) as response:

            response.raise_for_status()

            with tempfile.NamedTemporaryFile(
                dir=IMAGE_FOLDER,
                prefix=".download_",
                suffix=".tmp",
                delete=False
            ) as f:

                temp_file = Path(f.name)

                for chunk in response.iter_content(
                    chunk_size=1024 * 1024
                ):
                    if chunk:
                        f.write(chunk)

        return temp_file

    except Exception as e:
        print(f"Image download failed: {url}")
        print(f"Reason: {e}")

        if temp_file:
            try:
                temp_file.unlink(missing_ok=True)
            except Exception:
                pass

        return None


# ============================================================
# Synchronization
# ============================================================

def synchronize_images(posters):
    """
    Synchronize API posters with local image cache.

    MD5 is calculated locally because the API currently
    doesn't provide an MD5 value.
    """

    manifest = load_manifest()

    new_manifest = {}

    new_playlist = []

    active_ids = set()

    for poster in posters:

        poster_id = poster.get("_id")
        image_url = poster.get("imageUrl")

        if not poster_id or not image_url:
            continue

        # Only display active/non-deleted posters.
        if poster.get("isDeleted", False):
            continue

        if not poster.get("isActive", True):
            continue

        active_ids.add(poster_id)

        extension = get_file_extension(image_url)

        filename = f"{poster_id}{extension}"

        local_file = IMAGE_FOLDER / filename

        old_entry = manifest.get(poster_id, {})

        old_url = old_entry.get("url")
        old_md5 = old_entry.get("md5")

        needs_download = False

        # ----------------------------------------------------
        # First time poster
        # ----------------------------------------------------

        if not local_file.exists():
            needs_download = True

        # ----------------------------------------------------
        # URL changed
        # ----------------------------------------------------

        elif old_url != image_url:
            print(
                f"[SYNC] URL changed: {poster_id}"
            )
            needs_download = True

        # ----------------------------------------------------
        # Manifest doesn't contain MD5
        # ----------------------------------------------------

        elif not old_md5:
            print(
                f"[SYNC] MD5 missing: {poster_id}"
            )
            needs_download = True

        # ----------------------------------------------------
        # Download and compare
        # ----------------------------------------------------

        if needs_download:

            print(
                f"[SYNC] Downloading: {poster_id}"
            )

            temp_file = download_image(
                image_url,
                local_file
            )

            if temp_file is None:

                # If an old file exists, keep it.
                if local_file.exists():

                    print(
                        f"[SYNC] Keeping existing image: "
                        f"{poster_id}"
                    )

                    try:
                        current_md5 = calculate_md5(
                            local_file
                        )
                    except Exception:
                        current_md5 = None

                    new_manifest[poster_id] = {
                        "url": image_url,
                        "md5": current_md5,
                        "file": filename
                    }

                    new_playlist.append(
                        str(local_file)
                    )

                continue

            try:
                new_md5 = calculate_md5(temp_file)

                # Compare with existing image.
                if local_file.exists():

                    try:
                        current_md5 = calculate_md5(
                            local_file
                        )
                    except Exception:
                        current_md5 = None

                    if (
                        current_md5
                        and current_md5 == new_md5
                    ):
                        print(
                            f"[SYNC] Unchanged: "
                            f"{poster_id}"
                        )

                        temp_file.unlink(
                            missing_ok=True
                        )

                    else:
                        print(
                            f"[SYNC] Updated: "
                            f"{poster_id}"
                        )

                        os.replace(
                            temp_file,
                            local_file
                        )

                else:

                    print(
                        f"[SYNC] Added: "
                        f"{poster_id}"
                    )

                    os.replace(
                        temp_file,
                        local_file
                    )

                new_manifest[poster_id] = {
                    "url": image_url,
                    "md5": new_md5,
                    "file": filename
                }

                new_playlist.append(
                    str(local_file)
                )

            except Exception as e:

                print(
                    f"[SYNC] Failed processing "
                    f"{poster_id}: {e}"
                )

                try:
                    temp_file.unlink(
                        missing_ok=True
                    )
                except Exception:
                    pass

        else:

            # Existing image hasn't changed according
            # to our local manifest.
            new_manifest[poster_id] = {
                "url": image_url,
                "md5": old_md5,
                "file": filename
            }

            new_playlist.append(
                str(local_file)
            )

    # ========================================================
    # Remove posters no longer returned by API
    # ========================================================

    for poster_id, entry in manifest.items():

        if poster_id in active_ids:
            continue

        filename = entry.get("file")

        if filename:

            old_file = IMAGE_FOLDER / filename

            try:
                if old_file.exists():
                    old_file.unlink()

                    print(
                        f"[SYNC] Removed: "
                        f"{poster_id}"
                    )

            except Exception as e:
                print(
                    f"[SYNC] Could not remove "
                    f"{old_file}: {e}"
                )

    # ========================================================
    # Save manifest
    # ========================================================

    save_manifest(new_manifest)

    # ========================================================
    # Update playlist atomically
    # ========================================================

    with playlist_lock:
        playlist.clear()
        playlist.extend(new_playlist)

    print(
        f"[SYNC] Synchronization complete. "
        f"{len(new_playlist)} images available."
    )


# ============================================================
# Background synchronization thread
# ============================================================

def sync_worker():

    print("[SYNC] Background synchronization started")

    poll_interval = DEFAULT_POLL_INTERVAL

    while not stop_event.is_set():

        posters, api_interval = fetch_feed()

        if posters is not None:

            try:
                synchronize_images(posters)

                poll_interval = api_interval

            except Exception as e:

                print(
                    f"[SYNC] Synchronization error: {e}"
                )

        else:

            print(
                "[SYNC] API unavailable. "
                "Keeping cached images."
            )

        print(
            f"[SYNC] Next check in "
            f"{poll_interval} seconds"
        )

        stop_event.wait(poll_interval)


# ============================================================
# Playlist
# ============================================================

def get_playlist():

    with playlist_lock:
        return list(playlist)


def load_existing_cache():

    """
    If the Pi starts without network access,
    use previously downloaded images.
    """

    manifest = load_manifest()

    cached = []

    for poster_id, entry in manifest.items():

        filename = entry.get("file")

        if not filename:
            continue

        file_path = IMAGE_FOLDER / filename

        if file_path.exists():
            cached.append(str(file_path))

    with playlist_lock:
        playlist.clear()
        playlist.extend(cached)

    print(
        f"[CACHE] Loaded {len(cached)} cached images"
    )


# ============================================================
# Pygame
# ============================================================

def show_image(screen, image_path):

    try:

        image = pygame.image.load(
            image_path
        ).convert()

        screen_width, screen_height = (
            screen.get_size()
        )

        image_width, image_height = (
            image.get_size()
        )

        scale = min(
            screen_width / image_width,
            screen_height / image_height
        )

        new_width = int(
            image_width * scale
        )

        new_height = int(
            image_height * scale
        )

        image = pygame.transform.smoothscale(
            image,
            (
                new_width,
                new_height
            )
        )

        screen.fill((0, 0, 0))

        x = (
            screen_width - new_width
        ) // 2

        y = (
            screen_height - new_height
        ) // 2

        screen.blit(
            image,
            (x, y)
        )

        pygame.display.flip()

    except Exception as e:

        print(
            f"[DISPLAY] Failed to display "
            f"{image_path}: {e}"
        )


# ============================================================
# Main
# ============================================================

def main():

    print("===================================")
    print(" Raspberry Pi Digital Signage")
    print("===================================")
    print(f"Device ID : {DEVICE_ID}")
    print(f"API       : {API_URL}")
    print(f"Images    : {IMAGE_FOLDER}")
    print(f"Slide     : {SLIDE_DELAY} seconds")
    print("===================================")

    # Load old images first.
    load_existing_cache()

    # Start background API synchronization.
    sync_thread = threading.Thread(
        target=sync_worker,
        daemon=True
    )

    sync_thread.start()

    # Initialize pygame.
    pygame.init()

    info = pygame.display.Info()

    print(
        f"Slideshow: drive "
        f"{info.current_w} x {info.current_h}"
    )

    screen = pygame.display.set_mode(
        (0, 0),
        pygame.FULLSCREEN
    )

    pygame.mouse.set_visible(False)

    clock = pygame.time.Clock()

    current_index = 0
    last_change = 0

    running = True

    while running:

        # ----------------------------------------------------
        # Handle pygame events
        # ----------------------------------------------------

        for event in pygame.event.get():

            if event.type == pygame.QUIT:
                running = False

            elif event.type == pygame.KEYDOWN:

                if event.key == pygame.K_ESCAPE:
                    running = False

        # ----------------------------------------------------
        # Get current playlist
        # ----------------------------------------------------

        images = get_playlist()

        if images:

            # Prevent index going out of range after
            # synchronization changes the playlist.
            if current_index >= len(images):
                current_index = 0

            now = time.monotonic()

            if (
                last_change == 0
                or now - last_change >= SLIDE_DELAY
            ):

                image_path = images[current_index]

                print(
                    f"[DISPLAY] "
                    f"{current_index + 1}/"
                    f"{len(images)} "
                    f"{image_path}"
                )

                show_image(
                    screen,
                    image_path
                )

                current_index = (
                    current_index + 1
                ) % len(images)

                last_change = now

        else:

            # No images available.
            screen.fill((0, 0, 0))

            pygame.display.flip()

            last_change = time.monotonic()

        clock.tick(10)

    # --------------------------------------------------------
    # Shutdown
    # --------------------------------------------------------

    print("Stopping slideshow...")

    stop_event.set()

    sync_thread.join(timeout=2)

    pygame.quit()


if __name__ == "__main__":

    try:
        main()

    except KeyboardInterrupt:

        print("\nInterrupted")

        stop_event.set()

        pygame.quit()
