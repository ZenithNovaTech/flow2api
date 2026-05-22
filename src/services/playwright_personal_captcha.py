"""Persistent Playwright based personal reCAPTCHA solver.

This mode is intentionally separate from the existing `browser` mode:
- `browser` creates controlled pages that inject reCAPTCHA into a minimal page.
- `playwright_personal` keeps a visible, persistent browser profile on the real
  Flow origin so the operator can log in manually and reuse that environment.
"""

import asyncio
import os
from pathlib import Path
from typing import Any, Dict, Optional

from ..core.config import config
from ..core.logger import debug_logger

try:
    from playwright.async_api import BrowserContext, Page, async_playwright
except Exception:  # pragma: no cover - dependency errors are reported at runtime
    BrowserContext = Any  # type: ignore
    Page = Any  # type: ignore
    async_playwright = None  # type: ignore


class PlaywrightPersonalCaptchaService:
    """One persistent visible Playwright profile for Flow reCAPTCHA tokens."""

    _instance: Optional["PlaywrightPersonalCaptchaService"] = None
    _lock = asyncio.Lock()

    def __init__(self, db=None):
        self.db = db
        self._playwright = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._runtime_lock = asyncio.Lock()
        self._solve_lock = asyncio.Lock()
        self._last_fingerprint: Optional[Dict[str, Any]] = None
        self.website_key = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
        self.user_data_dir = Path(
            os.environ.get("PLAYWRIGHT_PERSONAL_USER_DATA_DIR", "data/playwright-personal-profile")
        ).resolve()

    @classmethod
    async def get_instance(cls, db=None) -> "PlaywrightPersonalCaptchaService":
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(db=db)
        elif db is not None and cls._instance.db is None:
            cls._instance.db = db
        return cls._instance

    def _check_available(self):
        if async_playwright is None:
            raise RuntimeError(
                "playwright 未安装或不可用。请运行: pip install playwright && python -m playwright install chromium"
            )

    async def _resolve_proxy(self, token_id: Optional[int] = None) -> Optional[Dict[str, str]]:
        raw_proxy_url = None
        try:
            if token_id and self.db:
                token = await self.db.get_token(token_id)
                if token and token.captcha_proxy_url and token.captcha_proxy_url.strip():
                    raw_proxy_url = token.captcha_proxy_url.strip()
            if not raw_proxy_url and self.db:
                captcha_config = await self.db.get_captcha_config()
                if captcha_config.browser_proxy_enabled and captcha_config.browser_proxy_url:
                    raw_proxy_url = captcha_config.browser_proxy_url.strip()
        except Exception as exc:
            debug_logger.log_warning(f"[PlaywrightPersonal] 读取代理配置失败: {exc}")

        self._last_fingerprint = {"proxy_url": raw_proxy_url} if raw_proxy_url else None
        if not raw_proxy_url:
            return None

        # Playwright accepts http://, https:// and socks5:// in `server`.
        return {"server": raw_proxy_url}

    async def _ensure_context(self, token_id: Optional[int] = None) -> BrowserContext:
        self._check_available()
        async with self._runtime_lock:
            if self._context:
                return self._context

            self.user_data_dir.mkdir(parents=True, exist_ok=True)
            self._playwright = await async_playwright().start()
            proxy = await self._resolve_proxy(token_id)
            executable_path = os.environ.get("BROWSER_EXECUTABLE_PATH", "").strip() or None
            headless = os.environ.get("PLAYWRIGHT_PERSONAL_HEADLESS", "").strip().lower() in {"1", "true", "yes"}
            args = [
                "--disable-blink-features=AutomationControlled",
                "--disable-quic",
                "--disable-features=UseDnsHttpsSvcb",
                "--no-first-run",
                "--no-default-browser-check",
                "--start-maximized",
            ]
            launch_kwargs: Dict[str, Any] = {
                "user_data_dir": str(self.user_data_dir),
                "headless": headless,
                "viewport": None,
                "locale": "en-US",
                "args": args,
                "proxy": proxy,
            }
            if executable_path:
                launch_kwargs["executable_path"] = executable_path
            else:
                # Prefer the user's installed Chrome because direct web usage works there.
                launch_kwargs["channel"] = os.environ.get("PLAYWRIGHT_PERSONAL_CHANNEL", "chrome")

            try:
                self._context = await self._playwright.chromium.launch_persistent_context(**launch_kwargs)
            except Exception as first_error:
                if "channel" in launch_kwargs and not executable_path:
                    debug_logger.log_warning(
                        f"[PlaywrightPersonal] 使用 channel=chrome 启动失败，回退 bundled chromium: {first_error}"
                    )
                    launch_kwargs.pop("channel", None)
                    self._context = await self._playwright.chromium.launch_persistent_context(**launch_kwargs)
                else:
                    raise

            await self._context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            debug_logger.log_info(
                f"[PlaywrightPersonal] 持久化浏览器已启动 profile={self.user_data_dir}"
            )
            return self._context

    async def _get_page(self, project_id: Optional[str] = None, token_id: Optional[int] = None) -> Page:
        context = await self._ensure_context(token_id)
        if self._page and not self._page.is_closed():
            return self._page
        if context.pages:
            self._page = context.pages[0]
        else:
            self._page = await context.new_page()
        await self._navigate_to_flow(project_id)
        return self._page

    async def _navigate_to_flow(self, project_id: Optional[str] = None):
        if not self._page or self._page.is_closed():
            return
        target_url = "https://labs.google/fx/tools/flow"
        if project_id:
            target_url = f"{target_url}/project/{project_id}"
        current = self._page.url or ""
        if current.startswith("https://labs.google/") and (not project_id or project_id in current):
            return
        try:
            await self._page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            debug_logger.log_warning(f"[PlaywrightPersonal] 打开 Flow 页面失败: {type(exc).__name__}: {exc}")

    async def _capture_fingerprint(self, page: Page):
        try:
            fp = await page.evaluate(
                """async () => {
                    const uaData = navigator.userAgentData;
                    let high = {};
                    try {
                        high = uaData && uaData.getHighEntropyValues
                            ? await uaData.getHighEntropyValues(['platform', 'platformVersion', 'architecture', 'model', 'uaFullVersion', 'fullVersionList'])
                            : {};
                    } catch (e) {}
                    return {
                        user_agent: navigator.userAgent || '',
                        accept_language: navigator.language || 'en-US',
                        sec_ch_ua: uaData && uaData.brands ? uaData.brands.map(b => `"${b.brand}";v="${b.version}"`).join(', ') : '',
                        sec_ch_ua_mobile: uaData ? (uaData.mobile ? '?1' : '?0') : '',
                        sec_ch_ua_platform: high.platform ? `"${high.platform}"` : ''
                    };
                }"""
            )
            if isinstance(fp, dict):
                previous_proxy = self._last_fingerprint.get("proxy_url") if isinstance(self._last_fingerprint, dict) else None
                if previous_proxy:
                    fp["proxy_url"] = previous_proxy
                self._last_fingerprint = fp
        except Exception as exc:
            debug_logger.log_warning(f"[PlaywrightPersonal] 采集指纹失败: {exc}")

    async def _ensure_recaptcha_ready(self, page: Page):
        ready_expr = "typeof grecaptcha !== 'undefined' && grecaptcha.enterprise && typeof grecaptcha.enterprise.execute === 'function'"
        try:
            await page.wait_for_function(ready_expr, timeout=8000)
            return
        except Exception:
            pass

        script_url = f"https://www.google.com/recaptcha/enterprise.js?render={self.website_key}"
        await page.evaluate(
            """(src) => new Promise((resolve, reject) => {
                if (typeof grecaptcha !== 'undefined' && grecaptcha.enterprise) return resolve(true);
                const existing = [...document.scripts].find(s => (s.src || '').includes('/recaptcha/enterprise.js'));
                if (existing) {
                    existing.addEventListener('load', () => resolve(true), {once: true});
                    existing.addEventListener('error', reject, {once: true});
                    return;
                }
                const s = document.createElement('script');
                s.src = src;
                s.async = true;
                s.onload = () => resolve(true);
                s.onerror = reject;
                document.head.appendChild(s);
            })""",
            script_url,
        )
        await page.wait_for_function(ready_expr, timeout=15000)

    async def open_login_window(self):
        """Open the persistent browser so the operator can sign in manually."""
        page = await self._get_page(None)
        await self._navigate_to_flow(None)
        debug_logger.log_info("[PlaywrightPersonal] 已打开 Flow 登录/项目页面，请在浏览器中手动确认登录状态")
        return page

    async def get_token(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        token_id: Optional[int] = None,
    ) -> Optional[str]:
        async with self._solve_lock:
            page = await self._get_page(project_id, token_id)
            await self._navigate_to_flow(project_id)
            await self._capture_fingerprint(page)
            await self._ensure_recaptcha_ready(page)
            token = await asyncio.wait_for(
                page.evaluate(
                    """({siteKey, action}) => new Promise((resolve, reject) => {
                        const timer = setTimeout(() => reject(new Error('recaptcha timeout')), 30000);
                        grecaptcha.enterprise.ready(() => {
                            grecaptcha.enterprise.execute(siteKey, {action})
                                .then(t => { clearTimeout(timer); resolve(t); })
                                .catch(e => { clearTimeout(timer); reject(e); });
                        });
                    })""",
                    {"siteKey": self.website_key, "action": action},
                ),
                timeout=35,
            )
            settle = float(getattr(config, "browser_recaptcha_settle_seconds", 3) or 0)
            if settle > 0:
                await asyncio.sleep(settle)
            debug_logger.log_info(f"[PlaywrightPersonal] 获取 reCAPTCHA token 成功 action={action}")
            return token

    def get_last_fingerprint(self) -> Optional[Dict[str, Any]]:
        return dict(self._last_fingerprint) if isinstance(self._last_fingerprint, dict) else None

    async def report_flow_error(self, project_id: str, error_reason: str, error_message: str = ""):
        debug_logger.log_warning(
            f"[PlaywrightPersonal] Flow error: project_id={project_id}, reason={error_reason}, message={error_message[:200]}"
        )

    async def close(self):
        async with self._runtime_lock:
            if self._context:
                try:
                    await self._context.close()
                except Exception:
                    pass
                self._context = None
                self._page = None
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None
