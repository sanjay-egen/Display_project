#!/usr/bin/env python3
import os, sys, json, time, uuid, hashlib, threading, tempfile
from pathlib import Path
from datetime import datetime, timezone
from queue import Queue, Empty
from urllib.parse import urlparse

import requests
import pygame

API_URL = os.environ.get("API_URL")
ANALYTICS_URL = os.environ.get("ANALYTICS_URL")
DEVICE_ID = os.environ.get("DEVICE_ID")
DEVICE_KEY = os.environ.get("DEVICE_KEY")
IMAGE_FOLDER = Path(os.environ.get("IMAGE_FOLDER", "/home/pi/image_receiver/images"))
MANIFEST_FILE = Path(os.environ.get("MANIFEST_FILE", "/home/pi/image_receiver/manifest.json"))
ANALYTICS_QUEUE_FILE = Path(os.environ.get("ANALYTICS_QUEUE_FILE", "/home/pi/image_receiver/analytics_queue.json"))
SLIDE_DELAY = float(os.environ.get("SLIDE_DELAY", "5"))
DEFAULT_POLL_INTERVAL = int(os.environ.get("DEFAULT_POLL_INTERVAL", "60"))
ANALYTICS_RETRY_SECONDS = int(os.environ.get("ANALYTICS_RETRY_SECONDS", "10"))

missing = [k for k, v in {
    "API_URL": API_URL, "ANALYTICS_URL": ANALYTICS_URL,
    "DEVICE_ID": DEVICE_ID, "DEVICE_KEY": DEVICE_KEY
}.items() if not v]
if missing:
    print("ERROR: Missing configuration: " + ", ".join(missing))
    sys.exit(1)

IMAGE_FOLDER.mkdir(parents=True, exist_ok=True)
playlist_lock = threading.Lock()
playlist = []
stop_event = threading.Event()
analytics_queue = Queue()
analytics_file_lock = threading.Lock()

session = requests.Session()
session.headers.update({
    "X-Device-Id": DEVICE_ID,
    "X-Device-Key": DEVICE_KEY,
    "User-Agent": "RaspberryPi-DigitalSignage/1.0"
})

def calculate_md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def load_json(path, default):
    if not path.exists():
        return default
    try:
        with open(path) as f:
            value = json.load(f)
        return value
    except Exception as e:
        print(f"[FILE] Read failed {path}: {e}")
        return default

def save_json_atomic(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(value, f, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        print(f"[FILE] Save failed {path}: {e}")
        try: tmp.unlink(missing_ok=True)
        except Exception: pass

def load_manifest():
    data = load_json(MANIFEST_FILE, {})
    return data if isinstance(data, dict) else {}

def save_manifest(data):
    save_json_atomic(MANIFEST_FILE, data)

def utc_iso(dt=None):
    dt = dt or datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"

def get_extension(url):
    try:
        ext = Path(urlparse(url).path).suffix.lower()
        if ext in (".jpg", ".jpeg", ".png", ".webp"):
            return ext
    except Exception:
        pass
    return ".jpg"

def fetch_feed():
    try:
        r = session.get(API_URL, timeout=(5, 15))
        r.raise_for_status()
        payload = r.json()
        if not payload.get("status"):
            print(f"[FEED] API failure: {payload.get('message')}")
            return None, DEFAULT_POLL_INTERVAL
        data = payload.get("data", {})
        posters = data.get("posters", [])
        try:
            interval = max(5, int(data.get("pollIntervalSeconds", DEFAULT_POLL_INTERVAL)))
        except Exception:
            interval = DEFAULT_POLL_INTERVAL
        return posters, interval
    except requests.RequestException as e:
        print(f"[FEED] Request failed: {e}")
    except ValueError as e:
        print(f"[FEED] Invalid JSON: {e}")
    except Exception as e:
        print(f"[FEED] Error: {e}")
    return None, DEFAULT_POLL_INTERVAL

def download_image(url):
    tmp = None
    try:
        with session.get(url, stream=True, timeout=(5, 30)) as r:
            r.raise_for_status()
            with tempfile.NamedTemporaryFile(
                dir=IMAGE_FOLDER, prefix=".download_", suffix=".tmp", delete=False
            ) as f:
                tmp = Path(f.name)
                for chunk in r.iter_content(1024 * 1024):
                    if chunk:
                        f.write(chunk)
        return tmp
    except Exception as e:
        print(f"[IMAGE] Download failed: {e}")
        if tmp:
            try: tmp.unlink(missing_ok=True)
            except Exception: pass
        return None

def synchronize_images(posters):
    manifest = load_manifest()
    new_manifest = {}
    new_playlist = []
    active_ids = set()

    for poster in posters:
        # poster_id is exactly poster["_id"] from the feed.
        poster_id = poster.get("_id")
        image_url = poster.get("imageUrl")
        if not poster_id or not image_url:
            continue
        if poster.get("isDeleted", False) or not poster.get("isActive", True):
            continue

        active_ids.add(poster_id)
        filename = f"{poster_id}{get_extension(image_url)}"
        local_file = IMAGE_FOLDER / filename
        old = manifest.get(poster_id, {})
        needs_download = (
            not local_file.exists()
            or old.get("url") != image_url
            or not old.get("md5")
        )

        if needs_download:
            print(f"[SYNC] Checking {poster_id}")
            tmp = download_image(image_url)
            if tmp is None:
                if local_file.exists():
                    md5 = calculate_md5(local_file)
                    new_manifest[poster_id] = {"url": image_url, "md5": md5, "file": filename}
                    new_playlist.append({"posterId": poster_id, "file": str(local_file)})
                continue

            try:
                new_md5 = calculate_md5(tmp)
                current_md5 = calculate_md5(local_file) if local_file.exists() else None
                if current_md5 == new_md5:
                    print(f"[SYNC] Unchanged {poster_id}")
                    tmp.unlink(missing_ok=True)
                else:
                    print(f"[SYNC] {'Updated' if local_file.exists() else 'Added'} {poster_id}")
                    os.replace(tmp, local_file)
                new_manifest[poster_id] = {"url": image_url, "md5": new_md5, "file": filename}
                new_playlist.append({"posterId": poster_id, "file": str(local_file)})
            except Exception as e:
                print(f"[SYNC] Processing failed for {poster_id}: {e}")
                try: tmp.unlink(missing_ok=True)
                except Exception: pass
        else:
            new_manifest[poster_id] = {"url": image_url, "md5": old["md5"], "file": filename}
            new_playlist.append({"posterId": poster_id, "file": str(local_file)})

    for poster_id, entry in manifest.items():
        if poster_id in active_ids:
            continue
        filename = entry.get("file")
        if filename:
            old_file = IMAGE_FOLDER / filename
            try:
                if old_file.exists():
                    old_file.unlink()
                    print(f"[SYNC] Removed {poster_id}")
            except Exception as e:
                print(f"[SYNC] Remove failed {old_file}: {e}")

    save_manifest(new_manifest)
    with playlist_lock:
        playlist[:] = new_playlist
    print(f"[SYNC] Complete: {len(new_playlist)} image(s)")

def sync_worker():
    interval = DEFAULT_POLL_INTERVAL
    while not stop_event.is_set():
        posters, api_interval = fetch_feed()
        if posters is not None:
            try:
                synchronize_images(posters)
                interval = api_interval
            except Exception as e:
                print(f"[SYNC] Error: {e}")
        else:
            print("[SYNC] Feed unavailable; keeping cache")
        stop_event.wait(interval)

def load_analytics_queue():
    data = load_json(ANALYTICS_QUEUE_FILE, [])
    return data if isinstance(data, list) else []

def save_analytics_queue(events):
    save_json_atomic(ANALYTICS_QUEUE_FILE, events)

def enqueue_analytics(event):
    with analytics_file_lock:
        events = load_analytics_queue()
        events.append(event)
        save_analytics_queue(events)
    analytics_queue.put(True)

def post_analytics(event):
    try:
        r = session.post(ANALYTICS_URL, json=event, timeout=(5, 15))
        if 200 <= r.status_code < 300:
            print(f"[ANALYTICS] Sent {event['posterId']} {event['durationMs']}ms")
            return True
        print(f"[ANALYTICS] HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[ANALYTICS] Failed: {e}")
    return False

def analytics_worker():
    with analytics_file_lock:
        pending = load_analytics_queue()
    for _ in pending:
        analytics_queue.put(True)

    while not stop_event.is_set():
        try:
            analytics_queue.get(timeout=1)
        except Empty:
            continue

        while not stop_event.is_set():
            with analytics_file_lock:
                events = load_analytics_queue()
            if not events:
                break
            event = events[0]
            if post_analytics(event):
                with analytics_file_lock:
                    current = load_analytics_queue()
                    if current:
                        current.pop(0)
                    save_analytics_queue(current)
            else:
                stop_event.wait(ANALYTICS_RETRY_SECONDS)
                break

def load_existing_cache():
    manifest = load_manifest()
    cached = []
    for poster_id, entry in manifest.items():
        filename = entry.get("file")
        if filename and (IMAGE_FOLDER / filename).exists():
            cached.append({"posterId": poster_id, "file": str(IMAGE_FOLDER / filename)})
    with playlist_lock:
        playlist[:] = cached
    print(f"[CACHE] Loaded {len(cached)} cached image(s)")

def get_playlist():
    with playlist_lock:
        return list(playlist)

def show_image(screen, image_path):
    try:
        image = pygame.image.load(image_path).convert()
        sw, sh = screen.get_size()
        iw, ih = image.get_size()
        scale = min(sw / iw, sh / ih)
        image = pygame.transform.smoothscale(image, (max(1, int(iw * scale)), max(1, int(ih * scale))))
        screen.fill((0, 0, 0))
        screen.blit(image, ((sw - image.get_width()) // 2, (sh - image.get_height()) // 2))
        pygame.display.flip()
        return True
    except Exception as e:
        print(f"[DISPLAY] Failed {image_path}: {e}")
        return False

def main():
    print("======================================")
    print(" Raspberry Pi Digital Signage")
    print("======================================")
    print(f"Device ID : {DEVICE_ID}")
    print(f"Feed API  : {API_URL}")
    print(f"Analytics : {ANALYTICS_URL}")
    print(f"Images    : {IMAGE_FOLDER}")
    print(f"Slide     : {SLIDE_DELAY}s")
    print("======================================")

    load_existing_cache()

    threading.Thread(target=sync_worker, daemon=True, name="FeedSync").start()
    threading.Thread(target=analytics_worker, daemon=True, name="Analytics").start()

    pygame.init()
    info = pygame.display.Info()
    print(f"[DISPLAY] {info.current_w} x {info.current_h}")
    screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    pygame.mouse.set_visible(False)
    clock = pygame.time.Clock()

    current_index = 0
    running = True

    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT or (
                event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE
            ):
                running = False

        images = get_playlist()
        if not images:
            screen.fill((0, 0, 0))
            pygame.display.flip()
            clock.tick(10)
            continue

        if current_index >= len(images):
            current_index = 0

        poster = images[current_index]
        poster_id = poster["posterId"]
        image_path = poster["file"]
        started_at = datetime.now(timezone.utc)

        print(f"[DISPLAY] {current_index + 1}/{len(images)} posterId={poster_id}")
        displayed = show_image(screen, image_path)

        if displayed:
            end_time = time.monotonic() + SLIDE_DELAY
            while running and time.monotonic() < end_time:
                for event in pygame.event.get():
                    if event.type == pygame.QUIT or (
                        event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE
                    ):
                        running = False
                clock.tick(20)

            duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
            event = {
                "eventId": str(uuid.uuid4()),
                "posterId": poster_id,
                "startedAt": utc_iso(started_at),
                "durationMs": duration_ms
            }
            print(f"[ANALYTICS] Queue posterId={poster_id} durationMs={duration_ms}")
            enqueue_analytics(event)

        current_index = (current_index + 1) % len(images)

    stop_event.set()
    pygame.quit()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        stop_event.set()
        pygame.quit()
