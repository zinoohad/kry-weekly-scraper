import base64
import json
import mimetypes
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# =========================
# Environment configuration
# =========================

BASE_URL = os.getenv("KRY_BASE_URL", "https://www.k-ry.org.il").rstrip("/")
LOGIN_URL = os.getenv("KRY_LOGIN_URL", BASE_URL)

USERNAME = os.environ["KRY_USERNAME"]
PASSWORD = os.environ["KRY_PASSWORD"]

DRIVE_FOLDER_ID = os.environ["DRIVE_FOLDER_ID"]
GOOGLE_OAUTH_TOKEN_JSON = os.environ["GOOGLE_OAUTH_TOKEN_JSON"]

STATE_FILENAME = os.getenv("STATE_FILENAME", "kry_seen_items.json")

HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"

# Optional selectors. Prefer leaving these empty unless needed.
OPEN_LOGIN_SELECTOR = os.getenv("KRY_OPEN_LOGIN_SELECTOR", "")
USERNAME_SELECTOR = os.getenv("KRY_USERNAME_SELECTOR", "")
PASSWORD_SELECTOR = os.getenv("KRY_PASSWORD_SELECTOR", "")
LOGIN_BUTTON_SELECTOR = os.getenv("KRY_LOGIN_BUTTON_SELECTOR", "")
ITEM_LINK_SELECTOR = os.getenv("KRY_ITEM_LINK_SELECTOR", 'a[href^="/protocols/"]')
PRINT_LINK_SELECTOR = os.getenv("KRY_PRINT_LINK_SELECTOR", 'a:has-text("גרסת הדפסה")')

DECISIONS_PATH = os.getenv("KRY_DECISIONS_PATH", "/decisions2")
PRINT_TEXT = os.getenv("KRY_PRINT_TEXT", "גרסת הדפסה של הפרוטוקול")


# =========================
# Logging / helpers
# =========================

def log(message: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] {message}", flush=True)


def safe_filename(name: str, fallback: str = "protocol") -> str:
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:180] or fallback


def is_forbidden_page(page) -> bool:
    title = page.title() or ""
    body_text = ""
    try:
        body_text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        pass

    return (
        "403" in title
        or "Forbidden" in title
        or "403" in body_text
        or "Forbidden" in body_text
    )


# =========================
# Google Drive
# =========================

def get_drive_service():
    raw = GOOGLE_OAUTH_TOKEN_JSON.strip()

    try:
        token_data = json.loads(raw)
    except json.JSONDecodeError:
        token_data = json.loads(base64.b64decode(raw).decode("utf-8"))

    creds = Credentials.from_authorized_user_info(
        token_data,
        scopes=["https://www.googleapis.com/auth/drive"],
    )

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())

    return build("drive", "v3", credentials=creds)


def drive_find_file(service, name: str, folder_id: str) -> Optional[Dict[str, Any]]:
    escaped_name = name.replace("'", "\\'")
    query = (
        f"name = '{escaped_name}' "
        f"and '{folder_id}' in parents "
        f"and trashed = false"
    )

    result = service.files().list(
        q=query,
        spaces="drive",
        fields="files(id, name, mimeType, modifiedTime)",
        pageSize=10,
    ).execute()

    files = result.get("files", [])
    return files[0] if files else None


def drive_download_state(service) -> Dict[str, Any]:
    existing = drive_find_file(service, STATE_FILENAME, DRIVE_FOLDER_ID)

    if not existing:
        log("No previous state file found in Drive. Starting fresh.")
        return {"seen": {}}

    request = service.files().get_media(fileId=existing["id"])

    import io
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)

    done = False
    while not done:
        _, done = downloader.next_chunk()

    buffer.seek(0)

    try:
        state = json.loads(buffer.read().decode("utf-8"))
        if "seen" not in state:
            state["seen"] = {}
        return state
    except Exception:
        log("Failed to parse state file. Starting with empty state.")
        return {"seen": {}}


def drive_upload_file(
    service,
    local_path: Path,
    drive_name: Optional[str] = None,
    update_existing: bool = False,
) -> str:
    drive_name = drive_name or local_path.name
    existing = drive_find_file(service, drive_name, DRIVE_FOLDER_ID)

    mime_type = mimetypes.guess_type(str(local_path))[0] or "application/octet-stream"
    media = MediaFileUpload(str(local_path), mimetype=mime_type, resumable=True)

    if existing and update_existing:
        updated = service.files().update(
            fileId=existing["id"],
            media_body=media,
            fields="id",
        ).execute()
        return updated["id"]

    if existing and not update_existing:
        log(f"Drive file already exists. Skipping upload: {drive_name}")
        return existing["id"]

    metadata = {
        "name": drive_name,
        "parents": [DRIVE_FOLDER_ID],
    }

    created = service.files().create(
        body=metadata,
        media_body=media,
        fields="id",
    ).execute()

    return created["id"]


def drive_upload_state(service, state: Dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        temp_path = Path(f.name)

    try:
        drive_upload_file(
            service=service,
            local_path=temp_path,
            drive_name=STATE_FILENAME,
            update_existing=True,
        )
    finally:
        temp_path.unlink(missing_ok=True)


# =========================
# Login
# =========================

def accept_cookie_banner(page) -> None:
    candidates = [
        page.get_by_role("button", name="אשר הכל"),
        page.get_by_text("אשר הכל", exact=True),
        page.locator("#accept"),
        page.locator("button#accept"),
        page.locator('[data-testid="uc-accept-all"]'),
    ]

    for candidate in candidates:
        try:
            if candidate.count() > 0:
                log("Accepting cookie banner")
                candidate.first.click()
                page.wait_for_timeout(1500)
                return
        except Exception:
            pass

    log("Cookie banner not found or already accepted")


def open_real_login_form(page) -> None:
    log("Opening real member login form")

    if OPEN_LOGIN_SELECTOR:
        log(f"Opening login form using env selector: {OPEN_LOGIN_SELECTOR}")
        page.locator(OPEN_LOGIN_SELECTOR).first.click(timeout=15000)
        page.wait_for_timeout(3000)
    else:
        # Critical:
        # "כניסה לחברים" is only the title inside the login form.
        # The real opener is the "התחברות" link in the top navigation.
        candidates = [
            page.get_by_role("link", name="התחברות"),
            page.get_by_role("button", name="התחברות"),
            page.locator("a").filter(has_text="התחברות"),
            page.locator("button").filter(has_text="התחברות"),
            page.locator("[role='button']").filter(has_text="התחברות"),
        ]

        clicked = False

        for candidate in candidates:
            try:
                if candidate.count() > 0:
                    log("Clicking התחברות to open member login form")
                    candidate.first.click(timeout=15000, force=True)
                    clicked = True
                    break
            except Exception as e:
                log(f"Failed opening login with התחברות candidate: {e}")

        if not clicked:
            try:
                page.screenshot(path="login_debug_full_page.png", full_page=True)
                log("Saved screenshot: login_debug_full_page.png")
            except Exception:
                pass

            try:
                with open("login_debug_page.html", "w", encoding="utf-8") as f:
                    f.write(page.content())
                log("Saved HTML: login_debug_page.html")
            except Exception:
                pass

            raise RuntimeError("Could not find/click התחברות link to open login form.")

    page.wait_for_timeout(3000)

    # Validate that the real form opened.
    if page.get_by_text("כניסה לחברים", exact=False).count() > 0:
        log("Login form title 'כניסה לחברים' is visible")
    else:
        log("Warning: login form title 'כניסה לחברים' was not found after clicking התחברות")

    if page.locator("input[type='password']").count() == 0 and page.get_by_placeholder("הסיסמה").count() == 0:
        debug_inputs(page, "after clicking התחברות but before failing")

        try:
            page.screenshot(path="login_debug_full_page.png", full_page=True)
            log("Saved screenshot: login_debug_full_page.png")
        except Exception:
            pass

        try:
            with open("login_debug_page.html", "w", encoding="utf-8") as f:
                f.write(page.content())
            log("Saved HTML: login_debug_page.html")
        except Exception:
            pass

        raise RuntimeError("Clicked התחברות, but password field did not appear.")

def debug_inputs(page, label: str) -> None:
    inputs = page.locator("input")
    log(f"Input count {label}: {inputs.count()}")

    for i in range(inputs.count()):
        try:
            item = inputs.nth(i)
            log(
                "INPUT "
                f"{i}: "
                f"type={item.get_attribute('type')} | "
                f"name={item.get_attribute('name')} | "
                f"id={item.get_attribute('id')} | "
                f"class={item.get_attribute('class')} | "
                f"placeholder={item.get_attribute('placeholder')} | "
                f"aria-label={item.get_attribute('aria-label')} | "
                f"autocomplete={item.get_attribute('autocomplete')}"
            )
        except Exception as e:
            log(f"Failed reading input {i}: {e}")


def login(page) -> None:
    log(f"Opening site: {LOGIN_URL}")
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_load_state("networkidle", timeout=60000)
    page.wait_for_timeout(3000)

    log(f"Initial URL: {page.url}")
    log(f"Initial title: {page.title()}")

    accept_cookie_banner(page)
    open_real_login_form(page)

    log(f"URL after opening login form: {page.url}")
    log(f"Title after opening login form: {page.title()}")

    debug_inputs(page, "after opening real login form")

    # Fill username/email.
    try:
        if USERNAME_SELECTOR:
            log(f"Using username selector from env: {USERNAME_SELECTOR}")
            page.locator(USERNAME_SELECTOR).first.fill(USERNAME)
        else:
            log('Using username placeholder: דוא"ל')
            page.get_by_placeholder('דוא"ל').fill(USERNAME, timeout=15000)
    except Exception as e:
        raise RuntimeError(f"Failed to fill username/email field: {e}")

    # Fill password.
    try:
        if PASSWORD_SELECTOR:
            log(f"Using password selector from env: {PASSWORD_SELECTOR}")
            page.locator(PASSWORD_SELECTOR).first.fill(PASSWORD)
        else:
            log("Using password placeholder: הסיסמה")
            page.get_by_placeholder("הסיסמה").fill(PASSWORD, timeout=15000)
    except Exception as e:
        raise RuntimeError(f"Failed to fill password field: {e}")

    # Submit login.
    try:
        if LOGIN_BUTTON_SELECTOR:
            log(f"Clicking login button using env selector: {LOGIN_BUTTON_SELECTOR}")
            page.locator(LOGIN_BUTTON_SELECTOR).first.click()
        else:
            log("Clicking login button by role/name: כניסה")
            page.get_by_role("button", name="כניסה").click(timeout=15000)
    except Exception as e:
        raise RuntimeError(f"Failed to click login submit button: {e}")

    page.wait_for_load_state("networkidle", timeout=60000)
    page.wait_for_timeout(6000)

    log(f"URL after login submit: {page.url}")
    log(f"Title after login submit: {page.title()}")

    # Check cookies names only. Do not print values.
    try:
        cookie_names = sorted({c.get("name") for c in page.context.cookies() if c.get("name")})
        log(f"Cookie names after login: {cookie_names}")
    except Exception as e:
        log(f"Could not read cookies after login: {e}")

    # Verify protected page access.
    decisions_url = urljoin(BASE_URL + "/", DECISIONS_PATH.lstrip("/"))
    log(f"Checking protected page access: {decisions_url}")

    page.goto(decisions_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_load_state("networkidle", timeout=60000)
    page.wait_for_timeout(4000)

    log(f"URL after protected page check: {page.url}")
    log(f"Title after protected page check: {page.title()}")

    if is_forbidden_page(page):
        raise RuntimeError("Login failed or user is not authorized: /decisions2 still returns 403.")

    log("Login finished and /decisions2 is accessible")


# =========================
# Discussions / decisions page
# =========================

def navigate_to_discussions(page) -> None:
    decisions_url = urljoin(BASE_URL + "/", DECISIONS_PATH.lstrip("/"))

    log(f"Navigating directly to discussions page: {decisions_url}")
    page.goto(decisions_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_load_state("networkidle", timeout=60000)
    page.wait_for_timeout(3000)

    log(f"Current URL after navigating to discussions: {page.url}")
    log(f"Page title after navigating to discussions: {page.title()}")

    if is_forbidden_page(page):
        raise RuntimeError("Access denied to /decisions2. Login did not persist or user lacks permission.")


def extract_items(page) -> List[Dict[str, str]]:
    log("Scanning items")

    items: Dict[str, Dict[str, str]] = {}

    if ITEM_LINK_SELECTOR:
        log(f"Using item link selector from env: {ITEM_LINK_SELECTOR}")
        links = page.locator(ITEM_LINK_SELECTOR)
    else:
        links = page.locator("a")

    count = links.count()
    log(f"Total links on decisions page: {count}")

    for i in range(count):
        try:
            link = links.nth(i)
            title = link.inner_text(timeout=1500).strip()
            href = link.get_attribute("href")

            if not href:
                continue

            url = urljoin(BASE_URL + "/", href)

            if not title:
                title = url

            combined = f"{title} {url}"

            # Broad filtering. Tune KRY_ITEM_LINK_SELECTOR later after seeing real page structure.
            if not ITEM_LINK_SELECTOR:
                if any(skip in combined for skip in ["facebook", "instagram", "youtube", "mailto:", "tel:"]):
                    continue

                if len(title) < 3:
                    continue

                # Keep likely content links. If too strict, set KRY_ITEM_LINK_SELECTOR.
                likely = any(
                    token in combined
                    for token in [
                        "דיון",
                        "דיונים",
                        "החלט",
                        "החלטות",
                        "פרוטוקול",
                        "protocol",
                        "decision",
                        "decisions",
                        "post",
                        "blog",
                    ]
                )

                if not likely:
                    continue

            item_id = url
            items[item_id] = {
                "id": item_id,
                "title": safe_filename(title, "protocol"),
                "url": url,
            }

        except Exception:
            continue

    result = list(items.values())
    log(f"Found {len(result)} candidate items")

    for idx, item in enumerate(result[:20]):
        log(f"ITEM {idx}: title={item['title']} | url={item['url']}")

    return result


# =========================
# Download print version
# =========================

def save_current_page_as_pdf(page, output_path: Path) -> Path:
    page.pdf(
        path=str(output_path),
        format="A4",
        print_background=True,
        margin={
            "top": "10mm",
            "right": "10mm",
            "bottom": "10mm",
            "left": "10mm",
        },
    )
    return output_path


def download_print_version(context, item: Dict[str, str], output_dir: Path) -> Optional[Path]:
    page = context.new_page()

    try:
        log(f"Opening item: {item['title']} | {item['url']}")
        page.goto(item["url"], wait_until="domcontentloaded", timeout=60000)
        page.wait_for_load_state("networkidle", timeout=60000)
        page.wait_for_timeout(3000)

        if is_forbidden_page(page):
            log(f"Item page is forbidden: {item['url']}")
            return None

        filename_base = safe_filename(item["title"], "protocol")

        if PRINT_LINK_SELECTOR:
            log(f"Using print selector: {PRINT_LINK_SELECTOR}")
            print_locator = page.locator(PRINT_LINK_SELECTOR).first
        else:
            print_locator = page.get_by_role("link", name="גרסת הדפסה של הפרוטוקול").first

        if print_locator.count() == 0:
            log(f"No print-version link found for: {item['title']}")
            fallback_pdf = output_dir / f"{filename_base}.pdf"
            save_current_page_as_pdf(page, fallback_pdf)
            log(f"Saved item page itself as fallback PDF: {fallback_pdf}")
            return fallback_pdf

        # Wix opens the PDF in a new tab/page.
        try:
            with context.expect_page(timeout=20000) as pdf_event:
                print_locator.click(timeout=15000)

            pdf_page = pdf_event.value
            pdf_page.wait_for_load_state("domcontentloaded", timeout=60000)
            page.wait_for_timeout(2000)

            pdf_url = pdf_page.url
            log(f"PDF page URL: {pdf_url}")

            pdf_page.close()

            if ".pdf" not in pdf_url.lower():
                log("Print page did not open a PDF URL. Saving opened page as PDF fallback.")
                fallback_pdf = output_dir / f"{filename_base}.pdf"
                save_current_page_as_pdf(page, fallback_pdf)
                return fallback_pdf

            response = context.request.get(pdf_url, timeout=60000)

            if not response.ok:
                log(f"Failed downloading PDF URL. Status={response.status}")
                return None

            pdf_name = safe_filename(Path(pdf_url.split("?")[0]).name or f"{filename_base}.pdf")
            if not pdf_name.lower().endswith(".pdf"):
                pdf_name += ".pdf"

            target_path = output_dir / pdf_name
            target_path.write_bytes(response.body())

            log(f"Downloaded PDF file: {target_path}")
            return target_path

        except PlaywrightTimeoutError:
            log("No PDF popup opened. Trying direct download event.")

        # Fallback: maybe browser treats it as download.
        try:
            with page.expect_download(timeout=20000) as download_info:
                print_locator.click(timeout=15000)

            download = download_info.value
            suggested_name = safe_filename(download.suggested_filename or f"{filename_base}.pdf")
            target_path = output_dir / suggested_name
            download.save_as(str(target_path))

            log(f"Downloaded file: {target_path}")
            return target_path

        except Exception as e:
            log(f"Failed direct download fallback: {e}")

        fallback_pdf = output_dir / f"{filename_base}.pdf"
        save_current_page_as_pdf(page, fallback_pdf)
        log(f"Saved current page as final fallback PDF: {fallback_pdf}")
        return fallback_pdf

    finally:
        page.close()


# =========================
# Main
# =========================

def main() -> None:
    drive = get_drive_service()
    state = drive_download_state(drive)
    seen: Dict[str, Any] = state.setdefault("seen", {})

    with tempfile.TemporaryDirectory() as tmp:
        output_dir = Path(tmp)

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=HEADLESS,
                downloads_path=str(output_dir),
            )

            context = browser.new_context(
                accept_downloads=True,
                locale="he-IL",
                timezone_id="Asia/Jerusalem",
                viewport={"width": 1440, "height": 1200},
            )

            page = context.new_page()

            login(page)
            navigate_to_discussions(page)

            items = extract_items(page)
            new_items = [item for item in items if item["id"] not in seen]

            log(f"New items: {len(new_items)}")

            for item in new_items:
                try:
                    downloaded = download_print_version(context, item, output_dir)

                    if not downloaded:
                        seen[item["id"]] = {
                            "title": item["title"],
                            "url": item["url"],
                            "status": "download_failed",
                            "checked_at": datetime.now(timezone.utc).isoformat(),
                        }
                        continue

                    drive_file_id = drive_upload_file(
                        service=drive,
                        local_path=downloaded,
                        drive_name=downloaded.name,
                        update_existing=False,
                    )

                    seen[item["id"]] = {
                        "title": item["title"],
                        "url": item["url"],
                        "drive_file_id": drive_file_id,
                        "filename": downloaded.name,
                        "status": "uploaded",
                        "uploaded_at": datetime.now(timezone.utc).isoformat(),
                    }

                    log(f"Uploaded to Drive: {downloaded.name}")

                except Exception as e:
                    log(f"Failed item: {item.get('title')} | {e}")

                    seen[item["id"]] = {
                        "title": item.get("title"),
                        "url": item.get("url"),
                        "status": "error",
                        "error": str(e),
                        "checked_at": datetime.now(timezone.utc).isoformat(),
                    }

            context.close()
            browser.close()

    state["last_run_at"] = datetime.now(timezone.utc).isoformat()
    drive_upload_state(drive, state)

    log("Done")


if __name__ == "__main__":
    main()