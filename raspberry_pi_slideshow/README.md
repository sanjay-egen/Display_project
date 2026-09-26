# Raspberry Pi Slideshow v2

Supports:
- Google Drive folder -> Pi local cache -> Pygame
- Existing laptop HTTP source
- Recursive Drive subfolders
- Incremental downloads (unchanged Drive images are NOT downloaded again)
- Offline playback from local cache
- systemd automatic startup/restart
- Atomic downloads so a partial image is never displayed

## Google Drive mode

1. Copy the files into `/home/pi/image_receiver/`.
2. Put your Google OAuth Desktop credentials at:
   `/home/pi/image_receiver/credentials.json`
3. Edit `slideshow.env` and set `DRIVE_FOLDER_ID`.
4. Install dependencies:
   `python3 -m pip install -r requirements.txt`
5. Run once interactively to authenticate:
   `cd /home/pi/image_receiver && source slideshow.env && python3 slideshow.py`
6. Stop with ESC.
7. Install the service:
   `sudo cp image-slideshow.service /etc/systemd/system/`
   `sudo systemctl daemon-reload`
   `sudo systemctl enable --now image-slideshow.service`
8. Logs:
   `journalctl -u image-slideshow.service -f`

The first authentication creates `token.json`. Keep both credentials.json and token.json private.

## Offline behavior

If Drive or the laptop is unavailable, the slideshow continues using images already in `/home/pi/image_receiver/images`.

## Laptop mode

Set `SOURCE=laptop`, `LAPTOP_IP=192.168.1.7`, and `SERVER_PORT=8000`.

For best incremental synchronization, make the laptop `/images` endpoint return metadata such as `name`, `size`, `mtime`, or `etag`. With a name-only endpoint, the program avoids repeatedly downloading files it already has, but it cannot detect an edited file reliably.

## systemd display note

The included service assumes X display `:0`. If Pygame fails under your Raspberry Pi OS desktop, run `echo $DISPLAY` and `echo $XAUTHORITY` from the graphical session and adjust the service. Wayland configurations may require a user graphical-session service instead.
