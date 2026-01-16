import os
import time
import logging
import glob
from pathlib import Path
from flask import Flask, request, jsonify, render_template, send_from_directory

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
from webdriver_manager.chrome import ChromeDriverManager

# ===========================
# CONFIG
# ===========================
SURTEL_LOGIN_URL = "https://mt3.suretel.co.za/pbx/login.php"
SURTEL_CALL_HISTORY_URL = "https://mt3.suretel.co.za/pbx/simplecdrs.php"

# Default credentials (override with env vars in production)
USERNAME = os.environ.get("SURTEL_USER", "elegancevip007@gmail.com")
PASSWORD = os.environ.get("SURTEL_PASS", "EleganceVip007")

# Put downloads in user's Downloads folder (This PC -> Downloads)
DOWNLOAD_DIR = os.path.join(Path.home(), "Downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# timeouts / waits
SAFE_PLAYER_WAIT = int(os.environ.get("SAFE_PLAYER_WAIT", "12"))   # wait for player & download link
ROW_POLL_INTERVAL = 1
ROW_POLL_TIMEOUT = 25
DOWNLOAD_WAIT_AFTER_CLICK = int(os.environ.get("DOWNLOAD_WAIT_AFTER_CLICK", "8"))  # seconds after clicking download
DOWNLOAD_DETECT_TIMEOUT = int(os.environ.get("DOWNLOAD_DETECT_TIMEOUT", "30"))  # seconds to detect a file

# headless toggle (set HEADLESS=0 to run with visible browser)
HEADLESS = os.environ.get("HEADLESS", "1") not in ("0", "false", "False")

# logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# flask app
app = Flask(__name__)


# ===========================
# Helper utilities
# ===========================
def safe_js_click(driver, element):
    """Click via JS after scrolling element into view; returns True if okay."""
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
        time.sleep(0.12)
        driver.execute_script("arguments[0].click();", element)
        return True
    except Exception as e:
        logging.debug(f"safe_js_click failed: {e}")
        return False


def newest_file_in_dir(dirpath, ignore_patterns=None):
    """Return newest file path in dir or None."""
    files = glob.glob(os.path.join(dirpath, "*"))
    if not files:
        return None
    files = sorted(files, key=os.path.getmtime, reverse=True)
    if ignore_patterns:
        for f in files:
            if any(p in os.path.basename(f) for p in ignore_patterns):
                continue
            if os.path.isfile(f):
                return f
        return None
    # default: return newest file
    for f in files:
        if os.path.isfile(f):
            return f
    return None


# ===========================
# Driver setup
# ===========================
def setup_driver():
    options = Options()
    # headless engine choice
    if HEADLESS:
        options.add_argument("--headless=new")

    # standard options
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1600,1200")

    # reduce issues with audio/modal in headless Chrome
    options.add_argument("--disable-features=AudioServiceOutOfProcess")
    options.add_argument("--autoplay-policy=no-user-gesture-required")
    options.add_argument("--disable-features=EnableDownloadBubble")

    # Chrome preferences to ensure downloads land in Downloads folder automatically
    prefs = {
        "download.default_directory": DOWNLOAD_DIR,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
        "safebrowsing.disable_download_protection": True,
        "profile.default_content_setting_values.automatic_downloads": 1,
        "profile.default_content_settings.popups": 0
    }
    options.add_experimental_option("prefs", prefs)

    # use webdriver_manager to fetch a matching chromedriver
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)

    # enable CDP download behavior for headless Chrome (works for many versions)
    try:
        driver.execute_cdp_cmd("Page.setDownloadBehavior", {
            "behavior": "allow",
            "downloadPath": DOWNLOAD_DIR
        })
        logging.info("CDP download behavior set.")
    except Exception as e:
        logging.debug(f"Could not set CDP download behavior: {e}")

    logging.info(f"Chrome initialized. Download dir: {DOWNLOAD_DIR}")
    return driver


# ===========================
# Login
# ===========================
def login(driver):
    wait = WebDriverWait(driver, 30)
    logging.info("Logging in...")
    driver.get(SURTEL_LOGIN_URL)

    username_field = wait.until(EC.presence_of_element_located((By.NAME, "username")))
    password_field = driver.find_element(By.NAME, "password")

    username_field.send_keys(USERNAME)
    password_field.send_keys(PASSWORD)

    # try a few ways to submit
    try:
        driver.find_element(By.XPATH, "//button[contains(text(),'Login')]").click()
    except Exception:
        try:
            driver.find_element(By.CSS_SELECTOR, "button[type='submit']").click()
        except Exception:
            driver.execute_script("""[...document.querySelectorAll('button')].find(b=>b.innerText.includes('Login'))?.click()""")

    time.sleep(2)
    logging.info("Logged in.")


# ===========================
# Poll for table rows
# ===========================
def wait_for_table_rows(driver, timeout=ROW_POLL_TIMEOUT):
    end = time.time() + timeout
    while time.time() < end:
        try:
            table = driver.find_element(By.XPATH, "//table[@id='list1']")
            main_rows = table.find_elements(By.XPATH, ".//tr[contains(@class,'jqgrow') and contains(@class,'ui-row-ltr')]")
            if main_rows:
                return main_rows
        except Exception:
            pass
        time.sleep(ROW_POLL_INTERVAL)
    return []


# ===========================
# Core download engine (universal locators + explicit a.download_link)
# ===========================
def download_recordings_safe(driver):
    wait = WebDriverWait(driver, 40)

    try:
        wait.until(EC.presence_of_element_located((By.XPATH, "//table[@id='list1']")))
    except TimeoutException:
        logging.warning("Table list1 not present.")
        return "No table found"

    main_rows = wait_for_table_rows(driver, timeout=ROW_POLL_TIMEOUT)
    logging.info(f"{len(main_rows)} main rows found.")
    if not main_rows:
        return "No rows"

    # universal locators to try
    SOUND_XPATHS = [
        ".//a[contains(@class,'open_recording')]",
        ".//a[contains(@class,'download_link') and contains(@href,'download=1')]",
        ".//a[contains(@onclick,'play')]",
        ".//a[contains(@onclick,'record')]",
        ".//a[contains(@class,'sound')]",
        ".//i[contains(@class,'fa') and (contains(@class,'play') or contains(@class,'sound') or contains(@class,'volume'))]/parent::a",
        ".//button[contains(@onclick,'play')]",
        ".//a[contains(@href,'play') or contains(@href,'record') or contains(@href,'download')]",
        ".//a"
    ]

    for i in range(len(main_rows)):
        # re-query to avoid stale element issues
        try:
            table = driver.find_element(By.XPATH, "//table[@id='list1']")
            main_rows = table.find_elements(By.XPATH, ".//tr[contains(@class,'jqgrow') and contains(@class,'ui-row-ltr')]")
            if i >= len(main_rows):
                break
            row = main_rows[i]
            logging.info(f"Processing main row {i+1}")
        except Exception as e:
            logging.warning(f"Could not re-query rows: {e}")
            break

        # expand row if arrow present
        try:
            arrow = row.find_element(By.XPATH, ".//span[contains(@class,'fa-arrow-circle-right')]")
            safe_js_click(driver, arrow)
            logging.info(f"Expanded row {i+1} via arrow.")
            time.sleep(0.6)
        except NoSuchElementException:
            # try click on row as fallback
            try:
                safe_js_click(driver, row)
                logging.info(f"Tried direct expand on row {i+1}.")
                time.sleep(0.6)
            except Exception:
                pass

        # find child rows (often in subtable with id like {rowid}_t)
        main_row_id = row.get_attribute("id") or ""
        child_rows = []
        if main_row_id:
            try:
                child_rows = driver.find_elements(By.XPATH, f"//table[contains(@id,'{main_row_id}_t')]//tr[contains(@class,'jqgrow')]")
            except Exception:
                child_rows = []
        if not child_rows:
            child_rows = [row]

        for child in child_rows:
            child_id = child.get_attribute("id") or "<no-id>"
            logging.info(f"  Processing child row id={child_id}")

            # make sure it's in view
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", child)
            except Exception:
                pass
            time.sleep(0.2)

            # try candidate locators inside the child row first
            sound_icon = None
            for xp in SOUND_XPATHS:
                try:
                    elems = child.find_elements(By.XPATH, xp)
                    if not elems:
                        continue
                    for e in elems:
                        try:
                            if not e.is_displayed():
                                continue
                            href = (e.get_attribute("href") or "").lower()
                            txt = (e.text or "").lower()
                            cls = (e.get_attribute("class") or "").lower()
                            if ("download" in href) or ("download" in txt) or ("play" in href) or ("play" in txt) or ("record" in href) or ("open_recording" in cls) or ("volume-up" in cls) or ("volume" in cls):
                                sound_icon = e
                                break
                            # last fallback: pick visible anchor/button
                            if xp == ".//a" and e.tag_name.lower() in ("a", "button"):
                                sound_icon = e
                                break
                        except Exception:
                            continue
                    if sound_icon:
                        logging.info(f"    Found sound icon using xpath: {xp}")
                        break
                except Exception:
                    continue

            # fallback: search main row
            if not sound_icon:
                for xp in SOUND_XPATHS:
                    try:
                        elems = row.find_elements(By.XPATH, xp)
                        if not elems:
                            continue
                        for e in elems:
                            try:
                                if not e.is_displayed():
                                    continue
                                href = (e.get_attribute("href") or "").lower()
                                txt = (e.text or "").lower()
                                cls = (e.get_attribute("class") or "").lower()
                                if ("download" in href) or ("download" in txt) or ("play" in href) or ("play" in txt) or ("record" in href) or ("open_recording" in cls) or ("volume-up" in cls) or ("volume" in cls):
                                    sound_icon = e
                                    break
                                if xp == ".//a" and e.tag_name.lower() in ("a", "button"):
                                    sound_icon = e
                                    break
                            except Exception:
                                continue
                        if sound_icon:
                            logging.info(f"    Found sound icon in main row using xpath: {xp}")
                            break
                    except Exception:
                        continue

            if not sound_icon:
                logging.warning(f"  No sound icon found for child row {child_id}; skipping.")
                continue

            # click the sound icon
            clicked = safe_js_click(driver, sound_icon)
            if not clicked:
                try:
                    sound_icon.click()
                    clicked = True
                except Exception as e:
                    logging.warning(f"  Could not click sound icon via element.click(): {e}")

            if not clicked:
                logging.warning(f"  Failed clicking sound icon for child {child_id}; skipping.")
                continue

            logging.info(f"  Clicked sound icon for child {child_id}. Waiting for player & download link...")

            # remember newest file before clicking download, so we can detect new file
            before = newest_file_in_dir(DOWNLOAD_DIR, ignore_patterns=[".crdownload"]) or ""
            t0 = time.time()

            # primary: look specifically for the .download_link anchor (your site uses this)
            download_btn = None
            try:
                download_btn = WebDriverWait(driver, SAFE_PLAYER_WAIT).until(
                    EC.element_to_be_clickable((By.XPATH, "//a[contains(@class,'download_link') and contains(@href,'download=1')]"))
                )
                logging.info("    Found download_link via exact selector.")
            except Exception:
                download_btn = None

            # fallback: broader clickable download anchors
            if not download_btn:
                try:
                    download_btn = WebDriverWait(driver, SAFE_PLAYER_WAIT).until(
                        EC.element_to_be_clickable((By.XPATH, "//a[contains(@href,'download=1') or contains(., 'Download') or contains(@class,'download_link')]"))
                    )
                    logging.info("    Found download link via broader selector.")
                except Exception:
                    download_btn = None

            # fallback: scan anchors looking for 'download' text/href
            if not download_btn:
                try:
                    anchors = driver.find_elements(By.XPATH, "//a")
                    for a in anchors:
                        try:
                            if not a.is_displayed():
                                continue
                            href = (a.get_attribute("href") or "").lower()
                            txt = (a.text or "").lower()
                            if "download" in href or "download" in txt or "save" in txt:
                                download_btn = a
                                logging.info("    Found download link via anchors scan.")
                                break
                        except Exception:
                            continue
                except Exception:
                    pass

            # final fallback: JS find+click first matching 'download' anchor
            js_fired = False
            if not download_btn:
                try:
                    js_click_download = """
                    const match = [...document.querySelectorAll('a')].find(a => 
                        (a.href && a.href.toLowerCase().includes('download=1')) || 
                        (a.href && a.href.toLowerCase().includes('download')) || 
                        (a.innerText && a.innerText.toLowerCase().includes('download')) ||
                        (a.innerText && a.innerText.toLowerCase().includes('save')));
                    if (match) { match.click(); return match.href || match.innerText; } else { return null; }
                    """
                    result = driver.execute_script(js_click_download)
                    if result:
                        logging.info("    Triggered download via JS fallback (returned: %s)", result)
                        js_fired = True
                except Exception as e:
                    logging.debug(f"    JS fallback error: {e}")

            # If we have a download_btn object, click it
            if download_btn:
                try:
                    safe_js_click(driver, download_btn)
                    logging.info(f"    Download clicked for child {child_id}")
                except Exception as e:
                    logging.warning(f"    Could not click download button directly: {e}")
            elif not js_fired:
                logging.info("    No download button object and JS fallback didn't fire; continuing.")

            # wait briefly to allow download to begin
            time.sleep(DOWNLOAD_WAIT_AFTER_CLICK)

            # Try to detect a new file that appeared after t0 (also handle .crdownload)
            new_file = None
            detect_end = time.time() + DOWNLOAD_DETECT_TIMEOUT
            while time.time() < detect_end:
                nf = newest_file_in_dir(DOWNLOAD_DIR, ignore_patterns=[])
                if nf:
                    # accept file if mtime is after t0 - 1 sec (tolerate small clocks)
                    try:
                        if os.path.getmtime(nf) >= t0 - 1:
                            # If it's a temporary download (.crdownload), wait until it finishes
                            base = os.path.basename(nf)
                            if base.endswith(".crdownload"):
                                # wait until .crdownload disappears and final file appears
                                final_name = base.replace(".crdownload", "")
                                final_path = os.path.join(DOWNLOAD_DIR, final_name)
                                # poll until final file exists or timeout
                                wait_end = time.time() + DOWNLOAD_DETECT_TIMEOUT
                                while time.time() < wait_end:
                                    if os.path.exists(final_path):
                                        new_file = final_path
                                        break
                                    time.sleep(0.8)
                                if new_file:
                                    break
                                # if final didn't appear, we'll consider the .crdownload as progress and continue polling
                            else:
                                new_file = nf
                                break
                    except Exception:
                        pass
                time.sleep(0.7)

            if new_file:
                logging.info(f"    Download likely saved to: {new_file}")
            else:
                logging.info("    No new file detected in Downloads after download click (check browser prompts or permissions).")

            # try to close modal
            try:
                close_btn = None
                try:
                    close_btn = driver.find_element(By.XPATH, "//button[@data-dismiss='modal' or contains(@class,'close') or contains(@class,'modal-close')]")
                except Exception:
                    close_btn = None
                if close_btn:
                    safe_js_click(driver, close_btn)
                    logging.info("    Closed player/modal.")
                else:
                    # fallback: send Escape
                    try:
                        driver.execute_script("document.dispatchEvent(new KeyboardEvent('keydown', {'key':'Escape'}));")
                        logging.debug("    Sent Escape key event to close modal.")
                    except Exception:
                        pass
            except Exception:
                pass

            # small pause before next child
            time.sleep(1)

    logging.info("Finished processing all rows.")
    return "Downloads attempted (check Downloads folder)"


# ===========================
# Orchestrator invoked by Flask
# ===========================
def run_surtel_pull(destination_number, start_date, end_date):
    driver = None
    try:
        driver = setup_driver()
        login(driver)

        logging.info("Opening Call History page...")
        driver.get(SURTEL_CALL_HISTORY_URL)
        time.sleep(1)

        wait = WebDriverWait(driver, 25)
        # apply date filters (use robust label-based XPaths)
        logging.info(f"Applying date range: {start_date} -> {end_date}")
        start_input = wait.until(EC.presence_of_element_located((By.XPATH, "//label[contains(text(),'Start date')]/following-sibling::div//input")))
        end_input = wait.until(EC.presence_of_element_located((By.XPATH, "//label[contains(text(),'End date')]/following-sibling::div//input")))

        driver.execute_script("arguments[0].value = arguments[1];", start_input, f"{start_date} 00:00")
        driver.execute_script("arguments[0].dispatchEvent(new Event('change'));", start_input)
        driver.execute_script("arguments[0].value = arguments[1];", end_input, f"{end_date} 23:59")
        driver.execute_script("arguments[0].dispatchEvent(new Event('change'));", end_input)
        time.sleep(0.4)

        # click filter robustly
        try:
            filter_btn = driver.find_element(By.XPATH, "//button[contains(text(),'Filter') and not(contains(@disabled,'disabled'))]")
            safe_js_click(driver, filter_btn)
        except Exception:
            driver.execute_script("""[...document.querySelectorAll('button')].find(b => b.innerText.includes('Filter'))?.click()""")
        time.sleep(1)
        logging.info("Date filter applied.")

        # apply destination
        logging.info(f"Filtering destination: {destination_number}")
        dst_input = wait.until(EC.presence_of_element_located((By.XPATH, "//label[contains(text(),'Destination')]/following-sibling::div//input")))
        dst_input.clear()
        dst_input.send_keys(destination_number)
        time.sleep(0.3)
        try:
            filter_btn = driver.find_element(By.XPATH, "//label[contains(text(),'Destination')]/ancestor::div[contains(@class,'form-group')]//button[contains(text(),'Filter')]")
            safe_js_click(driver, filter_btn)
        except Exception:
            driver.execute_script("""[...document.querySelectorAll('button')].find(b => b.innerText.includes('Filter'))?.click()""")
        time.sleep(1)
        logging.info("Destination filter applied.")

        # call the safe downloader
        result = download_recordings_safe(driver)
        return result

    except Exception as e:
        logging.exception("Fatal error in run_surtel_pull:")
        return f"Error: {e}"

    finally:
        if driver:
            try:
                # short pause to let downloads finish
                time.sleep(2)
            except Exception:
                pass
            try:
                driver.quit()
            except Exception:
                pass


# ===========================
# Flask endpoints
# ===========================
@app.route("/", methods=["GET"])
def home():
    # user already has index.html in templates folder
    return render_template("index.html")


@app.route("/downloads/<path:filename>")
def download_file(filename):
    return send_from_directory(DOWNLOAD_DIR, filename, as_attachment=True)


@app.route("/pull", methods=["POST"])
def pull_recordings():
    logging.info("RAW REQUEST RECEIVED")
    logging.info(str(request.data))
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"status": "error", "message": "Invalid JSON"}), 400

    destination = data.get("destination")
    start_date = data.get("start_date")
    end_date = data.get("end_date")
    if not (destination and start_date and end_date):
        return jsonify({"status": "error", "message": "Missing fields"}), 400

    # run synchronously
    result = run_surtel_pull(destination, start_date, end_date)
    return jsonify({"status": result})


# ===========================
# Run server
# ===========================
if __name__ == "__main__":
    logging.info(f"Starting Flask app. Downloads will go to: {DOWNLOAD_DIR}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
