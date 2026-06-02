# blazecrawl/core/browser.py
"""Headless browser manager (Playwright + Chromium).

The :class:`BrowserManager` is a singleton — there's one Chromium process per
application, with new contexts spun up per fetch.  Pages auto-scroll to
trigger lazy-loaded content and ``image``/``font``/``media`` resources are
blocked by default for speed.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Optional

from config import settings
from services.proxy import proxy_manager
from utils.helpers import next_user_agent

logger = logging.getLogger(__name__)

# Imported lazily inside _ensure_started so that ``import blazecrawl`` works
# even when playwright isn't installed yet (helpful for unit-testing tooling).
_playwright_async_api: Any = None


def _import_playwright() -> Any:
    """Import and cache the playwright async API module."""
    global _playwright_async_api
    if _playwright_async_api is None:
        from playwright import async_api as _pw_async  # noqa: WPS433 - lazy import
        _playwright_async_api = _pw_async
    return _playwright_async_api


class BrowserManager:
    """Singleton wrapper around a Playwright Chromium browser."""

    _instance: Optional["BrowserManager"] = None

    def __init__(self) -> None:
        self._playwright: Any = None
        self._browser: Any = None
        self._start_lock = asyncio.Lock()
        self._started = False

    # -------------------------------------------------- singleton accessor
    @classmethod
    def instance(cls) -> "BrowserManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------ lifecycle
    @property
    def is_ready(self) -> bool:
        return self._started and self._browser is not None

    async def start(self) -> None:
        """Launch Chromium (idempotent)."""
        if self._started:
            return
        async with self._start_lock:
            if self._started:
                return
            try:
                pw = _import_playwright()
                self._playwright = await pw.async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=settings.BROWSER_HEADLESS,
                    args=[
                        # Linux containers
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        # Hide the "Chrome is being controlled by automation" banner
                        # AND the CDP-injected `navigator.webdriver=true`.
                        "--disable-blink-features=AutomationControlled",
                        # Reduce fingerprint surface
                        "--disable-features=IsolateOrigins,site-per-process",
                        "--disable-site-isolation-trials",
                        "--disable-infobars",
                        "--no-default-browser-check",
                        "--no-first-run",
                        "--disable-extensions",
                        "--disable-popup-blocking",
                        "--disable-translate",
                        "--disable-background-timer-throttling",
                        "--disable-renderer-backgrounding",
                        "--disable-backgrounding-occluded-windows",
                    ],
                )
                self._started = True
                logger.info("Playwright Chromium launched (headless=%s)",
                            settings.BROWSER_HEADLESS)
            except Exception as exc:
                logger.error("failed to launch Playwright: %s", exc)
                await self._safe_cleanup()
                raise

    async def stop(self) -> None:
        """Close browser and stop Playwright (idempotent)."""
        async with self._start_lock:
            await self._safe_cleanup()
            self._started = False

    async def _safe_cleanup(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception as exc:  # pragma: no cover
            logger.debug("error closing browser: %s", exc)
        finally:
            self._browser = None
        try:
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception as exc:  # pragma: no cover
            logger.debug("error stopping playwright: %s", exc)
        finally:
            self._playwright = None

    # ------------------------------------------------------------ contexts
    @asynccontextmanager
    async def new_context(
        self,
        *,
        user_agent: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        proxy: Optional[str] = None,
    ) -> AsyncIterator[Any]:
        """Yield a fresh Playwright ``BrowserContext`` and close it after use."""
        await self.start()
        assert self._browser is not None  # for type-checkers
        ctx_kwargs: Dict[str, Any] = {
            "user_agent": user_agent or next_user_agent(),
            "ignore_https_errors": True,
            "java_script_enabled": True,
            "bypass_csp": True,
            # Realistic-looking browser context — many anti-bot scripts inspect
            # these and bail on the defaults (e.g. tiny viewport, no locale).
            "viewport": {"width": 1920, "height": 1080},
            "screen": {"width": 1920, "height": 1080},
            "device_scale_factor": 1,
            "locale": "en-US",
            "timezone_id": "America/New_York",
            "color_scheme": "light",
        }
        if extra_headers:
            ctx_kwargs["extra_http_headers"] = extra_headers
        proxy_url = proxy or await proxy_manager.next_proxy()
        if proxy_url:
            ctx_kwargs["proxy"] = {"server": proxy_url}

        context = await self._browser.new_context(**ctx_kwargs)

        # ----- Stealth init script ---------------------------------------
        # Patches a handful of JS fingerprints that headless Chromium leaks
        # by default. Most anti-bot scripts (Cloudflare BM, DataDome basic,
        # PerimeterX free tier) check at least one of these.
        try:
            await context.add_init_script(_STEALTH_INIT_JS)
        except Exception as exc:  # pragma: no cover (browser-bound)
            logger.debug("could not install stealth init script: %s", exc)

        # Route to block unwanted resource types.
        if settings.BLOCK_RESOURCES:
            blocked = set(settings.BLOCK_RESOURCES)

            async def _route(route: Any) -> None:  # pragma: no cover (browser-bound)
                try:
                    if route.request.resource_type in blocked:
                        await route.abort()
                    else:
                        await route.continue_()
                except Exception:
                    try:
                        await route.continue_()
                    except Exception:
                        pass

            try:
                await context.route("**/*", _route)
            except Exception as exc:
                logger.debug("could not install route handler: %s", exc)

        try:
            yield context
        finally:
            try:
                await context.close()
            except Exception:  # pragma: no cover
                pass

    # --------------------------------------------------------- page helpers
    async def render(
        self,
        url: str,
        *,
        timeout: Optional[int] = None,
        wait_for_selector: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        user_agent: Optional[str] = None,
        proxy: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Render *url* with JS and return ``{html, status, final_url}``.

        Raises any underlying Playwright errors after retry.
        """
        nav_timeout = timeout or settings.NAVIGATION_TIMEOUT
        result: Dict[str, Any] = {"html": "", "status": None, "final_url": url}

        async with self.new_context(
            user_agent=user_agent,
            extra_headers=extra_headers,
            proxy=proxy,
        ) as context:
            page = await context.new_page()
            try:
                response = await page.goto(
                    url,
                    timeout=nav_timeout,
                    wait_until="domcontentloaded",
                )
                if response is not None:
                    result["status"] = response.status
                # try to wait for networkidle (best-effort)
                try:
                    await page.wait_for_load_state(
                        "networkidle", timeout=min(nav_timeout, 10_000)
                    )
                except Exception:
                    logger.debug("networkidle wait timed out for %s", url)
                if wait_for_selector:
                    try:
                        await page.wait_for_selector(
                            wait_for_selector, timeout=min(nav_timeout, 10_000)
                        )
                    except Exception:
                        logger.debug(
                            "wait_for_selector %s timed out for %s",
                            wait_for_selector, url,
                        )
                if settings.AUTO_SCROLL:
                    await _auto_scroll(page)
                # final settling
                try:
                    await page.wait_for_load_state(
                        "networkidle", timeout=3_000
                    )
                except Exception:
                    pass
                result["html"] = await page.content()
                result["final_url"] = page.url
            finally:
                try:
                    await page.close()
                except Exception:
                    pass
        return result


async def _auto_scroll(page: Any) -> None:
    """Scroll the page in steps to trigger lazy-loaded content."""
    step_px = settings.AUTO_SCROLL_STEP_PX
    max_steps = settings.AUTO_SCROLL_MAX_STEPS
    try:
        await page.evaluate(
            """
            async ({stepPx, maxSteps}) => {
                await new Promise((resolve) => {
                    let total = 0;
                    let steps = 0;
                    const distance = stepPx;
                    const timer = setInterval(() => {
                        const sh = document.body.scrollHeight;
                        window.scrollBy(0, distance);
                        total += distance;
                        steps += 1;
                        if (total >= sh || steps >= maxSteps) {
                            clearInterval(timer);
                            resolve(null);
                        }
                    }, 120);
                });
            }
            """,
            {"stepPx": step_px, "maxSteps": max_steps},
        )
        # Give lazy listeners a moment.
        await asyncio.sleep(0.5)
        # Scroll back to top so screenshots etc. behave consistently.
        await page.evaluate("window.scrollTo(0, 0);")
    except Exception as exc:  # pragma: no cover (browser-bound)
        logger.debug("auto-scroll failed: %s", exc)


# Process-global accessor.
browser_manager = BrowserManager.instance()


# ---------------------------------------------------------------------------
# Stealth script — runs in every new page BEFORE any site JS executes.
#
# Patches the most common headless-Chromium tells:
#   - navigator.webdriver           -> undefined  (instead of true)
#   - navigator.plugins              -> length 5  (instead of 0)
#   - navigator.languages            -> ["en-US","en"]
#   - navigator.permissions.query    -> sane Notification result
#   - window.chrome                  -> stub object so `if (window.chrome)` passes
#   - WebGL UNMASKED_VENDOR/RENDERER -> Intel / Intel Iris (not "Brian Paul"/"Mesa")
#
# Tradeoff: this isn't a full Puppeteer-Extra-Stealth port — sites using
# commercial fingerprinting (Akamai, PerimeterX paid tier, Cloudflare Turnstile)
# can still detect us. But it handles the long tail of "checks navigator.webdriver"
# anti-bot scripts, which is what most non-FAANG sites use.
# ---------------------------------------------------------------------------
_STEALTH_INIT_JS = r"""
(() => {
  // ---- navigator.webdriver -----------------------------------------
  try {
    Object.defineProperty(Navigator.prototype, 'webdriver', {
      get: () => undefined,
      configurable: true,
    });
  } catch (e) {}

  // ---- navigator.plugins (must be non-empty) -----------------------
  try {
    const fakePlugin = (name, filename, desc) => {
      const p = Object.create(Plugin.prototype);
      Object.defineProperties(p, {
        name:        { value: name },
        filename:    { value: filename },
        description: { value: desc },
        length:      { value: 1 },
      });
      return p;
    };
    const plugins = [
      fakePlugin('PDF Viewer',           'internal-pdf-viewer', 'Portable Document Format'),
      fakePlugin('Chrome PDF Viewer',    'internal-pdf-viewer', 'Portable Document Format'),
      fakePlugin('Chromium PDF Viewer',  'internal-pdf-viewer', 'Portable Document Format'),
      fakePlugin('Microsoft Edge PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
      fakePlugin('WebKit built-in PDF',  'internal-pdf-viewer', 'Portable Document Format'),
    ];
    const arr = Object.create(PluginArray.prototype);
    plugins.forEach((p, i) => { arr[i] = p; arr[p.name] = p; });
    Object.defineProperty(arr, 'length', { value: plugins.length });
    Object.defineProperty(Navigator.prototype, 'plugins', {
      get: () => arr,
      configurable: true,
    });
  } catch (e) {}

  // ---- navigator.languages -----------------------------------------
  try {
    Object.defineProperty(Navigator.prototype, 'languages', {
      get: () => ['en-US', 'en'],
      configurable: true,
    });
  } catch (e) {}

  // ---- navigator.permissions.query (Notification denial leak) ------
  try {
    const origQuery = navigator.permissions && navigator.permissions.query;
    if (origQuery) {
      navigator.permissions.query = (params) => (
        params && params.name === 'notifications'
          ? Promise.resolve({ state: Notification.permission, onchange: null })
          : origQuery.call(navigator.permissions, params)
      );
    }
  } catch (e) {}

  // ---- window.chrome stub ------------------------------------------
  try {
    if (!window.chrome) {
      window.chrome = { runtime: {}, app: {}, csi: () => {}, loadTimes: () => {} };
    } else if (!window.chrome.runtime) {
      window.chrome.runtime = {};
    }
  } catch (e) {}

  // ---- WebGL vendor / renderer -------------------------------------
  try {
    const proto = WebGLRenderingContext.prototype;
    const origGetParameter = proto.getParameter;
    proto.getParameter = function (parameter) {
      // 37445 = UNMASKED_VENDOR_WEBGL, 37446 = UNMASKED_RENDERER_WEBGL
      if (parameter === 37445) return 'Intel Inc.';
      if (parameter === 37446) return 'Intel Iris OpenGL Engine';
      return origGetParameter.call(this, parameter);
    };
  } catch (e) {}

  // ---- Headless tells ----------------------------------------------
  try {
    Object.defineProperty(Navigator.prototype, 'hardwareConcurrency', {
      get: () => 8,
      configurable: true,
    });
    Object.defineProperty(Navigator.prototype, 'deviceMemory', {
      get: () => 8,
      configurable: true,
    });
  } catch (e) {}
})();
"""
