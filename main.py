"""
Lean, GPU-free serving API: reads already-published textbook content straight
from Supabase (chapter text) and Google Drive (kept images, public-read),
and hands it back as clean, read-to-use JSON.

Deliberately does NOT import anything from the extraction/classification
pipeline (PaddleOCR-VL, torch, vLLM, etc.) -- those only make sense on the
GPU instance that produces this content in the first place. This service's
only job is to serve what's already been published; it never touches the
GPU, never runs extraction, and stays this way so it can run anywhere
(Render, a small VM, etc.) on nothing but a few lightweight packages.

Only chapters with published=true are ever returned -- that flag is an
editorial review gate (an admin checks a chapter before it's ever servable),
enforced here, not just at publish time.

Required environment variables:
    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
"""

import os

import requests
from fastapi import FastAPI, HTTPException

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
_HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}

app = FastAPI(title="EduTeach Textbook Content API")


def _sb_get(table: str, **params):
    resp = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=_HEADERS, params=params, timeout=30)
    if not resp.ok:
        raise HTTPException(502, f"Supabase query failed: {resp.text}")
    return resp.json()


def _drive_image_url(file_id: str) -> str:
    return f"https://drive.google.com/uc?export=view&id={file_id}"


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
        select="image_id,caption,drive_file_id,order_index",
        order="order_index",
    )
    for img in images:
        img["url"] = _drive_image_url(img.pop("drive_file_id"))

    return {
        "book_id": book_id,
        "chapter_number": chapter_number,
        "chapter_title": chapter["chapter_title"],
        "page_start": chapter["page_start"],
        "page_end": chapter["page_end"],
        "content": chapter["content_markdown"],
        "images": images,
    }
