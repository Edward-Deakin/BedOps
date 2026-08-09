# Import required libraries
from flask import Flask, jsonify, request, Response
import os
import shutil
import yaml
import redis
import subprocess
import urllib.request
import zipfile
import socket
import re
import threading
import time
import tempfile
from datetime import datetime, timezone

# Initialise Flask app
app = Flask(__name__)

# Retrieve environment variables
WORLDS_DIR = os.environ.get('DATA_DIR', './worlds')
SHARED_TEMPLATE_DIR = os.environ.get('SHARED_TEMPLATE_DIR', '/mnt/shared/bedrock_template')
RADAR_CONN_STRING = os.environ.get('RADAR_CONN_STRING', 'redis://localhost:6379')
PORT_POOL_START = int(os.environ.get('PORT_POOL_START', 20000))
PORT_POOL_END = int(os.environ.get('PORT_POOL_END', 20050))

# Connect to radar
radar = redis.Redis.from_url(RADAR_CONN_STRING, decode_responses=True)

# Dictionary to track running subprocesses in memory
active_processes = {}

# Guards ensure_template_exists() so the startup thread and an incoming
# request can't download/extract the shared template at the same time
template_lock = threading.Lock()

# Helper for accepting the Bedrock EULA, required or bedrock_server refuses to boot
def ensure_eula_accepted(world_path):
    eula_path = os.path.join(world_path, 'eula.txt')
    with open(eula_path, 'w') as file:
        file.write('eula=true\n')


# Continuously drains a process's stdout into a bounded rolling buffer, and
# also mirrors it to a log file in the world folder so it can be tailed live
# from a terminal for diagnostics. Without this, bedrock_server blocks
# forever once the OS pipe buffer fills from its own startup logging, since
# nothing else was ever reading it.
def drain_process_output(process, buffer, world_path, max_lines=200):
    log_path = os.path.join(world_path, 'bedops-console.log')
    with open(log_path, 'w') as log_file:
        for line in iter(process.stdout.readline, ''):
            buffer.append(line)
            if len(buffer) > max_lines:
                del buffer[0]
            log_file.write(line)
            log_file.flush()
    process.stdout.close()


# Helper for retrieving IP from a specific container in Zerops private network
def get_internal_ip():
    try:
        hostname = socket.gethostname()
        return socket.gethostbyname(hostname)
    except Exception as e:
        print(f"Warning: Could not resolve internal IP: {e}")
        return "127.0.0.1"


# Pinned to a known-good pre-regression build. Mojang's "latest" Linux build
# (as served by their download-links API around 1.26.30.5) has a confirmed
# regression (BDS-23066) where the unconnected-pong is sent with no MOTD
# payload whenever enable-lan-visibility=false, which is required here to
# keep bedrock_server on its assigned port. 1.26.23.1 predates the regression.
BEDROCK_SERVER_DOWNLOAD_URL = 'https://www.minecraft.net/bedrockdedicatedserver/bin-linux/bedrock-server-1.26.23.1.zip'


# Helper for checking integrity of bedrock binaries and for downloading when missing
def ensure_template_exists():
    template_binary = os.path.join(SHARED_TEMPLATE_DIR, 'bedrock_server')
    resource_packs = os.path.join(SHARED_TEMPLATE_DIR, 'resource_packs')
    behavior_packs = os.path.join(SHARED_TEMPLATE_DIR, 'behavior_packs')

    # Verify that all critical binary and asset assets exist
    if os.path.exists(template_binary) and os.path.exists(resource_packs) and os.path.exists(behavior_packs):
        return

    with template_lock:
        # Re-check now that we hold the lock: another thread may have just
        # finished the download while we were waiting for it
        if os.path.exists(template_binary) and os.path.exists(resource_packs) and os.path.exists(behavior_packs):
            return

        print("Template missing or incomplete in shared storage. Downloading clean version...")

        # If folder exists but is incomplete, remove it to prevent extract conflicts
        if os.path.exists(SHARED_TEMPLATE_DIR):
            shutil.rmtree(SHARED_TEMPLATE_DIR)

        os.makedirs(SHARED_TEMPLATE_DIR, exist_ok=True)

        download_url = BEDROCK_SERVER_DOWNLOAD_URL

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


# Shared tail end of create/resume: sanity-check the directory, allocate a
# port, patch server.properties, accept the EULA, and boot bedrock_server.
# allow_template_repair gates the resource_packs self-heal — safe for a
# freshly-templated create, wrong for resume (a broken upload should error,
# not get silently replaced by a blank template pretending to be the
# player's world).
def _boot_world(world_name, hashed_pin, db_id, world_path, allow_template_repair):
    world_resource_packs = os.path.join(world_path, 'resource_packs')
    if not os.path.exists(world_resource_packs):
        if not allow_template_repair:
            return jsonify({'error': 'World file is missing resource_packs — invalid or corrupt upload.'}), 400
        print(f"Warning: {world_name} directory is missing resource_packs. Re-copying from template...")
        if os.path.exists(world_path):
            shutil.rmtree(world_path)
        shutil.copytree(SHARED_TEMPLATE_DIR, world_path)

    allocated_port = None
    try:
        # 1. Allocate a port
        for port in range(PORT_POOL_START, PORT_POOL_END + 1):
            if not radar.exists(f'port:{port}'):
                allocated_port = port
                break

        if not allocated_port:
            return jsonify({'error': 'There are no free ports to host the world.'}), 503

        # 2. Lock it in Radar
        container_ip = get_internal_ip()
        radar.set(f'port:{allocated_port}', f'{container_ip}:{allocated_port}')

        # 3. Configure server.properties dynamically
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

        # 4. Boot the server
        env = os.environ.copy()
        env['LD_LIBRARY_PATH'] = '.'

        process = subprocess.Popen(
            ['./bedrock_server'],
            cwd=world_path,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )

        # Drain stdout continuously for the life of the process so bedrock_server
        # never blocks on a full pipe buffer; recent lines are kept for crash diagnostics
        output_buffer = []
        threading.Thread(target=drain_process_output, args=(process, output_buffer, world_path), daemon=True).start()

        # Give the server a moment to fail fast (bad binary, port already in
        # use, etc.) before we tell radar/brain it's actually up.
        time.sleep(2)

        if process.poll() is not None:
            radar.delete(f'port:{allocated_port}')
            return jsonify({
                'error': f'bedrock_server exited immediately (code {process.returncode})',
                'output': ''.join(output_buffer).strip()
            }), 500

        active_processes[world_name] = {
            "process": process,
            "port": allocated_port,
            "path": world_path,
            "output": output_buffer
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


# Route for checking API
@app.route('/')
def index():
    return jsonify({'message': 'Machine is online!'})


# Route for reporting which worlds are actually running on this machine right now
@app.route('/api/worlds')
def list_worlds():
    return jsonify([
        {'world_name': name, 'port': data['port']}
        for name, data in active_processes.items()
    ])


# Route for creating fresh worlds
@app.route('/api/create', methods=['POST'])
def create_world():
    data = request.get_json() or {}

    world_name = data.get('world_name', 'BedOps World')
    hashed_pin = data.get('pin', '')
    db_id = data.get('db_id', '')

    if world_name in active_processes:
        return jsonify({'error': 'Server is already running.'}), 409

    try:
        ensure_template_exists()

        world_path = os.path.join(WORLDS_DIR, world_name)

        if not os.path.exists(world_path):
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
    except Exception as e:
        return jsonify({'error': f'Machine failed to prepare world: {e}'}), 500

    return _boot_world(world_name, hashed_pin, db_id, world_path, allow_template_repair=True)


# Route for resuming a world from a previously-downloaded zip — the upload
# IS the world's only remaining copy, there is nothing kept server-side to
# fall back to.
@app.route('/api/resume', methods=['POST'])
def resume_world():
    world_name = request.form.get('world_name', '')
    hashed_pin = request.form.get('pin', '')
    db_id = request.form.get('db_id', '')
    uploaded_file = request.files.get('world_file')

    if not world_name:
        return jsonify({'error': 'World name required.'}), 400
    if world_name in active_processes:
        return jsonify({'error': 'Server is already running.'}), 409
    if not uploaded_file:
        return jsonify({'error': 'No world file uploaded.'}), 400

    world_path = os.path.join(WORLDS_DIR, world_name)
    try:
        if os.path.exists(world_path):
            shutil.rmtree(world_path)
        os.makedirs(world_path, exist_ok=True)
        with zipfile.ZipFile(uploaded_file, 'r') as zip_ref:
            zip_ref.extractall(world_path)
    except zipfile.BadZipFile:
        shutil.rmtree(world_path, ignore_errors=True)
        return jsonify({'error': 'That file is not a valid world zip.'}), 400
    except Exception as e:
        shutil.rmtree(world_path, ignore_errors=True)
        return jsonify({'error': f'Could not unpack world file: {e}'}), 400

    return _boot_world(world_name, hashed_pin, db_id, world_path, allow_template_repair=False)


# Route for stopping worlds, zipping for download, and cleaning up — BedOps
# is stateless, so the zip handed back to the caller is the ONLY copy;
# nothing is retained here or in shared storage.
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

    # 1. Zip the world into a scratch temp dir (never shared storage)
    tmp_dir = tempfile.mkdtemp(prefix='bedops-dl-')
    print(f"Zipping {world_name} for download...")
    zip_path = shutil.make_archive(os.path.join(tmp_dir, world_name), 'zip', world_path)

    # 2. Free the port in Radar so Bouncer closes the UDP tunnel
    radar.delete(f'port:{port}')
    del active_processes[world_name]

    # 3. Delete the raw, uncompressed files — nothing kept server-side
    print(f"Deleting raw files for {world_name} — nothing is retained on the server.")
    shutil.rmtree(world_path)

    # Read + delete synchronously rather than send_file + call_on_close —
    # under this gunicorn setup the close callback doesn't reliably fire
    # (confirmed by testing: temp dirs survived past the response), which
    # would silently leak disk space on every stop. Read fully into memory
    # instead; world zips are small enough for this to be fine.
    with open(zip_path, 'rb') as f:
        data = f.read()
    shutil.rmtree(tmp_dir, ignore_errors=True)

    return Response(
        data,
        mimetype='application/zip',
        headers={'Content-Disposition': f'attachment; filename="{world_name}.zip"'}
    )


@app.route('/api/pause', methods=['POST'])
def pause_world():
    return stop_world()


if __name__ == '__main__':
    threading.Thread(target=ensure_template_exists, daemon=True).start()
    app.run(debug=True, host='0.0.0.0', port=5001)
