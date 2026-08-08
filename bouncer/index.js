// Import required libraries
const udpProxy = require('udp-proxy');
const ioredis = require('ioredis');
require('dotenv').config();

// Get radar
const radar = new ioredis(process.env.RADAR_CONN_STRING || 'redis://127.0.0.1:6379');

// Get UDP pool start/end and initialise an active route list
// Parsed as integers to ensure the loop functions correctly
const udp_start = parseInt(process.env.PORT_POOL_START || 20000);
const udp_end = parseInt(process.env.PORT_POOL_END || 20050);
const active_routes = {}

// Starting message
console.log('BOUNCER: Online and monitoring radar')

// Loop every three seconds
setInterval(async() => {
    for (let port = udp_start; port <= udp_end; port++) {
        try {
            // Check the high-speed timetable for this specific port
            const target = await radar.get(`port:${port}`);

            if (target && !active_routes[port]) {
                // A new world was detected! Spin up a bi-directional UDP tunnel.
                const [backendIP, backendPort] = target.split(':');

                active_routes[port] = udpProxy.createServer({
                    address: backendIP,
                    port: parseInt(backendPort),
                    ipv6: false,     // Internal Zerops containers use IPv4
                    localipv6: true, // We receive public traffic on the free IPv6 address
                    localaddress: '::',
                    localport: port,
                    timeOutTime: 15000 // Close idle network sockets after 15 seconds
                });

                console.log(`[ROUTE ADDED] Public IPv6 [::]:${port} --> Internal ${backendIP}:${backendPort}`);
            }
            else if (!target && active_routes[port]) {
                // The world shut down. Destroy the route to free up the public port.
                if (typeof active_routes[port].close === 'function') {
                    active_routes[port].close();
                }
                delete active_routes[port];
                console.log(`[ROUTE REMOVED] Port ${port} is now closed.`);
            }
        } catch (err) {
            console.error(`Radar sync error on port ${port}:`, err);
        }
    }
}, 3000)