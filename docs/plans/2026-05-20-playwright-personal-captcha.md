# Playwright Personal Captcha Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a `playwright_personal` captcha mode that reuses a persistent, visible Playwright browser profile for real Flow-page reCAPTCHA tokens.

**Architecture:** Add a small independent service beside the existing `browser` and `personal` services. Wire it into `FlowClient._get_recaptcha_token`, app startup/shutdown, and the management UI. Keep the first version minimal: one persistent context, visible browser, real Flow project URL, enterprise reCAPTCHA execution, and fingerprint capture.

**Tech Stack:** FastAPI, SQLite config, Playwright async API, existing Flow2API captcha abstractions.

---

### Task 1: Create Playwright personal service

**Files:**
- Create: `src/services/playwright_personal_captcha.py`

Implement singleton `PlaywrightPersonalCaptchaService` with `get_token(project_id, action, token_id=None)`, `get_last_fingerprint()`, `open_login_window()`, and `close()`.

### Task 2: Wire FlowClient

**Files:**
- Modify: `src/services/flow_client.py`

Add branch for `captcha_method == "playwright_personal"`. Set request fingerprint from the Playwright page.

### Task 3: Wire startup/shutdown

**Files:**
- Modify: `src/main.py`

Initialize service on startup and open a visible Flow page for manual login.

### Task 4: Wire admin UI

**Files:**
- Modify: `static/manage.html`

Add `playwright_personal` option and show personal/browser proxy settings for it.

### Task 5: Verify

Run compile checks, switch DB captcha method to `playwright_personal`, restart, manually login in opened browser if needed, then call image generation.
