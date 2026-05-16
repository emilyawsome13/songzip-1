const CLIENT_STORAGE_KEY = "songzip-client-id";
const ACCOUNT_STORAGE_KEY = "songzip-account-key";
const HEARTBEAT_MS = 20000;
const RECONNECT_MS = 2500;
const BRAND_NAME = "SongZip";
const PAYPAL_SDK_SRC =
  "https://www.paypal.com/sdk/js?client-id=BAAzAaVIHSG7fmLdqE0Pt97VUseA2jYJI8F3PaBILFmVMt2-h3OYOzqbsF9yvmsxizuydYQV4LKd1CDyIk&vault=true&intent=subscription";
const SUBSCRIPTION_PLANS = [
  {
    name: "Basic",
    containerId: "paypal-button-container-P-68Y262703G6930321NID6XTQ",
    statusId: "paypalBasicStatus",
    planId: "P-68Y262703G6930321NID6XTQ",
  },
  {
    name: "Plus",
    containerId: "paypal-button-container-P-95499278FS551045NNID6Y2Y",
    statusId: "paypalPlusStatus",
    planId: "P-95499278FS551045NNID6Y2Y",
  },
  {
    name: "Pro",
    containerId: "paypal-button-container-P-3HV972983J415051HNID6Z2A",
    statusId: "paypalProStatus",
    planId: "P-3HV972983J415051HNID6Z2A",
  },
];
const TIER_LABELS = {
  free: "Free",
  basic: "Basic",
  plus: "Plus",
  pro: "Pro",
};
const scriptLoaders = new Map();

// Settings model copy
const CATEGORY_COPY = {
  source: {
    title: "Sources",
    description: "Choose where audio and lyrics come from, plus the lookup rules used during resolution.",
  },
  output: {
    title: "Output",
    description: "Shape filenames, formats, and file-management behavior for completed sessions.",
  },
  download: {
    title: "Download Engine",
    description: "Tune the transfer, pacing, and conversion behavior used by the backend.",
  },
  metadata: {
    title: "Metadata & Library",
    description: "Control playlist numbering, tags, explicit handling, and post-processing behavior.",
  },
  advanced: {
    title: "Advanced",
    description: "Power-user arguments and less common options surfaced directly from the backend model.",
  },
};

const CATEGORY_MAP = {
  source: new Set([
    "audio_providers",
    "lyrics_providers",
    "search_query",
    "cookie_file",
    "proxy",
    "ytm_data",
    "only_verified_results",
    "detect_formats",
    "genius_token",
  ]),
  output: new Set([
    "output",
    "format",
    "bitrate",
    "overwrite",
    "m3u",
    "archive",
    "save_errors",
    "max_filename_length",
    "id3_separator",
  ]),
  download: new Set([
    "threads",
    "preload",
    "sponsor_block",
    "ffmpeg_args",
    "yt_dlp_args",
    "ffmpeg",
    "scan_for_songs",
    "filter_results",
    "restrict",
    "print_errors",
    "fetch_albums",
    "force_update_metadata",
    "load_config",
  ]),
  metadata: new Set([
    "playlist_numbering",
    "playlist_retain_track_cover",
    "album_type",
    "generate_lrc",
    "add_unavailable",
    "skip_explicit",
    "skip_album_art",
    "redownload",
    "create_skip_file",
    "respect_skip_file",
    "sync_without_deleting",
    "sync_remove_lrc",
    "ignore_albums",
  ]),
};

const OPTIONAL_BOOLEAN_KEYS = new Set([
  "skip_explicit",
  "redownload",
  "skip_album_art",
  "create_skip_file",
  "respect_skip_file",
  "sync_remove_lrc",
]);

const LONG_TEXT_KEYS = new Set([
  "search_query",
  "ffmpeg_args",
  "yt_dlp_args",
  "detect_formats",
  "ignore_albums",
  "save_errors",
  "archive",
  "m3u",
  "cookie_file",
  "proxy",
]);

const COMPATIBILITY_COPY = {
  mp3: {
    title: "Universal format ready",
    detail: "MP3 is the safest playback choice across Linux, Windows, macOS, iPhone, iPad, and Android devices.",
  },
  m4a: {
    title: "Strong mobile support",
    detail: "M4A works well on phones and desktops, but MP3 still has the broadest device compatibility.",
  },
  opus: {
    title: "Modern browser friendly",
    detail: "Opus is efficient, but some legacy players and older device workflows prefer MP3.",
  },
  ogg: {
    title: "Open format",
    detail: "OGG is strong on Linux and modern players, but MP3 remains safer for mixed-device delivery.",
  },
  flac: {
    title: "Archive quality",
    detail: "FLAC is best for preservation, not for universal playback on every browser and mobile workflow.",
  },
  wav: {
    title: "Editing format",
    detail: "WAV is large and best for production work. Use MP3 for everyday delivery.",
  },
};

const state = {
  clientId: getOrCreateClientId(),
  accountKey: getOrCreateAccountKey(),
  account: null,
  version: null,
  session: null,
  settings: null,
  optionsModel: null,
  authProviders: null,
  ws: null,
  heartbeat: null,
  reconnectTimer: null,
  installPromptEvent: null,
  pendingBanner: null,
  activePlan: "Basic",
  paypalReady: false,
  renderedPlans: new Set(),
  renderingPlans: new Set(),
  lastJobNoticeKey: null,
  lastUpgradePromptKey: null,
  upgradeModalTimer: null,
  suppressNextSocketCloseBanner: false,
};

const els = {};

// App bootstrap
document.addEventListener("DOMContentLoaded", () => {
  cacheElements();
  bindEvents();
  initRevealAnimations();
  els.sessionValue.textContent = shortId(state.clientId);
  renderAccountIdentity();
  showBanner("info", `Connecting to ${BRAND_NAME}...`);
  handleAuthCallbackResult();
  boot().catch((error) => {
    console.error(error);
    showBanner("error", `Startup failed: ${error.message}`);
  });
});

function initRevealAnimations() {
  const revealTargets = document.querySelectorAll("[data-reveal]");
  if (!revealTargets.length) {
    return;
  }

  if (typeof IntersectionObserver === "undefined") {
    revealTargets.forEach((element) => element.classList.add("is-visible"));
    return;
  }

  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) {
          return;
        }

        entry.target.classList.add("is-visible");
        observer.unobserve(entry.target);
      });
    },
    {
      root: null,
      rootMargin: "0px 0px -8% 0px",
      threshold: 0.18,
    }
  );

  revealTargets.forEach((element) => {
    observer.observe(element);
  });

  window.setTimeout(() => {
    revealTargets.forEach((element) => {
      element.classList.add("is-visible");
      observer.unobserve(element);
    });
  }, 1200);
}

function cacheElements() {
  Object.assign(els, {
    banner: document.getElementById("banner"),
    connectionStatus: document.getElementById("connectionStatus"),
    connectionDot: document.getElementById("connectionDot"),
    versionValue: document.getElementById("versionValue"),
    sessionValue: document.getElementById("sessionValue"),
    compatibilityStatus: document.getElementById("compatibilityStatus"),
    formatCompatibility: document.getElementById("formatCompatibility"),
    refreshStateButton: document.getElementById("refreshStateButton"),
    shareSiteButton: document.getElementById("shareSiteButton"),
    installButton: document.getElementById("installButton"),
    planTabList: document.querySelector("[data-plan-tabs]"),
    planTabs: Array.from(document.querySelectorAll("[data-plan-tab]")),
    planPanels: Array.from(document.querySelectorAll("[data-plan-panel]")),
    queryForm: document.getElementById("queryForm"),
    queryInput: document.getElementById("queryInput"),
    clearQueryButton: document.getElementById("clearQueryButton"),
    startDownloadButton: document.getElementById("startDownloadButton"),
    searchForm: document.getElementById("searchForm"),
    searchInput: document.getElementById("searchInput"),
    searchButton: document.getElementById("searchButton"),
    searchResults: document.getElementById("searchResults"),
    jobStatusPill: document.getElementById("jobStatusPill"),
    statTotal: document.getElementById("statTotal"),
    statActive: document.getElementById("statActive"),
    statCompleted: document.getElementById("statCompleted"),
    statFailed: document.getElementById("statFailed"),
    progressValue: document.getElementById("progressValue"),
    progressBar: document.getElementById("progressBar"),
    metaStatus: document.getElementById("metaStatus"),
    metaStarted: document.getElementById("metaStarted"),
    metaFinished: document.getElementById("metaFinished"),
    metaResolved: document.getElementById("metaResolved"),
    metaOutputRoot: document.getElementById("metaOutputRoot"),
    metaError: document.getElementById("metaError"),
    tierName: document.getElementById("tierName"),
    tierUsage: document.getElementById("tierUsage"),
    upgradeNowButton: document.getElementById("upgradeNowButton"),
    accountKeyValue: document.getElementById("accountKeyValue"),
    accountStatusCopy: document.getElementById("accountStatusCopy"),
    accountKeyInput: document.getElementById("accountKeyInput"),
    copyAccountKeyButton: document.getElementById("copyAccountKeyButton"),
    useAccountKeyButton: document.getElementById("useAccountKeyButton"),
    accountAuthStatus: document.getElementById("accountAuthStatus"),
    accountAuthCopy: document.getElementById("accountAuthCopy"),
    accountEmailInput: document.getElementById("accountEmailInput"),
    accountPasswordInput: document.getElementById("accountPasswordInput"),
    accountRegisterButton: document.getElementById("accountRegisterButton"),
    accountLoginButton: document.getElementById("accountLoginButton"),
    accountLogoutButton: document.getElementById("accountLogoutButton"),
    songList: document.getElementById("songList"),
    downloadsList: document.getElementById("downloadsList"),
    downloadAdvice: document.getElementById("downloadAdvice"),
    bundleLink: document.getElementById("bundleLink"),
    eventsList: document.getElementById("eventsList"),
    settingsForm: document.getElementById("settingsForm"),
    settingsGroups: document.getElementById("settingsGroups"),
    saveSettingsButton: document.getElementById("saveSettingsButton"),
    reloadSettingsButton: document.getElementById("reloadSettingsButton"),
    authProviders: document.getElementById("authProviders"),
    upgradeModal: document.getElementById("upgradeModal"),
    upgradeModalCopy: document.getElementById("upgradeModalCopy"),
    closeUpgradeModal: document.getElementById("closeUpgradeModal"),
    upgradePlanButtons: Array.from(document.querySelectorAll("[data-upgrade-plan]")),
  });

  if (els.closeUpgradeModal) {
    els.closeUpgradeModal.textContent = "x";
  }
}

function bindEvents() {
  bindIfPresent(els.refreshStateButton, "click", () => refreshState(true));
  bindIfPresent(els.shareSiteButton, "click", shareSite);
  bindIfPresent(els.installButton, "click", installApp);
  initPlanSwitcher();
  bindIfPresent(els.clearQueryButton, "click", () => {
    els.queryInput.value = "";
  });
  bindIfPresent(els.queryForm, "submit", submitDownloadQuery);
  bindIfPresent(els.searchForm, "submit", previewSearch);
  bindIfPresent(els.settingsForm, "submit", saveSettings);
  bindIfPresent(els.reloadSettingsButton, "click", reloadSettings);
  bindIfPresent(els.copyAccountKeyButton, "click", copyAccountKey);
  bindIfPresent(els.useAccountKeyButton, "click", applyAccountKey);
  bindIfPresent(els.accountRegisterButton, "click", registerAccount);
  bindIfPresent(els.accountLoginButton, "click", loginAccount);
  bindIfPresent(els.accountLogoutButton, "click", logoutAccount);
  bindIfPresent(els.accountKeyInput, "keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      applyAccountKey();
    }
  });
  bindIfPresent(els.accountPasswordInput, "keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      loginAccount();
    }
  });
  bindIfPresent(els.upgradeNowButton, "click", () => {
    openUpgradePrompt(
      (state.session || {}).subscription?.upgrade_prompt || {
        message: "Upgrade to unlock larger download volume.",
      }
    );
  });
  bindIfPresent(els.closeUpgradeModal, "click", closeUpgradePrompt);
  bindIfPresent(els.upgradeModal, "click", (event) => {
    if (event.target === els.upgradeModal) {
      closeUpgradePrompt();
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && els.upgradeModal && !els.upgradeModal.hidden) {
      closeUpgradePrompt();
    }
  });
  els.upgradePlanButtons?.forEach((button) => {
    button.addEventListener("click", () => {
      closeUpgradePrompt();
      const planName = button.dataset.upgradePlan;
      if (planName) {
        activatePlan(planName, { focusTab: true });
      }
      document.getElementById("plans")?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });

  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    state.installPromptEvent = event;
    if (els.installButton) {
      els.installButton.hidden = false;
    }
  });

  window.addEventListener("appinstalled", () => {
    state.installPromptEvent = null;
    if (els.installButton) {
      els.installButton.hidden = true;
    }
    showBanner("success", `${BRAND_NAME} is installed and ready to open like an app.`);
  });
}

async function boot() {
  await Promise.all([
    loadVersion(),
    loadSessionState(),
    loadAccountMe(),
    loadSettings(),
    loadOptionsModel(),
    loadAuthProviders().catch((error) => {
      console.error(error);
      state.authProviders = [];
    }),
  ]);
  renderAll();
  registerServiceWorker();
  connectSocket();
  initBilling().catch((error) => {
    console.error(error);
  });
}

function initPlanSwitcher() {
  if (!els.planTabs?.length || !els.planPanels?.length) {
    return;
  }

  const initialPlan = els.planTabs.find((tab) => tab.getAttribute("aria-selected") === "true")?.dataset.plan
    || els.planTabs[0].dataset.plan
    || state.activePlan;

  bindIfPresent(els.planTabList, "keydown", handlePlanTabKeydown);
  els.planTabs.forEach((tab) => {
    bindIfPresent(tab, "click", () => {
      activatePlan(tab.dataset.plan, { focusTab: false });
    });
  });

  activatePlan(initialPlan, { focusTab: false });
}

function handlePlanTabKeydown(event) {
  if (!els.planTabs?.length) {
    return;
  }

  const currentIndex = els.planTabs.findIndex((tab) => tab === document.activeElement);
  if (currentIndex < 0) {
    return;
  }

  let nextIndex = null;
  switch (event.key) {
    case "ArrowRight":
    case "ArrowDown":
      nextIndex = (currentIndex + 1) % els.planTabs.length;
      break;
    case "ArrowLeft":
    case "ArrowUp":
      nextIndex = (currentIndex - 1 + els.planTabs.length) % els.planTabs.length;
      break;
    case "Home":
      nextIndex = 0;
      break;
    case "End":
      nextIndex = els.planTabs.length - 1;
      break;
    default:
      break;
  }

  if (nextIndex === null) {
    return;
  }

  event.preventDefault();
  const nextTab = els.planTabs[nextIndex];
  activatePlan(nextTab?.dataset.plan, { focusTab: true });
}

function activatePlan(planName, options = {}) {
  if (!planName || !els.planTabs?.length || !els.planPanels?.length) {
    return;
  }

  state.activePlan = planName;

  els.planTabs.forEach((tab) => {
    const isActive = tab.dataset.plan === planName;
    tab.classList.toggle("is-active", isActive);
    tab.setAttribute("aria-selected", String(isActive));
    tab.tabIndex = isActive ? 0 : -1;

    if (isActive && options.focusTab) {
      tab.focus();
    }
  });

  els.planPanels.forEach((panel) => {
    const isActive = panel.dataset.planPanel === planName;
    panel.hidden = !isActive;
    panel.classList.toggle("is-active", isActive);
  });

  if (state.paypalReady) {
    ensurePlanRendered(planName);
  }
}

// Billing
async function initBilling() {
  try {
    await loadExternalScript(PAYPAL_SDK_SRC, {
      "data-sdk-integration-source": "button-factory",
    });
  } catch (error) {
    SUBSCRIPTION_PLANS.forEach((plan) => {
      const status = document.getElementById(plan.statusId);
      if (status) {
        status.textContent = "PayPal checkout could not load.";
      }
    });
    throw error;
  }

  state.paypalReady = true;
  ensurePlanRendered(state.activePlan || SUBSCRIPTION_PLANS[0].name);
}

function ensurePlanRendered(planName) {
  const plan = SUBSCRIPTION_PLANS.find((item) => item.name === planName);
  if (!plan || state.renderedPlans.has(plan.name) || state.renderingPlans.has(plan.name)) {
    return;
  }

  renderSubscriptionPlan(plan);
}

function renderSubscriptionPlan(plan) {
  const container = document.getElementById(plan.containerId);
  const status = document.getElementById(plan.statusId);
  if (!container || state.renderedPlans.has(plan.name) || state.renderingPlans.has(plan.name)) {
    return;
  }

  state.renderingPlans.add(plan.name);

  if (status) {
    status.hidden = false;
    status.textContent = "Loading secure PayPal checkout...";
  }

  try {
    if (!window.paypal?.Buttons) {
      if (status) {
        status.textContent = "PayPal checkout is unavailable right now.";
      }
      state.renderingPlans.delete(plan.name);
      return;
    }

    const buttons = window.paypal.Buttons({
      style: {
        shape: "rect",
        color: "gold",
        layout: "vertical",
        label: "subscribe",
      },
      createSubscription(data, actions) {
        return actions.subscription.create({
          plan_id: plan.planId,
          custom_id: state.accountKey,
        });
      },
      async onApprove(data) {
        if (status) {
          status.textContent = `${plan.name} subscription approved.`;
        }
        try {
          await activateSubscriptionTier(plan.name, data.subscriptionID);
          showBanner("success", `${plan.name} subscription started: ${data.subscriptionID}`);
        } catch (error) {
          console.error(error);
          showBanner("error", `${plan.name} started in PayPal, but the local tier could not be updated.`);
        }
      },
      onError(error) {
        console.error(error);
        if (status) {
          status.textContent = "PayPal checkout could not start.";
        }
        showBanner("error", `${plan.name} checkout could not start.`);
      },
    });

    if (status) {
      status.textContent = "Secure PayPal subscription checkout.";
    }

    buttons.render(`#${plan.containerId}`).then(() => {
      state.renderingPlans.delete(plan.name);
      state.renderedPlans.add(plan.name);
      if (status) {
        status.hidden = true;
      }
    }).catch((error) => {
      console.error(error);
      state.renderingPlans.delete(plan.name);
      if (status) {
        status.hidden = false;
        status.textContent = "PayPal checkout could not load.";
      }
    });
  } catch (error) {
    state.renderingPlans.delete(plan.name);
    if (status) {
      status.textContent = "PayPal checkout could not load.";
    }
    console.error(error);
  }
}

// Data loading
async function loadVersion() {
  const version = await api("/api/version");
  state.version = version;
  els.versionValue.textContent = version;
}

async function loadSessionState() {
  state.session = await api(withClient("/api/session/state"));
}

async function loadAccountMe() {
  state.account = await api(withClient("/api/account/me"));
}

async function loadSettings() {
  state.settings = await api(withClient("/api/settings"));
}

async function loadOptionsModel() {
  state.optionsModel = await api("/api/options_model");
}

async function loadAuthProviders() {
  state.authProviders = await api(withClient("/api/auth/providers"));
}

async function activateSubscriptionTier(tier, subscriptionId = null) {
  state.session = await api(withClient("/api/subscription/activate"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      tier: String(tier || "").toLowerCase(),
      subscription_id: subscriptionId,
    }),
  });
  renderState();
  closeUpgradePrompt();
}

// Rendering
function renderAll() {
  renderAccountAccess();
  renderAccountIdentity(state.session?.account);
  renderState();
  renderSettings();
  renderCompatibility();
  renderAuthProviders();
}

function renderState() {
  const session = state.session || {};
  const job = session.job || {};
  const stats = session.stats || {};
  const songs = Array.isArray(session.songs) ? session.songs : [];
  const downloads = Array.isArray(session.downloads) ? session.downloads : [];
  const events = Array.isArray(session.events) ? session.events : [];
  const bundle = session.bundle || null;
  const subscription = session.subscription || {};

  const status = job.status || "idle";
  setStatusPill(els.jobStatusPill, status);

  els.statTotal.textContent = formatCount(stats.total);
  els.statActive.textContent = formatCount(stats.active);
  els.statCompleted.textContent = formatCount(stats.completed);
  els.statFailed.textContent = formatCount(stats.failed);
  els.progressValue.textContent = `${formatProgress(stats.progress)}%`;
  els.progressBar.style.width = `${clampNumber(stats.progress, 0, 100)}%`;

  els.metaStatus.textContent = humanizeStatus(status);
  els.metaStarted.textContent = formatTime(job.started_at);
  els.metaFinished.textContent = formatTime(job.finished_at);
  els.metaResolved.textContent = `${formatCount(job.resolved_count)} songs`;
  els.metaOutputRoot.textContent = job.output_root || session.server?.output_root || "Unavailable";
  els.metaError.textContent = job.error || "None";

  renderSongList(songs);
  renderDownloads(downloads, bundle);
  renderEvents(events);
  renderCompatibility();
  renderAccountIdentity(session.account || { key: subscription.account_key });
  renderAccountAccess();
  renderSubscription(subscription);

  const jobNoticeKey = `${status}|${job.started_at || ""}|${job.error || ""}|${job.finished_at || ""}`;
  if (status === "error" && job.error && state.lastJobNoticeKey !== jobNoticeKey) {
    state.lastJobNoticeKey = jobNoticeKey;
    showBanner("error", `Download failed: ${job.error}`);
  } else if (
    (status === "complete" || status === "complete-with-errors") &&
    state.lastJobNoticeKey !== jobNoticeKey
  ) {
    state.lastJobNoticeKey = jobNoticeKey;
    showBanner(
      status === "complete-with-errors" ? "error" : "success",
      status === "complete-with-errors"
        ? `Download finished with issues: ${job.error || "check activity for details"}`
        : "Download finished."
    );
  }

  const upgradePrompt = subscription.upgrade_prompt || (
    subscription.upgrade_required
      ? { message: "Free tier limit reached. Upgrade to keep downloading songs." }
      : null
  );
  const upgradePromptKey = upgradePrompt
    ? `${subscription.tier || "free"}|${subscription.downloads_used || 0}|${upgradePrompt.held || 0}|${upgradePrompt.allowed || 0}`
    : null;
  if (subscription.upgrade_required && upgradePrompt && state.lastUpgradePromptKey !== upgradePromptKey) {
    state.lastUpgradePromptKey = upgradePromptKey;
    openUpgradePrompt(upgradePrompt);
  }
}

function renderSubscription(subscription) {
  const tier = String(subscription?.tier || "free").toLowerCase();
  const tierLabel = TIER_LABELS[tier] || titleizeKey(tier);
  const limit = Number(subscription?.limit);
  const used = Number(subscription?.downloads_used || 0);

  if (els.tierName) {
    els.tierName.textContent = tierLabel;
  }

  if (els.tierUsage) {
    if (Number.isFinite(limit) && limit > 0) {
      els.tierUsage.textContent = `${used} / ${limit} songs`;
    } else {
      els.tierUsage.textContent = "Paid tier active";
    }
  }

  if (els.upgradeNowButton) {
    els.upgradeNowButton.hidden = tier !== "free";
  }
}

function renderAccountIdentity(account = null) {
  const key = normalizeAccountKey(account?.key || state.accountKey);
  if (key && key !== state.accountKey) {
    state.accountKey = key;
    window.localStorage.setItem(ACCOUNT_STORAGE_KEY, key);
  }

  if (els.accountKeyValue) {
    els.accountKeyValue.textContent = state.accountKey || "Not ready";
  }

  if (els.accountKeyInput && document.activeElement !== els.accountKeyInput) {
    els.accountKeyInput.value = state.accountKey || "";
  }

  const authenticated = Boolean(state.account?.authenticated || account?.authenticated);
  if (els.accountStatusCopy) {
    els.accountStatusCopy.textContent = authenticated
      ? `Signed in${state.account?.account?.email ? ` as ${state.account.account.email}` : ""}. Your tier follows this account automatically.`
      : "Use this same key on another device to restore the same paid tier and usage state.";
  }
}

function renderAccountAccess() {
  const authenticated = Boolean(state.account?.authenticated);
  const email = state.account?.account?.email || "";

  if (els.accountAuthStatus) {
    els.accountAuthStatus.textContent = authenticated ? "Signed In" : "Guest";
    els.accountAuthStatus.className = "status-pill";
    els.accountAuthStatus.classList.add(authenticated ? "connected" : "idle");
  }

  if (els.accountAuthCopy) {
    els.accountAuthCopy.textContent = authenticated
      ? `Signed in as ${email}. This account now owns the tier and can restore it on other devices.`
      : "Create a SongZip account to carry your tier and billing state across devices without pasting a key every time.";
  }

  if (els.accountEmailInput) {
    els.accountEmailInput.disabled = authenticated;
    if (!authenticated && !els.accountEmailInput.value && email) {
      els.accountEmailInput.value = email;
    }
  }

  if (els.accountPasswordInput) {
    els.accountPasswordInput.disabled = authenticated;
    if (authenticated) {
      els.accountPasswordInput.value = "";
    }
  }

  if (els.accountRegisterButton) {
    els.accountRegisterButton.hidden = authenticated;
  }

  if (els.accountLoginButton) {
    els.accountLoginButton.hidden = authenticated;
  }

  if (els.accountLogoutButton) {
    els.accountLogoutButton.hidden = !authenticated;
  }

  if (els.accountKeyInput) {
    els.accountKeyInput.disabled = authenticated;
  }

  if (els.useAccountKeyButton) {
    els.useAccountKeyButton.hidden = authenticated;
  }
}

function openUpgradePrompt(prompt) {
  if (!els.upgradeModal) {
    return;
  }

  window.clearTimeout(state.upgradeModalTimer);

  if (els.upgradeModalCopy) {
    els.upgradeModalCopy.textContent =
      prompt?.message || "Upgrade to keep downloading more songs.";
  }

  els.upgradeModal.hidden = false;
  document.body.classList.add("modal-open");
  window.requestAnimationFrame(() => {
    els.upgradeModal?.classList.add("is-open");
    els.closeUpgradeModal?.focus();
  });
}

function closeUpgradePrompt() {
  if (els.upgradeModal) {
    els.upgradeModal.classList.remove("is-open");
    document.body.classList.remove("modal-open");
    window.clearTimeout(state.upgradeModalTimer);
    state.upgradeModalTimer = window.setTimeout(() => {
      if (els.upgradeModal) {
        els.upgradeModal.hidden = true;
      }
    }, 180);
  }
}

async function copyAccountKey() {
  try {
    await navigator.clipboard.writeText(state.accountKey);
    showBanner("success", "Account key copied. Use this same key on another device to restore your tier.");
  } catch (error) {
    console.error(error);
    showBanner("error", "Could not copy the account key.");
  }
}

async function applyAccountKey() {
  if (state.account?.authenticated) {
    showBanner("info", "Sign out first if you want to switch to a different guest account key.");
    return;
  }

  const nextKey = normalizeAccountKey(els.accountKeyInput?.value);
  if (!nextKey) {
    showBanner("error", "Enter a valid account key first.");
    return;
  }

  if (nextKey === state.accountKey) {
    showBanner("success", "This device is already using that account key.");
    return;
  }

  state.accountKey = nextKey;
  state.lastUpgradePromptKey = null;
  window.localStorage.setItem(ACCOUNT_STORAGE_KEY, nextKey);
  renderAccountIdentity();

  try {
    await Promise.all([loadSessionState(), loadAuthProviders()]);
    renderState();
    renderAuthProviders();
    if (state.ws) {
      state.suppressNextSocketCloseBanner = true;
      state.ws.close();
    } else {
      connectSocket();
    }
    showBanner("success", "Account key switched. This device will now use that shared SongZip tier.");
  } catch (error) {
    console.error(error);
    showBanner("error", `Could not switch account key: ${error.message}`);
  }
}

function collectAccountCredentials() {
  return {
    email: String(els.accountEmailInput?.value || "").trim(),
    password: String(els.accountPasswordInput?.value || ""),
  };
}

async function registerAccount() {
  const credentials = collectAccountCredentials();
  if (!credentials.email || !credentials.password) {
    showBanner("error", "Enter an email and password first.");
    return;
  }

  try {
    setBusy(els.accountRegisterButton, true);
    const response = await api(withClient("/api/account/register"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(credentials),
    });
    await applyAuthenticatedAccountResponse(response, "Account created and signed in.");
  } catch (error) {
    console.error(error);
    showBanner("error", `Could not create the account: ${error.message}`);
  } finally {
    setBusy(els.accountRegisterButton, false);
  }
}

async function loginAccount() {
  const credentials = collectAccountCredentials();
  if (!credentials.email || !credentials.password) {
    showBanner("error", "Enter your email and password first.");
    return;
  }

  try {
    setBusy(els.accountLoginButton, true);
    const response = await api(withClient("/api/account/login"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(credentials),
    });
    await applyAuthenticatedAccountResponse(response, "Signed in successfully.");
  } catch (error) {
    console.error(error);
    showBanner("error", `Could not sign in: ${error.message}`);
  } finally {
    setBusy(els.accountLoginButton, false);
  }
}

async function logoutAccount() {
  try {
    setBusy(els.accountLogoutButton, true);
    await api(withClient("/api/account/logout"), {
      method: "POST",
    });
    state.account = { authenticated: false, account: null, account_key: state.clientId };
    state.accountKey = normalizeAccountKey(state.clientId);
    state.lastUpgradePromptKey = null;
    window.localStorage.setItem(ACCOUNT_STORAGE_KEY, state.accountKey);
    renderAccountIdentity();
    renderAccountAccess();
    await Promise.all([loadSessionState(), loadAuthProviders()]);
    renderState();
    renderAuthProviders();
    if (state.ws) {
      state.suppressNextSocketCloseBanner = true;
      state.ws.close();
    } else {
      connectSocket();
    }
    showBanner("success", "Signed out.");
  } catch (error) {
    console.error(error);
    showBanner("error", `Could not sign out: ${error.message}`);
  } finally {
    setBusy(els.accountLogoutButton, false);
  }
}

async function applyAuthenticatedAccountResponse(response, successMessage) {
  const account = response?.account || null;
  const subscription = response?.subscription || null;
  if (!account?.account_key) {
    throw new Error("Account response did not include an account key.");
  }

  state.account = {
    authenticated: true,
    account,
    account_key: account.account_key,
  };
  state.accountKey = normalizeAccountKey(account.account_key);
  state.lastUpgradePromptKey = null;
  window.localStorage.setItem(ACCOUNT_STORAGE_KEY, state.accountKey);
  renderAccountIdentity({ key: state.accountKey, authenticated: true });
  renderAccountAccess();

  if (subscription) {
    state.session = state.session || {};
    state.session.subscription = subscription;
  }

  await Promise.all([loadSessionState(), loadAuthProviders(), loadAccountMe()]);
  renderState();
  renderAuthProviders();
  if (state.ws) {
    state.suppressNextSocketCloseBanner = true;
    state.ws.close();
  } else {
    connectSocket();
  }
  showBanner("success", successMessage);
}

function renderSongList(songs) {
  if (!songs.length) {
    els.songList.innerHTML = `<div class="empty-state">No songs have been queued in this session yet.</div>`;
    return;
  }

  els.songList.innerHTML = songs
    .map((songState) => {
      const song = songState.song || {};
      const cover = song.cover_url ? `<img src="${escapeHtml(song.cover_url)}" alt="">` : "";
      const spotifyLink = song.url
        ? `<a class="button button-secondary" href="${escapeHtml(song.url)}" target="_blank" rel="noreferrer">Open Source</a>`
        : "";
      return `
        <article class="song-card">
          <div class="song-cover">${cover}</div>
          <div>
            <p class="song-title">${escapeHtml(songState.display_name || "Untitled song")}</p>
            <div class="song-subtitle">
              ${escapeHtml(song.album_name || song.list_name || "Preparing metadata")}
            </div>
            <div class="song-progress">
              <div class="mini-track">
                <div class="mini-bar" style="width:${clampNumber(songState.progress, 0, 100)}%"></div>
              </div>
            </div>
            <div class="song-subtitle">
              ${escapeHtml(songState.message || "Queued")} | ${formatProgress(songState.progress)}%
            </div>
          </div>
          <div class="song-aside">
            <span class="song-queue-number">#${formatCount(songState.queue_position)}</span>
            <span class="status-pill ${escapeHtml(songState.status || "queued")}">${escapeHtml(
              humanizeStatus(songState.status || "queued")
            )}</span>
            ${spotifyLink}
          </div>
        </article>
      `;
    })
    .join("");
}

function renderDownloads(downloads, bundle) {
  if (!downloads.length) {
    els.downloadsList.innerHTML =
      `<div class="empty-state">Completed files will appear here after a job finishes.</div>`;
    els.downloadAdvice.textContent =
      "Use Save to Device for single files or the ZIP button for the whole batch.";
  } else {
    els.downloadsList.innerHTML = downloads
      .map((download) => {
        const fileLink = withClient(
          `/api/download/file?file=${encodeURIComponent(download.path || "")}`
        );
        const fileName = getFileNameFromPath(download.path) || `${download.display_name || "download"}.mp3`;
        const source = download.url
          ? `<a class="button button-secondary" href="${escapeHtml(download.url)}" target="_blank" rel="noreferrer">Source</a>`
          : "";
        return `
          <article class="download-card">
            <div>
              <p class="download-title">${escapeHtml(download.display_name || "Downloaded file")}</p>
              <div class="download-meta">${escapeHtml(download.path || "")}</div>
            </div>
            <div class="download-actions">
              <button
                class="button button-primary"
                type="button"
                data-device-download-url="${escapeHtml(fileLink)}"
                data-device-download-name="${escapeHtml(fileName)}"
              >
                Save to Device
              </button>
              ${source}
            </div>
          </article>
        `;
      })
      .join("");
    els.downloadAdvice.textContent =
      "Tap Save to Device for one file, or use the ZIP button for the full batch.";
  }

  if (bundle && bundle.path) {
    els.bundleLink.classList.remove("disabled");
    els.bundleLink.removeAttribute("aria-disabled");
    els.bundleLink.href = withClient("/api/download/bundle");
    els.bundleLink.setAttribute("download", "");
    els.bundleLink.dataset.deviceDownloadUrl = withClient("/api/download/bundle");
    els.bundleLink.dataset.deviceDownloadName = bundle.name || "songzip-bundle.zip";
    els.bundleLink.textContent = `Save ZIP Bundle (${formatCount(bundle.count)} files)`;
  } else {
    els.bundleLink.classList.add("disabled");
    els.bundleLink.setAttribute("aria-disabled", "true");
    els.bundleLink.removeAttribute("download");
    delete els.bundleLink.dataset.deviceDownloadUrl;
    delete els.bundleLink.dataset.deviceDownloadName;
    els.bundleLink.href = "#";
    els.bundleLink.textContent = "ZIP Not Ready";
  }

  attachDeviceDownloadHandlers();
}

function renderEvents(events) {
  if (!events.length) {
    els.eventsList.innerHTML =
      `<div class="empty-state">Session events will appear here once the page connects.</div>`;
    return;
  }

  const latest = [...events].reverse().slice(0, 30);
  els.eventsList.innerHTML = latest
    .map((event) => {
      const details = event.details
        ? `<div class="event-details">${escapeHtml(formatDetails(event.details))}</div>`
        : "";
      return `
        <article class="event-card">
          <div class="event-topline">
            <span class="event-kind">${escapeHtml(event.kind || "system")}</span>
            <span class="event-level ${escapeHtml(event.level || "info")}">${escapeHtml(
              event.level || "info"
            )}</span>
          </div>
          <div class="download-title">${escapeHtml(event.message || "Event")}</div>
          <div class="download-meta">${escapeHtml(formatTime(event.timestamp))}</div>
          ${details}
        </article>
      `;
    })
    .join("");
}

function renderSettings() {
  const optionsModel = state.optionsModel;
  const settings = state.settings;
  if (!optionsModel || !settings) {
    els.settingsGroups.innerHTML = `<div class="empty-state">Settings are still loading.</div>`;
    return;
  }

  const grouped = {
    source: [],
    output: [],
    download: [],
    metadata: [],
    advanced: [],
  };

  Object.keys(optionsModel)
    .sort()
    .forEach((key) => {
      grouped[getCategoryForKey(key)].push(key);
    });

  els.settingsGroups.innerHTML = Object.entries(grouped)
    .filter(([, keys]) => keys.length > 0)
    .map(([category, keys]) => {
      const copy = CATEGORY_COPY[category];
      return `
        <section class="settings-group">
          <h3>${escapeHtml(copy.title)}</h3>
          <p>${escapeHtml(copy.description)}</p>
          <div class="settings-grid">
            ${keys.map((key) => renderSettingField(key, optionsModel[key], settings[key])).join("")}
          </div>
        </section>
      `;
    })
    .join("");

  renderCompatibility();
}

// Settings
function renderSettingField(key, option, currentValue) {
  const help = option.help || "No description available from the backend parser.";
  const label = titleizeKey(key);
  const type = inferFieldType(key, option, currentValue);
  const fullClass = LONG_TEXT_KEYS.has(key) || type === "textarea" ? "setting-field full" : "setting-field";

  if (type === "checkbox") {
    return `
      <div class="${fullClass}">
        <label for="setting-${escapeHtml(key)}">${escapeHtml(label)}</label>
        <label class="setting-checkbox">
          <input
            id="setting-${escapeHtml(key)}"
            type="checkbox"
            data-setting-key="${escapeHtml(key)}"
            data-setting-type="checkbox"
            ${currentValue ? "checked" : ""}
          >
          <span>${escapeHtml(help)}</span>
        </label>
      </div>
    `;
  }

  if (type === "optional-boolean") {
    return `
      <div class="${fullClass}">
        <label for="setting-${escapeHtml(key)}">${escapeHtml(label)}</label>
        <select
          id="setting-${escapeHtml(key)}"
          data-setting-key="${escapeHtml(key)}"
          data-setting-type="optional-boolean"
        >
          <option value="" ${currentValue === null || typeof currentValue === "undefined" ? "selected" : ""}>Auto</option>
          <option value="true" ${currentValue === true ? "selected" : ""}>Enabled</option>
          <option value="false" ${currentValue === false ? "selected" : ""}>Disabled</option>
        </select>
        <div class="field-help">${escapeHtml(help)}</div>
      </div>
    `;
  }

  if (type === "select") {
    const options = (option.choices || [])
      .map((choice) => {
        const selected = String(currentValue) === String(choice) ? "selected" : "";
        return `<option value="${escapeHtml(String(choice))}" ${selected}>${escapeHtml(
          String(choice)
        )}</option>`;
      })
      .join("");
    return `
      <div class="${fullClass}">
        <label for="setting-${escapeHtml(key)}">${escapeHtml(label)}</label>
        <select
          id="setting-${escapeHtml(key)}"
          data-setting-key="${escapeHtml(key)}"
          data-setting-type="select"
        >
          ${options}
        </select>
        <div class="field-help">${escapeHtml(help)}</div>
      </div>
    `;
  }

  if (type === "list" || type === "textarea") {
    const value = Array.isArray(currentValue) ? currentValue.join("\n") : currentValue ?? "";
    return `
      <div class="${fullClass}">
        <label for="setting-${escapeHtml(key)}">${escapeHtml(label)}</label>
        <textarea
          id="setting-${escapeHtml(key)}"
          rows="${type === "list" ? 4 : 3}"
          data-setting-key="${escapeHtml(key)}"
          data-setting-type="${escapeHtml(type)}"
          placeholder="${type === "list" ? "One item per line" : ""}"
        >${escapeHtml(String(value))}</textarea>
        <div class="field-help">${escapeHtml(help)}</div>
      </div>
    `;
  }

  if (type === "number") {
    return `
      <div class="${fullClass}">
        <label for="setting-${escapeHtml(key)}">${escapeHtml(label)}</label>
        <input
          id="setting-${escapeHtml(key)}"
          type="number"
          data-setting-key="${escapeHtml(key)}"
          data-setting-type="number"
          value="${escapeHtml(currentValue ?? "")}"
        >
        <div class="field-help">${escapeHtml(help)}</div>
      </div>
    `;
  }

  return `
    <div class="${fullClass}">
      <label for="setting-${escapeHtml(key)}">${escapeHtml(label)}</label>
      <input
        id="setting-${escapeHtml(key)}"
        type="text"
        data-setting-key="${escapeHtml(key)}"
        data-setting-type="text"
        value="${escapeHtml(currentValue ?? "")}"
      >
      <div class="field-help">${escapeHtml(help)}</div>
    </div>
  `;
}

function renderCompatibility() {
  const settings = state.settings || {};
  const format = String(settings.format || "mp3").toLowerCase();
  const match = COMPATIBILITY_COPY[format] || COMPATIBILITY_COPY.mp3;
  if (els.compatibilityStatus) {
    els.compatibilityStatus.textContent = match.title;
  }
  if (els.formatCompatibility) {
    els.formatCompatibility.textContent = match.detail;
  }
}

function renderAuthProviders() {
  if (!els.authProviders) {
    return;
  }

  if (!Array.isArray(state.authProviders)) {
    els.authProviders.innerHTML = `<div class="empty-state">Checking available account connections...</div>`;
    return;
  }

  if (!state.authProviders.length) {
    els.authProviders.innerHTML = `<div class="empty-state">No account providers are available right now.</div>`;
    return;
  }

  els.authProviders.innerHTML = state.authProviders
    .map((provider) => {
      const connected = Boolean(provider.connected);
      const configured = Boolean(provider.configured);
      const cardClass = [
        "auth-provider-card",
        connected ? "is-connected" : "",
        !configured ? "is-missing" : "",
      ]
        .filter(Boolean)
        .join(" ");
      const statusClass = [
        "auth-provider-status",
        connected ? "is-connected" : "",
        !configured ? "is-missing" : "",
      ]
        .filter(Boolean)
        .join(" ");

      let statusText = "Ready to connect";
      let copy = "Use this account for personal-library access in the dashboard.";
      if (!configured) {
        statusText = "Setup needed";
        copy = "This provider needs app credentials and a registered callback URL before people can connect.";
      } else if (connected) {
        statusText = "Connected";
        copy = provider.account_label
          ? `Connected as ${provider.account_label}.`
          : "Connected to this dashboard session.";
      }

      const pills = [];
      if (provider.account_id) {
        pills.push(`<span class="auth-provider-pill">${escapeHtml(provider.account_id)}</span>`);
      }
      if (provider.connected_at) {
        pills.push(
          `<span class="auth-provider-pill">Linked ${escapeHtml(formatShortDate(provider.connected_at))}</span>`
        );
      }

      const missingText =
        !configured && Array.isArray(provider.setup_missing) && provider.setup_missing.length
          ? `<div class="auth-provider-note">Missing env vars: ${escapeHtml(provider.setup_missing.join(", "))}</div>`
          : "";

      const actions = connected
        ? `
          <button class="button button-secondary" type="button" data-auth-disconnect="${escapeHtml(
            provider.provider
          )}">
            Disconnect
          </button>
        `
        : `
          <button
            class="button button-primary"
            type="button"
            data-auth-connect="${escapeHtml(provider.provider)}"
            ${!configured ? "disabled" : ""}
          >
            Connect
          </button>
        `;

      const helpLink = provider.setup_help_url
        ? `<a class="button button-ghost" href="${escapeHtml(provider.setup_help_url)}" target="_blank" rel="noreferrer">Docs</a>`
        : "";

      return `
        <article class="${cardClass}">
          <div class="auth-provider-head">
            <p class="auth-provider-name">${escapeHtml(provider.label || provider.provider || "Provider")}</p>
            <span class="${statusClass}">${escapeHtml(statusText)}</span>
          </div>
          <div class="auth-provider-copy">${escapeHtml(copy)}</div>
          ${pills.length ? `<div class="auth-provider-meta">${pills.join("")}</div>` : ""}
          ${missingText}
          <div class="auth-provider-actions">
            ${actions}
            ${helpLink}
          </div>
        </article>
      `;
    })
    .join("");

  els.authProviders.querySelectorAll("[data-auth-connect]").forEach((button) => {
    button.addEventListener("click", () => startProviderAuth(button.dataset.authConnect));
  });
  els.authProviders.querySelectorAll("[data-auth-disconnect]").forEach((button) => {
    button.addEventListener("click", () =>
      disconnectProviderAuth(button.dataset.authDisconnect)
    );
  });
}

// Actions
async function refreshState(showMessage = false) {
  try {
    await Promise.all([loadSessionState(), loadAuthProviders(), loadAccountMe()]);
    renderState();
    renderAuthProviders();
    renderAccountAccess();
    if (showMessage) {
      showBanner("success", "Session state refreshed.");
    }
  } catch (error) {
    console.error(error);
    showBanner("error", `Could not refresh state: ${error.message}`);
  }
}

async function startProviderAuth(provider) {
  if (!provider) {
    return;
  }

  try {
    const response = await api(withClient("/api/auth/start"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider }),
    });

    if (!response || !response.auth_url) {
      throw new Error("Provider login URL was not returned.");
    }

    window.location.assign(response.auth_url);
  } catch (error) {
    console.error(error);
    showBanner("error", `Could not start ${titleizeKey(provider)} sign-in: ${error.message}`);
  }
}

async function disconnectProviderAuth(provider) {
  if (!provider) {
    return;
  }

  try {
    await api(withClient("/api/auth/disconnect"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider }),
    });
    await loadAuthProviders();
    renderAuthProviders();
    showBanner("success", `${titleizeKey(provider)} disconnected.`);
  } catch (error) {
    console.error(error);
    showBanner("error", `Could not disconnect ${titleizeKey(provider)}: ${error.message}`);
  }
}

async function reloadSettings() {
  try {
    await Promise.all([loadSettings(), loadOptionsModel()]);
    renderSettings();
    showBanner("success", "Settings reloaded from the backend.");
  } catch (error) {
    console.error(error);
    showBanner("error", `Could not reload settings: ${error.message}`);
  }
}

async function saveSettings(event) {
  event.preventDefault();
  try {
    setBusy(els.saveSettingsButton, true);
    const payload = collectSettingsPayload();
    state.settings = await api(withClient("/api/settings/update"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    renderSettings();
    showBanner("success", "Settings saved.");
  } catch (error) {
    console.error(error);
    showBanner("error", `Could not save settings: ${error.message}`);
  } finally {
    setBusy(els.saveSettingsButton, false);
  }
}

function collectSettingsPayload() {
  const payload = {};
  const fields = els.settingsGroups.querySelectorAll("[data-setting-key]");
  fields.forEach((field) => {
    const key = field.dataset.settingKey;
    const type = field.dataset.settingType;
    if (!key || !type) {
      return;
    }

    if (type === "checkbox") {
      payload[key] = field.checked;
      return;
    }

    if (type === "optional-boolean") {
      payload[key] = field.value === "" ? null : field.value === "true";
      return;
    }

    if (type === "number") {
      payload[key] = field.value === "" ? null : Number(field.value);
      return;
    }

    if (type === "list") {
      payload[key] = field.value
        .split(/\r?\n|,/)
        .map((item) => item.trim())
        .filter(Boolean);
      return;
    }

    payload[key] = field.value;
  });
  return payload;
}

async function submitDownloadQuery(event) {
  event.preventDefault();
  const query = els.queryInput.value.trim();
  if (!query) {
    showBanner("error", "Add at least one URL or search phrase before starting a job.");
    return;
  }

  const subscription = state.session?.subscription || {};
  if (
    String(subscription.tier || "free").toLowerCase() === "free"
    && Number(subscription.remaining) === 0
  ) {
    openUpgradePrompt(
      subscription.upgrade_prompt || {
        message: "Free tier limit reached. Upgrade to keep downloading songs.",
      }
    );
    showBanner("error", "Free tier limit reached. Upgrade to continue.");
    return;
  }

  try {
    setBusy(els.startDownloadButton, true);
    state.session = await api(withClient("/api/download/query"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    });
    renderState();
    showBanner("success", "Queued. Watch progress below.");
  } catch (error) {
    console.error(error);
    showBanner("error", `Could not start the download: ${error.message}`);
  } finally {
    setBusy(els.startDownloadButton, false);
  }
}

async function previewSearch(event) {
  event.preventDefault();
  const query = els.searchInput.value.trim();
  if (!query) {
    showBanner("error", "Enter a search phrase to preview matches.");
    return;
  }

  try {
    setBusy(els.searchButton, true);
    const results = await api(`/api/songs/search?query=${encodeURIComponent(query)}`);
    renderSearchResults(results);
  } catch (error) {
    console.error(error);
    showBanner("error", `Search preview failed: ${error.message}`);
  } finally {
    setBusy(els.searchButton, false);
  }
}

function renderSearchResults(results) {
  if (!Array.isArray(results) || !results.length) {
    els.searchResults.innerHTML = `<div class="empty-state">No matching songs were returned for that search.</div>`;
    return;
  }

  const topResults = results.slice(0, 6);
  els.searchResults.innerHTML = topResults
    .map((song) => {
      const cover = song.cover_url ? `<img src="${escapeHtml(song.cover_url)}" alt="">` : "";
      return `
        <article class="search-card">
          <div class="search-cover">${cover}</div>
          <div>
            <p class="search-title">${escapeHtml(song.artist || "Unknown artist")} - ${escapeHtml(
              song.name || "Untitled track"
            )}</p>
            <div class="search-meta">
              ${escapeHtml(song.album_name || "Unknown album")} | ${escapeHtml(String(song.year || ""))}
            </div>
            <div class="search-actions">
              <button class="button button-primary" type="button" data-fill-query="${escapeHtml(
                song.url || ""
              )}">
                Add
              </button>
              <a class="button button-secondary" href="${escapeHtml(song.url || "#")}" target="_blank" rel="noreferrer">
                Open Source
              </a>
            </div>
          </div>
        </article>
      `;
    })
    .join("");

  els.searchResults.querySelectorAll("[data-fill-query]").forEach((button) => {
    button.addEventListener("click", () => {
      const nextValue = button.dataset.fillQuery || "";
      els.queryInput.value = els.queryInput.value.trim()
        ? `${els.queryInput.value.trim()}\n${nextValue}`
        : nextValue;
      showBanner("success", "Track added to the queue.");
    });
  });
}

// Realtime connection
function connectSocket() {
  clearTimeout(state.reconnectTimer);
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    return;
  }

  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const socketUrl = `${protocol}//${window.location.host}/api/ws?client_id=${encodeURIComponent(
    state.clientId
  )}&account_key=${encodeURIComponent(state.accountKey)}`;

  state.ws = new WebSocket(socketUrl);

  state.ws.addEventListener("open", () => {
    setConnectionStatus("connected");
    if (state.pendingBanner) {
      showBanner(state.pendingBanner.type, state.pendingBanner.message);
      state.pendingBanner = null;
    } else {
      showBanner("success", `${BRAND_NAME} connected. Live updates are on.`);
    }
    if (state.heartbeat) {
      clearInterval(state.heartbeat);
    }
    state.heartbeat = window.setInterval(() => {
      if (state.ws && state.ws.readyState === WebSocket.OPEN) {
        state.ws.send(JSON.stringify({ type: "heartbeat", at: Date.now() }));
      }
    }, HEARTBEAT_MS);
  });

  state.ws.addEventListener("message", (event) => {
    try {
      const payload = JSON.parse(event.data);
      if (payload.type === "state" && payload.state) {
        state.session = payload.state;
        renderState();
      }
    } catch (error) {
      console.error("Could not parse websocket message", error);
    }
  });

  state.ws.addEventListener("close", () => {
    setConnectionStatus("disconnected");
    if (state.suppressNextSocketCloseBanner) {
      state.suppressNextSocketCloseBanner = false;
    } else {
      showBanner("error", `${BRAND_NAME} disconnected. Retrying automatically...`);
    }
    if (state.heartbeat) {
      clearInterval(state.heartbeat);
      state.heartbeat = null;
    }
    state.reconnectTimer = window.setTimeout(connectSocket, RECONNECT_MS);
  });

  state.ws.addEventListener("error", () => {
    setConnectionStatus("disconnected");
  });
}

function setConnectionStatus(status) {
  if (els.connectionStatus) {
    els.connectionStatus.textContent = humanizeStatus(status);
  }
  if (els.connectionDot) {
    els.connectionDot.className = "connection-dot";
    els.connectionDot.classList.add(status);
  }
}

function setStatusPill(element, status) {
  element.textContent = humanizeStatus(status);
  element.className = "status-pill";
  element.classList.add(status);
}

function showBanner(type, message) {
  if (!els.banner) {
    return;
  }
  els.banner.hidden = false;
  els.banner.className = `status-banner ${type}`;
  els.banner.textContent = message;
}

function handleAuthCallbackResult() {
  const url = new URL(window.location.href);
  const provider = url.searchParams.get("auth_provider");
  const status = url.searchParams.get("auth_status");
  const message = url.searchParams.get("auth_message");
  if (!provider || !status || !message) {
    return;
  }

  state.pendingBanner = {
    type: status === "connected" ? "success" : "error",
    message,
  };
  showBanner(state.pendingBanner.type, state.pendingBanner.message);

  url.searchParams.delete("auth_provider");
  url.searchParams.delete("auth_status");
  url.searchParams.delete("auth_message");
  window.history.replaceState({}, document.title, url.toString());
}

// Sharing and downloads
async function shareSite() {
  const shareUrl = window.location.href;
  try {
    if (navigator.share) {
      await navigator.share({
        title: BRAND_NAME,
        text: `Open ${BRAND_NAME}`,
        url: shareUrl,
      });
      return;
    }

    await navigator.clipboard.writeText(shareUrl);
    showBanner("success", "Site link copied to the clipboard.");
  } catch (error) {
    console.error(error);
    showBanner("error", "Could not share the site link.");
  }
}

async function installApp() {
  if (!state.installPromptEvent) {
    showBanner("info", "Install mode is not available in this browser yet.");
    return;
  }

  try {
    await state.installPromptEvent.prompt();
    await state.installPromptEvent.userChoice;
  } catch (error) {
    console.error(error);
  }
}

function attachDeviceDownloadHandlers() {
  document.querySelectorAll("[data-device-download-url]").forEach((element) => {
    if (element.dataset.downloadBound === "true") {
      return;
    }

    element.dataset.downloadBound = "true";
    element.addEventListener("click", async (event) => {
      event.preventDefault();
      const url = element.dataset.deviceDownloadUrl;
      const fileName = element.dataset.deviceDownloadName || "download";
      if (!url) {
        return;
      }

      await saveToCurrentDevice(url, fileName);
    });
  });
}

async function saveToCurrentDevice(url, fileName) {
  const isMobile = /Android|iPhone|iPad|iPod/i.test(navigator.userAgent || "");

  try {
    if (isMobile && navigator.canShare && typeof File !== "undefined") {
      const response = await fetch(url);
      if (!response.ok) {
        throw new Error(`Download failed with ${response.status}`);
      }

      const blob = await response.blob();
      const file = new File([blob], fileName, {
        type: blob.type || "application/octet-stream",
      });

      if (navigator.canShare({ files: [file] })) {
        await navigator.share({
          files: [file],
          title: fileName,
          text: "Save this file to your device",
        });
        showBanner("success", "Opened your phone's save/share sheet.");
        return;
      }
    }

    const link = document.createElement("a");
    link.href = url;
    link.download = fileName;
    link.rel = "noopener";
    document.body.appendChild(link);
    link.click();
    link.remove();
    showBanner("success", "Download started on this device.");
  } catch (error) {
    console.error(error);
    showBanner("error", `Could not download to this device: ${error.message}`);
  }
}

// Platform services
async function registerServiceWorker() {
  if (!("serviceWorker" in navigator)) {
    return;
  }

  try {
    await navigator.serviceWorker.register("/sw.js");
  } catch (error) {
    console.error("Service worker registration failed", error);
  }
}

function loadExternalScript(src, attributes = {}) {
  const scriptKey = JSON.stringify([src, attributes]);
  if (scriptLoaders.has(scriptKey)) {
    return scriptLoaders.get(scriptKey);
  }

  const loader = new Promise((resolve, reject) => {
    const existing = document.querySelector(`script[src="${src}"]`);
    if (existing) {
      if (existing.dataset.loaded === "true") {
        resolve();
        return;
      }

      existing.addEventListener("load", () => resolve(), { once: true });
      existing.addEventListener("error", () => reject(new Error(`Could not load ${src}`)), {
        once: true,
      });
      return;
    }

    const script = document.createElement("script");
    script.src = src;
    script.async = true;
    Object.entries(attributes).forEach(([key, value]) => {
      script.setAttribute(key, value);
    });
    script.addEventListener(
      "load",
      () => {
        script.dataset.loaded = "true";
        resolve();
      },
      { once: true }
    );
    script.addEventListener("error", () => reject(new Error(`Could not load ${src}`)), {
      once: true,
    });
    document.head.appendChild(script);
  });

  scriptLoaders.set(scriptKey, loader);
  return loader;
}

// Utilities
async function api(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const text = await response.text();
    let message = text || `Request failed with ${response.status}`;
    try {
      const json = JSON.parse(text);
      message = json.detail || json.message || message;
    } catch (_error) {
      // Keep the plain text fallback.
    }
    throw new Error(message);
  }

  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return response.json();
  }

  return response.text();
}

function withClient(path) {
  const joiner = path.includes("?") ? "&" : "?";
  return `${path}${joiner}client_id=${encodeURIComponent(state.clientId)}&account_key=${encodeURIComponent(
    state.accountKey
  )}`;
}

function getOrCreateClientId() {
  const existing = window.localStorage.getItem(CLIENT_STORAGE_KEY);
  if (existing) {
    return existing;
  }

  const generated =
    typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `songzip-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  window.localStorage.setItem(CLIENT_STORAGE_KEY, generated);
  return generated;
}

function getOrCreateAccountKey() {
  const url = new URL(window.location.href);
  const fromUrl = normalizeAccountKey(url.searchParams.get("account"));
  if (fromUrl) {
    window.localStorage.setItem(ACCOUNT_STORAGE_KEY, fromUrl);
    return fromUrl;
  }

  const existing = normalizeAccountKey(window.localStorage.getItem(ACCOUNT_STORAGE_KEY));
  if (existing) {
    return existing;
  }

  const generated = normalizeAccountKey(
    `songzip-${Math.random().toString(36).slice(2, 8)}-${Date.now().toString(36)}`
  );
  window.localStorage.setItem(ACCOUNT_STORAGE_KEY, generated);
  return generated;
}

function normalizeAccountKey(value) {
  return String(value || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64);
}

function shortId(value) {
  if (!value) {
    return "Unavailable";
  }
  return value.length > 16 ? `${value.slice(0, 8)}...${value.slice(-6)}` : value;
}

function formatShortDate(value) {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return "recently";
  }

  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
  }).format(parsed);
}

function getCategoryForKey(key) {
  for (const [category, keys] of Object.entries(CATEGORY_MAP)) {
    if (keys.has(key)) {
      return category;
    }
  }
  return "advanced";
}

function inferFieldType(key, option, currentValue) {
  if (OPTIONAL_BOOLEAN_KEYS.has(key)) {
    return "optional-boolean";
  }

  if (option.choices && option.choices.length > 0) {
    return "select";
  }

  if (option.type === "bool" || typeof currentValue === "boolean") {
    return "checkbox";
  }

  if (option.type === "list" || Array.isArray(currentValue)) {
    return "list";
  }

  if (option.type === "int" || typeof currentValue === "number") {
    return "number";
  }

  if (LONG_TEXT_KEYS.has(key)) {
    return "textarea";
  }

  return "text";
}

function titleizeKey(key) {
  return key
    .replace(/_/g, " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

function humanizeStatus(status) {
  return String(status || "idle")
    .replace(/-/g, " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

function formatProgress(value) {
  const numeric = Number(value);
  if (Number.isNaN(numeric)) {
    return "0";
  }
  return numeric.toFixed(numeric % 1 === 0 ? 0 : 1);
}

function formatCount(value) {
  return Number.isFinite(Number(value)) ? String(value) : "0";
}

function formatTime(value) {
  if (!value) {
    return "Not available";
  }

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return String(value);
  }

  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function formatDetails(details) {
  if (typeof details === "string") {
    return details;
  }

  try {
    return JSON.stringify(details, null, 2);
  } catch (_error) {
    return String(details);
  }
}

function clampNumber(value, min, max) {
  const numeric = Number(value);
  if (Number.isNaN(numeric)) {
    return min;
  }
  return Math.min(max, Math.max(min, numeric));
}

function setBusy(element, busy) {
  if (!element) {
    return;
  }
  element.disabled = busy;
}

function getFileNameFromPath(filePath) {
  if (!filePath) {
    return "";
  }

  const normalized = String(filePath).split(/[\\/]/);
  return normalized[normalized.length - 1] || "";
}

function bindIfPresent(element, eventName, handler) {
  if (!element) {
    return;
  }

  element.addEventListener(eventName, handler);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
