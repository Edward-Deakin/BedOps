# Import required libraries
from flask import Flask, jsonify, Response, render_template, request
import os
import requests
import psycopg2
# Imported check_password_hash to verify user credentials on shutdown
from werkzeug.security import generate_password_hash, check_password_hash
import dotenv

# Load environment variables
dotenv.load_dotenv()

# Initialise Flask app
app = Flask(__name__)
WORKER_URL = os.environ.get("WORKER_URL", "http://machine:5001")  # Defaulted to internal hostname

# Zerops PostgresSQL connection credentials
DB_HOST = os.environ.get('DB_HOST', 'db')
DB_USER = os.environ.get('DB_USER', 'db')
DB_PASSWORD = os.environ.get('DB_PASSWORD')
DB_PORT = os.environ.get('DB_PORT', '5432')
if not DB_USER or not DB_PASSWORD or not DB_PORT:
    print('⚠️ Missing credentials for the database')


# Zerops PostgresSQL helpers
def get_db():
    return psycopg2.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASSWORD,
        port=DB_PORT,
        dbname=DB_USER
    )


def init_db():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
                    CREATE TABLE IF NOT EXISTS worlds
                    (
                        id
                        SERIAL
                        PRIMARY
                        KEY,
                        world_name
                        TEXT
                        UNIQUE
                        NOT
                        NULL,
                        hashed_pin
                        TEXT
                        NOT
                        NULL
                    );
                    """)
        conn.commit()
        cur.close()
        conn.close()
        print("✅ DB intialised")
    except Exception as e:
        print(f"❌ Error initialising DB\n{e}")


# Initialise Zerops PostgresSQL
init_db()


# Route for main HTML page
@app.route('/')
def index():
    return render_template('index.html')


# API route reporting worlds that are actually running right now.
# Deliberately NOT the full history of every world ever created — this app
# has no accounts, so a full history would be a public log of everyone's
# world names with no auth to gate it.
@app.route('/api/worlds')
def list_worlds():
    try:
        response = requests.get(f'{WORKER_URL}/api/worlds', timeout=5)
        response.raise_for_status()
        return jsonify(response.json())
    except Exception as e:
        return jsonify({'error': f'Could not reach machine for live status\n{e}'}), 502


# API route for creating a world
@app.route('/api/create', methods=['POST'])
def create():
    # Get the data from the request
    data = request.get_json()

    # Extract the non-hashed PIN and the world name
    raw_pin = data.get('pin', '')
    world_name = data.get('world_name', '')

    if not raw_pin:
        return jsonify({'error': 'PIN required for world creation'}), 400

    # Hash the raw PIN
    hashed_pin = generate_password_hash(raw_pin)

    # Save the world combination to Zerops PostgresSQL
    try:
        conn = get_db()
        cur = conn.cursor()
        # Used ON CONFLICT to handle users booting existing worlds
        cur.execute("""
                    INSERT INTO worlds (world_name, hashed_pin)
                    VALUES (%s, %s) ON CONFLICT (world_name) DO
                    UPDATE SET hashed_pin = EXCLUDED.hashed_pin
                        RETURNING id;
                    """, (world_name, hashed_pin))
        world_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        return jsonify({'error': f'Error creating world\n{e}'}), 400

    # Forward hashed data to machine worker to create world
    try:
        response = requests.post(
            url=f'{WORKER_URL}/api/create',
            json={
                'world_name': world_name,
                'pin': hashed_pin,
                'db_id': world_id
            },
            timeout=300  # Extended slightly for extraction times
        )

        # Return worker response to frontend
        return jsonify(response.json()), response.status_code
    except Exception as e:
        return jsonify({'error': f'Error creating world\n{e}'}), 400


# API route for stopping/pausing a world
@app.route('/api/stop', methods=['POST'])
def stop():
    data = request.get_json()
    raw_pin = data.get('pin', '')
    world_name = data.get('world_name', '')

    if not raw_pin or not world_name:
        return jsonify({'error': 'World name and PIN required'}), 400

    # Verify the user's PIN against the PostgreSQL database
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT hashed_pin FROM worlds WHERE world_name = %s;", (world_name,))
        result = cur.fetchone()
        cur.close()
        conn.close()

        if not result:
            return jsonify({'error': 'World not found in database.'}), 404

        stored_hash = result[0]
        if not check_password_hash(stored_hash, raw_pin):
            return jsonify({'error': 'Invalid PIN. Access denied.'}), 401

    except Exception as e:
        return jsonify({'error': f'Database error\n{e}'}), 500

    # Forward authenticated stop request to the machine worker
    try:
        upstream = requests.post(
            url=f'{WORKER_URL}/api/stop',
            json={'world_name': world_name},
            timeout=300,
            # Allow time for the worker to zip the world
            stream=True
        )
    except Exception as e:
        return jsonify({'error': f'Error communicating with worker\n{e}'}), 500

    # Success means machine sent the zip back — stream it straight through to
    # the browser. BedOps is stateless: this download IS the only backup.
    if upstream.headers.get('Content-Type', '').startswith('application/zip'):
        return Response(
            upstream.iter_content(chunk_size=65536),
            content_type='application/zip',
            headers={
                'Content-Disposition': upstream.headers.get(
                    'Content-Disposition', f'attachment; filename="{world_name}.zip"'
                )
            }
        )

    # Anything else is an error response from machine — relay it as JSON
    try:
        return jsonify(upstream.json()), upstream.status_code
    except ValueError:
        return jsonify({'error': 'Unexpected response from worker.'}), 502


# API route for resuming a world from a previously-downloaded zip. The
# uploaded file is the only remaining copy of that world, so it doubles as
# the credential — same trust model as "whoever has the file owns the world"
# already implied by handing it to the player instead of keeping an archive.
@app.route('/api/resume', methods=['POST'])
def resume():
    world_name = request.form.get('world_name', '')
    raw_pin = request.form.get('pin', '')
    uploaded_file = request.files.get('world_file')

    if not raw_pin or not world_name:
        return jsonify({'error': 'World name and PIN required'}), 400
    if not uploaded_file:
        return jsonify({'error': 'World file required'}), 400

    hashed_pin = generate_password_hash(raw_pin)

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
                    INSERT INTO worlds (world_name, hashed_pin)
                    VALUES (%s, %s) ON CONFLICT (world_name) DO
                    UPDATE SET hashed_pin = EXCLUDED.hashed_pin
                        RETURNING id;
                    """, (world_name, hashed_pin))
        world_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        return jsonify({'error': f'Error registering world\n{e}'}), 400

    try:
        response = requests.post(
            url=f'{WORKER_URL}/api/resume',
            data={'world_name': world_name, 'pin': hashed_pin, 'db_id': world_id},
            files={'world_file': (uploaded_file.filename, uploaded_file.stream, 'application/zip')},
            timeout=300
        )
        return jsonify(response.json()), response.status_code
    except Exception as e:
        return jsonify({'error': f'Error communicating with worker\n{e}'}), 400


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5002)
