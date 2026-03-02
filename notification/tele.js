require('dotenv').config();
const fs = require('fs');
const http = require('http');
const path = require('path');
const TelegramBot = require('node-telegram-bot-api');

// --- CONFIGURATION ---
const PORT = 3000;

const MIME_TYPES = {
  '.html': 'text/html',
  '.json': 'application/json',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.svg': 'image/svg+xml',
  '.css': 'text/css',
  '.js': 'application/javascript',
};

// ==========================================
// TELEGRAM BOT SETUP
// ==========================================
const BOT_TOKEN = process.env.BOT_TOKEN;
const bot = new TelegramBot(BOT_TOKEN, { polling: true });

bot.onText(/\/start/, (msg) => {
  const userName = msg.from.first_name || 'Passenger';
  bot.sendMessage(
    msg.chat.id,
    `Hello ${userName}!\n\nWe are currently checking your bag status. Please wait for updates here.`
  );
});

bot.on('polling_error', (err) => {
  console.error('❌ Polling error:', err.message);
});

console.log('🤖 Telegram Bot is listening for /start commands...');

// ==========================================
// HELPERS
// ==========================================
const os = require('os');

function getLocalIpAddress() {
  const interfaces = os.networkInterfaces();
  for (const name of Object.keys(interfaces)) {
    for (const iface of interfaces[name]) {
      if (iface.family === 'IPv4' && !iface.internal) {
        return iface.address;
      }
    }
  }
  return 'localhost';
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let body = '';
    req.on('data', chunk => { body += chunk; });
    req.on('end', () => {
      try { resolve(JSON.parse(body)); }
      catch (e) { resolve({}); }
    });
    req.on('error', reject);
  });
}

function writeCurrentScan(updates) {
  const scanPath = path.join(__dirname, 'current_scan.json');
  let current = {};
  try { current = JSON.parse(fs.readFileSync(scanPath, 'utf8')); } catch (_) { }
  const merged = { ...current, ...updates, updatedAt: new Date().toISOString() };
  fs.writeFileSync(scanPath, JSON.stringify(merged, null, 2));
  console.log(`📝 current_scan.json updated:`, merged);
}

// ==========================================
// NOTIFICATION FUNCTIONS
// ==========================================

function sendTelegramAlert(chatId, bagId, owner) {
  const baseUrl = process.env.PUBLIC_URL || `http://${getLocalIpAddress()}:${PORT}`;
  const message =
    `🧳 *LEBAG ALERT*\n\n` +
    `Your bag (Owner: *${owner}*) is 2 minutes away from collection!\n\n` +
    `📍 Track live status here:\n${baseUrl}?tag=${encodeURIComponent(bagId)}`;

  bot.sendMessage(chatId, message, { parse_mode: 'Markdown' })
    .then(() => console.log(`✅ Telegram sent to ${chatId} for bag ${bagId}`))
    .catch((err) => console.error(`❌ Telegram FAILED for ${chatId}:`, err.message));
}

function sendTelegramZoneUpdate(chatId, owner, zone) {
  if (!chatId) return;
  const zoneEmoji = zone === 'A' ? '🔵' : '🟢';
  const message = `${zoneEmoji} *Bag Update:* ${owner}'s bag has moved to *Zone ${zone}* on the collection belt.`;
  bot.sendMessage(chatId, message, { parse_mode: 'Markdown' })
    .then(() => console.log(`✅ Zone update sent to ${chatId}`))
    .catch((err) => console.error(`❌ Zone update FAILED:`, err.message));
}

function sendTelegramCollected(chatId, owner) {
  if (!chatId) return;
  const message = `✅ *Bag Collected!*\n\n${owner}'s bag has been picked up. Thank you for using LeBag! ✈️`;
  bot.sendMessage(chatId, message, { parse_mode: 'Markdown' })
    .then(() => console.log(`✅ Collected notification sent to ${chatId}`))
    .catch((err) => console.error(`❌ Collected notification FAILED:`, err.message));
}

// ==========================================
// HTTP SERVER (serves static files + API)
// ==========================================

const server = http.createServer(async (req, res) => {
  const urlPath = req.url.split('?')[0];

  // --- API ENDPOINTS ---

  // POST /api/trigger_notification  — called by LeBag server.py on QR scan
  if (req.method === 'POST' && urlPath === '/api/trigger_notification') {
    const body = await readBody(req);
    const { bag_id, chat_id, owner } = body;

    if (!bag_id || !chat_id || !owner) {
      res.writeHead(400, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ error: 'Missing bag_id, chat_id or owner' }));
    }

    console.log(`🔔 Trigger notification: owner=${owner}, bag=${bag_id}, chat=${chat_id}`);
    writeCurrentScan({
      tagId: bag_id,
      owner: owner,
      chat_id: chat_id,
      zone: null,
      status: 'On Belt',
      scannedAt: new Date().toISOString(),
    });
    sendTelegramAlert(chat_id, bag_id, owner);

    res.writeHead(200, { 'Content-Type': 'application/json' });
    return res.end(JSON.stringify({ message: 'Notification triggered' }));
  }

  // POST /api/zone_update  — called by tracker.py via server.py relay
  if (req.method === 'POST' && urlPath === '/api/zone_update') {
    const body = await readBody(req);
    const { owner, zone, chat_id } = body;

    if (!owner || !zone) {
      res.writeHead(400, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ error: 'Missing owner or zone' }));
    }

    console.log(`📍 Zone update: ${owner} → Zone ${zone}`);

    // Read chat_id from current_scan.json if not passed
    let resolvedChatId = chat_id;
    if (!resolvedChatId) {
      try {
        const scan = JSON.parse(fs.readFileSync(path.join(__dirname, 'current_scan.json'), 'utf8'));
        if (scan.owner === owner) resolvedChatId = scan.chat_id;
      } catch (_) { }
    }

    writeCurrentScan({ owner, zone, status: `Zone ${zone}` });
    if (resolvedChatId) sendTelegramZoneUpdate(resolvedChatId, owner, zone);

    res.writeHead(200, { 'Content-Type': 'application/json' });
    return res.end(JSON.stringify({ message: 'Zone updated' }));
  }

  // POST /api/bag_collected  — called by tracker.py via server.py relay
  if (req.method === 'POST' && urlPath === '/api/bag_collected') {
    const body = await readBody(req);
    const { owner, chat_id } = body;

    if (!owner) {
      res.writeHead(400, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ error: 'Missing owner' }));
    }

    console.log(`✅ Bag collected: ${owner}`);

    let resolvedChatId = chat_id;
    if (!resolvedChatId) {
      try {
        const scan = JSON.parse(fs.readFileSync(path.join(__dirname, 'current_scan.json'), 'utf8'));
        if (scan.owner === owner) resolvedChatId = scan.chat_id;
      } catch (_) { }
    }

    writeCurrentScan({ owner, zone: null, status: 'Collected' });
    if (resolvedChatId) sendTelegramCollected(resolvedChatId, owner);

    res.writeHead(200, { 'Content-Type': 'application/json' });
    return res.end(JSON.stringify({ message: 'Collection recorded' }));
  }

  // --- STATIC FILE SERVING ---

  if (urlPath === '/current_scan.json') {
    try {
      const data = fs.readFileSync(path.join(__dirname, 'current_scan.json'));
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(data);
    } catch {
      res.writeHead(404);
      return res.end('{}');
    }
  }

  if (urlPath.startsWith('/assets/')) {
    const filePath = path.join(__dirname, urlPath);
    const ext = path.extname(filePath).toLowerCase();
    try {
      const data = fs.readFileSync(filePath);
      res.writeHead(200, { 'Content-Type': MIME_TYPES[ext] || 'application/octet-stream' });
      return res.end(data);
    } catch {
      res.writeHead(404);
      return res.end('File not found');
    }
  }

  // Serve dashboard.html at /dashboard (operator belt view)
  if (urlPath === '/dashboard') {
    try {
      const html = fs.readFileSync(path.join(__dirname, 'dashboard.html'));
      res.writeHead(200, { 'Content-Type': 'text/html' });
      return res.end(html);
    } catch {
      res.writeHead(500);
      return res.end('Error loading dashboard');
    }
  }

  // Serve index.html for everything else (passenger tracking, ?tag=xxx links)
  try {
    const html = fs.readFileSync(path.join(__dirname, 'index.html'));
    res.writeHead(200, { 'Content-Type': 'text/html' });
    return res.end(html);
  } catch {
    res.writeHead(500);
    return res.end('Error loading page');
  }
});

server.listen(PORT, () => {
  const ip = getLocalIpAddress();
  console.log(`\n🌐 Web server running at http://${ip}:${PORT}`);
  console.log(`🛄 Operator dashboard:  http://${ip}:${PORT}/dashboard`);
  console.log(`📡 API endpoints ready:`);
  console.log(`   POST http://${ip}:${PORT}/api/trigger_notification`);
  console.log(`   POST http://${ip}:${PORT}/api/zone_update`);
  console.log(`   POST http://${ip}:${PORT}/api/bag_collected\n`);
});
