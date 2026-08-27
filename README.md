# EduTeach Textbook Content API

Read-only REST API serving already-published textbook content (chapter text
+ image URLs) from Supabase. Runs no extraction or classification itself --
that all happens in `eduteach-ingest-service`, which is the only service
that ever writes to the database this API reads from.

## How the three repos fit together

```
                 uploads a PDF, polls job status
edu_teach_textbook_api_interface  ─────────────────►  eduteach-ingest-service
   (the "Find a textbook" site)                              │
         │                                                    │ writes books / chapters / images
         │ reads published content                            ▼
         └──────────────────────────────────────►         Supabase
                                                        (Postgres + Storage)
                                                                ▲
                                                                │ reads published content
                                                      eduteach-textbook-api
                                                       (this repo)
```

- **`eduteach-ingest-service`** ([repo](https://github.com/jashwanthsai678/eduteach_ingest_service)) — the only writer. This repo never talks to it directly; it only reads whatever that service has already published to Supabase.
- **`edu_teach_textbook_api_interface`** ([repo](https://github.com/jashwanthsai678/edu_teach_textbook_api_interface)) — the frontend. It's this repo's main consumer, calling these endpoints to show the Library list and chapter links.
- **This repo** — read-only. Holds `SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY` but only ever issues `GET` requests against Supabase's REST interface, never writes.

## Endpoints

- `GET /` -- self-describing root: lists every endpoint plus the deterministic `book_id` formula, meant for a script or agent that only has this API's base URL.
- `GET /published/books` -- list every published book. Optional query filters: `board` (case-insensitive, partial match), `grade` (exact), `subject` (case-insensitive, partial match), `language` (exact).
- `GET /published/books/{book_id}/chapters` -- list one book's published chapters (titles, page ranges).
- `GET /published/books/{book_id}/chapters/{chapter_number}` -- one chapter's full content: noise-free text with `<img id="..."/>` placeholders, plus every image resolved to a fetchable URL (or a caption-only entry, tagged `usage: "generate"`/`"draw"`, when there's no file).
- `GET /docs`, `GET /openapi.json` -- FastAPI's interactive docs and machine-readable spec, auto-generated.

Book IDs are deterministic (`{board}_class{grade}_{subject}_{language}`,
lowercased, spaces -> underscores), so a caller that already knows a
school's board/grade/subject/language can construct one directly and skip
the list-books call entirely.

## The Supabase schema

Three tables, all in the default `public` schema, plus one Storage bucket.
This repo only reads them; `eduteach-ingest-service` is the only writer and
its README is the authoritative source for how each row gets created.

### `textbook_books`
| column | notes |
|---|---|
| `id` | uuid, primary key |
| `school_id` | uuid -- a single shared-library placeholder value today |
| `book_id` | the deterministic slug used in every URL path |
| `board`, `grade`, `subject`, `language` | what `/published/books`' filters match against |
| `source_pdf`, `source_pdf_sha256` | not currently exposed by any endpoint |
| `chapter_count` | the *detected* total, not the published count -- this API computes `chapters_published` separately via a live count query against `textbook_chapters` |

### `textbook_chapters`
| column | notes |
|---|---|
| `id` | uuid, primary key |
| `book_uuid` | FK -> `textbook_books.id` |
| `chapter_number`, `chapter_title`, `page_start`, `page_end` | returned as-is by the chapters-list and chapter-content endpoints |
| `content_markdown` | returned as-is as the chapter-content endpoint's `content` field |
| `published` | **every query this API makes filters on `published=eq.true`** -- an unpublished chapter is invisible to every endpoint, not just hidden by convention |

### `textbook_images`
| column | notes |
|---|---|
| `id` | uuid, primary key |
| `chapter_uuid` | FK -> `textbook_chapters.id` |
| `image_id` | matches an `<img id="...">` placeholder in the chapter's `content_markdown` |
| `caption` | returned as `caption`; coalesced to `""` if null (a real null-caption row broke this before -- see `_resolve_image_url`/the response model) |
| `storage_path` | nullable; resolved to a public Storage URL via `_public_storage_url`, or `null` if there's no file |
| `drive_file_id` | legacy, nullable -- if set, resolved to a Google Drive URL instead (an older, pre-Storage row) |
| `order_index` | used to `order=order_index` the images array |
| `usage` | returned as-is; coalesced to `"direct"` for any row from before this field existed |

### Storage bucket
- `textbook-images`, public -- resolved via `_public_storage_url` to `{SUPABASE_URL}/storage/v1/object/public/{storage_path}`, no signing needed.

## If you're changing the database

Every read in this repo goes through `main.py`'s `_sb_get()` calls, each
with an explicit `select=` list -- a column rename or drop breaks whichever
endpoint's `select=` still names the old column. Check:

- `list_published_books` -- selects `id,book_id,board,grade,subject,language,chapter_count` from `textbook_books`, plus a separate `_sb_count` query per book against `textbook_chapters` for `chapters_published`.
- `list_published_chapters` -- selects `chapter_number,chapter_title,page_start,page_end` from `textbook_chapters`.
- `get_published_chapter` -- selects `id,chapter_title,page_start,page_end,content_markdown` from `textbook_chapters`, then `image_id,caption,drive_file_id,storage_path,order_index,usage` from `textbook_images`.

A new nullable column generally doesn't need a change here unless you want
it exposed. A renamed or removed column needs the matching `select=`
string updated, and likely the Pydantic response models (`BookSummary`,
`ChapterSummary`, `ChapterImage`, `ChapterContent`) too, since those are
what actually validate and shape the JSON this API returns.

`edu_teach_textbook_api_interface`'s `app.js` reads this API's *response*
shape directly (field names like `chapters_published`, `total_chapters`,
`content`, `images[].usage`) -- see that repo's README for exactly which
fields it depends on.

## Environment variables (set in Render's dashboard, never committed here)

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

## Local run

```
pip install -r requirements.txt
uvicorn main:app --reload
```

## Render deploy

- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
