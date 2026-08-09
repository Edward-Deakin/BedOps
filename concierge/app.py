# Import required libraries
from flask import Flask, jsonify, request, render_template, Response
import os
import re
import requests
import secrets
import time
import dotenv

# Load environment variables
dotenv.load_dotenv()

# Initialise Flask app
app = Flask(__name__)
BRAIN_URL = os.environ.get('BRAIN_URL', 'http://brain:5002')

# Scratch space for one-time stop-download links — concierge is a chat
# interface, it can't hand back binary bytes directly, so a stopped world's
# zip is held here just long enough for the user to click through.
DOWNLOAD_DIR = '/tmp/concierge-downloads'
DOWNLOAD_MAX_AGE_SECONDS = 30 * 60


def _cleanup_stale_downloads():
    if not os.path.isdir(DOWNLOAD_DIR):
        return
    now = time.time()
    for name in os.listdir(DOWNLOAD_DIR):
        path = os.path.join(DOWNLOAD_DIR, name)
        try:
            if now - os.path.getmtime(path) > DOWNLOAD_MAX_AGE_SECONDS:
                os.remove(path)
        except OSError:
            pass

# --- Intent parsing (deterministic — no LLM call, so a live demo never
# depends on external API latency/availability/quota) --------------------

PIN_RE = re.compile(r'\b(\d{3,8})\b')
QUOTED_RE = re.compile(r'''["']([^"']{1,40})["']''')
CALLED_NAME_RE = re.compile(
    r'''(?:called|named)\s+["']?([A-Za-z0-9][A-Za-z0-9 _-]{0,30}?)["']?(?=\s+(?:with|using|pin|code|and)\b|[.,!?]|\s*$)''',
    re.IGNORECASE,
)

LIST_RE = re.compile(r"\b(list|status|who'?s online|who is online|what'?s running|show worlds|which worlds|worlds running)\b", re.IGNORECASE)
STOP_RE = re.compile(r'\b(stop|shut ?down|shutdown|pause|kill|turn off|halt)\b', re.IGNORECASE)
CREATE_RE = re.compile(r'\b(create|spin[\s-]?up|start|new world|make|boot|launch)\b', re.IGNORECASE)

# Words stripped out when falling back to "whatever's left is the name"
# (covers phrasing with no called/named keyword, e.g. "stop Skyline pin 4242")
FILLER_WORDS = {
    'a', 'an', 'the', 'to', 'for', 'please', 'can', 'you', 'my', 'me',
    'world', 'worlds', 'server', 'pin', 'code', 'with', 'using', 'and',
    'called', 'named', 'name', 'is',
    'create', 'spin', 'up', 'start', 'new', 'make', 'boot', 'launch',
    'stop', 'shut', 'down', 'shutdown', 'pause', 'kill', 'turn', 'off', 'halt',
}


def extract_name_and_pin(text):
    pin_match = PIN_RE.search(text)
    pin = pin_match.group(1) if pin_match else None

    quoted = QUOTED_RE.search(text)
    if quoted:
        return quoted.group(1).strip(), pin

    called = CALLED_NAME_RE.search(text)
    if called:
        return called.group(1).strip(), pin

    # Fallback: strip the pin + punctuation + known filler/intent words;
    # whatever's left is taken as the name (world names here are short,
    # single tokens in practice).
    stripped = PIN_RE.sub(' ', text)
    stripped = re.sub(r'[^\w\s]', ' ', stripped)
    tokens = [t for t in stripped.split() if t.lower() not in FILLER_WORDS]
    world_name = ' '.join(tokens).strip() or None
    return world_name, pin


def parse_intent(text):
    if LIST_RE.search(text):
        return {'intent': 'list'}
    if STOP_RE.search(text):
        world_name, pin = extract_name_and_pin(text)
        return {'intent': 'stop', 'world_name': world_name, 'pin': pin}
    if CREATE_RE.search(text):
        world_name, pin = extract_name_and_pin(text)
        return {'intent': 'create', 'world_name': world_name, 'pin': pin}
    return {'intent': 'unknown'}


HELP_TEXT = (
    "I can create, stop, or list your BedOps worlds. Try:\n"
    "  • create a world called Skyline pin 4242\n"
    "  • stop Skyline pin 4242\n"
    "  • who's online"
)


# --- Actions against brain's existing API --------------------------------

def handle_list():
    try:
        response = requests.get(f'{BRAIN_URL}/api/worlds', timeout=10)
        response.raise_for_status()
        worlds = response.json()  # brain now reports ONLY currently-running worlds
    except Exception as e:
        return f"I couldn't reach brain to check worlds right now ({e})."

    if not worlds:
        return "Nothing is running right now."

    lines = [f"  • {w['world_name']} — port {w['port']}" for w in worlds]
    return "Worlds running right now:\n" + "\n".join(lines)


def missing_fields_reply(world_name, pin, action_example):
    missing = []
    if not world_name:
        missing.append('a name')
    if not pin:
        missing.append('a PIN')
    return f"I need {' and '.join(missing)} to {action_example}"


def handle_create(world_name, pin):
    if not world_name or not pin:
        return missing_fields_reply(world_name, pin, "create a world. Example: create a world called Skyline pin 4242")

    try:
        response = requests.post(
            f'{BRAIN_URL}/api/create',
            json={'world_name': world_name, 'pin': pin},
            timeout=300,
        )
        data = response.json()
    except Exception as e:
        return f"Something went wrong reaching brain: {e}"

    if response.ok:
        return f"'{world_name}' is booting up on port {data.get('port')}. Connect there once it's up."
    return f"Couldn't create '{world_name}': {data.get('error', 'unknown error')}"


def handle_stop(world_name, pin):
    if not world_name or not pin:
        return missing_fields_reply(world_name, pin, "stop a world. Example: stop Skyline pin 4242")

    try:
        response = requests.post(
            f'{BRAIN_URL}/api/stop',
            json={'world_name': world_name, 'pin': pin},
            timeout=300,
        )
    except Exception as e:
        return f"Something went wrong reaching brain: {e}"

    # Success means brain sent back the world's zip. A chat reply can't
    # carry binary bytes, so stash it briefly and hand back a one-time link
    # instead — same "download IS the only backup" model as the dashboard.
    if response.headers.get('Content-Type', '').startswith('application/zip'):
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        _cleanup_stale_downloads()
        token = secrets.token_urlsafe(16)
        with open(os.path.join(DOWNLOAD_DIR, f'{token}.zip'), 'wb') as f:
            f.write(response.content)
        download_url = f"{request.host_url.rstrip('/')}/download/{token}/{world_name}.zip"
        return f"'{world_name}' stopped. Download it here (link works once, expires in 30 min): {download_url}"

    try:
        data = response.json()
    except ValueError:
        return f"Unexpected response from brain (status {response.status_code})."
    return f"Couldn't stop '{world_name}': {data.get('error', 'unknown error')}"


# --- Routes ----------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/chat', methods=['POST'])
def chat():
    data = request.get_json() or {}
    message = (data.get('message') or '').strip()

    if not message:
        return jsonify({'reply': HELP_TEXT})

    try:
        parsed = parse_intent(message)

        if parsed['intent'] == 'list':
            reply = handle_list()
        elif parsed['intent'] == 'create':
            reply = handle_create(parsed['world_name'], parsed['pin'])
        elif parsed['intent'] == 'stop':
            reply = handle_stop(parsed['world_name'], parsed['pin'])
        else:
            reply = HELP_TEXT

        return jsonify({'reply': reply})
    except Exception as e:
        return jsonify({'reply': f"Something broke on my end: {e}"}), 500


# One-time download link handed out by handle_stop. Filesystem-backed
# (not an in-memory dict) so it works regardless of which gunicorn worker
# handles the request.
@app.route('/download/<token>/<path:filename>')
def download(token, filename):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', token):
        return "Invalid download link.", 404

    path = os.path.join(DOWNLOAD_DIR, f'{token}.zip')
    if not os.path.isfile(path):
        return "This download link has expired or already been used.", 404

    # Read + delete synchronously rather than send_file + call_on_close —
    # under this gunicorn setup the close callback doesn't reliably fire
    # (confirmed by testing: the file survived two downloads), which would
    # silently break the one-time-use guarantee. World zips are small
    # enough that loading the file fully into memory here is fine.
    with open(path, 'rb') as f:
        data = f.read()
    os.remove(path)

    return Response(
        data,
        mimetype='application/zip',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5003)
