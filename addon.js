#!/usr/bin/env node
/**
 * Druggo - Stremio/Nuvio-compatible live TV addon (no external dependencies).
 * Serves verified live streams scraped from public Stremio-compatible sources.
 *
 * Run:  node addon.js
 * Add in Nuvio/Stremio: http://127.0.0.1:7000/manifest.json
 * (For GitHub-hosted static use, serve this directory; or deploy the server.)
 *
 * NOTE: Many live stream URLs are tokenized/signed/geo-restricted and may expire.
 * The GitHub Action refreshes data/streams.json by scraping and rechecking.
 */
const http = require('http');
const fs = require('fs');
const path = require('path');
const url = require('url');

const PORT = process.env.PORT || 7000;
const DATA_PATH = path.join(__dirname, 'data', 'streams.json');

function loadData() {
  try { return JSON.parse(fs.readFileSync(DATA_PATH, 'utf8')); }
  catch (e) { return { meta: { count: 0, ephemeral_count: 0 }, items: [] }; }
}

const manifest = {
  id: 'community.druggo.live-tv',
  version: '1.0.0',
  name: 'Druggo',
  description: 'Druggo live TV/sports - verified streams scraped from public Stremio-compatible sources. Some URLs are signed/ephemeral and may expire; refreshed by automation.',
  logo: 'https://raw.githubusercontent.com/nuvio-tv/assets/main/icon.png',
  resources: ['catalog', 'meta', 'stream'],
  types: ['tv', 'channel'],
  catalogs: [
    {
      type: 'tv',
      id: 'druggo_live',
      name: 'Druggo Live TV',
      extra: [
        { name: 'search', isRequired: false },
        { name: 'genre', isRequired: false, options: [] },
        { name: 'skip', isRequired: false }
      ]
    }
  ],
  idPrefixes: ['dg_', 'nl_'],
  behaviorHints: { configurable: false, configurationRequired: false }
};

function json(res, obj, cache = 300) {
  res.writeHead(200, {
    'Content-Type': 'application/json; charset=utf-8',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': '*',
    'Cache-Control': `max-age=${cache}, public`
  });
  res.end(JSON.stringify(obj));
}
function notFound(res) {
  res.writeHead(404, { 'Access-Control-Allow-Origin': '*', 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ err: 'not found' }));
}

function buildManifest(data) {
  const addons = [...new Set(data.items.map(i => i.addon))].filter(Boolean).sort();
  const m = JSON.parse(JSON.stringify(manifest));
  m.catalogs[0].extra.find(e => e.name === 'genre').options = addons;
  m.description += ` (${data.meta.count || data.items.length} checked, ${data.meta.ephemeral_count || 0} ephemeral; updated ${data.meta.generated || 'n/a'}).`;
  return m;
}

const server = http.createServer((req, res) => {
  if (req.method === 'OPTIONS') {
    res.writeHead(204, { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': '*', 'Access-Control-Allow-Methods': 'GET,OPTIONS' });
    return res.end();
  }
  const parsed = url.parse(req.url, true);
  const p = parsed.pathname.replace(/\.json$/, '');
  const q = parsed.query || {};
  const data = loadData();

  if (p === '/' || p === '') return json(res, { addon: 'Druggo', manifest: '/manifest.json', streams: data.meta.count || data.items.length }, 60);
  if (p === '/manifest') return json(res, buildManifest(data), 300);

  let m = p.match(/^\/catalog\/([^/]+)\/([^/]+)(?:\/(.*))?$/);
  if (m) {
    const [, type, id, extraStr] = m;
    if (id !== 'druggo_live') return json(res, { metas: [] });
    let extra = {};
    if (extraStr) extraStr.split('&').forEach(kv => { const [k, v] = kv.split('='); if (k) extra[decodeURIComponent(k)] = decodeURIComponent(v || ''); });
    const search = (extra.search || q.search || '').toLowerCase();
    const genre = extra.genre || q.genre || '';
    const skip = parseInt(extra.skip || q.skip || '0', 10) || 0;
    let items = data.items.filter(i => (type === 'tv' || type === 'channel'));
    if (genre) items = items.filter(i => i.addon === genre);
    if (search) items = items.filter(i => (i.name + ' ' + (i.description || '') + ' ' + i.addon).toLowerCase().includes(search));
    return json(res, { metas: items.slice(skip, skip + 100).map(i => ({
      id: i.id, type: 'tv', name: i.name, description: i.description,
      posterShape: 'square', genres: [i.addon, i.ephemeral ? 'ephemeral' : 'stable'].filter(Boolean),
      releaseInfo: i.ephemeral ? 'SIGNED' : 'OK'
    })) });
  }

  m = p.match(/^\/meta\/([^/]+)\/([^/]+)$/);
  if (m) {
    const item = data.items.find(i => i.id === m[2]);
    if (!item) return json(res, { meta: null });
    const qdesc = (item.qualities || []).slice(0, 6).map(x => `- ${x.resolution || '?'} ${x.bandwidth || ''} ${x.frame_rate ? x.frame_rate + 'fps' : ''}`).join('\n');
    return json(res, { meta: {
      id: item.id, type: 'tv', name: item.name,
      description: (item.description || '') + (qdesc ? '\n\nParsed qualities:\n' + qdesc : '') + '\n\n⚠ Verify with check_streams.py; signed URLs may expire.',
      posterShape: 'square', genres: [item.addon].filter(Boolean),
      behaviorHints: { defaultVideoId: item.id }
    }});
  }

  m = p.match(/^\/stream\/([^/]+)\/([^/]+)$/);
  if (m) {
    const item = data.items.find(i => i.id === m[2]);
    if (!item) return json(res, { streams: [] });
    const s = Object.assign({}, item.stream);
    if (s.name !== 'Druggo') s.name = 'Druggo';
    return json(res, { streams: [s] });
  }

  notFound(res);
});

server.listen(PORT, '127.0.0.1', () => {
  const data = loadData();
  console.log(`Druggo addon running on http://127.0.0.1:${PORT}`);
  console.log(`Manifest: http://127.0.0.1:${PORT}/manifest.json`);
  console.log(`Loaded ${data.meta.count || data.items.length} streams.`);
});
