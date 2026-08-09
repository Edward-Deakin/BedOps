# BedOps

A free, on-demand Minecraft Bedrock Dedicated Server host. Pick a name and a PIN,
get a real `bedrock_server` process running behind a public address in seconds —
genuinely stateless: stop a world and it downloads straight to your browser, upload
it later to resume. Nothing is kept server-side.

Built for the [Zerops Challenge](https://www.wemakedevs.org/hackathons/zerops).

**Live:**
- Dashboard — https://brain-16-5002.prg1.zerops.app
- Concierge (natural-language ops agent) — https://concierge-16-5003.prg1.zerops.app

## Architecture

A monorepo, one folder per Zerops service:

| Service | Stack | Role |
|---|---|---|
| `brain/` | Python/Flask | Public API + dashboard. Owns PIN auth and world records (Postgres). |
| `machine/` | Python/Flask | Runs the real `bedrock_server` binary per world; allocates ports; patches `server.properties` per boot. |
| `bouncer/` | Node.js | Dynamic public UDP proxy. Zerops has no declarative port *range* support, so this polls a shared state store (`radar`, Valkey) every 3s and opens/closes UDP tunnels in real time as worlds start and stop. |
| `concierge/` | Python/Flask | Natural-language ops agent — its own service, its own URL. Drives the same `brain` API a human uses via the dashboard: *"create a world called Skyline pin 4242"*, *"who's online"*, *"stop Skyline"*. Deterministic intent parsing (no LLM call), chosen specifically so a live demo never depends on external API latency, quota, or auth. |

Managed dependencies: Postgres (world/PIN records), Valkey (`radar` — the live
port-allocation table bridging `bouncer` and `machine`), object storage (backs the
Bedrock server binary template).

## Why it's stateless

Stopping a world zips it and streams the download directly to the browser (or, via
`concierge`, a one-time link) — no server-side archive. Uploading that zip back
through "Upload World" resumes it. The file you hold **is** the backup; BedOps
never stores it for you.

## Connecting

Each world is a dedicated UDP port (`worldname.bedops.edwarddeakin.uk:<port>`) —
Bedrock/RakNet has no SNI/SRV support, so routing is by port, not by hostname alone.

## Deploying

`zerops.yaml` at the repo root defines all four services. Public UDP port mapping
(beyond the internal `protocol: udp` declarations already in the yaml) is a
dashboard-only step per port — see Zerops' Direct Port Access docs.
