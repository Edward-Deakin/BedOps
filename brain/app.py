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
        response = requests.post(
            url=f'{WORKER_URL}/api/stop',
            json={'world_name': world_name},
            timeout=300
            # Allow time for the worker to zip the world
        )

        return jsonify(response.json()), response.status_code
    except Exception as e:
        return jsonify({'error': f'Error communicating with worker\n{e}'}), 500


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5002)
