# Why This Harvester Was Complicated

## The Deceptively Simple Task
**Goal:** Download 44 monthly Excel/CSV files from a public website  
**Reality:** Took 9 debugging iterations to achieve 100% coverage  
**The Problem:** Real-world websites are *messy*

---

## What Made It Hard

### 1. **The HTML Structure Lies to You**
The website doesn't follow its own patterns. Some months have nice headings like "May 2024", others have no heading at all. Some files are under a heading, others are just floating in the DOM.

**Attempt 1:** Parse by heading → Found 51 files (overcounted, grabbed wrong file types)  
**Attempt 2:** Strict link text matching → Found only 19/44 files (missed variations)

**The Trap:** You can't trust any single structural pattern because the website is inconsistent.

---

### 2. **Link Text Varies Wildly**
The same type of file has different link text across months:

```html
<a href="...">may sessions</a>           <!-- lowercase -->
<a href="...">May Sessions</a>           <!-- title case -->
<a href="...">March session download</a> <!-- singular + extra word -->
<a href="...">April Session Download</a> <!-- capitals + singular -->
```

**Lesson:** Case-insensitive regex caught variations, but we found 84 matching links for only 35 unique months. The website has duplicate links everywhere.

---

### 3. **Filenames Are Inconsistent AND Wrong**
Files have different naming patterns, and worse—some filenames contain *typos*:

| Filename | What It Claims | What It Actually Is |
|----------|---------------|---------------------|
| `MAY-26-SESSIONS.xlsx` | May 2026 | May 2026 ✓ |
| `Sessions-JAN-24.xlsx` | Jan 2024 | Jan 2024 ✓ |
| `OCT-SESSIONS-NEW.xlsx` | ??? (no year!) | Oct 2025 |
| `Sessions-OCT-24.xlsx` | Oct 2024 | **Oct 2023** (typo!) |

**The Oct 2023 Bug:** The filename says "24" but it was uploaded in `2023/12/`. After inspedting the data, it was clearly mislabeled.

**Solution:** Trust the WordPress URL upload path (`/uploads/YYYY/MM/`) over the filename.

---

### 4. **Missing Year Information**
9 out of 44 files have NO year in the filename:

**Discovery:** We had to infer years from the URL upload date using lag logic:
```python
# Files uploaded 1-3 months after the data period
if data_month > upload_month:
    return upload_year - 1  # Data is from previous year
```

**Example:**  
`NOV-SESSIONS.xlsx` uploaded in `/2026/02/` → November **2025** (not 2026)

Without this, 9 months were completely invisible to our parser.

---

### 5. **Malformed HTML: The Split Link**
June 2025 broke everything because the HTML is malformed:

```html
<!-- Normal link -->
<a href="MAY-24-SESSIONS.xlsx">May Sessions</a>

<!-- June 2025 - WHAT? -->
<a href="JUNE-SESSIONS-CPS.csv">June</a>
<a href="JUNE-SESSIONS-CPS.csv">Sessions</a>
```

The link text is split across TWO separate `<a>` tags pointing to the same file!

**Fix:** Accept standalone month names OR "sessions" if the filename contains "session". Ugly, but necessary.

---

### 6. **Serverless Compute Silent Failures**
After finally getting the parser to find all 44 files, the harvester reported "SUCCESS" but only 4 files appeared in the volume. The other 40 were stuck in `/Workspace/tmp/`.

**Root Cause:**  
- Serverless compute doesn't support `/Workspace/tmp/` reliably
- `dbutils.fs.cp("file:/Workspace/tmp/...", volume)` silently failed
- No error messages, no exceptions—just missing files

**False Fix #1:** Tried `/dbfs/Volumes/...` → DBFS doesn't exist on serverless  
**Actual Fix:** Use Python's `tempfile.NamedTemporaryFile()` (local `/tmp`) + `dbutils.fs.cp("file:...", volume)`

**Lesson:** Serverless compute environments have invisible restrictions. What works in notebooks fails silently in jobs.

---

## The Final Architecture

After 9 iterations, the harvester uses a **three-strategy hierarchy** with redundancy:

1. **Filename + URL Cross-Validation**  
   Extract date from filename, verify against URL. If conflict → trust URL.

2. **Heading Context Parsing**  
   Extract month from link text, find nearest year heading, combine.

3. **URL-Based Inference**  
   Extract month from anywhere, infer year from upload path lag logic.

If Strategy 1 fails, try Strategy 2. If that fails, use Strategy 3. This redundancy handles every edge case.

---

## Key Insights

### Never Trust User-Generated Content
- Filenames have typos → validate against CMS metadata (URL paths)
- Link text varies → normalize and pattern match
- HTML structure is inconsistent → use multiple fallback strategies

### WordPress URLs Are Reliable Metadata
The upload path `/uploads/YYYY/MM/` is maintained by the CMS, not by humans. It's the **only** trustworthy source for dates when filenames are ambiguous.

### Serverless Environments Have Hidden Constraints
Operations that work in interactive notebooks (writing to `/Workspace/tmp/`, using `/dbfs` paths) fail silently in serverless jobs. Always test in the actual execution environment.

### Completeness Validation Drives Debugging
Computing `expected_months - found_months` immediately reveals:
- Which months are missing (guides investigation)
- When you're done (100% coverage = ship it)

Without this, we'd still be guessing whether we found "enough" files.

---

## The Journey

| Iteration | Strategy | Coverage | Why It Failed |
|-----------|----------|----------|---------------|
| 1 | Heading-based parsing | 51 files | Overcounted (wrong file types) |
| 2 | Strict regex | 19/44 | Too strict (missed variations) |
| 3 | Case-insensitive regex | 35/44 | Missed files without years |
| 4 | Added URL year inference | 42/44 | Missed split HTML + typo |
| 5 | Handle split links | 43/44 | Missed Oct 2023 typo |
| 6 | URL overrides conflicts | **44/44** | Parser complete ✓ |
| 7 | Serverless execution | 4/44 copied | `/Workspace/tmp/` fails |
| 8 | Tried `/dbfs` paths | 0/44 copied | DBFS blocked on serverless |
| 9 | Local `/tmp` + dbutils | **44/44** ✓ | **Success!** |
