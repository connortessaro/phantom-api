// Phantom purchase flow. No external deps. No analytics. No third-party assets.
(() => {
  const $ = (id) => document.getElementById(id);
  const overlay     = $("purchase-overlay");
  const purchase    = $("purchase");
  const summary     = $("payment-summary");
  const qrImg       = $("qr");
  const addrEl      = $("xmr-address");
  const amountEl    = $("xmr-amount");
  const statusEl    = $("payment-status");
  const countdownEl = $("payment-countdown");
  const keyBox      = $("api-key-box");
  const keyValue    = $("api-key-value");
  const closeBtn    = $("purchase-close");

  let pollTimer = null;
  let countdownTimer = null;
  let expiresAt = null;

  // ── Modal open/close
  function openModal() {
    overlay.hidden = false;
    overlay.setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";
  }
  function closeModal() {
    overlay.hidden = true;
    overlay.setAttribute("aria-hidden", "true");
    document.body.style.overflow = "";
    stopAll();
  }
  closeBtn?.addEventListener("click", closeModal);
  overlay?.addEventListener("click", (e) => {
    if (e.target === overlay) closeModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !overlay.hidden) closeModal();
  });

  // ── Buy buttons (bundles)
  document.querySelectorAll("[data-buy]").forEach(btn => {
    btn.addEventListener("click", () => offerRail(JSON.parse(btn.dataset.buy)));
  });

  // ── Buy custom amount
  $("custom-buy")?.addEventListener("click", () => {
    const amt = parseFloat($("custom-input").value);
    if (!(amt >= 10 && amt <= 1000)) {
      alert("Amount must be between $10 and $1000.");
      return;
    }
    offerRail({ amount_usd: amt });
  });

  // ── Claim by payment ID. Routes through the existing #claim/ hash flow:
  // the hashchange listener fires handleClaimReturn → opens modal in
  // "claiming" state → polls /v1/purchase/<id>/status → key drops on
  // first response with status=completed.
  function _submitClaim() {
    const raw = ($("claim-input")?.value || "").trim();
    if (!raw) return;
    // payment_id is secrets.token_urlsafe(16) — 22 chars of URL-safe base64.
    // Guard against arbitrary garbage so we don't pollute the hash + poll loop.
    if (!/^[A-Za-z0-9_-]{8,64}$/.test(raw)) {
      alert("That doesn't look like a payment id.");
      return;
    }
    window.location.hash = "#claim/" + raw;
  }
  $("claim-go")?.addEventListener("click", _submitClaim);
  $("claim-input")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); _submitClaim(); }
  });

  // ── Coin chooser (hybrid rail). Wraps every BUY click. Lets user pick
  // XMR direct (MoneroPay) vs other crypto (NowPayments). If only one rail
  // is configured on the server side, both buttons just call /v1/purchase
  // with the chosen rail and the server picks for us.
  // Cached bundle data + surcharge from /v1/bundles. Refreshed on first
  // chooser open. Falls back to hardcoded bundle prices if endpoint
  // unavailable (e.g., static-only frontend deploy).
  const _BUNDLE_FALLBACK = {
    small:  { price_usd: 10,  credit_usd: 10 },
    medium: { price_usd: 50,  credit_usd: 55 },
    large:  { price_usd: 200, credit_usd: 230 },
    whale:  { price_usd: 500, credit_usd: 600 },
  };
  let _bundleCache = null;
  let _surchargePct = 5;
  async function _loadBundleData() {
    if (_bundleCache) return _bundleCache;
    try {
      const r = await fetch("/v1/bundles");
      if (!r.ok) throw new Error("bundles fetch failed");
      const d = await r.json();
      _surchargePct = d.multi_crypto_surcharge_pct ?? 5;
      _bundleCache = {};
      (d.data || []).forEach(b => { _bundleCache[b.name] = b; });
      return _bundleCache;
    } catch (_) {
      _bundleCache = _BUNDLE_FALLBACK;
      return _bundleCache;
    }
  }

  function offerRail(body) {
    const overlay = $("rail-overlay");
    const summary = $("rail-summary");
    if (!overlay) {
      // Fallback if chooser HTML missing — go straight to purchase.
      startPurchase(body);
      return;
    }
    if (summary) {
      const label = body.bundle ? body.bundle.toUpperCase() : "$" + body.amount_usd;
      summary.textContent = `Order: ${label} bundle.`;
    }
    // Populate per-rail prices. Uses bundle cache for bundles, or
    // computes inline for custom amounts.
    _loadBundleData().then(cache => {
      const factor = 1 + (_surchargePct / 100);
      const pctEl = $("rail-multi-pct");
      if (pctEl) pctEl.textContent = `+${_surchargePct}%`;
      let sticker, credit;
      if (body.bundle && cache && cache[body.bundle]) {
        sticker = cache[body.bundle].price_usd;
        credit  = cache[body.bundle].credit_usd;
      } else if (body.amount_usd) {
        sticker = Number(body.amount_usd);
        credit  = sticker; // custom = 1:1
      } else {
        sticker = 0; credit = 0;
      }
      const multi = Math.round(sticker * factor * 100) / 100;
      const fmt = (n) => "$" + Number(n).toLocaleString(undefined, {minimumFractionDigits: 0, maximumFractionDigits: 2});
      const setText = (id, v) => { const el = $(id); if (el) el.textContent = v; };
      setText("rail-xmr-price",    fmt(sticker));
      setText("rail-xmr-credit",   fmt(credit));
      setText("rail-multi-price",  fmt(multi));
      setText("rail-multi-credit", fmt(credit));
    });
    overlay.hidden = false;
    overlay.setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";
    const xmrBtn = $("rail-xmr-btn");
    const multiBtn = $("rail-multi-btn");
    const closeBtn = $("rail-close");
    const closeRail = () => {
      overlay.hidden = true;
      overlay.setAttribute("aria-hidden", "true");
      document.body.style.overflow = "";
    };
    const pick = (rail) => {
      closeRail();
      startPurchase({ ...body, rail });
    };
    xmrBtn.onclick = () => pick("xmr");
    multiBtn.onclick = () => pick("multi");
    closeBtn.onclick = closeRail;
    overlay.onclick = (e) => { if (e.target === overlay) closeRail(); };
  }

  // ── Copy buttons
  document.body.addEventListener("click", (e) => {
    const btn = e.target.closest(".copybtn");
    if (!btn) return;
    const text = $(btn.dataset.copy)?.textContent || "";
    navigator.clipboard.writeText(text).then(() => {
      const orig = btn.textContent;
      btn.textContent = "Copied ✓";
      setTimeout(() => (btn.textContent = orig), 1500);
    });
  });

  async function startPurchase(body) {
    // Open the modal IMMEDIATELY in loading state so the user gets feedback in <100ms.
    // The backend POST takes 1-5s because it hits the operator's wallet over Tor
    // (legacy XMR rail) or NowPayments invoice creation (new rail).
    showCreating();
    openModal();
    try {
      const r = await fetch("/v1/purchase", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        const detail = await r.text().catch(() => "");
        showCreatingError("Error creating payment: " + r.status + " " + detail);
        return;
      }
      const p = await r.json();
      // NowPayments rail returns a checkout_url for redirect. Legacy XMR rail
      // returns xmr_address + xmr_amount for in-page QR rendering. Dispatch on shape.
      if (p.checkout_url) {
        // Save payment_id in localStorage so the #claim/<id> return handler
        // can find it. Survives browser close. NowPayments redirects back to
        // /#claim/<id> on success.
        try {
          localStorage.setItem("phantom_pending_pid", p.payment_id);
          localStorage.setItem("phantom_pending_bundle", p.bundle || "");
          localStorage.setItem("phantom_pending_credit", String(p.credit_usd || ""));
        } catch (_) {}
        // Show interstitial with payment_id before redirect so user can
        // copy/screenshot it. Critical: if they close the browser before
        // NowPayments redirects back, they need this id to recover the key
        // via /v1/purchase/<id>/status.
        showPreRedirect(p.payment_id, p.checkout_url, p.credit_usd);
        return;
      }
      renderPurchase(p);
      poll(p.payment_id);
      startCountdown(p.expires_at);
    } catch (e) {
      showCreatingError("Network error. Try again.");
    }
  }

  // Pre-redirect interstitial: show payment_id prominently so user can
  // save it before leaving phantom. NowPayments redirect carries it in the
  // success_url, but if user closes browser or pays then drops, this is
  // their recovery handle.
  function showPreRedirect(pid, checkoutUrl, creditUsd) {
    keyBox.hidden = true;
    summary.innerHTML = `▸ redirecting to <strong>NowPayments</strong> for $${creditUsd || ""} credit.`;
    addrEl.textContent = "(set on NowPayments)";
    amountEl.textContent = "(set on NowPayments)";
    const pidEl = $("payment-id");
    if (pidEl) pidEl.textContent = pid;
    statusEl.textContent = "save your payment id, then continue";
    setStatusDot("pending");
    const skel = $("qr-skel");
    if (skel) {
      skel.hidden = false;
      skel.innerHTML = `
        <div style="text-align:center; padding:1rem;">
          <p style="font-size:.85rem; margin:0 0 .5rem;"><strong>Copy your PAYMENT ID first.</strong></p>
          <p style="font-size:.78rem; color:var(--warn,#ff6600); margin:0 0 1rem;">
            This is your only recovery handle.<br>
            Lose it and your payment is unclaimable.
          </p>
          <button id="continue-checkout" class="btn btn-primary" style="display:inline-block;">&gt;&gt; CONTINUE TO PAY</button>
        </div>`;
    }
    qrImg.hidden = true;
    const dl = $("qr-deeplink");
    if (dl) dl.hidden = true;
    setTimeout(() => {
      const goBtn = $("continue-checkout");
      goBtn?.addEventListener("click", () => {
        window.location.href = checkoutUrl;
      });
    }, 50);
  }

  // ── NowPayments return handler.
  // Customer hits NowPayments-hosted checkout, pays, gets redirected back to
  // /#claim/<payment_id>. We open the modal in "polling" state and wait for
  // the IPN webhook to flip status to ready → completed (key drops).
  function handleClaimReturn() {
    const hash = window.location.hash;
    if (!hash.startsWith("#claim/")) return;
    const pid = hash.slice("#claim/".length).trim();
    if (!pid) return;
    showClaiming(pid);
    openModal();
    poll(pid);
  }

  function showClaiming(pid) {
    keyBox.hidden = true;
    summary.innerHTML = "▸ payment received. confirming on chain. key drops here in 1-5 min.";
    addrEl.textContent = "(confirmed)";
    amountEl.textContent = "(confirmed)";
    const pidEl = $("payment-id");
    if (pidEl) pidEl.textContent = pid;
    statusEl.textContent = "confirming…";
    setStatusDot("confirming");
    const skel = $("qr-skel");
    if (skel) {
      skel.hidden = false;
      skel.innerHTML = `
        <div style="text-align:center; padding:1rem;">
          <div class="qr-spinner" style="margin:0 auto 1rem;"></div>
          <p style="font-size:.85rem; margin:0 0 .5rem;"><strong>WAITING ON CONFIRMATION…</strong></p>
          <p style="font-size:.75rem; color:var(--fg-2); margin:0;">
            Safe to leave this tab. Return any time to<br>
            <code style="font-size:.75rem; color:var(--xmr);">phantom.codes/#claim/${pid.slice(0,12)}…</code><br>
            (copy the PAYMENT ID below if you haven't yet)
          </p>
        </div>`;
    }
    qrImg.hidden = true;
    const dl = $("qr-deeplink");
    if (dl) dl.hidden = true;
  }

  // Fire on initial load + on hash changes (in case customer navigates within site)
  if (window.location.hash.startsWith("#claim/")) {
    // Defer until DOM is ready (script may load before body parsed in some setups)
    document.addEventListener("DOMContentLoaded", handleClaimReturn);
    if (document.readyState !== "loading") handleClaimReturn();
  }
  window.addEventListener("hashchange", handleClaimReturn);

  // Resume prompt: if a pending payment_id is stashed and we're not already
  // mid-flow (no #claim/ hash), offer to resume. Survives browser close.
  function maybeOfferResume() {
    if (window.location.hash.startsWith("#claim/")) return;
    let pid;
    try { pid = localStorage.getItem("phantom_pending_pid"); } catch (_) {}
    if (!pid) return;
    // Mount resume banner above pricing section.
    const pricing = document.getElementById("pricing");
    if (!pricing || document.getElementById("resume-banner")) return;
    const banner = document.createElement("div");
    banner.id = "resume-banner";
    banner.style.cssText = "max-width:840px; margin:1.5rem auto; padding:1rem 1.25rem; border:1px solid var(--xmr,#ff6600); background:rgba(255,102,0,0.06); font-size:.9rem;";
    banner.innerHTML = `
      <p style="margin:0 0 .5rem;"><strong>// PENDING PAYMENT</strong></p>
      <p style="margin:0 0 .75rem;">You started a purchase but didn't claim your key. ID: <code>${escapeHtml(pid)}</code></p>
      <button id="resume-claim-btn" class="btn btn-primary" style="margin-right:.5rem;">&gt;&gt; CHECK PAYMENT</button>
      <button id="resume-dismiss-btn" class="btn btn-ghost">dismiss</button>`;
    pricing.parentNode.insertBefore(banner, pricing);
    document.getElementById("resume-claim-btn").addEventListener("click", () => {
      window.location.hash = "#claim/" + pid;
    });
    document.getElementById("resume-dismiss-btn").addEventListener("click", () => {
      try {
        localStorage.removeItem("phantom_pending_pid");
        localStorage.removeItem("phantom_pending_bundle");
        localStorage.removeItem("phantom_pending_credit");
      } catch (_) {}
      banner.remove();
    });
  }
  if (document.readyState !== "loading") maybeOfferResume();
  else document.addEventListener("DOMContentLoaded", maybeOfferResume);

  // Clear stash once key is shown (handled in success path below).

  function showCreating() {
    keyBox.hidden = true;
    summary.innerHTML = "▸ talking to wallet over Tor. Usually 2-5 seconds…";
    addrEl.textContent = "(loading)";
    amountEl.textContent = "(loading)";
    statusEl.textContent = "creating subaddress…";
    countdownEl.textContent = "";
    setStatusDot("pending");
    const skel = $("qr-skel");
    if (skel) {
      skel.hidden = false;
      skel.innerHTML = '<div class="qr-spinner"></div><span class="qr-skel-label">CREATING…</span>';
    }
    qrImg.hidden = true;
    const dl = $("qr-deeplink");
    if (dl) dl.hidden = true;
  }

  function showCreatingError(msg) {
    summary.innerHTML = '<span class="warn">' + escapeHtml(msg) + '</span>';
    statusEl.textContent = "failed";
    setStatusDot("expired");
    const skel = $("qr-skel");
    if (skel) skel.innerHTML = '<span class="qr-skel-label warn">failed</span>';
  }

  function renderPurchase(p) {
    keyBox.hidden = true;
    summary.innerHTML = `▸ <strong>${escapeHtml(p.bundle)}</strong> bundle. <strong>$${p.credit_usd}</strong> credit on confirmation.`;
    addrEl.textContent = p.xmr_address;
    amountEl.textContent = p.xmr_amount;
    const pidEl = $("payment-id");
    if (pidEl) pidEl.textContent = p.payment_id;

    // QR skeleton on while loading
    const skel = $("qr-skel");
    const dl   = $("qr-deeplink");
    if (skel) skel.hidden = false;
    qrImg.hidden = true;
    if (dl) dl.hidden = true;

    const showLoaded = () => {
      if (skel) skel.hidden = true;
      qrImg.hidden = false;
      if (dl) {
        dl.hidden = false;
        dl.href = `monero:${p.xmr_address}?tx_amount=${p.xmr_amount}`;
      }
    };
    const showError = () => {
      if (skel) {
        skel.innerHTML = '<span class="qr-skel-label warn">QR failed. Copy address manually ↑</span>';
      }
    };
    // Use decode() — reliable across cached/uncached cases. Onload as fallback.
    qrImg.onerror = showError;
    qrImg.src = `/v1/purchase/${encodeURIComponent(p.payment_id)}/qr.svg`;
    qrImg.decode().then(showLoaded).catch(showError);

    setStatusDot("pending");
    statusEl.textContent = "waiting for transaction…";
    expiresAt = new Date(p.expires_at);
  }

  function setStatusDot(state) {
    const dot = $("status-dot");
    if (dot) dot.dataset.state = state;
  }

  function poll(id) {
    if (pollTimer) clearInterval(pollTimer);
    let intervalMs = 5000;
    let tries = 0;
    const tick = async () => {
      tries++;
      try {
        const r = await fetch(`/v1/purchase/${encodeURIComponent(id)}/status`);
        if (!r.ok) return;
        const s = await r.json();
        updateStatus(s);
        if (s.status === "completed" && s.api_key) {
          stopAll();
          revealKey(s.api_key);
        } else if (s.status === "expired") {
          stopAll();
          statusEl.textContent = "Payment expired. Click Buy again to start over.";
        }
      } catch (e) {
        // Network blip — keep polling.
      }
      // Backoff: poll fast first minute, slow after.
      if (tries > 12 && intervalMs < 15000) {
        intervalMs = 15000;
        clearInterval(pollTimer);
        pollTimer = setInterval(tick, intervalMs);
      }
    };
    pollTimer = setInterval(tick, intervalMs);
    tick();
  }

  function updateStatus(s) {
    const confs = s.required_confirmations || 10;
    const label = {
      pending:    "waiting for transaction…",
      confirming: `transaction seen. waiting for ${confs} confirmation${confs === 1 ? "" : "s"}…`,
      ready:      "confirmed. issuing your key…",
      completed:  "done.",
      expired:    "payment expired.",
    }[s.status] || s.status;
    statusEl.textContent = label;
    setStatusDot(s.status);
  }

  function startCountdown(iso) {
    if (countdownTimer) clearInterval(countdownTimer);
    const tick = () => {
      if (!expiresAt) return;
      const left = Math.floor((expiresAt - new Date()) / 1000);
      if (left <= 0) {
        countdownEl.textContent = "Expired.";
        clearInterval(countdownTimer);
        return;
      }
      const m = String(Math.floor(left / 60)).padStart(2, "0");
      const s = String(left % 60).padStart(2, "0");
      countdownEl.textContent = `Expires in ${m}:${s}`;
    };
    countdownTimer = setInterval(tick, 1000);
    tick();
  }

  function stopAll() {
    if (pollTimer) clearInterval(pollTimer);
    if (countdownTimer) clearInterval(countdownTimer);
    pollTimer = null;
    countdownTimer = null;
  }

  function revealKey(key) {
    keyBox.hidden = false;
    keyValue.textContent = key;
    keyBox.scrollIntoView({ behavior: "smooth", block: "start" });
    // Clear the pending-payment stash now that the key has been delivered.
    try {
      localStorage.removeItem("phantom_pending_pid");
      localStorage.removeItem("phantom_pending_bundle");
      localStorage.removeItem("phantom_pending_credit");
    } catch (_) {}
    document.getElementById("resume-banner")?.remove();
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  // ── Populate model table (with tabs + search)
  function tierBadge(tier) {
    if (tier === "tee") {
      return `<span class="tier tier-tee" title="Full TEE: prompt content invisible to Redpill, phantom, and anyone else. Verifiable via /v1/inference-attest.">TEE</span>`;
    }
    if (tier === "proxy") {
      return `<span class="tier tier-proxy" title="Gateway runs in Intel TDX, but the model itself runs on the vendor's normal infrastructure. The vendor (OpenAI, Anthropic, Google, x.ai) SEES YOUR PROMPT CONTENT. Phantom hides only your identity from them.">PROXY</span>`;
    }
    return "";
  }
  function kindLabel(kind) {
    if (kind === "embedding") return ` <span class="muted">[embed]</span>`;
    return "";
  }

  let allModels = [];
  let activeFilter = "tee-chat";
  let searchQuery = "";

  function matchesFilter(m, filter) {
    if (filter === "all") return true;
    if (filter === "embedding") return m.kind === "embedding";
    if (filter === "tee-chat") return m.tier === "tee" && m.kind === "chat";
    if (filter === "proxy-chat") return m.tier === "proxy" && m.kind === "chat";
    return true;
  }

  function matchesSearch(m, q) {
    if (!q) return true;
    const hay = (m.id + " " + (m.description || "") + " " + (m.providers || []).join(" ")).toLowerCase();
    return hay.includes(q);
  }

  function renderTable() {
    const rows = $("model-rows");
    const empty = $("model-empty");
    if (!rows) return;
    const filterPool = allModels.filter(m => matchesFilter(m, activeFilter));
    const filtered = filterPool.filter(m => matchesSearch(m, searchQuery));
    updateResultCount(filtered.length, filterPool.length);
    updateExpandButton(filtered.length);
    if (filtered.length === 0) {
      rows.innerHTML = "";
      if (empty) empty.hidden = false;
      return;
    }
    if (empty) empty.hidden = true;
    rows.innerHTML = filtered
      .sort((a, b) => {
        // TEE before PROXY when "all" tab; otherwise sort by price within tier
        if (a.tier !== b.tier) return a.tier === "tee" ? -1 : 1;
        return a.input_per_m_usd_user - b.input_per_m_usd_user;
      })
      .map(m => `
        <tr>
          <td>${tierBadge(m.tier)}</td>
          <td><code>${escapeHtml(m.id)}</code>${kindLabel(m.kind)}<br><span class="muted model-desc" title="${escapeHtml(m.description || "")}">${escapeHtml(m.description || "")}</span></td>
          <td>${(m.context || 0).toLocaleString()}</td>
          <td>$${m.input_per_m_usd_user.toFixed(2)}</td>
          <td>$${m.output_per_m_usd_user.toFixed(2)}</td>
        </tr>`)
      .join("");
  }

  function updateResultCount(shown, pool) {
    const el = $("model-result-count");
    if (!el) return;
    if (!searchQuery) {
      el.hidden = true;
      return;
    }
    el.hidden = false;
    el.innerHTML = `showing <em>${shown}</em> of ${pool}`;
  }

  // ~9 rows fit in 520px max-height. Show expand button when more.
  const _COLLAPSED_ROW_THRESHOLD = 9;

  function updateExpandButton(shownCount) {
    const btn = $("model-expand-btn");
    const scroll = $("model-table-scroll");
    if (!btn || !scroll) return;
    if (shownCount <= _COLLAPSED_ROW_THRESHOLD) {
      btn.hidden = true;
      scroll.classList.remove("is-expanded");
      btn.textContent = "show all ↓";
      return;
    }
    btn.hidden = false;
    const expanded = scroll.classList.contains("is-expanded");
    btn.textContent = expanded
      ? "collapse ↑"
      : `show all ${shownCount} ↓`;
  }

  function bindExpand() {
    const btn = $("model-expand-btn");
    const scroll = $("model-table-scroll");
    if (!btn || !scroll) return;
    btn.addEventListener("click", () => {
      scroll.classList.toggle("is-expanded");
      const rows = $("model-rows")?.children.length || 0;
      updateExpandButton(rows);
      if (!scroll.classList.contains("is-expanded")) {
        scroll.scrollTop = 0;
      }
    });
  }

  function updateCounts() {
    const sel = (id) => document.getElementById(id);
    const counts = {
      "tee-chat":    allModels.filter(m => m.tier === "tee"   && m.kind === "chat").length,
      "proxy-chat":  allModels.filter(m => m.tier === "proxy" && m.kind === "chat").length,
      "embedding":   allModels.filter(m => m.kind === "embedding").length,
      "all":         allModels.length,
    };
    for (const [k, v] of Object.entries(counts)) {
      const el = sel("count-" + k);
      if (el) el.textContent = String(v);
    }
  }

  function bindTabs() {
    document.querySelectorAll(".model-tab").forEach(btn => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".model-tab").forEach(b => {
          b.classList.remove("is-active");
          b.setAttribute("aria-selected", "false");
        });
        btn.classList.add("is-active");
        btn.setAttribute("aria-selected", "true");
        activeFilter = btn.dataset.filter;
        renderTable();
      });
    });
    const search = $("model-search");
    if (search) {
      search.addEventListener("input", (e) => {
        searchQuery = e.target.value.trim().toLowerCase();
        renderTable();
      });
    }
  }

  fetch("/v1/models")
    .then(r => r.json())
    .then(d => {
      allModels = d.data || [];
      updateCounts();
      bindTabs();
      bindExpand();
      renderTable();
      updateHeroStats();
    })
    .catch(() => {});

  function formatBigNumber(n) {
    if (!n || n < 1) return "0";
    if (n < 1000)    return String(n);
    if (n < 1e6)     return (n / 1e3).toFixed(n < 1e4 ? 1 : 0) + "K";
    if (n < 1e9)     return (n / 1e6).toFixed(n < 1e7 ? 1 : 0) + "M";
    return (n / 1e9).toFixed(1) + "B";
  }

  function updateHeroStats() {
    const teeChat = allModels.filter(m => m.tier === "tee" && m.kind === "chat").length;
    const total   = allModels.length;
    const teeSpan = document.getElementById("hero-stat-tee");
    if (teeSpan && teeChat) teeSpan.textContent = String(teeChat);
    // Best-effort live stats; falls back to model counts only if stats endpoint missing.
    fetch("/v1/stats")
      .then(r => r.ok ? r.json() : null)
      .catch(() => null)
      .then(stats => renderProofStrip(teeChat, total, stats));
  }

  function renderProofStrip(teeChat, total, stats) {
    const proofStrip = document.getElementById("hero-proof-strip");
    if (!proofStrip) return;
    // Counts already in hero-sub — don't duplicate them here. Only show
    // request/token volume once it crosses a floor (anti-social-proof), plus
    // the per-request verification link.
    const parts = [];
    if (stats) {
      if (stats.requests_served >= 100) {
        parts.push(`<span class="proof-stat"><strong>${formatBigNumber(stats.requests_served)}</strong> requests served</span>`);
      }
      if (stats.tokens_processed >= 50_000) {
        if (parts.length) parts.push(`<span class="proof-sep">·</span>`);
        parts.push(`<span class="proof-stat"><strong>${formatBigNumber(stats.tokens_processed)}</strong> tokens processed</span>`);
      }
    }
    if (parts.length) parts.push(`<span class="proof-sep">·</span>`);
    parts.push(`<a class="proof-stat" href="/docs.html#attestation">▸ verify any response →</a>`);
    proofStrip.innerHTML = parts.join("");
  }
})();
