import base64
import json
import mimetypes
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional
from urllib.parse import urljoin

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


BASE_URL = os.getenv("KRY_BASE_URL", "https://k-ry.org.il")
USERNAME = os.environ["KRY_USERNAME"]
PASSWORD = os.environ["KRY_PASSWORD"]

DRIVE_FOLDER_ID = os.environ["DRIVE_FOLDER_ID"]

SECTION_TEXT = os.getenv("KRY_SECTION_TEXT", "דיונים והחלטות")
PRINT_TEXT = os.getenv("KRY_PRINT_TEXT", "גרסת הדפסה של הפרוטוקול")
STATE_FILENAME = "kry_seen_items.json"

HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"

USERNAME_SELECTOR = os.getenv("KRY_USERNAME_SELECTOR", "")
PASSWORD_SELECTOR = os.getenv("KRY_PASSWORD_SELECTOR", "")
LOGIN_BUTTON_SELECTOR = os.getenv("KRY_LOGIN_BUTTON_SELECTOR", "")
ITEM_LINK_SELECTOR = os.getenv("KRY_ITEM_LINK_SELECTOR", "")
PRINT_LINK_SELECTOR = os.getenv("KRY_PRINT_LINK_SELECTOR", "")


def log(msg: str):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:160] or "protocol"


def get_drive_service():
    raw = os.environ["GOOGLE_OAUTH_TOKEN_JSON"].strip()

    try:
        token_data = json.loads(raw)
    except json.JSONDecodeError:
        token_data = json.loads(base64.b64decode(raw).decode("utf-8"))

    creds = Credentials.from_authorized_user_info(
        token_data,
        scopes=["https://www.googleapis.com/auth/drive"]
    )

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())

    return build("drive", "v3", credentials=creds)


def drive_find_file(service, name: str) -> Optional[Dict[str, Any]]:
    escaped = name.replace("'", "\\'")
    query = f"name = '{escaped}' and '{DRIVE_FOLDER_ID}' in parents and trashed = false"

    res = service.files().list(
        q=query,
        spaces="drive",
        fields="files(id, name)",
        pageSize=5
    ).execute()

    files = res.get("files", [])
    return files[0] if files else None


def drive_download_state(service) -> Dict[str, Any]:
    existing = drive_find_file(service, STATE_FILENAME)
    if not existing:
        return {"seen": {}}

    request = service.files().get_media(fileId=existing["id"])

    import io
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)

    done = False
    while not done:
        _, done = downloader.next_chunk()

    buffer.seek(0)
    return json.loads(buffer.read().decode("utf-8"))


def drive_upload_file(service, local_path: Path, drive_name: Optional[str] = None, update_existing=False) -> str:
    drive_name = drive_name or local_path.name
    existing = drive_find_file(service, drive_name)

    mime_type = mimetypes.guess_type(str(local_path))[0] or "application/octet-stream"
    media = MediaFileUpload(str(local_path), mimetype=mime_type, resumable=True)

    if existing and update_existing:
        updated = service.files().update(
            fileId=existing["id"],
            media_body=media,
            fields="id"
        ).execute()
        return updated["id"]

    if existing:
        return existing["id"]

    metadata = {
        "name": drive_name,
        "parents": [DRIVE_FOLDER_ID]
    }

    created = service.files().create(
        body=metadata,
        media_body=media,
        fields="id"
    ).execute()

    return created["id"]


def drive_upload_state(service, state: Dict[str, Any]):
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        temp_path = Path(f.name)

    try:
        drive_upload_file(service, temp_path, STATE_FILENAME, update_existing=True)
    finally:
        temp_path.unlink(missing_ok=True)


def login(page):
    login_url = os.getenv("KRY_LOGIN_URL", urljoin(BASE_URL, "/login.asp"))

    log(f"Opening login page: {login_url}")
    page.goto(login_url, wait_until="domcontentloaded", timeout=60000)

    log("Logging in")

    if USERNAME_SELECTOR:
        page.locator(USERNAME_SELECTOR).fill(USERNAME)
    else:
        if page.locator("input[type='email']").count() > 0:
            page.locator("input[type='email']").first.fill(USERNAME)
        elif page.locator("input[type='text']").count() > 0:
            page.locator("input[type='text']").first.fill(USERNAME)
        else:
            raise RuntimeError("Username field not found. Set KRY_USERNAME_SELECTOR.")

    if PASSWORD_SELECTOR:
        page.locator(PASSWORD_SELECTOR).fill(PASSWORD)
    else:
        if page.locator("input[type='password']").count() == 0:
            raise RuntimeError("Password field not found. Set KRY_PASSWORD_SELECTOR.")
        page.locator("input[type='password']").first.fill(PASSWORD)

    if LOGIN_BUTTON_SELECTOR:
        page.locator(LOGIN_BUTTON_SELECTOR).click()
    else:
        if page.locator("input[type='submit']").count() > 0:
            page.locator("input[type='submit']").first.click()
        elif page.locator("button[type='submit']").count() > 0:
            page.locator("button[type='submit']").first.click()
        else:
            page.keyboard.press("Enter")

    page.wait_for_load_state("networkidle", timeout=60000)

    # Basic login validation
    if page.locator("input[type='password']").count() > 0:
        raise RuntimeError("Login probably failed: password field is still visible after submit.")

    log("Login finished")


def navigate_to_discussions(page):
    log(f"Navigating to {SECTION_TEXT}")

    locator = page.get_by_text(SECTION_TEXT, exact=False)
    if locator.count() == 0:
        raise RuntimeError(f"Could not find section: {SECTION_TEXT}")

    locator.first.click()
    page.wait_for_load_state("networkidle", timeout=60000)


def extract_items(page):
    log("Scanning items")

    if ITEM_LINK_SELECTOR:
        links = page.locator(ITEM_LINK_SELECTOR)
    else:
        links = page.locator("a")

    items = {}

    for i in range(links.count()):
        try:
            link = links.nth(i)
            title = link.inner_text(timeout=1000).strip()
            href = link.get_attribute("href")

            if not title or not href:
                continue

            url = urljoin(BASE_URL, href)
            combined = f"{title} {url}"

            if not ITEM_LINK_SELECTOR:
                if not any(x in combined for x in ["דיון", "החלט", "פרוטוקול", "נוהל", "נהלים"]):
                    continue

            items[url] = {
                "id": url,
                "title": title,
                "url": url
            }

        except Exception:
            continue

    result = list(items.values())
    log(f"Found {len(result)} candidate items")
    return result


def download_print_version(context, item, output_dir: Path) -> Optional[Path]:
    page = context.new_page()

    try:
        page.goto(item["url"], wait_until="domcontentloaded", timeout=60000)
        page.wait_for_load_state("networkidle", timeout=60000)

        if PRINT_LINK_SELECTOR:
            print_button = page.locator(PRINT_LINK_SELECTOR).first
        else:
            print_button = page.get_by_text(PRINT_TEXT, exact=False).first

        if print_button.count() == 0:
            log(f"No print version found for {item['title']}")
            return None

        filename = safe_filename(item["title"])

        try:
            with page.expect_download(timeout=15000) as download_info:
                print_button.click()

            download = download_info.value
            suggested_name = safe_filename(download.suggested_filename or f"{filename}.pdf")
            target = output_dir / suggested_name
            download.save_as(str(target))
            return target

        except PlaywrightTimeoutError:
            log("No direct download. Saving print page as PDF.")

            page.wait_for_load_state("networkidle", timeout=30000)
            target = output_dir / f"{filename}.pdf"

            page.pdf(
                path=str(target),
                format="A4",
                print_background=True
            )

            return target

    finally:
        page.close()


def main():
    drive = get_drive_service()
    state = drive_download_state(drive)
    seen = state.setdefault("seen", {})

    with tempfile.TemporaryDirectory() as tmp:
        output_dir = Path(tmp)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=HEADLESS)
            context = browser.new_context(
                accept_downloads=True,
                locale="he-IL",
                timezone_id="Asia/Jerusalem"
            )

            page = context.new_page()

            login(page)
            navigate_to_discussions(page)

            items = extract_items(page)
            new_items = [x for x in items if x["id"] not in seen]

            log(f"New items: {len(new_items)}")

            for item in new_items:
                try:
                    downloaded = download_print_version(context, item, output_dir)

                    if not downloaded:
                        seen[item["id"]] = {
                            "title": item["title"],
                            "url": item["url"],
                            "status": "print_not_found",
                            "checked_at": datetime.now(timezone.utc).isoformat()
                        }
                        continue

                    drive_file_id = drive_upload_file(drive, downloaded)

                    seen[item["id"]] = {
                        "title": item["title"],
                        "url": item["url"],
                        "drive_file_id": drive_file_id,
                        "filename": downloaded.name,
                        "status": "uploaded",
                        "uploaded_at": datetime.now(timezone.utc).isoformat()
                    }

                    log(f"Uploaded: {downloaded.name}")

                except Exception as e:
                    seen[item["id"]] = {
                        "title": item["title"],
                        "url": item["url"],
                        "status": "error",
                        "error": str(e),
                        "checked_at": datetime.now(timezone.utc).isoformat()
                    }

                    log(f"Failed: {item['title']} | {e}")

            context.close()
            browser.close()

    state["last_run_at"] = datetime.now(timezone.utc).isoformat()
    drive_upload_state(drive, state)
    log("Done")


if __name__ == "__main__":
    main()