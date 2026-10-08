
Github_Repo= https://github.com/bluekitsune-sad/memeCollector




 # PRD — Meme Comment Archive & AI Search

 # Meme Comment Archive & AI Search

 **Version:** 1.1\
 **Status:** Development specification\
 **Product type:** Local-first personal media archive\
 **Primary platform:** Desktop / local web application\
 **Target user:** Individual user collecting reaction images and GIFs from comic/webcomic comment sections

---

 ## 0\. Implementation Decisions (v1.1)

 These decisions were made by the product owner and override earlier recommendations in this document:

 ### Target sites for MVP

 The first release must support these sites' **comment-section media** (images, GIFs, and videos where present):

 1. `asurascans.com` (starting point: `https://asurascans.com/comics`)
 2. `mangadex.org`
 3. `mangapark.net`
 4. `comix` (comixship / comix domain as resolvable at build time)

 Additional sites can be added later through the adapter interface. If a URL is entered that no adapter can handle, the application must **explicitly report "site not supported"** rather than failing silently, so the project can be extended with a new adapter.

 ### AI provider

 The MVP uses **OpenRouter** (OpenAI-compatible chat-completions API with vision) for description/tag generation and text embeddings, configured via `OPENROUTER_API_KEY` in `.env`. The provider interface remains replaceable (PRD §16); a deterministic mock provider must exist so tests run without a key.

 ### Frontend

 The frontend is **Next.js (React + TypeScript)**, replacing the plain React recommendation in §33.

 ### Duplicate flag lifecycle

 New requirement — see **§12.1 Duplicate Flag Lifecycle**: every item is flagged `dup`/`nondup` after scanning; `dup` items are auto-deleted after 7 days unless manually unflagged (`unflagged` state); a dedicated Dup status filter is required in the library UI.

---

 ## 1\. Product Overview

 Meme Comment Archive is a local application that automatically collects image and GIF media from the comment sections of supported comic/webcomic websites.

 The application will:

 1. Accept a comic/chapter/page URL.
2. Navigate through the relevant pages.
3. Identify comments on each page.
4. Extract only images and GIFs belonging to comments.
5. Download the media locally.
6. Detect exact and near-duplicate files.
7. Generate thumbnails/previews.
8. Use an AI vision model to describe and tag each meme.
9. Store metadata in a local database.
10. Generate semantic embeddings for AI-powered search.
11. Provide a local web interface for browsing and searching the collection.
12. Preserve the original source URL and page information for every item.

 The system should be designed so that support for a new website can be added through a **site adapter/plugin**, rather than rewriting the entire application.

---

 # 2\. Problem

 Interesting memes and reaction images frequently appear inside comic/webcomic comment sections.

 Finding them later is difficult because:

 - the images are not organized into a useful collection;
- filenames are usually meaningless;
- GIFs are difficult to search;
- the same meme may appear multiple times;
- traditional filename/tag search is insufficient;
- manually downloading hundreds or thousands of images is tedious;
- users may remember the _meaning_ of a meme but not its filename.

 Example:

 The user remembers:

 > "I need that GIF where the cartoon guy slowly turns around looking confused."

 A normal filename search may fail completely.

 The application should allow that natural-language search and return the relevant media.

---

 # 3\. Product Goal

 Create a personal searchable meme library where the user can type something like:

 > confused reaction

 or:

 > someone realizing they made a huge mistake

 and quickly find relevant images/GIFs.

 The application should eventually feel more like **Google Photos for memes** than a traditional file manager.

---

 # 4\. Non-Goals

 The first version will NOT attempt to:

 - scrape arbitrary websites automatically;
- bypass CAPTCHAs;
- bypass authentication or access restrictions;
- circumvent anti-bot protections;
- download media from private/protected content without authorization;
- repost collected content;
- automatically publish memes;
- become a public meme-sharing service;
- train a custom AI model from scratch.

 The collector should respect the website's terms, access rules, and reasonable request rates.

---

 # 5\. Core User Flow

 ## 5.1 Add a source

 User opens the application and selects:

 **Add Source**

 They enter:

```
https://example.com/comic/chapter-42/page-17
```

 The application detects the supported website.

 The user can choose:

 - Current page
- Current chapter
- Multiple chapters
- Entire supported comic
- Custom list of URLs

---

 ## 5.2 Scan

 The application displays:

```
Scanning...

Pages scanned:       37
Comments discovered: 842
Media found:         217
New media:           193
Duplicates:           24

██████████████░░░░░░ 72%
```

 The user can pause or cancel the job.

---

 ## 5.3 Download

 The collector downloads only media identified as belonging to comments.

 Supported initial formats:

 - JPG
- JPEG
- PNG
- WebP
- GIF
- animated WebP
- optionally MP4/WebM later

 Each file receives an internal unique ID.

 Example:

```
media/
    00000001.jpg
    00000002.gif
    00000003.webp
```

 Original filenames are preserved as metadata where possible.

---

 # 6\. Media Identification

 The scraper must distinguish between:

 ### Should download

```
Comment
 ├── text
 ├── user avatar       ← ignore
 ├── attached image    ← download
 └── attached GIF      ← download
```

 ### Should NOT download

```
Page
 ├── comic panel       ← ignore
 ├── site logo         ← ignore
 ├── navigation icon   ← ignore
 ├── advertisement     ← ignore
 ├── user avatar       ← ignore
 └── comment attachment ← download
```

 This distinction is one of the most important requirements.

 The system should not simply download every `<img>` element on the page.

---

 # 7\. Website Adapter Architecture

 The scraper should use a modular adapter system.

 Example:

```
scraper/
    core/
        crawler.py
        downloader.py
        media_detector.py

    adapters/
        base.py
        site_a.py
        site_b.py
        ...
```

 Every website adapter implements a common interface.

 Conceptually:

```
class SiteAdapter:

    def can_handle(url):
        ...

    def discover_pages(url):
        ...

    def find_comments(page):
        ...

    def find_comment_media(comment):
        ...

    def get_comment_metadata(comment):
        ...
```

 This means adding another website later does not require rewriting the crawler.

 For JavaScript-heavy sites, the system should support browser automation. Playwright is appropriate because it can automate Chromium, Firefox, and WebKit and provides both synchronous and asynchronous Python APIs.  Playwright+1

---

 # 8\. Browser Automation

 Use:

 **Python + Playwright**

 The crawler should initially operate in headless mode.

 A debug mode should allow the user to watch the browser.

 Example:

```
Normal mode:
[background browser]

Debug mode:
┌──────────────────────────────────┐
│ Chrome                           │
│                                  │
│ Comic page                       │
│                                  │
│ Comments                         │
│                                  │
└──────────────────────────────────┘
```

 Playwright supports Chromium, Firefox, and WebKit, and can be run headlessly or with a visible browser window.  Playwright+1

---

 # 9\. Authentication

 Some websites may require the user to be logged in.

 The application should support an optional persistent browser profile.

 Example:

```
Browser Profile

[ ] Use existing browser session

Profile:
C:\MemeArchive\browser-profile\
```

 The application must never request or store the user's website password itself.

 The user logs in through the browser.

---

 # 10\. Crawl Controls

 The user should be able to configure:

```
Pages:
[Current page        ▼]

Maximum pages:
[100]

Request delay:
[1.5] seconds

Download limit:
[500] files

Skip existing:
[✓]

Skip duplicates:
[✓]

Download GIFs:
[✓]

Download static images:
[✓]
```

 Default behavior should be conservative.

 The crawler should not aggressively hammer a website.

---

 # 11\. Download Manager

 The downloader should:

 - use streaming downloads;
- follow redirects;
- detect content type;
- validate downloaded files;
- retry temporary failures;
- use exponential backoff;
- enforce configurable size limits;
- avoid duplicate downloads;
- write files atomically.

 Instead of:

```
download → directly save final file
```

 use:

```
download
    ↓
temporary file
    ↓
validate
    ↓
hash
    ↓
move to final location
```

 This prevents corrupted partial files from entering the library.

---

 # 12\. Duplicate Detection

 There should be multiple levels of duplicate detection.

 ## Level 1 — URL duplicate

 If the same media URL has already been downloaded:

```
skip
```

 ## Level 2 — Exact file duplicate

 Calculate a cryptographic hash such as SHA-256.

 Example:

```
SHA256:
abc123...
```

 If another downloaded file has the same hash:

```
duplicate = true
```

 ## Level 3 — Visual duplicate

 Use perceptual hashing.

 This can identify cases such as:

```
image A
1920x1080

image B
1280x720
```

 even though the files are technically different.

 The system should store:

```
sha256
phash
width
height
file_size
format
```

 ## 12.1 Duplicate Flag Lifecycle

 After download (and on demand), the system scans each media item against the
 rest of the library and assigns a **dup status flag**:

```
dup_status = "nondup"    ← no duplicate found
dup_status = "dup"       ← duplicate of another item
dup_status = "unflagged" ← user manually cleared the dup flag
```

 Rules:

 1. Every newly downloaded item is scanned and flagged `dup` or `nondup`
    automatically.
 2. Items flagged `dup` are scheduled for deletion **one week (7 days)** after
    the flag was set. A background job performs the deletion; the item shows
    its expiry date in the UI.
 3. Before expiry, the user can **unflag** a `dup` item, moving it to
    `unflagged`. Unflagged items are never auto-deleted.
 4. The media library provides a **Dup status filter** to view `dup`
    (pending deletion), `nondup`, and `unflagged` items separately.
 5. Auto-deletion removes only the duplicate file/thumbnail/preview and its
    DB rows; at least one copy (the retained original) always remains.
 6. When duplicates exist, the item collected first (`created_at`) is
    retained; later copies are flagged `dup`. The user may unflag a later
    copy before expiry; the system never silently re-flags.
 7. The auto-delete job runs daily and appears in the Jobs UI. Items
    flagged `dup` show a "Pending deletion" indicator in the gallery and
    detail page.

---

 # 13\. GIF Processing

 GIFs require special treatment.

 The system should store the original GIF.

 It should also generate:

```
original.gif
thumbnail.webp
```

 For AI analysis, extract representative frames.

 Example:

```
GIF
 ↓
Frame 1
Frame 20
Frame 40
Frame 60
 ↓
AI analysis
```

 The system should not need to send every frame to an AI model.

 For short GIFs:

 - first frame;
- middle frame;
- final frame.

 For longer GIFs:

 - sample frames at regular intervals.

 The AI result should describe the overall animation rather than one isolated frame.

---

 # 14\. Image Processing

 For each media item generate:

```
original
thumbnail
preview
```

 Example:

```
media/
    00001234.gif

thumbnails/
    00001234.webp

previews/
    00001234.webp
```

 Thumbnails should be optimized for fast browsing.

 Original files must never be modified.

---

 # 15\. AI Analysis

 Each unique media item can be processed by an AI vision model.

 The AI should produce structured metadata.

 Example:

```
{
  "description": "A cartoon character looks shocked and slowly turns toward the viewer.",
  "tags": [
    "shocked",
    "surprised",
    "confused",
    "reaction",
    "cartoon"
  ],
  "emotions": [
    "surprise",
    "confusion"
  ],
  "subjects": [
    "cartoon character"
  ],
  "meme_context": "reaction meme",
  "suggested_search_phrases": [
    "shocked reaction",
    "confused reaction",
    "when you realize something is wrong"
  ]
}
```

 AI output should be stored in the database.

---

 # 16\. AI Must Be Replaceable

 The application must NOT tightly couple itself to one AI provider.

 Create an interface such as:

```
class VisionProvider:

    def analyze_image(self, image):
        ...

    def analyze_gif(self, frames):
        ...

    def generate_embedding(self, text):
        ...
```

 Possible implementations later:

```
providers/
    local.py
    openai.py
    ...
```

 This allows the user to switch between:

 - local AI;
- cloud AI;
- another compatible provider.

---

 # 17\. Local AI Support

 Local AI should be supported eventually, but it does not have to block the MVP.

 Recommended progression:

 ### MVP

 Cloud/API vision model.

 ### V2

 Local vision model.

 ### V3

 Automatic provider selection.

 Example:

```
AI Provider

○ Cloud
○ Local
○ Automatic
```

---

 # 18\. AI Processing Queue

 AI analysis should happen asynchronously.

 Do NOT make the scraper wait for AI analysis.

 Correct architecture:

```
Scraper
   ↓
Downloaded media
   ↓
Database
   ↓
AI Queue
   ↓
Vision analysis
   ↓
Embedding generation
```

 This allows the user to continue scraping while AI processing happens in the background.

---

 # 19\. Processing Status

 Every item should have a processing state.

```
DOWNLOADED
ANALYZING
ANALYZED
EMBEDDING
READY
FAILED
```

 The UI should show:

```
Library

Total: 8,421

Ready:       8,012
Processing:    382
Failed:        27
```

---

 # 20\. Database

 Use:

 **SQLite**

 The database should contain at minimum:

 ### media

```
id
file_path
thumbnail_path
original_filename
mime_type
extension
file_size
width
height
duration
sha256
phash
created_at
```

 ### source

```
id
site
page_url
chapter
page_number
comment_id
media_url
author_name
```

 ### ai\_metadata

```
media_id
description
tags
emotions
subjects
meme_context
ai_provider
model
processed_at
```

 ### embeddings

```
media_id
embedding
embedding_model
created_at
```

 ### jobs

```
id
job_type
status
progress
error
created_at
completed_at
```

 SQLite FTS5 should be used for normal text search because it provides indexed full-text search, ranking, prefix queries, phrase queries, and boolean query support.  SQLite

---

 # 21\. Search System

 The application should combine three search methods.

 ## A. Filename/source search

 Example:

```
chapter 42
```

 ## B. Text/tag search

 Example:

```
angry cat
```

 Powered by SQLite FTS5.

 ## C. Semantic search

 Example:

```
when someone says something unbelievably stupid
```

 The application converts the query into an embedding and finds visually/semantically relevant media.

 A vector similarity system such as FAISS is suitable for this layer; FAISS is designed specifically for efficient similarity search over dense vectors and has Python support.  Faiss+1

---

 # 22\. Hybrid Search

 The best search system should eventually combine:

```
keyword score
       +
semantic score
       +
tag score
       +
metadata score
```

 Example:

```
Final Score =
    0.25 × keyword
  + 0.50 × semantic
  + 0.20 × tag
  + 0.05 × metadata
```

 The exact weights should be configurable later.

---

 # 23\. Search Examples

 The user should be able to search:

```
angry
```

```
confused cartoon
```

```
shocked reaction
```

```
someone realizing they messed up
```

```
when your friend says something dumb
```

```
sad but funny
```

```
GIF where someone slowly turns around
```

 The last examples are particularly important because they demonstrate why semantic search is valuable.

---

 # 24\. Search Filters

 Search results should support filters:

```
Type:
[All] [Image] [GIF]

Source:
[All sites]

Emotion:
[Angry]
[Happy]
[Confused]
[Sad]
[Surprised]

Format:
[JPG]
[PNG]
[GIF]
[WebP]

Date collected:
[Any]

Chapter:
[Any]

AI status:
[Ready]
[Processing]
[Failed]
```

---

 # 25\. Main UI

 The application should run locally and open in a browser.

 Example:

```
┌───────────────────────────────────────────────────────────┐
│ MemeVault                              ⚙ Settings         │
├───────────────────────────────────────────────────────────┤
│                                                           │
│ 🔍  confused reaction when someone realizes they're wrong │
│                                                           │
├───────────────────────────────────────────────────────────┤
│ Filters                                                   │
│                                                           │
│ [All] [GIF] [Images]  [Funny] [Reaction]                  │
│                                                           │
├───────────────────────────────────────────────────────────┤
│                                                           │
│  ┌────────┐  ┌────────┐  ┌────────┐  ┌────────┐          │
│  │        │  │        │  │        │  │        │          │
│  │  MEME  │  │  MEME  │  │  GIF   │  │  MEME  │          │
│  │        │  │        │  │ ▶      │  │        │          │
│  └────────┘  └────────┘  └────────┘  └────────┘          │
│                                                           │
└───────────────────────────────────────────────────────────┘
```

---

 # 26\. Media Detail Page

 Clicking a meme opens:

```
┌─────────────────────────────────────┐
│                                     │
│              MEME                   │
│                                     │
└─────────────────────────────────────┘

Description:
Cartoon character looking confused...

Tags:
#confused #reaction #cartoon

Source:
Chapter 42 — Page 17

Original source:
example.com/...

[Open Source]

[Copy File]
[Copy Image]
[Edit Tags]
[Delete]
```

 For GIFs, the animation should play automatically.

---

 # 27\. Manual Editing

 AI will sometimes get things wrong.

 The user must be able to edit:

 - description;
- tags;
- emotions;
- title;
- source information;
- favorite status.

 Example:

```
Description

[Man dramatically staring at camera]

Tags

[awkward] [reaction] [stare] [+ Add]

Emotion

[awkward ▼]
```

 Manual edits must override AI-generated values where appropriate.

---

 # 28\. Favorites

 The user can mark media as:

```
❤️ Favorite
```

 Favorites should have their own page.

---

 # 29\. Collections

 Users can create collections.

 Examples:

```
Collections

🔥 Best reactions
😂 Funny
😐 Awkward
😭 Sad memes
💀 Completely unhinged
🎬 GIFs
```

 A media item can belong to multiple collections.

---

 # 30\. Random Meme

 Add a:

 **Random Meme**

 button.

 It selects a random item from the library.

 Optional filters:

```
Random from:
○ Everything
○ Favorites
○ GIFs
○ Collection
```

---

 # 31\. Source Tracking

 Every item must preserve provenance.

 Example:

```
Source
────────────────────
Website: ExampleComic
Chapter: 42
Page: 17
Comment ID: 918273
Original URL: ...
Media URL: ...
Collected: 2026-10-06
```

 This makes the archive much easier to audit and manage.

---

 # 32\. Storage Structure

 Recommended structure:

```
meme-vault/
│
├── app/
│   ├── api/
│   ├── scraper/
│   ├── adapters/
│   ├── ai/
│   ├── search/
│   ├── database/
│   └── ui/
│
├── data/
│   ├── media/
│   ├── thumbnails/
│   ├── previews/
│   ├── embeddings/
│   ├── browser-profile/
│   └── database.sqlite
│
├── logs/
│
├── tests/
│
├── config/
│
├── .env.example
├── requirements.txt
└── README.md
```

---

 # 33\. Recommended Technology Stack

 ## Backend

 **Python**

 Reason:

 - excellent scraping ecosystem;
- strong image-processing ecosystem;
- AI libraries;
- SQLite support;
- easy scripting;
- easy background jobs.

 ## Browser automation

 **Playwright**

 Used for JavaScript-heavy websites and dynamic comments.  Playwright

 ## HTML parsing

 **BeautifulSoup**

 Used after obtaining HTML when DOM parsing is sufficient.

 ## Database

 **SQLite**

 Primary local database.

 ## Text search

 **SQLite FTS5**

 For keyword/full-text search.  SQLite

 ## Vector search

 **FAISS**

 For semantic similarity search.  Faiss

 ## Image processing

 **Pillow**

 ## Animation/video processing

 **FFmpeg**

 ## API

 **FastAPI**

 Recommended for the local backend.

 ## Frontend

 Initial recommendation:

 **React + TypeScript**

 Alternative for a simpler first version:

 **HTMX + server-rendered HTML**

 For this particular project, React is preferable if the interface eventually becomes a polished media browser.

 **Decision (v1.1):** The frontend is **Next.js (React + TypeScript)** — see §0.

---

 # 34\. Application Architecture

```
                       ┌──────────────────┐
                       │   Web UI          │
                       │ React/TypeScript  │
                       └────────┬─────────┘
                                │
                                ▼
                       ┌──────────────────┐
                       │    FastAPI       │
                       │     Backend      │
                       └────────┬─────────┘
                                │
          ┌─────────────────────┼─────────────────────┐
          │                     │                     │
          ▼                     ▼                     ▼
   ┌─────────────┐       ┌─────────────┐      ┌─────────────┐
   │   Scraper   │       │ AI Pipeline │      │   Search    │
   └──────┬──────┘       └──────┬──────┘      └──────┬──────┘
          │                     │                     │
          ▼                     ▼                     ▼
   ┌─────────────┐       ┌─────────────┐      ┌─────────────┐
   │ Downloader  │       │ Vision AI   │      │ SQLite FTS5 │
   └──────┬──────┘       │ Embeddings  │      │    + FAISS  │
          │              └──────┬──────┘      └─────────────┘
          │                     │
          └──────────────┬──────┘
                         ▼
                  ┌──────────────┐
                  │    SQLite    │
                  │   Database   │
                  └──────┬───────┘
                         │
                         ▼
                  ┌──────────────┐
                  │ Local Media  │
                  │    Storage   │
                  └──────────────┘
```

---

 # 35\. Background Jobs

 Long-running operations should never block the web UI.

 Jobs include:

```
crawl
download
deduplicate
thumbnail generation
AI analysis
embedding generation
index rebuilding
```

 The UI should display job progress.

 Example:

```
Jobs

● Crawling Chapter 42
  173 / 240 pages

● AI analysis
  821 / 1,203 items

✓ Completed Chapter 41
  94 new memes
```

 For MVP, a simple local worker/queue is sufficient. A heavier distributed task system is unnecessary.

---

 # 36\. Error Handling

 Every failure should be recorded.

 Example:

```
Failed to download:

URL: ...
Reason: HTTP 404
Attempts: 3

[Retry]
```

 AI failure:

```
AI analysis failed.

Reason:
Provider timeout

[Retry]
```

 A failed item should never stop an entire crawl.

---

 # 37\. Rate Limiting

 The crawler must have configurable limits.

 Example defaults:

```
Concurrent requests: 2
Delay between pages: 1 second
Download concurrency: 2
AI concurrency: 2
```

 The user can increase them if appropriate.

 The system should handle HTTP 429 responses gracefully.

---

 # 38\. Caching

 The crawler should maintain a crawl history.

 If the same page has already been scanned:

```
Page already scanned.

Last scan:
2026-10-05

[Scan Again]
[Skip]
```

 This avoids repeatedly processing the same content.

---

 # 39\. Incremental Crawling

 A major requirement is that the user should not need to re-download everything.

 Example:

 First scan:

```
1,000 comments
300 media
```

 Later:

```
1,050 comments
17 new media
```

 The system should download only the new items.

---

 # 40\. Configuration

 Configuration should be available through both UI and config file.

 Example:

```
storage:
  media_directory: "./data/media"
  thumbnail_directory: "./data/thumbnails"

crawler:
  delay_seconds: 1.0
  concurrency: 2
  max_file_size_mb: 100

ai:
  provider: "cloud"
  model: "vision-model"

search:
  semantic_weight: 0.5
  keyword_weight: 0.3
```

 Sensitive API keys should be stored outside source control.

---

 # 41\. Security

 The application should:

 - bind to localhost by default;
- not expose the API publicly;
- never store website passwords;
- keep API keys out of the database;
- validate downloaded content;
- sanitize filenames;
- prevent path traversal;
- limit file sizes;
- avoid executing downloaded files;
- treat external URLs as untrusted input.

---

 # 42\. Privacy

 The application should be local-first.

 By default:

```
Media → Local
Database → Local
Search → Local
Embeddings → Local
```

 If a cloud AI provider is enabled:

```
Selected media
      ↓
Cloud AI
      ↓
Description/tags
      ↓
Local database
```

 The UI should clearly indicate when media is being sent to an external provider.

---

 # 43\. Copyright / Responsible Use

 The application is intended as a personal archival/search tool.

 It should:

 - respect website terms;
- respect robots/access rules where applicable;
- avoid bypassing technical access controls;
- use reasonable crawl rates;
- retain source attribution;
- avoid automatically redistributing collected content.

 The application should include a small notice in Settings explaining that the user is responsible for ensuring their collection and use comply with applicable site terms and copyright law.

---

 # 44\. MVP Scope

 The first release should be much smaller than the complete vision.

 ## MVP must have

 - four supported websites: asurascans.com, mangadex.org, mangapark.net, comix (§0);
- clear "site not supported" error for unknown URLs;
- URL input;
- page/chapter crawling;
- comment detection;
- image extraction;
- GIF extraction;
- video extraction where present in comments;
- downloading;
- SHA-256 duplicate detection;
- duplicate flag lifecycle (`dup`/`nondup`/`unflagged`, 7-day auto-delete, unflag, Dup status filter — §12.1);
- thumbnails;
- SQLite database;
- basic AI description;
- AI tags (OpenRouter);
- keyword search;
- basic semantic search;
- simple gallery;
- media detail page;
- source URL tracking;
- favorites;
- basic crawl progress.

 ## MVP does NOT need

 - multiple websites;
- sophisticated recommendation algorithms;
- mobile application;
- social features;
- accounts;
- cloud synchronization;
- custom AI training;
- automatic meme generation.

---

 ## Browser automation

 ## HTML parsing

 # 45\. Version 2

 V2 should add:

 - multiple website adapters;
- perceptual duplicate detection;
- better GIF understanding;
- advanced filters;
- collections;
- hybrid search;
- local AI;
- bulk editing;
- improved gallery;
- search history;
- similar-meme button.

 Example:

```
[Meme]

🔎 Find Similar
```

 This searches for visually/semantically similar memes.

---

 # 46\. Version 3

 Potential future features:

 - browser extension;
- right-click "Save to MemeVault";
- automatic clipboard importing;
- drag-and-drop importing;
- OCR;
- text extraction from memes;
- face/object recognition;
- automatic emotion classification;
- meme template detection;
- NSFW filtering;
- duplicate clustering;
- automatic collections;
- natural-language library organization.

---

 # 47\. OCR

 A future OCR system should extract text embedded in images.

 Example:

```
Image:

"When you finally fix the bug"

OCR:
when you finally fix the bug

Tags:
programming
bug
developer
relief
```

 This makes text-heavy memes dramatically easier to search.

---

 # 48\. Meme Understanding

 Eventually AI should produce multiple levels of metadata.

 ### Visual

```
cat
person
cartoon
car
explosion
```

 ### Emotion

```
angry
sad
confused
happy
shocked
```

 ### Situation

```
realization
failure
success
awkward silence
argument
waiting
```

 ### Usage

```
reaction
agreement
disagreement
sarcasm
celebration
confusion
```

 This produces much more useful search.

---

 # 49\. Example End-to-End Scenario

 User enters:

```
https://comic.example/chapter/50
```

 Application discovers:

```
Chapter 50
  ↓
24 pages
  ↓
1,823 comments
  ↓
347 media attachments
```

 After duplicate detection:

```
347 attachments
 ↓
291 unique files
```

 AI processing:

```
291 files
 ↓
291 descriptions
 ↓
1,428 tags
 ↓
291 embeddings
```

 Final library:

```
291 new memes
```

 User searches:

```
"reaction when someone says something incredibly stupid"
```

 Search engine returns:

```
1. confused_character.gif     94%
2. shocked_man.jpg            91%
3. cartoon_stare.gif          88%
4. disappointed_cat.jpg       84%
```

 The user clicks the GIF and copies it.

---

 # 50\. Performance Requirements

 For a library of approximately:

```
10,000 media files
```

 the application should:

 - open the gallery quickly;
- search text in well under one second under normal hardware;
- return semantic results within a few seconds;
- generate thumbnails asynchronously;
- never load all original images into browser memory at once.

 The gallery must use lazy loading / pagination or virtualized rendering.

---

 # 51\. Scalability

 The architecture should comfortably support:

```
10,000 files     MVP target
100,000 files    strong target
1,000,000 files  future target
```

 At very large sizes, the application may eventually need a more specialized vector database, but SQLite + FAISS should be more than sufficient for an initial personal collection.

---

 # 52\. Testing

 Testing should cover:

 ### Scraper

 - correct comment detection;
- ignored page images;
- dynamic comments;
- lazy-loaded images;
- pagination;
- infinite scrolling;
- broken media;
- redirects.

 ### Downloader

 - successful download;
- failed download;
- retry;
- duplicate URL;
- corrupted file;
- oversized file.

 ### Media processing

 - JPG;
- PNG;
- WebP;
- GIF;
- animated GIF;
- malformed GIF;
- very large images.

 ### AI

 - successful analysis;
- timeout;
- invalid response;
- malformed JSON;
- provider unavailable.

 ### Search

 - exact tags;
- partial keywords;
- semantic query;
- empty query;
- no results.

---

 # 53\. Logging

 Use structured logs.

 Example:

```
2026-10-06 13:42:11 INFO  crawler started
2026-10-06 13:42:12 INFO  page discovered page=17
2026-10-06 13:42:13 INFO  comment media found count=8
2026-10-06 13:42:14 INFO  downloaded media_id=192
2026-10-06 13:42:14 INFO  duplicate detected media_id=193
2026-10-06 13:42:15 INFO  AI analysis started media_id=192
```

 Logs should be useful for debugging site adapters.

---

 # 54\. Project Milestones

 ## Milestone 1 — Basic downloader

 Build:

```
URL
 ↓
Page
 ↓
Find comments
 ↓
Find images/GIFs
 ↓
Download
```

 No AI.

 Success criteria:

 > Given one supported page, the application downloads only media attached to comments.

---

 ## Milestone 2 — Library

 Add:

 - SQLite;
- metadata;
- thumbnails;
- gallery;
- duplicate detection.

 Success criteria:

 > Downloaded media can be browsed and traced back to its source.

---

 ## Milestone 3 — AI

 Add:

 - image descriptions;
- tags;
- AI queue;
- processing status.

 Success criteria:

 > Every unique media item receives useful searchable metadata.

---

 ## Milestone 4 — Search

 Add:

 - FTS5;
- embeddings;
- FAISS;
- semantic search.

 Success criteria:

 > Natural-language queries return relevant memes.

---

 ## Milestone 5 — Polish

 Add:

 - collections;
- favorites;
- advanced filters;
- better UI;
- retry tools;
- crawl history;
- settings.

---

 # 55\. Definition of Done — MVP

 The MVP is complete when the following workflow works from beginning to end:

```
1. Start application

2. Enter supported comic URL

3. Application scans pages

4. Application identifies comments

5. Application extracts attached images/GIFs

6. Application downloads them

7. Application detects duplicates

8. Application creates thumbnails

9. Application stores metadata

10. AI analyzes new media

11. AI generates tags/descriptions

12. Embeddings are generated

13. User opens gallery

14. User searches:
    "confused reaction"

15. Relevant memes appear

16. User opens a result

17. User can see:
    - image/GIF
    - AI description
    - tags
    - source page
    - original URL

18. User can favorite/edit/delete the item.
```

 If all of these work reliably, the MVP is successful.

---

 # 56\. Suggested Repository

```
meme-vault/
│
├── backend/
│   ├── main.py
│   │
│   ├── api/
│   │   ├── routes_media.py
│   │   ├── routes_search.py
│   │   ├── routes_scraper.py
│   │   └── routes_jobs.py
│   │
│   ├── database/
│   │   ├── models.py
│   │   ├── database.py
│   │   └── migrations/
│   │
│   ├── scraper/
│   │   ├── crawler.py
│   │   ├── downloader.py
│   │   ├── detector.py
│   │   └── adapters/
│   │       ├── base.py
│   │       └── example_site.py
│   │
│   ├── media/
│   │   ├── hashing.py
│   │   ├── thumbnails.py
│   │   └── gif.py
│   │
│   ├── ai/
│   │   ├── provider.py
│   │   ├── vision.py
│   │   └── embeddings.py
│   │
│   ├── search/
│   │   ├── keyword.py
│   │   ├── semantic.py
│   │   └── hybrid.py
│   │
│   └── jobs/
│       ├── queue.py
│       ├── crawl_job.py
│       └── ai_job.py
│
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   ├── pages/
│   │   ├── api/
│   │   └── App.tsx
│   └── package.json
│
├── data/
│   ├── media/
│   ├── thumbnails/
│   ├── previews/
│   ├── embeddings/
│   └── database.sqlite
│
├── tests/
│
├── .env.example
├── docker-compose.yml
└── README.md
```

---

 # 57\. Design Principle

 The most important architectural decision is:

 > **Separate collection, processing, and search.**

 Do not make one giant script that does everything.

 Instead:

```
COLLECT
  ↓
STORE
  ↓
PROCESS
  ↓
INDEX
  ↓
SEARCH
```

 This means if you later replace the AI model, you do not need to scrape the website again.

 If you replace FAISS, you don't need to redownload the images.

 If you add another website, your existing library remains untouched.

---

 # 58\. Final Product Vision

 The finished application should feel like a personal **AI-powered meme hard drive**.

 You dump thousands of random images and GIFs into it, and the application turns them into an organized searchable collection.

 The user shouldn't need to remember:

```
00018291.gif
```

 They should only need to remember:

 > "That GIF of the guy looking at the camera like he just realized he's screwed."

 And the application should find it.

 A few implementation choices in this PRD are deliberately grounded in current tooling: Playwright is well-suited to dynamically rendered comment pages, SQLite FTS5 gives us local indexed text search, and FAISS provides the semantic/vector-search layer.  Playwright+2

 **My recommendation for the actual build:** don't start with the AI. Build **Milestone 1 first against the actual website you have in mind**. Once we know exactly how that site's comments and image attachments are represented, I can turn this PRD into the actual project structure, database schema, API endpoints, scraper interface, and a step-by-step implementation plan.