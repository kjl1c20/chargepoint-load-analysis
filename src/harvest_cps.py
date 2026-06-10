"""Download raw monthly CPS session files from chargeplacescotland.org.

Bronze layer ingestion — stores raw xlsx/csv files exactly as published.
No transformation. Transformation happens in the Silver layer.
"""

import re
import os
import json
import uuid
import hashlib
import logging
import tempfile
from pathlib import Path
from datetime import datetime
from dateutil.relativedelta import relativedelta

import requests
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# Import dbutils for Jobs/serverless execution
try:
    from pyspark.sql import SparkSession
    from pyspark.dbutils import DBUtils
    spark = SparkSession.builder.getOrCreate()
    dbutils = DBUtils(spark)
except ImportError:
    # Fallback for local testing - dbutils will be injected by notebook context
    dbutils = None


# Configuration (environment-aware)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
CPS_URL = os.getenv(
    "CPS_URL", 
    "https://chargeplacescotland.org/monthly-charge-point-performance/"
)
VOLUME_PATH = os.getenv(
    "BRONZE_VOLUME_PATH", 
    "/Volumes/chargepoint_analysis/bronze/raw_cps"
)
DATA_START_YEAR = int(os.getenv("DATA_START_YEAR", "2022"))
DATA_START_MONTH = int(os.getenv("DATA_START_MONTH", "10"))
DATA_START = (DATA_START_YEAR, DATA_START_MONTH)

WEBPAGE_TIMEOUT = int(os.getenv("WEBPAGE_TIMEOUT_SECONDS", "60"))
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "180"))
PREFERRED_FILENAME_KEYWORDS = os.getenv("PREFERRED_KEYWORDS", "CLEAN,NEW").split(",")
MIN_FILE_SIZE_BYTES = int(os.getenv("MIN_FILE_SIZE_BYTES", "50000"))

# Maximum parent elements to traverse when searching for heading context
MAX_HEADING_SEARCH_DEPTH = 50

# Logging setup
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL), 
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger(__name__)

# Log effective configuration on module load
logger.info("Configuration: CPS_URL=%s, VOLUME_PATH=%s, DATA_START=%s", 
           CPS_URL, VOLUME_PATH, DATA_START)
logger.info("Timeouts: webpage=%ds, download=%ds", WEBPAGE_TIMEOUT, DOWNLOAD_TIMEOUT)

MONTH_MAP = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6,
    "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12
}


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((requests.RequestException, requests.Timeout))
)
def fetch_with_retry(url: str, timeout: int = 60) -> requests.Response:
    """Fetch URL with exponential backoff retry.
    
    Args:
        url: URL to fetch
        timeout: Request timeout in seconds
        
    Returns:
        Response object
        
    Raises:
        requests.RequestException: After 3 failed attempts
    """
    logger.debug("Fetching URL: %s (timeout=%ds)", url, timeout)
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp


def extract_month_year_from_filename(filename: str) -> tuple[int, int] | None:
    """Extract (year, month) from CPS filename patterns."""
    filename_upper = filename.upper()
    
    # Pattern 1: MONTH-YY or MONTH-YYYY
    match = re.search(r'(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*-(\d{2,4})', 
                     filename_upper)
    if match:
        month_str, year_str = match.groups()
        month = MONTH_MAP.get(month_str[:3].lower())
        if month:
            year = int(year_str)
            if year < 100:
                year = 2000 + year
            return (year, month)
    
    # Pattern 2: MONTH_YYYY or MONTH-YYYY (4-digit year)
    match = re.search(r'(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*[_-](\d{4})', 
                     filename_upper)
    if match:
        month_str, year_str = match.groups()
        month = MONTH_MAP.get(month_str[:3].lower())
        if month:
            return (int(year_str), month)
    
    return None


def extract_upload_date_from_url(url: str) -> tuple[int, int] | None:
    """
    Extract upload year/month from WordPress URL pattern.
    
    Pattern: /uploads/YYYY/MM/filename.xlsx
    Returns: (upload_year, upload_month)
    """
    match = re.search(r'/uploads/(\d{4})/(\d{2})/', url)
    if match:
        upload_year = int(match.group(1))
        upload_month = int(match.group(2))
        return (upload_year, upload_month)
    return None


def infer_data_year_from_upload(data_month: int, upload_year: int, upload_month: int) -> int:
    """
    Infer data year from upload date.
    
    Logic: Files are typically uploaded 1-3 months after the data period.
    If data_month > upload_month, the data is from the previous year.
    
    Examples:
    - May data uploaded in Jul 2025 → May 2025
    - Nov data uploaded in Feb 2026 → Nov 2025 (previous year)
    - Oct data uploaded in Dec 2025 → Oct 2025
    """
    if data_month > upload_month:
        return upload_year - 1
    else:
        return upload_year


def find_year_from_heading(link) -> int | None:
    """Find year from nearest heading before this link."""
    current = link
    for _ in range(MAX_HEADING_SEARCH_DEPTH):
        current = current.previous_sibling or (current.parent if current.parent else None)
        if current is None:
            break
        if hasattr(current, 'name') and current.name in ['h2', 'h3', 'h4']:
            heading_text = current.get_text(strip=True)
            # Look for YYYY pattern
            year_match = re.search(r'\b(20\d{2})\b', heading_text)
            if year_match:
                return int(year_match.group(1))
    return None


def extract_month_from_link_text(link_text: str) -> int | None:
    """Extract month number from link text like 'may sessions' or 'april session download'."""
    for month_name, month_num in MONTH_MAP.items():
        if link_text.lower().startswith(month_name):
            return month_num
    return None


def is_preferred_file(filename: str, existing: str) -> bool:
    """Check if filename is preferred over existing based on keywords.
    
    Args:
        filename: Candidate filename
        existing: Existing filename
        
    Returns:
        True if filename should replace existing
    """
    for keyword in PREFERRED_FILENAME_KEYWORDS:
        if keyword in filename.upper() and keyword not in existing.upper():
            return True
    return False


def fetch_session_files() -> dict[tuple[int, int], str]:
    """
    Parse webpage to find ALL session files.
    
    Strategy (in priority order):
    1. Extract year/month from FILENAME, but validate against URL if available
    2. Extract month from link text + year from nearest heading
    3. Extract month from filename/link + year from URL upload path (fallback)
    
    Returns: dict mapping (year, month) -> download_url
    """
    resp = fetch_with_retry(CPS_URL, timeout=WEBPAGE_TIMEOUT)
    soup = BeautifulSoup(resp.text, 'html.parser')
    
    session_files = {}
    
    # Find ALL links on the page
    for link in soup.find_all('a', href=True):
        link_text = link.get_text(strip=True).lower()
        href = link['href']
        
        # Check if it's a session file
        if not re.search(r'\.(xlsx|csv)$', href, re.I):
            continue
        
        filename = href.rsplit('/', 1)[-1]
        
        # Check if this is a session file
        # Pattern 1: Standard format - "month sessions" or "month session download"
        # Pattern 2: Split HTML format - just "month name" or "sessions" if filename has both
        is_session_file = False
        if re.match(r'^(\w+)\s+sessions?(\s+download)?$', link_text):
            is_session_file = True
        elif ('session' in filename.lower() and 
              (link_text in MONTH_MAP or link_text == 'sessions')):
            # Handle malformed HTML like "June" / "Sessions" as separate links
            is_session_file = True
        
        if not is_session_file:
            continue
        
        month_year = None
        upload_date = extract_upload_date_from_url(href)
        
        # Strategy 1: Try to extract from filename first (includes year)
        filename_date = extract_month_year_from_filename(filename)
        
        if filename_date and upload_date:
            # Validate filename year against upload date
            # If they differ, the filename year is likely a typo or version number
            filename_year, filename_month = filename_date
            upload_year, upload_month = upload_date
            
            # Infer the correct year from upload date
            inferred_year = infer_data_year_from_upload(filename_month, upload_year, upload_month)
            
            # If filename year conflicts with inferred year, trust the URL
            if filename_year != inferred_year:
                logger.warning("Year conflict for %s: filename says %d, URL suggests %d - using URL",
                              filename, filename_year, inferred_year)
                month_year = (inferred_year, filename_month)
            else:
                month_year = filename_date
        elif filename_date:
            month_year = filename_date
        
        # Strategy 2: If no year in filename, try heading context
        if not month_year:
            month = extract_month_from_link_text(link_text)
            year = find_year_from_heading(link)
            if month and year:
                month_year = (year, month)
        
        # Strategy 3: If still no match, use URL upload date as fallback
        if not month_year:
            month = extract_month_from_link_text(link_text)
            if not month:
                # Try to extract month from filename too
                for month_name, month_num in MONTH_MAP.items():
                    if month_name.upper() in filename.upper():
                        month = month_num
                        break
            
            if month and upload_date:
                upload_year, upload_month = upload_date
                year = infer_data_year_from_upload(month, upload_year, upload_month)
                month_year = (year, month)
                logger.info("Inferred from URL: %s %d (uploaded %d/%02d)", 
                           datetime(year, month, 1).strftime("%b"), year, 
                           upload_year, upload_month)
        
        if not month_year:
            logger.warning("Could not parse date from: %s (link: '%s')", filename, link_text)
            continue
        
        year, month = month_year
        month_key = (year, month)
        
        # If we already have this month, keep the "better" one
        if month_key in session_files:
            existing_filename = session_files[month_key].rsplit('/', 1)[-1]
            # Prefer files based on configurable keywords
            if is_preferred_file(filename, existing_filename):
                session_files[month_key] = href
                logger.info("Updated: %s %d → %s (replaced %s)", 
                           datetime(year, month, 1).strftime("%b"), year, 
                           filename, existing_filename)
        else:
            session_files[month_key] = href
            logger.info("Found: %s %d → %s", 
                       datetime(year, month, 1).strftime("%b"), year, filename)
    
    return session_files


def validate_completeness(session_files: dict[tuple[int, int], str]) -> dict:
    """Check for missing months from Oct 2022 to May 2026."""
    start_year, start_month = DATA_START
    expected = []
    dt = datetime(start_year, start_month, 1)
    
    # Generate expected months (Oct 2022 to present, capped at May 2026)
    end_date = min(datetime.now(), datetime(2026, 5, 31))
    while dt <= end_date:
        expected.append((dt.year, dt.month))
        dt += relativedelta(months=1)
    
    expected_set = set(expected)
    available_set = set(session_files.keys())
    missing = sorted(expected_set - available_set)
    
    # Check for gaps
    has_gaps = False
    if available_set:
        first, last = min(available_set), max(available_set)
        has_gaps = any((y, m) not in available_set for y, m in expected if first <= (y, m) <= last)
    
    return {
        "expected": expected,
        "available": sorted(available_set),
        "missing": missing,
        "coverage": len(available_set) / len(expected) * 100 if expected else 100.0,
        "has_gaps": has_gaps,
        "total_expected": len(expected),
        "total_available": len(available_set)
    }


def validate_downloaded_file(filepath: Path, url: str) -> bool:
    """Validate downloaded file integrity.
    
    Args:
        filepath: Path to downloaded file
        url: Source URL (for logging)
        
    Returns:
        True if file passes validation
        
    Raises:
        ValueError: If file fails validation
    """
    # Check file size
    file_size = filepath.stat().st_size
    if file_size == 0:
        raise ValueError(f"Downloaded file is empty: {filepath}")
    
    # Check minimum expected size (session files are typically >50KB)
    if file_size < MIN_FILE_SIZE_BYTES:
        logger.warning("Downloaded file unusually small (%d bytes): %s", 
                      file_size, filepath)
    
    # Check file extension matches content
    if filepath.suffix.lower() == '.xlsx':
        # Validate Excel magic bytes (XLSX are ZIP files)
        magic = filepath.read_bytes()[:4]
        if magic != b'PK\x03\x04':
            raise ValueError(f"File claims to be .xlsx but has invalid magic bytes: {filepath}")
    
    logger.debug("File validation passed: %s (%d bytes)", filepath.name, file_size)
    return True


def extract_file_metadata(filepath: Path) -> dict:
    """Extract metadata for Bronze table schema evolution tracking.
    
    Args:
        filepath: Path to file
        
    Returns:
        Metadata dict with filename, size, hash, timestamp
    """
    with open(filepath, 'rb') as f:
        file_hash = hashlib.sha256(f.read()).hexdigest()
    
    return {
        "filename": filepath.name,
        "size_bytes": filepath.stat().st_size,
        "sha256": file_hash,
        "downloaded_at": datetime.now().isoformat()
    }


def download_file(url: str, year: int, month: int) -> bool:
    """Download file to Volume using serverless-compatible temp handling.
    
    Returns True if new, False if exists, raises on error.
    
    Serverless-compatible approach:
    - Write temp files directly inside the volume as .tmp_UUID_filename
    - Use dbutils.fs.mv for atomic rename to final location
    - Clean up temp files on both success and failure
    """
    if dbutils is None:
        raise RuntimeError("dbutils not available - cannot access volumes")
    
    filename = url.rsplit("/", 1)[-1]
    target = f"{VOLUME_PATH}/{filename}"
    month_str = datetime(year, month, 1).strftime("%b %Y")
    
    # Check if exists
    try:
        dbutils.fs.ls(target)
        logger.info("Skip %s (%s exists)", month_str, filename)
        return False
    except Exception:
        # File doesn't exist, proceed with download
        logger.debug("File not found (expected): %s", target)
    
    # Write temp file directly inside volume (serverless-compatible)
    temp_filename = f".tmp_{uuid.uuid4().hex}_{filename}"
    temp_path = f"{VOLUME_PATH}/{temp_filename}"
    
    try:
        # Fetch file with retry
        resp = fetch_with_retry(url, timeout=DOWNLOAD_TIMEOUT)
        file_content = resp.content
        
        # Validate file size
        file_size = len(file_content)
        if file_size == 0:
            raise ValueError(f"Downloaded file is empty: {filename}")
        
        if file_size < MIN_FILE_SIZE_BYTES:
            logger.warning("Downloaded file unusually small (%d bytes): %s", file_size, filename)
        
        # Check file extension matches content (for Excel files)
        if filename.lower().endswith('.xlsx'):
            magic = file_content[:4]
            if magic != b'PK\x03\x04':
                raise ValueError(f"File claims to be .xlsx but has invalid magic bytes: {filename}")
        
        # Compute SHA256 hash
        file_hash = hashlib.sha256(file_content).hexdigest()
        metadata = {
            "filename": filename,
            "size_bytes": file_size,
            "sha256": file_hash,
            "downloaded_at": datetime.now().isoformat()
        }
        logger.debug("File metadata: %s", json.dumps(metadata))
        
        # Write to temp location inside volume
        with open(temp_path, 'wb') as f:
            f.write(file_content)
        
        # Atomic move to final location
        dbutils.fs.mv(temp_path, target)
        logger.info("Downloaded %s: %s (%.1f MB)", month_str, filename, file_size / 1e6)
        
        return True
    except Exception as e:
        # Clean up temp file on failure
        try:
            dbutils.fs.rm(temp_path)
        except Exception:
            pass
        raise


def log_harvest_metrics(result: dict):
    """Emit structured metrics for monitoring.
    
    Args:
        result: Harvest result dict
    """
    metrics = {
        "event": "harvest_complete",
        "timestamp": datetime.now().isoformat(),
        "files_new": result["new"],
        "files_existing": result["existing"],
        "files_failed": result["failed"],
        "coverage_pct": result["completeness"]["coverage"],
        "missing_months": len(result["completeness"]["missing"]),
        "has_gaps": result["completeness"]["has_gaps"],
        "volume_file_count": result["volume_files"]
    }
    
    # Emit as structured log (parseable by log aggregators)
    logger.info("METRICS: %s", json.dumps(metrics))
    
    # Alert conditions
    if result["completeness"]["coverage"] < 95:
        logger.error("ALERT: Coverage dropped below 95%% (%.1f%%)", 
                    metrics["coverage_pct"])
    
    if result["failed"] > 0:
        logger.error("ALERT: %d file downloads failed", result["failed"])


def harvest() -> dict:
    """Main harvest function: validate completeness, download files, report results."""
    
    logger.info("=" * 70)
    logger.info("PARSING WEBPAGE FOR SESSION FILES")
    logger.info("=" * 70)
    
    # Parse webpage to find session files
    session_files = fetch_session_files()
    logger.info("Found %d session files on webpage", len(session_files))
    
    # Validate completeness
    logger.info("")
    logger.info("=" * 70)
    logger.info("DATA COMPLETENESS CHECK")
    logger.info("=" * 70)
    
    completeness = validate_completeness(session_files)
    
    logger.info("Available: %d/%d months (%.1f%%)",
               completeness["total_available"], 
               completeness["total_expected"],
               completeness["coverage"])
    
    if completeness["missing"]:
        logger.warning("⚠️  MISSING %d MONTHS:", len(completeness["missing"]))
        by_year = {}
        for year, month in completeness["missing"]:
            by_year.setdefault(year, []).append(datetime(year, month, 1).strftime("%b"))
        for year in sorted(by_year.keys()):
            logger.warning("  %d: %s", year, ", ".join(by_year[year]))
    else:
        logger.info("✓ All expected months available")
    
    if completeness["has_gaps"]:
        logger.warning("⚠️  GAP DETECTED in month sequence")
    
    logger.info("=" * 70)
    
    # Download files
    logger.info("")
    logger.info("DOWNLOADING FILES")
    logger.info("-" * 70)
    
    new, existing, failed = 0, 0, 0
    failed_months = []
    
    for (year, month), url in sorted(session_files.items()):
        try:
            if download_file(url, year, month):
                new += 1
            else:
                existing += 1
        except Exception as e:
            failed += 1
            month_str = datetime(year, month, 1).strftime("%b %Y")
            failed_months.append(month_str)
            logger.error("Failed %s: %s", month_str, e)
    
    # Final report
    try:
        files = sorted([f.name for f in dbutils.fs.ls(VOLUME_PATH) if not f.isDir()])
    except Exception as e:
        logger.error("Failed to list volume files: %s", e)
        files = []
    
    logger.info("")
    logger.info("=" * 70)
    logger.info("HARVEST SUMMARY")
    logger.info("=" * 70)
    logger.info("Files: %d new, %d existing, %d failed", new, existing, failed)
    logger.info("Volume contains: %d files", len(files))
    
    if failed_months:
        logger.error("Failed months: %s", ", ".join(failed_months))
    
    if completeness["missing"]:
        logger.warning("⚠️  %d months missing from website", len(completeness["missing"]))
    elif failed == 0:
        logger.info("✓ All expected months harvested successfully")
    
    logger.info("=" * 70)
    
    result = {
        "new": new,
        "existing": existing,
        "failed": failed,
        "completeness": completeness,
        "volume_files": len(files),
        "session_files_found": len(session_files)
    }
    
    # Emit structured metrics for monitoring
    log_harvest_metrics(result)
    
    return result


if __name__ == "__main__":
    logger.info("Starting CPS harvest → %s", VOLUME_PATH)
    logger.info("Expected range: Oct 2022 to May 2026 (44 months)")
    result = harvest()
