"""
Lean, GPU-free serving API: reads already-published textbook content straight
from Supabase (chapter text) and hands it back as clean, read-to-use JSON.

Deliberately does NOT import anything from the extraction/classification
pipeline (PaddleOCR-VL, torch, vLLM, etc.) -- those only make sense on the
GPU instance that produces this content in the first place. This service's
only job is to serve what's already been published; it never touches the
GPU, never runs extraction, and stays this way so it can run anywhere
(Render, a small VM, etc.) on nothing but a few lightweight packages.

Only chapters with published=true are ever returned -- that flag is an
editorial review gate (an admin checks a chapter before it's ever servable),
enforced here, not just at publish time.

Images come from TWO different places depending on which book/pipeline
produced them, and each row only ever has one of the two populated:
  - drive_file_id set    -> uploaded by this project's own publish_book.py,
    hosted on Google Drive (public-read). Permanent link, no expiry.
  - storage_path set,    -> from the original (pre-existing) ingestion
    drive_file_id null      pipeline, sitting in the private Supabase
                             Storage bucket "textbook-assets". Since that
                             bucket is private, these need a fresh SIGNED
                             url generated per request -- never cached or
                             stored, since a signed url expires.

Required environment variables:
    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
"""

import os

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
_HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}

_LEGACY_PRIVATE_BUCKET = "textbook-assets"  # older pipeline's rows -- bare paths, private, needs a signed URL
_PUBLIC_BUCKET_PREFIX = "textbook-images/"  # this project's own rows -- public bucket, direct URL works
_SIGNED_URL_EXPIRY_SECONDS = 6 * 60 * 60  # 6 hours -- long enough for one real reading/generation session

app = FastAPI(title="EduTeach Textbook Content API")

# Everything this API serves is already public, read-only, published content --
# no auth, no per-user data -- so an open CORS policy is appropriate rather than
# an allowlist. Needed for the browser-based textbook-browser front-end (and any
# future school-facing app) to call this directly from client-side JS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


class BookSummary(BaseModel):
    book_id: str = Field(..., description="Stable, deterministic ID built from board+grade+subject+language (e.g. 'ts_scert_class5_maths_en'). Use this to fetch chapters -- it can often be constructed directly without calling this endpoint first, see the root endpoint's 'tip'.")
    board: str = Field(..., description="Education board, e.g. 'TS SCERT', 'CBSE'.")
    grade: str = Field(..., description="Grade/class, e.g. '5'.")
    subject: str = Field(..., description="Subject name, e.g. 'Maths'.")
    language: str = Field(..., description="Language code, e.g. 'en'.")
    total_chapters: int = Field(..., description="Total chapters detected in the source PDF.")
    chapters_published: int = Field(..., description="Chapters actually published and servable right now. Can be less than total_chapters while a book is still being ingested -- check this before assuming the whole book is available.")


class ChapterSummary(BaseModel):
    chapter_number: int
    chapter_title: str
    page_start: int
    page_end: int


class ChapterImage(BaseModel):
    image_id: str = Field(..., description="Matches an <img id=\"...\"/> placeholder in the chapter's content field.")
    caption: str = Field(..., description="A text description of the image -- always present, even when the image itself couldn't be resolved to a URL.")
    order_index: int
    url: str | None = Field(None, description="Direct, fetchable image URL. Null only if resolving the storage location failed -- the caption still describes what the image shows.")


class ChapterContent(BaseModel):
    book_id: str
    chapter_number: int
    chapter_title: str
    page_start: int
    page_end: int
    content: str = Field(..., description="Chapter text, noise-free, with <img id=\"...\"/> placeholders where images belong -- cross-reference each placeholder's id against the images list.")
    images: list[ChapterImage]


def _sb_get(table: str, **params):
    resp = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=_HEADERS, params=params, timeout=30)
    if not resp.ok:
        raise HTTPException(502, f"Supabase query failed: {resp.text}")
    return resp.json()


def _sb_count(table: str, **params) -> int:
    """Exact row count via PostgREST's Prefer: count=exact -- returns the
    count from the response's Content-Range header without fetching any
    actual rows, so this is cheap even for a book with many chapters."""
    headers = {**_HEADERS, "Prefer": "count=exact"}
    resp = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=headers, params={**params, "select": "id", "limit": 1}, timeout=30)
    if not resp.ok:
        raise HTTPException(502, f"Supabase count failed: {resp.text}")
    content_range = resp.headers.get("content-range", "")
    if "/" not in content_range:
        return 0
    try:
        return int(content_range.rsplit("/", 1)[-1])
    except ValueError:
        return 0


def _drive_image_url(file_id: str) -> str:
    return f"https://drive.google.com/uc?export=view&id={file_id}"


def _signed_storage_url(storage_path: str) -> str | None:
    """storage_path is a real path inside the private textbook-assets
    bucket -- resolve it to a temporary signed URL, fresh every call, since
    these expire and must never be cached or stored as if permanent."""
    resp = requests.post(
        f"{SUPABASE_URL}/storage/v1/object/sign/{_LEGACY_PRIVATE_BUCKET}/{storage_path}",
        headers={**_HEADERS, "Content-Type": "application/json"},
        json={"expiresIn": _SIGNED_URL_EXPIRY_SECONDS},
        timeout=30,
    )
    if not resp.ok:
        return None
    signed_path = resp.json().get("signedURL")
    return f"{SUPABASE_URL}/storage/v1{signed_path}" if signed_path else None


def _public_storage_url(storage_path: str) -> str:
    """storage_path already carries its own bucket prefix (e.g.
    "textbook-images/book/ch01/img.jpg") -- that bucket is public, so no
    signing needed, just a direct, permanent URL."""
    return f"{SUPABASE_URL}/storage/v1/object/public/{storage_path}"


def _resolve_image_url(img: dict) -> str | None:
    """Three possible shapes per row, checked in order:
    1. drive_file_id set -- legacy rows from before the Supabase Storage
       migration, still served from Google Drive.
    2. storage_path starting with "textbook-images/" -- this project's
       current uploads, public bucket, permanent direct URL.
    3. storage_path as a bare path -- the older (pre-existing) pipeline's
       rows, private textbook-assets bucket, needs a fresh signed URL.
    storage_path values starting with "external:" are this project's own
    old Drive-placeholder rows and never resolve as a real Supabase path."""
    if img.get("drive_file_id"):
        return _drive_image_url(img["drive_file_id"])
    storage_path = img.get("storage_path") or ""
    if storage_path.startswith(_PUBLIC_BUCKET_PREFIX):
        return _public_storage_url(storage_path)
    if storage_path and not storage_path.startswith("external:"):
        return _signed_storage_url(storage_path)
    return None


@app.get("/")
async def root():
    """Self-description for a script or agent that only has this API's base
    URL and needs to figure out what's available without a human reading
    separate documentation first. For the full machine-readable contract
    (every field, every type), see /openapi.json; for a human-browsable
    version of the same thing, see /docs (both generated automatically by
    the framework, not hand-maintained here)."""
    return {
        "service": "EduTeach Textbook Content API",
        "description": "Read-only REST API serving already-published, real textbook content (text and images) for grounding AI-generated teaching materials.",
        "openapi_spec": "/openapi.json",
        "interactive_docs": "/docs",
        "endpoints": [
            {
                "method": "GET", "path": "/published/books",
                "description": "List published textbooks, optionally filtered by board/grade/subject/language.",
            },
            {
                "method": "GET", "path": "/published/books/{book_id}/chapters",
                "description": "List one book's published chapters (titles, page ranges).",
            },
            {
                "method": "GET", "path": "/published/books/{book_id}/chapters/{chapter_number}",
                "description": "Get one chapter's full content: text plus every image resolved to a fetchable URL.",
            },
        ],
        "tip": "book_id is deterministic, not random -- board.lower().replace(' ','_') + '_class' + grade + '_' + subject.lower().replace(' ','_') + '_' + language. E.g. board='TS SCERT', grade=5, subject='Maths', language='en' -> 'ts_scert_class5_maths_en'. If you already know a school's board/grade/subject, you can often construct this directly and skip listing books entirely.",
    }


@app.get("/published/books", response_model=list[BookSummary], summary="List published textbooks")
async def list_published_books(
    board: str | None = Query(None, description="Filter by board (case-insensitive, partial match allowed), e.g. 'TS SCERT'."),
    grade: str | None = Query(None, description="Filter by exact grade, e.g. '5'."),
    subject: str | None = Query(None, description="Filter by subject (case-insensitive, partial match allowed), e.g. 'Maths'."),
    language: str | None = Query(None, description="Filter by exact language code, e.g. 'en'."),
):
    """chapter_count on the book row is the TOTAL the pipeline detected in
    the PDF, set once up front -- it does NOT track how many chapters have
    actually been published so far. Since chapters now publish one at a
    time as they finish (rather than all at the end), a book that was
    stopped or is still mid-run can genuinely have fewer real chapters than
    that number. total_chapters/chapters_published here are reported
    separately and honestly, computed live from the real chapter rows,
    instead of presenting a partially-ingested book as if it were complete.

    With no filters, returns the entire catalog -- fine today with a
    handful of books, but a caller that already knows what it wants should
    prefer filtering (or constructing book_id directly, see the root
    endpoint) rather than fetching everything and filtering client-side,
    since this list only grows over time."""
    filters = {}
    if board:
        filters["board"] = f"ilike.*{board}*"
    if grade:
        filters["grade"] = f"eq.{grade}"
    if subject:
        filters["subject"] = f"ilike.*{subject}*"
    if language:
        filters["language"] = f"eq.{language}"

    books = _sb_get(
        "textbook_books",
        select="id,book_id,board,grade,subject,language,chapter_count",
        **filters,
    )
    for b in books:
        published = _sb_count("textbook_chapters", book_uuid=f"eq.{b['id']}")
        b["total_chapters"] = b.pop("chapter_count")
        b["chapters_published"] = published
        b.pop("id", None)
    return books


@app.get("/published/books/{book_id}/chapters", response_model=list[ChapterSummary], summary="List a book's published chapters")
async def list_published_chapters(book_id: str):
    books = _sb_get("textbook_books", book_id=f"eq.{book_id}", select="id")
    if not books:
        raise HTTPException(404, f"unknown book_id {book_id!r}")
    book_uuid = books[0]["id"]

    return _sb_get(
        "textbook_chapters",
        book_uuid=f"eq.{book_uuid}",
        published="eq.true",
        select="chapter_number,chapter_title,page_start,page_end",
        order="chapter_number",
    )


@app.get("/published/books/{book_id}/chapters/{chapter_number}", response_model=ChapterContent, summary="Get one chapter's full content")
async def get_published_chapter(book_id: str, chapter_number: int):
    """The served payload: content_markdown as-is (noise-free, compressed,
    <img id="..."> placeholders in place) plus each referenced image
    resolved to a real, publicly-fetchable Drive URL."""
    books = _sb_get("textbook_books", book_id=f"eq.{book_id}", select="id")
    if not books:
        raise HTTPException(404, f"unknown book_id {book_id!r}")
    book_uuid = books[0]["id"]

    chapters = _sb_get(
        "textbook_chapters",
        book_uuid=f"eq.{book_uuid}",
        chapter_number=f"eq.{chapter_number}",
        published="eq.true",
        select="id,chapter_title,page_start,page_end,content_markdown",
    )
    if not chapters:
        raise HTTPException(404, f"chapter {chapter_number} not found or not yet published for book {book_id!r}")
    chapter = chapters[0]

    images = _sb_get(
        "textbook_images",
        chapter_uuid=f"eq.{chapter['id']}",
        select="image_id,caption,drive_file_id,storage_path,order_index",
        order="order_index",
    )
    for img in images:
        img["url"] = _resolve_image_url(img)
        img["caption"] = img.get("caption") or ""  # real rows from older ingestion runs can have this NULL
        img.pop("drive_file_id", None)
        img.pop("storage_path", None)

    return {
        "book_id": book_id,
        "chapter_number": chapter_number,
        "chapter_title": chapter["chapter_title"] or "",
        "page_start": chapter["page_start"],
        "page_end": chapter["page_end"],
        "content": chapter["content_markdown"] or "",
        "images": images,
    }
