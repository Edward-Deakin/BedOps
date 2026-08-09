# Import required libraries
from flask import Flask, jsonify, request
import os
import shutil
import yaml
import redis
import subprocess
import urllib.request
import zipfile
import socket
import re
import json
import threading
import time
from datetime import datetime, timezone

# Initialise Flask app
app = Flask(__name__)

# Retrieve environment variables
WORLDS_DIR = os.environ.get('DATA_DIR', './worlds')
SHARED_TEMPLATE_DIR = os.environ.get('SHARED_TEMPLATE_DIR', '/mnt/shared/bedrock_template')
ARCHIVES_DIR = os.environ.get('ARCHIVES_DIR', '/mnt/shared/archives')
RADAR_CONN_STRING = os.environ.get('RADAR_CONN_STRING', 'redis://localhost:6379')
PORT_POOL_START = int(os.environ.get('PORT_POOL_START', 20000))
PORT_POOL_END = int(os.environ.get('PORT_POOL_END', 20050))

# Connect to radar
radar = redis.Redis.from_url(RADAR_CONN_STRING, decode_responses=True)

# Dictionary to track running subprocesses in memory
active_processes = {}

# Helper for accepting the Bedrock EULA, required or bedrock_server refuses to boot
def ensure_eula_accepted(world_path):
    eula_path = os.path.join(world_path, 'eula.txt')
    with open(eula_path, 'w') as file:
        file.write('eula=true\n')


# Helper for retrieving IP from a specific container in Zerops private network
def get_internal_ip():
    try:
        hostname = socket.gethostname()
        return socket.gethostbyname(hostname)
    except Exception as e:
        print(f"Warning: Could not resolve internal IP: {e}")
        return "127.0.0.1"


# Helper for checking integrity of bedrock binaries and for downloading when missing
def ensure_template_exists():
    template_binary = os.path.join(SHARED_TEMPLATE_DIR, 'bedrock_server')
    resource_packs = os.path.join(SHARED_TEMPLATE_DIR, 'resource_packs')
    behavior_packs = os.path.join(SHARED_TEMPLATE_DIR, 'behavior_packs')

    # Verify that all critical binary and asset assets exist
    if os.path.exists(template_binary) and os.path.exists(resource_packs) and os.path.exists(behavior_packs):
        return

    print("Template missing or incomplete in shared storage. Downloading clean version...")

    # If folder exists but is incomplete, remove it to prevent extract conflicts
    if os.path.exists(SHARED_TEMPLATE_DIR):
        shutil.rmtree(SHARED_TEMPLATE_DIR)

    os.makedirs(SHARED_TEMPLATE_DIR, exist_ok=True)

    req = urllib.request.Request(
        'https://net-secondary.web.minecraft-services.net/api/v1.0/download/links',
        headers={'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64)'}
    )

    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read().decode('utf-8'))

    download_url = None
    for link in data.get('result', {}).get('links', []):
        if link.get('downloadType') == 'serverBedrockLinux':
            download_url = link.get('downloadUrl')
            break

    if not download_url:
        raise Exception("Could not find the Linux Bedrock server URL in the Mojang API response.")

    zip_path = os.path.join(SHARED_TEMPLATE_DIR, 'server.zip')

    print(f"Downloading server from {download_url}...")

    req_zip = urllib.request.Request(download_url, headers={'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64)'})
    with urllib.request.urlopen(req_zip) as response, open(zip_path, 'wb') as out_file:
        shutil.copyfileobj(response, out_file)

    print("Extracting template files...")
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(SHARED_TEMPLATE_DIR)

    os.remove(zip_path)
    os.chmod(template_binary, 0o755)
    print("Template successfully downloaded and extracted.")


# Route for checking API
@app.route('/')
def index():
    return jsonify({'message': 'Machine is online!'})

# Route for creating or resuming worlds
@app.route('/api/create', methods=['POST'])
def create_world():
    data = request.get_json() or {}

    world_name = data.get('world_name', 'BedOps World')
    hashed_pin = data.get('pin', '')
    db_id = data.get('db_id', '')

    if world_name in active_processes:
        return jsonify({'error': 'Server is already running.'}), 409

    allocated_port = None
    try:
        ensure_template_exists()
        os.makedirs(ARCHIVES_DIR, exist_ok=True)

        world_path = os.path.join(WORLDS_DIR, world_name)
        archive_path = os.path.join(ARCHIVES_DIR, f"{world_name}.zip")

        # 1. Restore from ZIP if it exists, otherwise use fresh template
        if not os.path.exists(world_path):
            if os.path.exists(archive_path):
                print(f"Restoring {world_name} from archive...")
                shutil.unpack_archive(archive_path, world_path)
            else:
                print(f"Creating fresh world for {world_name}...")
                shutil.copytree(SHARED_TEMPLATE_DIR, world_path)

                with open(os.path.join(world_path, 'bedops.yaml'), 'w') as file:
                    yaml.dump({
                        'version': '1.0',
                        'created': datetime.now(timezone.utc).isoformat(),
                        'world_name': world_name,
                        'db_id': db_id,
                        'hashed_pin': hashed_pin,
                        'platform': 'BedOps powered by Zerops'
                    }, file)

        # Sanity check: Ensure resource_packs exist in active world directory
        world_resource_packs = os.path.join(world_path, 'resource_packs')
        if not os.path.exists(world_resource_packs):
            print(f"Warning: {world_name} directory is missing resource_packs. Re-copying from template...")
            if os.path.exists(world_path):
                shutil.rmtree(world_path)
            shutil.copytree(SHARED_TEMPLATE_DIR, world_path)

        # 2. Allocate a port
        allocated_port = None
        for port in range(PORT_POOL_START, PORT_POOL_END + 1):
            if not radar.exists(f'port:{port}'):
                allocated_port = port
                break

        if not allocated_port:
            return jsonify({'error': 'There are no free ports to host the world.'}), 503

        # 3. Lock it in Radar
        container_ip = get_internal_ip()
        radar.set(f'port:{allocated_port}', f'{container_ip}:{allocated_port}')

        # 4. Configure server.properties dynamically
        properties_path = os.path.join(world_path, 'server.properties')

        with open(properties_path, 'r') as file:
            props = file.read()

        # Replace default port assignments with the allocated port
        props = re.sub(r'server-port=\d+', f'server-port={allocated_port}', props)
        props = re.sub(r'server-portv6=\d+', f'server-portv6={allocated_port + 1000}', props)
        # Bedrock forces default ports if LAN visibility is true, so we MUST disable it
        props = re.sub(r'enable-lan-visibility=true', 'enable-lan-visibility=false', props)
        props = re.sub(r'server-name=.*', f'server-name={world_name}', props)

        with open(properties_path, 'w') as file:
            file.write(props)

        ensure_eula_accepted(world_path)

        executable_path = os.path.join(world_path, 'bedrock_server')
        os.chmod(executable_path, 0o755)

        # 5. Boot the server
        env = os.environ.copy()
        env['LD_LIBRARY_PATH'] = '.'

        process = subprocess.Popen(
            ['./bedrock_server'],
            cwd=world_path,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        # Give the server a moment to fail fast (bad binary, port already in
        # use, etc.) before we tell radar/brain it's actually up.
        time.sleep(2)

        if process.poll() is not None:
            crash_output = (process.stdout.read() if process.stdout else '') + \
                (process.stderr.read() if process.stderr else '')
            radar.delete(f'port:{allocated_port}')
            return jsonify({
                'error': f'bedrock_server exited immediately (code {process.returncode})',
                'output': crash_output.strip()
            }), 500

        active_processes[world_name] = {
            "process": process,
            "port": allocated_port,
            "path": world_path
        }

        return jsonify({
            'status': 'success',
            'message': f'Server {world_name} booted',
            'port': allocated_port
        }), 201

    except Exception as e:
        if allocated_port:
            radar.delete(f'port:{allocated_port}')
        return jsonify({'error': f'Machine failed to boot world: {e}'}), 500


# Route for stopping worlds, zipping, and cleaning up
@app.route('/api/stop', methods=['POST'])
def stop_world():
    data = request.get_json() or {}
    world_name = data.get('world_name')

    if not world_name or world_name not in active_processes:
        return jsonify({'error': f'World {world_name} is not running on this machine.'}), 404

    server_data = active_processes[world_name]
    process = server_data['process']
    port = server_data['port']
    world_path = server_data['path']

    try:
        if process.poll() is None:
            process.stdin.write("stop\n")
            process.stdin.flush()
            process.wait(timeout=15)

    except subprocess.TimeoutExpired:
        print(f"Server {world_name} hung on shutdown. Force killing.")
        process.terminate()
    except Exception as e:
        print(f"Error during shutdown of {world_name}: {e}")
        process.terminate()

    # 1. Zip the world directory directly into shared storage
    os.makedirs(ARCHIVES_DIR, exist_ok=True)
    archive_base = os.path.join(ARCHIVES_DIR, world_name)

    print(f"Archiving {world_name} to shared storage...")
    shutil.make_archive(archive_base, 'zip', world_path)

    # 2. Delete the raw, uncompressed files from the active compute container
    print(f"Deleting raw files for {world_name} to save space...")
    shutil.rmtree(world_path)

    # 3. Free the port in Radar so Bouncer closes the UDP tunnel
    radar.delete(f'port:{port}')
    del active_processes[world_name]

    return jsonify({
        'status': 'success',
        'message': f'Server {world_name} gracefully stopped, archived to shared storage, and cleaned up.'
    }), 200


@app.route('/api/pause', methods=['POST'])
def pause_world():
    return stop_world()


if __name__ == '__main__':
    threading.Thread(target=ensure_template_exists, daemon=True).start()
    app.run(debug=True, host='0.0.0.0', port=5001)