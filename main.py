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
import subprocess
import platform
import uuid
import json
from threading import Thread
import socket
import argparse

# Argument parsing
parser = argparse.ArgumentParser(description="Plex to Discord Presence Script")
parser.add_argument('-s', '--Silent', action='store_true', help="Run the script in silent mode without displaying the UI.")
parser.add_argument('-g', '--GUI', action='store_true', help="Run the script with a graphical user interface.")
# The help argument is automatically provided by argparse, so no need to redefine it.
args = parser.parse_args()


# Load configuration from INI file
config = configparser.ConfigParser()

def load_config():
    config.read('config.ini')

load_config()

# Set up logging before using it
LOG_FILE = config['Logging']['LOG_FILE']
LOG_LEVEL = config['Logging']['LOG_LEVEL']

logging.basicConfig(filename=LOG_FILE, level=LOG_LEVEL,
                    format='%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]', filemode='w')
logger = logging.getLogger(__name__)

logger.debug("Configuration file loaded.")

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

# Platform priority (convert to list for easier handling)
PLATFORM_PRIORITY = config['Settings']['PLATFORM_PRIORITY'].split(', ')

# Load saved album cover IDs from file
def load_album_cover_ids(filename='album_cover_ids.json'):
    if os.path.exists(filename):
        with open(filename, 'r') as f:
            logger.debug("Loaded album cover IDs from file.")
            return json.load(f)
    logger.debug("No album cover IDs file found, starting with an empty dictionary.")
    return {}

# Save album cover IDs to file
def save_album_cover_ids(album_cover_ids, filename='album_cover_ids.json'):
    with open(filename, 'w') as f:
        json.dump(album_cover_ids, f, indent=4, separators=(',', ': '))
    logger.debug("Saved album cover IDs to file.")

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
        logger.debug("Serving the latest album cover.")
        return send_file(BytesIO(cover.getvalue()), mimetype='image/jpeg')
    logger.warning("No image available to serve as the latest album cover.")
    return Response("No image available", status=404)

@app.route('/album_cover/<cover_id>')
def album_cover(cover_id):
    if cover_id not in album_covers:
        logger.warning(f"Album cover ID {cover_id} not found.")
        return Response("No image available", status=404)
    cover = album_covers[cover_id]
    cover.seek(0)
    logger.debug(f"Serving album cover with ID {cover_id}.")
    return send_file(BytesIO(cover.getvalue()), mimetype='image/jpeg')

def resize_image(image_data):
    with Image.open(BytesIO(image_data)) as img:
        width, height = img.size
        logger.debug(f"Original image size: {width}x{height}.")
        
        # Calculate new size
        if width < ALBUM_COVER_SIZE or height < ALBUM_COVER_SIZE:
            new_width = max(width, ALBUM_COVER_SIZE)
            new_height = max(height, ALBUM_COVER_SIZE)
        else:
            new_width = min(width, ALBUM_COVER_SIZE)
            new_height = min(height, ALBUM_COVER_SIZE)
        
        logger.debug(f"Resizing image to maintain aspect ratio, new size: {new_width}x{new_height}.")
        
        # Maintain aspect ratio
        img.thumbnail((new_width, new_height), Image.LANCZOS)

        # Save to BytesIO
        output = BytesIO()
        try:
            img.convert("RGB").save(output, format='JPEG')
            logger.debug("Image converted and resized to JPEG.")
        except Exception as e:
            logger.error(f"JPEG conversion failed: {e}. Saving as PNG instead.")
            output = BytesIO()
            img.save(output, format='PNG')
            logger.debug("Image resized and saved as PNG.")
        return output

def generate_random_id():
    random_id = uuid.uuid4().hex[:ALBUM_COVER_URL_LENGTH]
    logger.debug(f"Generated random album cover ID: {random_id}.")
    return random_id

def get_currently_playing_music_for_user():
    global current_song_id, current_album_id, latest_cover_id
    
    plex = PlexServer(PLEX_URL, PLEX_TOKEN)
    sessions = plex.sessions()

    if not sessions:
        logger.info("No active Plex sessions found.")
        return None

    # Filter and sort sessions by platform priority
    valid_sessions = [
        session for session in sessions 
        if session.usernames[0] == SPECIFIC_USERNAME and session.type == 'track'
    ]
    valid_sessions.sort(
        key=lambda x: PLATFORM_PRIORITY.index(x.player.product) if x.player.product in PLATFORM_PRIORITY else len(PLATFORM_PRIORITY)
    )

    if not valid_sessions:
        logger.info(f"No valid sessions found for user {SPECIFIC_USERNAME}.")
        return None

    # Choose the session based on the highest priority
    session = valid_sessions[0]
    
    title = session.title
    artist = session.grandparentTitle  # Artist name
    album = session.parentTitle  # Album name
    album_cover_url = plex.url(session.thumb)  # Album cover URL
    state = session.player.state
    current_time = session.viewOffset  # Current time in milliseconds
    duration = session.duration  # Total duration in milliseconds

    logger.info(f"Currently playing: {title} by {artist} from the album {album} on {session.player.product}.")

    # Generate new ID if the song changes and it belongs to a different album
    if title != current_song_id or album != current_album_id:
        current_song_id = title
        current_album_id = album
        if album in album_cover_ids:
            cover_id = album_cover_ids[album]
            logger.debug(f"Found existing album cover ID for album {album}: {cover_id}.")
        else:
            cover_id = generate_random_id()
            album_cover_ids[album] = cover_id
            save_album_cover_ids(album_cover_ids)
    else:
        cover_id = latest_cover_id  # Keep the same cover ID if the song and album are the same
        logger.debug(f"Using existing album cover ID {cover_id} for album {album}.")

    # Download the album cover with authentication
    headers = {
        'X-Plex-Token': PLEX_TOKEN
    }
    response = requests.get(album_cover_url, headers=headers)
    if response.status_code == 200:
        resized_image = resize_image(response.content)
        album_covers[cover_id] = resized_image
        latest_cover_id = cover_id
        logger.info(f"Successfully processed album cover for {album}.")
    else:
        logger.error(f"Failed to download album cover from {album_cover_url}, status code: {response.status_code}")

    return {
        "title": title,
        "artist": artist,
        "album": album,
        "album_cover_id": cover_id,
        "state": state,
        "current_time": current_time,
        "duration": duration,
        "platform": session.player.product  # Include the platform for reference
    }

def update_discord_presence(rpc, music_info, local_ip):
    if music_info and music_info['state'] == 'playing':
        end_time = int(time.time() + (music_info['duration'] - music_info['current_time']) / 1000)

        try:
            rpc.update(
                details=f"{music_info['title']}",
                state=f"{translations[language]['by']} {music_info['artist']}",
                large_image=f"{ALBUM_COVER_URL}/{music_info['album_cover_id']}",  # Serve the image from the local server
                large_text=f"{translations[language]['album']}: {music_info['album']}",
                small_image=f"plexamp" if music_info['platform'] == "Plexamp" else "plexweb",
                small_text=f"{translations[language]['listening_to']} {music_info['title']} on {music_info['platform']}",
                end=end_time
            )
            logger.info(f"Updated Discord presence with currently playing track: {music_info['title']} by {music_info['artist']} on {music_info['platform']}.")
        except Exception as e:
            logger.error(f"Failed to update Discord presence: {e}")
    else:
        rpc.clear()
        logger.info("Cleared Discord presence as there is no active playing session.")

def clear_logs():
    open(LOG_FILE, 'w').close()
    logger.info("Logs cleared.")

def open_logs():
    file_path = LOG_FILE

    # Temporarily disable logging to the file by removing the handler if it exists
    if logger.handlers:
        file_handler = logger.handlers[0]
        logger.removeHandler(file_handler)

    try:
        if platform.system() == 'Darwin':  # macOS
            subprocess.call(['open', file_path])
        elif platform.system() == 'Windows':  # Windows
            # Explicitly call notepad with the log file
            subprocess.call(['notepad.exe', file_path])
        else:  # Linux
            subprocess.call(['xdg-open', file_path])
        logger.info("Logs opened.")
    except Exception as e:
        print(f"Failed to open logs: {e}")
    finally:
        # Re-enable logging to the file if it was previously removed
        if 'file_handler' in locals():
            logger.addHandler(file_handler)




def display_interface(current_music):
    os.system('cls' if os.name == 'nt' else 'clear')  # Clear the terminal for each update
    print("\n---------")
    if current_music:
        print(f"Current playing Song: {current_music['title']}")
        print(f"Current playing Album: {current_music['album']}\n")
        print(f"Link to Album Cover: {ALBUM_COVER_URL}")
        print(f"Link to Album Cover ID: {ALBUM_COVER_URL}/{current_music['album_cover_id']}")
    else:
        print("No music currently playing.")
    
    print("\nOptions:")
    print("1. Open Logs")
    print("2. Clear Logs")
    print("3. Exit")
    print("--------")

def main_interface(silent_mode=False):
    global last_music_info

    # Display initial message if no GUI and in silent mode
    if not silent_mode:
        print("\n---------")
        print("Waiting for music to start...")
        print("---------")

    while True:
        current_music = get_currently_playing_music_for_user()

        if current_music is None:
            # No music is playing
            if not silent_mode and last_music_info is not None:
                os.system('cls' if os.name == 'nt' else 'clear')
                print("\n---------")
                print("No music currently playing.")
                print("Waiting for a song to start...")
                print("---------")

            # Reset last_music_info to ensure the message is displayed
            last_music_info = None

        elif current_music != last_music_info:
            # Music is playing
            if not silent_mode:
                display_interface(current_music)
            update_discord_presence(rpc, current_music, local_ip)
            last_music_info = current_music

        time.sleep(1)  # Check for updates every second

        if not silent_mode:
            # Check for user input without blocking the update loop
            if os.name == 'nt':
                import msvcrt
                if msvcrt.kbhit():
                    key = msvcrt.getch().decode('utf-8')
                    if key == '1':
                        open_logs()
                    elif key == '2':
                        clear_logs()
                    elif key == '3':
                        rpc.clear()
                        logger.info("Exiting...")
                        break
            else:
                import select
                if select.select([sys.stdin], [], [], 0.1)[0]:
                    choice = sys.stdin.read(1)
                    if choice == '1':
                        open_logs()
                    elif choice == '2':
                        clear_logs()
                    elif choice == '3':
                        rpc.clear()
                        logger.info("Exiting...")
                        break

if __name__ == "__main__":
    try:
        rpc = Presence(DISCORD_CLIENT_ID)
        rpc.connect()
        logger.info("Successfully connected to Discord.")
    except InvalidID:
        logger.error("The Discord Client ID is invalid. Please check your config.ini and make sure you have the correct Client ID.")
        exit(1)
    except Exception as e:
        logger.error(f"Failed to connect to Discord: {e}")
        exit(1)

    # Start the Flask server in a separate thread
    try:
        local_ip = socket.gethostbyname(socket.gethostname())
        logger.info(f"Local IP address detected: {local_ip}.")
    except Exception as e:
        logger.error(f"Failed to get local IP address: {e}")
        exit(1)
    
    server_thread = Thread(target=app.run, kwargs={'host': '0.0.0.0', 'port': 8081})
    server_thread.daemon = True
    server_thread.start()
    logger.info("Started Flask server to serve album covers.")

    try:
        last_music_info = None
        main_interface(silent_mode=args.Silent)
    except KeyboardInterrupt:
        rpc.clear()
        logger.info("Exiting due to keyboard interrupt.")
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        rpc.clear()
    finally:
        logger.info("Script execution completed.")
