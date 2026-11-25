/**
 * Cloudflare Worker relay — optional egress tier.
 *
 * GitHub Actions runners sit in Azure datacenter ranges that many anti-bot
 * systems block outright. This worker forwards a fetch from Cloudflare's
 * egress IPs. Free plan: 100k requests/day, far more than a daily crawl needs.
 *
 * Deploy:
 *   wrangler deploy worker.js --name free-llm-relay
 *   wrangler secret put RELAY_SECRET
 * Then set RELAY_URL=https://<worker>.<subdomain>.workers.dev and the same
 * RELAY_SECRET as repository secrets. Without them the pipeline simply skips
 * this tier (see scrape.py: fetch_relay).
 *
 * It only fetches public http(s) URLs, refuses private ranges, and never
 * touches cookies or credentials.
 */

const MAX_BYTES = 8 * 1024 * 1024;
const TIMEOUT_MS = 25_000;

function isPrivateHost(host) {
  const h = host.toLowerCase();
  if (h === "localhost" || h.endsWith(".local") || h.endsWith(".internal")) return true;
  if (h === "::1" || h === "[::1]") return true;
  const v4 = h.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
  if (v4) {
    const [a, b] = [Number(v4[1]), Number(v4[2])];
    if (a === 10 || a === 127 || a === 0) return true;
    if (a === 169 && b === 254) return true;
    if (a === 172 && b >= 16 && b <= 31) return true;
    if (a === 192 && b === 168) return true;
    if (a === 100 && b >= 64 && b <= 127) return true;
  }
  return false;
}

function timingSafeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export default {
  async fetch(request, env) {
    const auth = request.headers.get("Authorization") || "";
    const expected = `Bearer ${env.RELAY_SECRET || ""}`;
    if (!env.RELAY_SECRET || !timingSafeEqual(auth, expected)) {
      return new Response("Unauthorized", { status: 401 });
    }

    const target = new URL(request.url).searchParams.get("url");
    if (!target) return new Response("Missing url", { status: 400 });

    let parsed;
    try {
      parsed = new URL(target);
    } catch {
      return new Response("Bad url", { status: 400 });
    }
    if (!/^https?:$/.test(parsed.protocol) || isPrivateHost(parsed.hostname)) {
      return new Response("Forbidden target", { status: 403 });
    }

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
    try {
      const upstream = await fetch(parsed.toString(), {
        redirect: "follow",
        signal: controller.signal,
        headers: {
          "User-Agent": "Mozilla/5.0 (compatible; FreeLLMResearch/1.0; +https://github.com/)",
          "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
        },
        cf: { cacheTtl: 300, cacheEverything: false },
      });

      const reader = upstream.body?.getReader();
      let received = 0;
      const chunks = [];
      if (reader) {
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          received += value.byteLength;
          if (received > MAX_BYTES) {
            controller.abort();
            break;
          }
          chunks.push(value);
        }
      }
      const body = new Uint8Array(received > MAX_BYTES ? 0 : received);
      let offset = 0;
      for (const c of chunks) {
        if (offset + c.byteLength > body.byteLength) break;
        body.set(c, offset);
        offset += c.byteLength;
      }
      return new Response(body, {
        status: upstream.status,
        headers: {
          "Content-Type": upstream.headers.get("content-type") || "text/html; charset=utf-8",
          "X-Relay-Status": String(upstream.status),
        },
      });
    } catch (err) {
      return new Response(`Relay error: ${err}`.slice(0, 200), { status: 502 });
    } finally {
      clearTimeout(timer);
    }
  },
};
