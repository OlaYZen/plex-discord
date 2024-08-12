import configparser
import time
import requests
from plexapi.server import PlexServer
from pypresence import Presence, InvalidID
from flask import Flask, send_file, Response
from PIL import Image
from io import BytesIO
import os
import logging
import uuid
import json

# Load configuration from INI file
config = configparser.ConfigParser()
config.read('config.ini')

# Load translations from INI file with UTF-8 encoding
translations = configparser.ConfigParser()
with open('translations.ini', 'r', encoding='utf-8') as f:
    translations.read_file(f)
language = config['Language']['LANGUAGE']

# Plex Settings
PLEX_URL = config['Plex']['PLEX_URL']
PLEX_TOKEN = config['Plex']['PLEX_TOKEN']
SPECIFIC_USERNAME = config['Plex']['SPECIFIC_USERNAME']

# Discord Settings
DISCORD_CLIENT_ID = config['Discord']['DISCORD_CLIENT_ID']

# Album Cover Settings
ALBUM_COVER_SIZE = config.getint('Settings', 'ALBUM_COVER_SIZE')
ALBUM_COVER_URL = config['Settings']['ALBUM_COVER_URL']
ALBUM_COVER_URL_LENGTH = config.getint('Settings', 'ALBUM_COVER_URL_LENGTH')

# Logging Settings
LOG_FILE = config['Logging']['LOG_FILE']
LOG_LEVEL = config['Logging']['LOG_LEVEL']

# Set up logging
logging.basicConfig(filename=LOG_FILE, level=LOG_LEVEL,
                    format='%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]', filemode='w')
logger = logging.getLogger(__name__)

# Load saved album cover IDs from file
def load_album_cover_ids(filename='album_cover_ids.json'):
    if os.path.exists(filename):
        with open(filename, 'r') as f:
            return json.load(f)
    return {}

# Save album cover IDs to file
def save_album_cover_ids(album_cover_ids, filename='album_cover_ids.json'):
    with open(filename, 'w') as f:
        json.dump(album_cover_ids, f, indent=4, separators=(',', ': '))


# Load album cover IDs
album_cover_ids = load_album_cover_ids()

# Flask app to serve album covers
app = Flask(__name__)
album_covers = {}
latest_cover_id = None
current_song_id = None
current_album_id = None

@app.route('/album_cover')
def latest_album_cover():
    if latest_cover_id and latest_cover_id in album_covers:
        cover = album_covers[latest_cover_id]
        cover.seek(0)
        return send_file(BytesIO(cover.getvalue()), mimetype='image/jpeg')
    return Response("No image available", status=404)

@app.route('/album_cover/<cover_id>')
def album_cover(cover_id):
    if cover_id not in album_covers:
        return Response("No image available", status=404)
    cover = album_covers[cover_id]
    cover.seek(0)
    return send_file(BytesIO(cover.getvalue()), mimetype='image/jpeg')

def resize_image(image_data):
    with Image.open(BytesIO(image_data)) as img:
        width, height = img.size
        # Calculate new size
        if width < ALBUM_COVER_SIZE or height < ALBUM_COVER_SIZE:
            new_width = max(width, ALBUM_COVER_SIZE)
            new_height = max(height, ALBUM_COVER_SIZE)
        else:
            new_width = min(width, ALBUM_COVER_SIZE)
            new_height = min(height, ALBUM_COVER_SIZE)
        
        # Maintain aspect ratio
        img.thumbnail((new_width, new_height), Image.LANCZOS)

        # Save to BytesIO
        output = BytesIO()
        try:
            img.convert("RGB").save(output, format='JPEG')
        except Exception as e:
            logger.error(f"JPEG conversion failed: {e}, saving as PNG instead.")
            output = BytesIO()
            img.save(output, format='PNG')
        return output

def generate_random_id():
    return uuid.uuid4().hex[:ALBUM_COVER_URL_LENGTH]

def get_currently_playing_music_for_user():
    global current_song_id, current_album_id, latest_cover_id
    
    plex = PlexServer(PLEX_URL, PLEX_TOKEN)
    sessions = plex.sessions()

    if not sessions:
        return None

    for session in sessions:
        user = session.usernames[0]
        if user == SPECIFIC_USERNAME and session.type == 'track':
            title = session.title
            artist = session.grandparentTitle  # Artist name
            album = session.parentTitle  # Album name
            album_cover_url = plex.url(session.thumb)  # Album cover URL
            state = session.player.state
            current_time = session.viewOffset  # Current time in milliseconds
            duration = session.duration  # Total duration in milliseconds

            # Generate new ID if the song changes and it belongs to a different album
            if title != current_song_id or album != current_album_id:
                current_song_id = title
                current_album_id = album
                if album in album_cover_ids:
                    cover_id = album_cover_ids[album]
                else:
                    cover_id = generate_random_id()
                    album_cover_ids[album] = cover_id
                    save_album_cover_ids(album_cover_ids)
            else:
                cover_id = latest_cover_id  # Keep the same cover ID if the song and album are the same

            # Download the album cover with authentication
            headers = {
                'X-Plex-Token': PLEX_TOKEN
            }
            response = requests.get(album_cover_url, headers=headers)
            if response.status_code == 200:
                resized_image = resize_image(response.content)
                album_covers[cover_id] = resized_image
                latest_cover_id = cover_id
                logger.info(f"Album cover URL: {ALBUM_COVER_URL}/{cover_id}")
                logger.info(f"Downloaded and resized album cover from {album_cover_url}")
            else:
                logger.error(f"Failed to download album cover from {album_cover_url}, status code: {response.status_code}")

            return {
                "title": title,
                "artist": artist,
                "album": album,
                "album_cover_id": cover_id,
                "state": state,
                "current_time": current_time,
                "duration": duration
            }

    return None

def update_discord_presence(rpc, music_info, local_ip):
    if music_info and music_info['state'] == 'playing':
        end_time = int(time.time() + (music_info['duration'] - music_info['current_time']) / 1000)

        rpc.update(
            details=f"{music_info['title']}",
            state=f"{translations[language]['by']} {music_info['artist']}",
            large_image=f"{ALBUM_COVER_URL}/{music_info['album_cover_id']}",  # Serve the image from the local server
            large_text=f"{translations[language]['album']}: {music_info['album']}",
            small_image=f"plexamp",
            small_text=f"{translations[language]['listening_to']} {music_info['title']}",
            end=end_time
        )
    else:
        rpc.clear()

if __name__ == "__main__":
    try:
        rpc = Presence(DISCORD_CLIENT_ID)
        rpc.connect()
    except InvalidID:
        logger.error("The Discord Client ID is invalid. Please check your config.ini and make sure you have the correct Client ID.")
        exit(1)

    # Start the Flask server in a separate thread
    from threading import Thread
    import socket

    # Get local IP address
    local_ip = socket.gethostbyname(socket.gethostname())
    
    server_thread = Thread(target=app.run, kwargs={'host': '0.0.0.0', 'port': 8081})
    server_thread.daemon = True
    server_thread.start()

    try:
        last_music_info = None
        while True:
            music_info = get_currently_playing_music_for_user()
            if music_info != last_music_info or (music_info and music_info['state'] != last_music_info['state']):
                update_discord_presence(rpc, music_info, local_ip)
                last_music_info = music_info
            time.sleep(1)  # Update every second
    except KeyboardInterrupt:
        rpc.clear()
        logger.info("Exiting...")
    except Exception as e:
        logger.error(f"An error occurred: {e}")
        rpc.clear()
