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
from fastapi import FastAPI, HTTPException

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
_HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}

_LEGACY_PRIVATE_BUCKET = "textbook-assets"  # older pipeline's rows -- bare paths, private, needs a signed URL
_PUBLIC_BUCKET_PREFIX = "textbook-images/"  # this project's own rows -- public bucket, direct URL works
_SIGNED_URL_EXPIRY_SECONDS = 6 * 60 * 60  # 6 hours -- long enough for one real reading/generation session

app = FastAPI(title="EduTeach Textbook Content API")


def _sb_get(table: str, **params):
    resp = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=_HEADERS, params=params, timeout=30)
    if not resp.ok:
        raise HTTPException(502, f"Supabase query failed: {resp.text}")
    return resp.json()


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
    return {"service": "EduTeach Textbook Content API", "try": "/published/books"}


@app.get("/published/books")
async def list_published_books():
    return _sb_get(
        "textbook_books",
        select="book_id,board,grade,subject,language,chapter_count",
    )


@app.get("/published/books/{book_id}/chapters")
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


@app.get("/published/books/{book_id}/chapters/{chapter_number}")
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
        img.pop("drive_file_id", None)
        img.pop("storage_path", None)

    return {
        "book_id": book_id,
        "chapter_number": chapter_number,
        "chapter_title": chapter["chapter_title"],
        "page_start": chapter["page_start"],
        "page_end": chapter["page_end"],
        "content": chapter["content_markdown"],
        "images": images,
    }
