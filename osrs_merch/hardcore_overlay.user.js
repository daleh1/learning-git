// ==UserScript==
// @name         Hardcore GE Merch Overlay
// @namespace    learning-git/osrs-merch
// @version      1.0
// @description  Suggested GE flips on hardcore.malkana.dev, built only from the data the page itself loads (no extra requests to the site).
// @match        https://hardcore.malkana.dev/*
// @run-at       document-start
// @grant        none
// ==/UserScript==

/*
 * How it works
 * - The page fetches /v1/items (every item; prices lag ~40 min) and /v1/recent (the live trade feed)
 *   about every 30 s. This script wraps window.fetch, reads COPIES of those responses, and never sends
 *   its own requests to the site, so it cannot add load or trip rate limits.
 * - The only other request is the OSRS Wiki item mapping (buy limits, members flag), once a day, cached.
 * - Everything stays in this browser (localStorage). Personal use only; see the site's terms.
 */
(function () {
  "use strict";

  // ------------------------------------------------------------------ settings & state
  const KEY = "hcMerch:";
  const load = (k, d) => { try { const v = localStorage.getItem(KEY + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } };
  const save = (k, v) => { try { localStorage.setItem(KEY + k, JSON.stringify(v)); } catch (e) {} };

  const CAPTURE = 0.25;            // share of one side's 4h flow you can realistically fill
  const FILL_TTL = 2 * 3600;       // forget live trades older than this (s)
  const S = {
    items: null, itemsAt: 0,       // last /v1/items payload the page received
    fills: load("fills", {}),      // itemId -> {buy, buyTs, sell, sellTs}
    feedAt: 0, reporters: null,
    mapping: null,                 // itemId -> {limit, members}
    checks: load("checks", {}),    // itemId -> {bought, sold, at}
    opts: Object.assign({ budget: "", slots: 8, minVol: 5, maxRoi: 100, f2p: false, ttl: 30, open: true }, load("opts", {})),
  };

  // ------------------------------------------------------------------ capture the page's own data
  function ingest(url, data) {
    if (!data || typeof data !== "object") return;
    if (/\/v1\/items(\?|$)/.test(url) && Array.isArray(data.items) && !/[?&]ids=/.test(url)) {
      S.items = data.items; S.itemsAt = Date.now();
    } else if (/\/v1\/recent(\?|$)/.test(url) && Array.isArray(data.events)) {
      for (const ev of data.events) {
        if (ev.kind !== "fill" || !ev.price || !ev.ts) continue;
        const f = S.fills[ev.itemId] || (S.fills[ev.itemId] = {});
        const side = ev.side === "buy" ? "buy" : "sell";   // "buy": someone bought here = your sell target
        if (ev.ts >= (f[side + "Ts"] || 0)) { f[side] = ev.price; f[side + "Ts"] = ev.ts; }
      }
      const cutoff = Date.now() / 1000 - FILL_TTL;
      for (const k of Object.keys(S.fills)) {
        const f = S.fills[k];
        if (Math.max(f.buyTs || 0, f.sellTs || 0) < cutoff) delete S.fills[k];
      }
      if (data.stats) S.reporters = data.stats.reporters24;
      S.feedAt = Date.now();
      save("fills", S.fills);
    } else return;
    schedule();
  }

  const origFetch = window.fetch;
  window.fetch = function (input, init) {
    const p = origFetch.apply(this, arguments);
    try {
      const url = typeof input === "string" ? input : (input && input.url) || "";
      if (/\/v1\/(items|recent)/.test(url)) {
        p.then(r => { if (r.ok) r.clone().json().then(d => ingest(url, d)).catch(() => {}); }).catch(() => {});
      }
    } catch (e) {}
    return p;
  };
  // Also catch XMLHttpRequest, in case the page uses it somewhere.
  const origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url) {
    if (/\/v1\/(items|recent)/.test(String(url))) {
      this.addEventListener("load", () => { try { ingest(String(url), JSON.parse(this.responseText)); } catch (e) {} });
    }
    return origOpen.apply(this, arguments);
  };

  // ------------------------------------------------------------------ wiki mapping (limits), daily
  function loadMapping() {
    const cached = load("mapping", null);
    if (cached && Date.now() - cached.at < 24 * 3600 * 1000) { S.mapping = cached.m; return; }
    origFetch("https://prices.runescape.wiki/api/v1/osrs/mapping")
      .then(r => r.json())
      .then(list => {
        const m = {};
        for (const it of list) m[it.id] = { limit: it.limit || null, members: !!it.members };
        S.mapping = m; save("mapping", { at: Date.now(), m }); schedule();
      })
      .catch(() => { S.mapping = {}; });
  }

  // ------------------------------------------------------------------ analysis
  const TAX_EXEMPT = new Set(["old school bond", "chisel", "gardening trowel", "glassblowing pipe", "hammer", "needle",
    "pestle and mortar", "rake", "saw", "secateurs", "seed dibber", "shears", "spade", "watering can(0)"]);
  const tax = (p, name) => (p < 50 || TAX_EXEMPT.has((name || "").toLowerCase())) ? 0 : Math.min(Math.floor(p * 0.02), 5e6);

  function parseGp(t) {
    t = String(t || "").trim().toLowerCase().replace(/,/g, "");
    if (!t) return 0;
    const m = { k: 1e3, m: 1e6, b: 1e9 }[t.slice(-1)];
    const v = parseFloat(m ? t.slice(0, -1) : t);
    return isNaN(v) ? 0 : Math.floor(v * (m || 1));
  }
  function gp(n) {
    if (n == null) return "–";
    const a = Math.abs(n);
    if (a >= 1e9) return (n / 1e9).toFixed(2) + "b";
    if (a >= 1e6) return (n / 1e6).toFixed(2) + "m";
    if (a >= 1e4) return (n / 1e3).toFixed(1) + "k";
    return n.toLocaleString();
  }
  const ago = s => s < 60 ? Math.max(0, Math.floor(s)) + "s" : s < 3600 ? Math.floor(s / 60) + "m" : Math.floor(s / 3600) + "h" + Math.floor(s % 3600 / 60) + "m";

  function flips() {
    const now = Date.now() / 1000, out = [];
    const ttl = (+S.opts.ttl || 30) * 60;
    for (const it of S.items || []) {
      let sell = it.lastFillBuy || it.lastBuy, sellT = it.lastFillBuyTime || it.lastBuyTime;
      let buy = it.lastFillSell || it.lastSell, buyT = it.lastFillSellTime || it.lastSellTime;
      let src = "delayed list";
      const f = S.fills[it.id];
      if (f) {
        if (f.buyTs > (sellT || 0)) { sell = f.buy; sellT = f.buyTs; src = "live trades"; }
        if (f.sellTs > (buyT || 0)) { buy = f.sell; buyT = f.sellTs; src = "live trades"; }
      }
      const c = S.checks[it.id];
      const checked = c && (now * 1000 - c.at) / 1000 <= ttl ? c : null;
      if (checked) {
        if (checked.bought) { sell = checked.bought; sellT = checked.at / 1000; }
        if (checked.sold) { buy = checked.sold; buyT = checked.at / 1000; }
        src = "your check";
      }
      if (!sell || !buy) continue;
      const m = (S.mapping || {})[it.id] || {};
      const t = tax(sell, it.name), margin = sell - t - buy;
      const avgMargin = it.avgBuy24 && it.avgSell24 ? it.avgBuy24 - tax(it.avgBuy24, it.name) - it.avgSell24 : null;
      const flow = Math.floor((it.vol24 || 0) / 2 / 6 * CAPTURE);
      out.push({
        id: it.id, name: it.name, buy, sell, tax: t, margin, avgMargin, roi: margin / buy,
        units: m.limit ? Math.min(m.limit, flow) : flow, limit: m.limit, members: m.members,
        vol: it.vol24 || 0, age: now - Math.min(sellT || 0, buyT || 0), src, checked,
      });
    }
    return out;
  }

  function recommend(all) {
    const o = S.opts, budget = parseGp(o.budget), slots = Math.max(1, Math.min(8, +o.slots || 8));
    const cands = all.filter(f => f.margin > 0 && f.units > 0 && f.vol >= (+o.minVol || 0) &&
        f.roi <= (+o.maxRoi || 100) / 100 && (!o.f2p || !f.members) &&
        (f.checked || f.avgMargin == null || f.avgMargin > 0))
      .map(f => Object.assign({}, f, { conf: f.checked || f.avgMargin == null ? f.margin : Math.min(f.margin, f.avgMargin) }))
      .sort((a, b) => b.conf * b.units - a.conf * a.units);
    let left = budget || Infinity; const picks = [];
    for (const c of cands) {
      if (picks.length >= slots) break;
      const units = Math.min(c.units, Math.floor(left / c.buy));
      if (units <= 0) continue;
      picks.push(Object.assign(c, { units, cost: units * c.buy, profit: units * c.conf }));
      left -= units * c.buy;
    }
    return picks;
  }

  // ------------------------------------------------------------------ panel
  let root, timer = null, checking = null;
  const el = (tag, props = {}, ...kids) => { const e = document.createElement(tag); Object.assign(e, props); for (const k of kids) e.append(k); return e; };
  function schedule() { if (!timer) timer = setTimeout(() => { timer = null; render(); }, 300); }

  const CSS = `
    #hcm { position: fixed; right: 16px; bottom: 16px; z-index: 2147483647; width: 600px; max-width: calc(100vw - 32px);
      max-height: 75vh; overflow: auto; background: #1d1b16; color: #ece7db; border: 1px solid #3a362c; border-radius: 12px;
      box-shadow: 0 10px 30px rgba(0,0,0,.45); font: 13px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    #hcm.min { width: auto; }
    #hcm .hd { display: flex; align-items: center; gap: 8px; padding: 9px 12px; border-bottom: 1px solid #3a362c; cursor: pointer; position: sticky; top: 0; background: #1d1b16; }
    #hcm .hd b { color: #e0b04a; }
    #hcm .hd span { color: #9c968a; font-size: 12px; margin-left: auto; }
    #hcm .bd { padding: 10px 12px; }
    #hcm .ctl { display: flex; flex-wrap: wrap; gap: 8px 12px; margin-bottom: 8px; }
    #hcm label { display: flex; flex-direction: column; font-size: 11px; color: #9c968a; gap: 2px; }
    #hcm label.cb { flex-direction: row; align-items: center; gap: 5px; color: #ece7db; font-size: 12px; padding-top: 14px; }
    #hcm input[type=text], #hcm input[type=number] { width: 78px; font: inherit; padding: 4px 6px; border: 1px solid #3a362c; border-radius: 6px; background: #14130f; color: #ece7db; }
    #hcm table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
    #hcm th, #hcm td { padding: 5px 6px; border-bottom: 1px solid #2e2b24; text-align: right; white-space: nowrap; }
    #hcm th { color: #9c968a; font-size: 11px; font-weight: 600; }
    #hcm td.n, #hcm th.n { text-align: left; white-space: normal; }
    #hcm .r { color: #e0b04a; font-weight: 700; }
    #hcm .g { color: #5cc27a; font-weight: 600; }
    #hcm .bad { color: #ef7d74; }
    #hcm .dim { color: #9c968a; }
    #hcm button { font: inherit; font-size: 12px; border: 1px solid #3a362c; background: #2a2720; color: #ece7db; border-radius: 6px; padding: 2px 8px; cursor: pointer; }
    #hcm button.ok { color: #5cc27a; }
    #hcm .chk { background: #24221c; }
    #hcm .chk td { text-align: left; }
    #hcm .chk input { width: 90px; margin: 0 6px 0 4px; }
    #hcm .tot { margin-top: 8px; color: #9c968a; }
    #hcm .tot b { color: #ece7db; }
    #hcm .note { margin-top: 6px; font-size: 11px; color: #9c968a; }`;

  function mount() {
    if (root || !document.body) return;
    document.head.append(el("style", { textContent: CSS }));
    root = el("div", { id: "hcm" });
    document.body.append(root);
    render();
    setInterval(render, 15000);   // keep ages/expiry fresh
  }

  function input(key, label, type = "text") {
    const i = el("input", { type, value: S.opts[key] });
    i.addEventListener("change", () => { S.opts[key] = type === "checkbox" ? i.checked : i.value; save("opts", S.opts); render(); });
    if (type === "checkbox") { i.checked = !!S.opts[key]; return el("label", { className: "cb" }, i, label); }
    return el("label", {}, label, i);
  }

  function render() {
    if (!root) return;
    const o = S.opts;
    root.className = o.open ? "" : "min";
    root.replaceChildren();
    const head = el("div", { className: "hd" }, el("b", { textContent: "Merch overlay" }),
      el("span", { textContent: o.open ? "click to minimise" : "click to open" }));
    head.onclick = () => { o.open = !o.open; save("opts", o); render(); };
    root.append(head);
    if (!o.open) return;

    const body = el("div", { className: "bd" });
    root.append(body);
    body.append(el("div", { className: "ctl" },
      input("budget", "Budget (e.g. 20m)"), input("slots", "GE slots", "number"), input("minVol", "Min 24h vol", "number"),
      input("maxRoi", "Max ROI %", "number"), input("ttl", "Checks valid (min)", "number"), input("f2p", "F2P only", "checkbox")));

    if (!S.items) {
      body.append(el("div", { className: "dim", textContent:
        "Waiting for this page to load its market data… Open the home/market page and leave it open. " +
        "(If the page itself shows no data, the site may be rate limiting your connection; wait and reload.)" }));
      return;
    }
    const now = Date.now();
    const all = flips(), picks = recommend(all);
    const t = el("table");
    t.append(el("tr", {}, ...["#", "Item", "Buy", "Sell at", "Profit", "Prices from", ""].map((h, i) => el("th", { textContent: h, className: i === 1 ? "n" : "" }))));
    picks.forEach((p, i) => {
      const btn = el("button", { textContent: p.checked ? "✓ checked" : "Check", className: p.checked ? "ok" : "" });
      btn.onclick = () => { checking = checking === p.id ? null : p.id; render(); };
      t.append(el("tr", {},
        el("td", { className: "r", textContent: i + 1 }),
        el("td", { className: "n", textContent: p.name }),
        el("td", { textContent: `${p.units.toLocaleString()} @ ${p.buy.toLocaleString()}` }),
        el("td", { textContent: p.sell.toLocaleString() }),
        el("td", { className: "g", textContent: gp(p.profit) }),
        el("td", { className: "dim", textContent: p.src === "your check"
          ? `your check, ${ago((now - p.checked.at) / 1000)} ago` : `${p.src}, ${ago(p.age)} old` }),
        el("td", {}, btn)));
      if (checking === p.id) t.append(checkRow(p));
    });
    body.append(t);
    if (!picks.length) body.append(el("div", { className: "dim", textContent: "No trades match these settings yet." }));
    const cost = picks.reduce((s, p) => s + p.cost, 0), prof = picks.reduce((s, p) => s + p.profit, 0);
    if (picks.length) body.append(el("div", { className: "tot" }, "Total ", el("b", { textContent: gp(cost) }),
      " invested → ", el("b", { textContent: gp(prof) }), " expected per 4h buy-limit cycle (after 2% tax)."));
    const live = Object.keys(S.fills).length;
    body.append(el("div", { className: "note", textContent:
      `Using only data this page loaded: item list ${ago((now - S.itemsAt) / 1000)} ago (its prices lag ~40 min), ` +
      `live trade feed ${S.feedAt ? ago((now - S.feedAt) / 1000) + " ago" : "not seen yet"}, live prices for ${live} items` +
      (S.reporters ? `, ${S.reporters.toLocaleString()} players reporting today` : "") +
      (S.mapping && Object.keys(S.mapping).length ? "." : ". (Buy limits loading…)") +
      " Margin check: buy 1 well above price and sell 1 well below, then enter what you paid/got." }));
  }

  function checkRow(p) {
    const c = S.checks[p.id] || {};
    const b = el("input", { type: "text", value: c.bought || "", placeholder: String(p.sell) });
    const s = el("input", { type: "text", value: c.sold || "", placeholder: String(p.buy) });
    const prev = el("span", { className: "dim" });
    const upd = () => {
      const bv = parseGp(b.value), sv = parseGp(s.value);
      if (!bv || !sv) { prev.textContent = ""; return; }
      const m = bv - tax(bv, p.name) - sv;
      prev.textContent = ` margin ${m.toLocaleString()} gp/item`;
      prev.className = m > 0 ? "g" : "bad";
    };
    b.oninput = s.oninput = upd; upd();
    const ok = el("button", { textContent: "Save" });
    ok.onclick = () => {
      const bv = parseGp(b.value), sv = parseGp(s.value);
      if (bv || sv) S.checks[p.id] = { bought: bv || null, sold: sv || null, at: Date.now() };
      save("checks", S.checks); checking = null; render();
    };
    const clr = el("button", { textContent: "Clear" });
    clr.onclick = () => { delete S.checks[p.id]; save("checks", S.checks); checking = null; render(); };
    return el("tr", { className: "chk" }, el("td", { colSpan: 7 },
      "Bought 1 for", b, "Sold 1 for", s, ok, " ", clr, prev));
  }

  loadMapping();
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount); else mount();
})();
