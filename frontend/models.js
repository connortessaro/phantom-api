// Dedicated /models page. Use-case primary nav + hero card + alternatives.
// Customers pick a JOB ("coding agent", "vision", "uncensored"); page shows
// one recommended model big, then 4-6 alternatives below.
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    })[c]);
  }

  // ─── Use-case taxonomy ─────────────────────────────────────────────────────
  // Each entry defines a JOB the customer wants to do. `match` is a predicate
  // run against each /v1/models entry; `pick` is the recommended hero id;
  // `pitch` is the one-line positioning customer reads in the chip header.
  // Ranking: hero first, then `where`-matches sorted by `sort`.
  const USE_CASES = [
    {
      id: "code",
      label: "Coding agent",
      icon: "▸",
      pitch: "Long-horizon coding. Tool use. Refactors across many files.",
      pick: "phantom/kimi-k2.6",
      match: m => m.kind === "chat" && (
        /kimi|coder|glm-5|glm-4\.7|gpt-oss-120b|qwen3\.5-397b/.test(m.id)
      ),
      sort: (a, b) => (b.context || 0) - (a.context || 0),
    },
    {
      id: "reason",
      label: "Reason + math",
      icon: "◆",
      pitch: "Frontier reasoning. Planning, math, complex analysis.",
      pick: "phantom/deepseek-v4-pro",
      match: m => m.kind === "chat" && (
        /deepseek-v|qwen3\.5-397b|glm-5\.1|glm-5(?!\.)/.test(m.id)
      ),
      sort: (a, b) => (b.context || 0) - (a.context || 0),
    },
    {
      id: "vision",
      label: "Vision",
      icon: "◉",
      pitch: "Image input. GUI screenshots. Video frame understanding.",
      pick: "phantom/qwen3-vl-30b-a3b-instruct",
      match: m => m.kind === "chat" && /-vl-/.test(m.id),
      sort: (a, b) => (b.context || 0) - (a.context || 0),
    },
    {
      id: "image",
      label: "Image gen",
      icon: "✦",
      pitch: "Generate images from text. Vendor models, anonymous to you.",
      pick: "stability/stable-diffusion-3-5-large",
      match: m => m.kind === "image",
      sort: (a, b) => a.id.localeCompare(b.id),
    },
    {
      id: "embed",
      label: "Embed",
      icon: "▦",
      pitch: "Vector embeddings. Semantic search, RAG.",
      pick: "phantom/qwen3-embedding-8b",
      match: m => m.kind === "embedding",
      sort: (a, b) => (a.input_per_m_usd_user || 0) - (b.input_per_m_usd_user || 0),
    },
    {
      id: "uncensored",
      label: "Uncensored",
      icon: "○",
      pitch: "No content filter. Adult, edge cases, jailbreak-free.",
      pick: "phantom/uncensored-24b",
      match: m => m.kind === "chat" && /uncensored|venice/i.test(m.id),
      sort: (a, b) => (a.input_per_m_usd_user || 0) - (b.input_per_m_usd_user || 0),
    },
    {
      id: "cheap",
      label: "Cheapest",
      icon: "↓",
      pitch: "Lowest $/M tokens. Simple tasks, dev/test, throwaway calls.",
      pick: "phantom/gpt-oss-20b",
      match: m => m.kind === "chat" && m.tier === "tee",
      sort: (a, b) => totalPerM(a) - totalPerM(b),
      limit: 6,
    },
    {
      id: "frontier",
      label: "Frontier (proxy)",
      icon: "✕",
      pitch: "Closed-weight via TDX gateway. Anonymous to vendor — but vendor sees prompt content.",
      pick: "anthropic/claude-opus-4.7",
      match: m => m.kind === "chat" && m.tier === "proxy",
      sort: (a, b) => totalPerM(b) - totalPerM(a),  // premium first
      limit: 6,
    },
  ];

  function totalPerM(m) {
    return (m.input_per_m_usd_user || 0) + (m.output_per_m_usd_user || 0);
  }

  // ─── Curated one-line pitches ──────────────────────────────────────────────
  // Override upstream descriptions with consistent voice. Unknown models fall
  // back to a truncated upstream description.
  const PITCHES = {
    "phantom/kimi-k2.6":                  "Long-horizon coding agent. 262K context. Strong tool use.",
    "phantom/kimi-k2.5":                  "Prior-gen Kimi. Same shape, cheaper.",
    "phantom/glm-5.1":                    "Premium reasoning + coding. 202K context.",
    "phantom/glm-5":                      "Confidential systems engineering. 202K context.",
    "phantom/glm-4.7":                    "Agentic coding + tool use. 202K context.",
    "phantom/glm-4.7-flash":              "Fast agentic coding. 202K context, low cost.",
    "phantom/gpt-oss-120b":               "Balanced reasoning MoE. Default pick.",
    "phantom/gpt-oss-20b":                "Small fast MoE. Cheap throwaway calls.",
    "phantom/deepseek-v4-pro":            "DeepSeek V4 Pro. New flagship. 800K context.",
    "phantom/deepseek-v3.2":              "DeepSeek V3.2. Open-weight reasoning + code.",
    "phantom/deepseek-chat-v3.1":         "DeepSeek 3.1. Balanced reasoning.",
    "phantom/qwen-2.5-7b-instruct":       "Tiny + fast. Cheapest tier.",
    "phantom/qwen3.5-27b":                "Large Qwen. Big context.",
    "phantom/qwen3.5-397b-a17b":          "397B MoE (17B active). Biggest open-weight in TEE.",
    "phantom/qwen3.5-122b-a10b":          "Sweet spot between 27b and 397b.",
    "phantom/qwen3-32b":                  "Mid-tier Qwen.",
    "phantom/qwen3-30b-a3b-instruct-2507":"Qwen3 30B MoE. Cheap workhorse.",
    "phantom/qwen3-vl-30b-a3b-instruct":  "Vision MoE. Images, GUI, video frames.",
    "phantom/qwen2.5-vl-72b-instruct":    "Premium vision. 72B dense.",
    "phantom/qwen3-coder-next":           "Sparse-MoE 80B coder. Agentic edits.",
    "phantom/qwen3.6-35b-a3b":            "Newer Qwen 3.6 MoE.",
    "phantom/qwen3.6-35b-a3b-uncensored": "Qwen 3.6 uncensored MoE. No content filter.",
    "phantom/uncensored-24b":             "Venice 24B. No refusals, no filter.",
    "phantom/gemma-4-26b-a4b-uncensored": "Gemma 4 uncensored. Google-base, jailbreak-free.",
    "phantom/gemma-4-31b-it":             "Google Gemma 4 31B. Multilingual.",
    "phantom/gemma-3-27b-it":             "Google Gemma 3 27B. Multilingual + vision.",
    "phantom/llama-3.3-70b-instruct":     "Meta Llama 3.3 70B.",
    "phantom/minimax-m2.5":               "MiniMax M2.5. 196K context.",
    "phantom/qwen3-embedding-8b":         "Multilingual semantic search. 32K context.",
    "phantom/all-minilm-l6-v2":           "Tiny + fast. 384-d vectors, 512 ctx.",

    // Proxy frontier
    "anthropic/claude-opus-4.7":          "Claude Opus 4.7. 1M context.",
    "anthropic/claude-sonnet-4.6":        "Sonnet 4.6. Balanced reasoning + code review.",
    "anthropic/claude-haiku-4.5":         "Cheap fast Claude.",
    "openai/gpt-5.4":                     "GPT-5.4. 1M context.",
    "openai/gpt-5.5":                     "GPT-5.5. Latest.",
    "openai/gpt-5":                       "GPT-5 flagship. 400K context.",
    "openai/gpt-5-mini":                  "Cheap GPT-5 variant.",
    "openai/gpt-5-nano":                  "Tiny GPT-5. Dev/test.",
    "google/gemini-3-pro-preview":        "Gemini 3 Pro. 1M context.",
    "google/gemini-2.5-pro":              "Gemini 2.5 Pro.",
    "google/gemini-2.5-flash":            "Fast Gemini.",
    "x-ai/grok-4":                        "Grok 4. 256K context.",
    "x-ai/grok-4.1-fast":                 "Grok 4.1 Fast. 2M context, ultra cheap.",

    // Images
    "stability/stable-diffusion-3-5-large":  "SD 3.5 Large. Photoreal at high fidelity.",
    "stability/stable-diffusion-3-5-medium": "SD 3.5 Medium. Faster, cheaper.",
    "stability/stable-diffusion-ultra":      "SD Ultra. Premium quality.",
    "openai/dall-e-3":                        "DALL-E 3. OpenAI flagship.",
    "recraft/recraft-v3":                     "Recraft v3. Brand-consistent raster.",
    "recraft/recraft-v3-svg":                 "Recraft v3 SVG. Vector logos.",
    "segmind/sd3-turbo":                      "SD3 Turbo. Fastest, cheapest.",
  };

  function pitchFor(m) {
    if (PITCHES[m.id]) return PITCHES[m.id];
    const d = (m.description || "").split(/[.!?]/)[0];
    return d ? (d.length > 100 ? d.slice(0, 97) + "…" : d) : m.id;
  }

  // ─── Token-stretch math ────────────────────────────────────────────────────
  // For chat models, show "$50 buys ~X tokens" using balanced 50/50 input/output.
  // Embeddings price the same as input-only. Image models show flat per-image.
  function tokenStretch(m, dollars = 50) {
    if (m.kind === "image") {
      return null;
    }
    const inP = m.input_per_m_usd_user || 0;
    const outP = m.output_per_m_usd_user || 0;
    if (m.kind === "embedding") {
      if (inP <= 0) return null;
      const tokens = (dollars / inP) * 1e6;
      return `$${dollars} → ${fmtBig(tokens)} tokens`;
    }
    const avg = (inP + outP) / 2;
    if (avg <= 0) return null;
    const tokens = (dollars / avg) * 1e6;
    return `$${dollars} → ~${fmtBig(tokens)} tokens`;
  }
  function fmtBig(n) {
    if (n < 1_000) return n.toFixed(0);
    if (n < 1_000_000) return (n / 1_000).toFixed(n < 10_000 ? 1 : 0) + "k";
    if (n < 1_000_000_000) return (n / 1_000_000).toFixed(n < 10_000_000 ? 1 : 0) + "M";
    return (n / 1_000_000_000).toFixed(1) + "B";
  }

  // ─── Tier badge ────────────────────────────────────────────────────────────
  function tierBadge(tier) {
    if (tier === "tee") {
      return `<span class="tier tier-tee" title="Hardware-attested TEE. Prompt invisible to phantom + vendor. Verify per-request.">TEE</span>`;
    }
    return `<span class="tier tier-proxy" title="TDX gateway hides your identity. Vendor still reads prompt content.">PROXY</span>`;
  }

  // ─── Curl snippet ──────────────────────────────────────────────────────────
  function curlSnippet(m) {
    if (m.kind === "image") {
      return `curl https://api.phantom.codes/v1/images/generations \\
  -H "Authorization: Bearer YOUR_PHANTOM_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"model":"${m.id}","prompt":"A misty cyberpunk alley","n":1,"size":"1024x1024"}'`;
    }
    if (m.kind === "embedding") {
      return `curl https://api.phantom.codes/v1/embeddings \\
  -H "Authorization: Bearer YOUR_PHANTOM_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"model":"${m.id}","input":"text to embed"}'`;
    }
    return `curl https://api.phantom.codes/v1/chat/completions \\
  -H "Authorization: Bearer YOUR_PHANTOM_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"model":"${m.id}","messages":[{"role":"user","content":"Hello"}]}'`;
  }

  // ─── Card renderers ────────────────────────────────────────────────────────
  function renderHero(m, uc) {
    const inP = (m.input_per_m_usd_user ?? 0).toFixed(2);
    const outP = (m.output_per_m_usd_user ?? 0).toFixed(2);
    const ctx = (m.context || 0).toLocaleString();
    const stretch = tokenStretch(m, 50);
    return `
      <article class="model-card model-card-hero" data-tier="${m.tier}" data-kind="${m.kind}">
        <div class="hero-banner">
          <span class="hero-tag">▸ RECOMMENDED FOR ${uc.label.toUpperCase()}</span>
          ${tierBadge(m.tier)}
        </div>
        <h3 class="hero-name"><code>${escapeHtml(m.id)}</code></h3>
        <p class="hero-pitch">${escapeHtml(pitchFor(m))}</p>
        <div class="hero-meta">
          <span class="meta-item">${ctx} ctx</span>
          <span class="meta-sep">·</span>
          <span class="meta-item">$${inP}/M in</span>
          <span class="meta-sep">·</span>
          <span class="meta-item">$${outP}/M out</span>
          ${stretch ? `<span class="meta-sep">·</span><span class="meta-stretch">${stretch}</span>` : ""}
        </div>
        <div class="hero-actions">
          <button type="button" class="btn-copy-curl" data-curl="${escapeHtml(curlSnippet(m))}">
            ▸ copy curl
          </button>
          <a href="/docs.html#quickstart" class="btn-ghost">how to wire it up ↗</a>
          <a href="/#pricing" class="btn-ghost">buy credit ↗</a>
        </div>
      </article>`;
  }

  function renderAlt(m) {
    const inP = (m.input_per_m_usd_user ?? 0).toFixed(2);
    const outP = (m.output_per_m_usd_user ?? 0).toFixed(2);
    const ctx = (m.context || 0).toLocaleString();
    const stretch = tokenStretch(m, 50);
    return `
      <article class="model-card model-card-alt" data-tier="${m.tier}" data-kind="${m.kind}">
        <div class="alt-head">
          ${tierBadge(m.tier)}
          <code class="alt-name">${escapeHtml(m.id)}</code>
        </div>
        <p class="alt-pitch">${escapeHtml(pitchFor(m))}</p>
        <div class="alt-meta">
          ${ctx} ctx · $${inP}/M in · $${outP}/M out
          ${stretch ? `<br><span class="meta-stretch">${stretch}</span>` : ""}
        </div>
        <button type="button" class="btn-copy-curl small" data-curl="${escapeHtml(curlSnippet(m))}">
          ▸ copy curl
        </button>
      </article>`;
  }

  // ─── State + render ────────────────────────────────────────────────────────
  let allModels = [];
  let modelById = new Map();
  let activeUc = "code";

  function pickModelsForUc(uc) {
    const pool = allModels.filter(uc.match);
    pool.sort(uc.sort || ((a, b) => 0));
    const limit = uc.limit || 8;
    return pool.slice(0, limit);
  }

  function renderActive() {
    const uc = USE_CASES.find(u => u.id === activeUc) || USE_CASES[0];
    document.querySelector(".uc-pitch").textContent = uc.pitch;

    const picks = pickModelsForUc(uc);
    let hero = modelById.get(uc.pick);
    // If the curated hero isn't in this filter (unlikely), promote first match.
    if (!hero || !uc.match(hero)) hero = picks[0];
    const alts = picks.filter(m => m.id !== (hero && hero.id));

    const heroSlot = $("hero-slot");
    const altSlot = $("alt-slot");
    if (!heroSlot || !altSlot) return;

    if (!hero) {
      heroSlot.innerHTML = `<p class="muted-center">No models match this category yet.</p>`;
      altSlot.innerHTML = "";
      return;
    }

    heroSlot.innerHTML = renderHero(hero, uc);
    altSlot.innerHTML = alts.length === 0
      ? `<p class="muted-center">No alternatives in this category.</p>`
      : alts.map(renderAlt).join("");
    bindCopyButtons();
  }

  function bindCopyButtons() {
    document.querySelectorAll(".btn-copy-curl").forEach(btn => {
      if (btn.dataset.wired) return;
      btn.dataset.wired = "1";
      btn.addEventListener("click", () => {
        const text = btn.dataset.curl || "";
        navigator.clipboard.writeText(text).then(() => {
          const orig = btn.textContent;
          btn.textContent = "copied ✓";
          btn.classList.add("ok");
          setTimeout(() => {
            btn.textContent = orig;
            btn.classList.remove("ok");
          }, 1500);
        });
      });
    });
  }

  function renderUcBar() {
    const bar = $("usecase-bar");
    if (!bar) return;
    bar.innerHTML = USE_CASES.map((uc, i) => `
      <button type="button" class="uc-chip ${uc.id === activeUc ? "is-active" : ""}"
              data-uc="${uc.id}" role="tab" aria-selected="${uc.id === activeUc}">
        <span class="uc-icon" aria-hidden="true">${uc.icon}</span>${escapeHtml(uc.label)}
      </button>`).join("");
    bar.querySelectorAll(".uc-chip").forEach(btn => {
      btn.addEventListener("click", () => {
        activeUc = btn.dataset.uc;
        bar.querySelectorAll(".uc-chip").forEach(b => {
          b.classList.toggle("is-active", b.dataset.uc === activeUc);
          b.setAttribute("aria-selected", b.dataset.uc === activeUc);
        });
        renderActive();
      });
    });
  }

  // Honor URL hash like /models.html#code to deep-link to a use case.
  function applyHash() {
    const h = (window.location.hash || "").replace(/^#/, "");
    if (h && USE_CASES.some(u => u.id === h)) {
      activeUc = h;
    }
  }
  applyHash();
  window.addEventListener("hashchange", () => {
    applyHash();
    document.querySelectorAll(".uc-chip").forEach(b => {
      b.classList.toggle("is-active", b.dataset.uc === activeUc);
      b.setAttribute("aria-selected", b.dataset.uc === activeUc);
    });
    renderActive();
  });

  fetch("/v1/models")
    .then(r => r.json())
    .then(d => {
      allModels = d.data || [];
      modelById = new Map(allModels.map(m => [m.id, m]));
      const total = $("models-total");
      if (total) total.textContent = String(allModels.length);
      renderUcBar();
      renderActive();
    })
    .catch(() => {
      const tagline = $("models-tagline");
      if (tagline) tagline.textContent = "catalog temporarily offline. try again in a minute.";
      $("hero-slot").innerHTML = `<p class="muted-center">Catalog unreachable. <a href="/health">check status</a>.</p>`;
    });
})();
