# EduTeach Textbook Content API

Serves already-published textbook content (chapter text + image links) from
Supabase and Google Drive. Does not run any extraction/classification itself
-- that happens separately, on a GPU instance, before content ever reaches
this service.

## Endpoints

- `GET /published/books` -- list books with published content
- `GET /published/books/{book_id}/chapters` -- list a book's published chapters
- `GET /published/books/{book_id}/chapters/{chapter_number}` -- one chapter's
  text + image URLs

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
