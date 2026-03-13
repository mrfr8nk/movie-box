import os
import traceback
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from moviebox_api import (
    Search,
    Trending,
    Homepage,
    PopularSearch,
    MovieDetails,
    TVSeriesDetails,
    DownloadableMovieFilesDetail,
    DownloadableTVSeriesFilesDetail,
    resolve_media_file_to_be_downloaded,
    MIRROR_HOSTS,
    SELECTED_HOST,
)
from moviebox_api.requests import Session
from moviebox_api.constants import SubjectType


# Global session
session: Optional[Session] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage the global session lifecycle."""
    global session
    session = Session()
    yield
    await session.close()


app = FastAPI(
    title="MovieBox API",
    description="Unofficial REST API for moviebox.ph — Search, stream, and download movies & TV series.",
    version="0.3.5",
    lifespan=lifespan,
)

# CORS — allow all origins for API usage
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def error_response(e: Exception) -> Dict[str, Any]:
    """Format error response."""
    return {"success": False, "error": str(e), "type": type(e).__name__}


# Request/Response Models
class MirrorRequest(BaseModel):
    host: str


# ──────────────────────────────────────────────
# SEARCH
# ──────────────────────────────────────────────
@app.get("/api/v1/search")
async def search_content(
    query: str = Query(..., description="Search query"),
    type: int = Query(0, description="0=All, 1=Movies, 2=TV Series"),
    page: int = Query(1, description="Page number", ge=1),
    per_page: int = Query(24, description="Results per page", ge=1, le=50),
):
    """Search for movies and TV series by title."""
    try:
        subject_type = SubjectType(type)
        search = Search(
            session, 
            query=query, 
            subject_type=subject_type, 
            page=page, 
            per_page=per_page
        )
        result = await search.get_content()
        return {"success": True, **result}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=error_response(e))


# ──────────────────────────────────────────────
# TRENDING
# ──────────────────────────────────────────────
@app.get("/api/v1/trending")
async def get_trending(
    page: int = Query(0, description="Page number", ge=0),
    per_page: int = Query(18, description="Results per page", ge=1, le=50),
):
    """Get trending movies and TV series."""
    try:
        trending = Trending(session, page=page, per_page=per_page)
        result = await trending.get_content()
        return {"success": True, **result}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=error_response(e))


# ──────────────────────────────────────────────
# HOMEPAGE
# ──────────────────────────────────────────────
@app.get("/api/v1/homepage")
async def get_homepage():
    """Get homepage/landing page content."""
    try:
        homepage = Homepage(session)
        result = await homepage.get_content()
        return {"success": True, **result}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=error_response(e))


# ──────────────────────────────────────────────
# POPULAR SEARCHES
# ──────────────────────────────────────────────
@app.get("/api/v1/popular")
async def get_popular():
    """Get popular/hot search queries."""
    try:
        popular = PopularSearch(session)
        result = await popular.get_content()
        return {"success": True, **result}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=error_response(e))


# ──────────────────────────────────────────────
# ITEM DETAILS (by search result item)
# ──────────────────────────────────────────────
@app.get("/api/v1/details")
async def get_details(
    query: str = Query(..., description="Title to search for"),
    index: int = Query(0, description="Index of the search result", ge=0),
    type: int = Query(0, description="0=All, 1=Movies, 2=TV Series"),
):
    """Search and get detailed info about a specific result item."""
    try:
        subject_type = SubjectType(type)
        search = Search(session, query=query, subject_type=subject_type)
        search_model = await search.get_content_model()

        if not search_model.items or index >= len(search_model.items):
            raise HTTPException(
                status_code=404, 
                detail=f"Item not found at index {index}"
            )

        item = search_model.items[index]
        details_obj = search.get_item_details(item)

        if isinstance(details_obj, MovieDetails):
            details = await details_obj.get_content()
            return {
                "success": True, 
                "type": "movie", 
                "search_item": item.model_dump(), 
                "details": details
            }
        elif isinstance(details_obj, TVSeriesDetails):
            details = await details_obj.get_content()
            seasons = await details_obj.get_seasons_content()
            return {
                "success": True,
                "type": "series",
                "search_item": item.model_dump(),
                "details": details,
                "seasons": seasons,
            }
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=error_response(e))


# ──────────────────────────────────────────────
# MOVIE DOWNLOAD LINKS
# ──────────────────────────────────────────────
@app.get("/api/v1/movie/links")
async def get_movie_links(
    query: str = Query(..., description="Movie title to search for"),
    index: int = Query(0, description="Index in search results", ge=0),
    quality: str = Query(
        "BEST", 
        description="Quality: WORST, BEST, 360P, 480P, 720P, 1080P",
        regex="^(WORST|BEST|360P|480P|720P|1080P)$"
    ),
):
    """Search for a movie and get its download/stream links."""
    try:
        search = Search(session, query=query, subject_type=SubjectType.MOVIES)
        search_model = await search.get_content_model()

        if not search_model.items or index >= len(search_model.items):
            raise HTTPException(status_code=404, detail="Movie not found")

        item = search_model.items[index]
        downloadable = DownloadableMovieFilesDetail(session, item)
        files_metadata = await downloadable.get_content_model()

        target_file = resolve_media_file_to_be_downloaded(quality.upper(), files_metadata)

        # Get all available qualities
        quality_map = files_metadata.get_quality_downloads_map()
        available_qualities = {}
        for q, meta in quality_map.items():
            available_qualities[q] = {
                "resolution": meta.resolution,
                "size": meta.size_string,
                "url": meta.path,
            }

        # Subtitles
        subtitles = []
        for cap in files_metadata.caption_files:
            subtitles.append({
                "language": cap.language,
                "language_short": cap.language_short,
                "url": cap.path,
            })

        return {
            "success": True,
            "title": item.title,
            "selected_quality": {
                "resolution": target_file.resolution,
                "size": target_file.size_string,
                "download_url": target_file.path,
                "stream_url": target_file.path,
            },
            "available_qualities": available_qualities,
            "subtitles": subtitles,
        }
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=error_response(e))


# ──────────────────────────────────────────────
# SERIES EPISODE LINKS
# ──────────────────────────────────────────────
@app.get("/api/v1/series/links")
async def get_series_links(
    query: str = Query(..., description="Series title to search for"),
    index: int = Query(0, description="Index in search results", ge=0),
    season: int = Query(..., description="Season number", ge=1),
    episode: int = Query(..., description="Episode number", ge=1),
    quality: str = Query(
        "BEST", 
        description="Quality: WORST, BEST, 360P, 480P, 720P, 1080P",
        regex="^(WORST|BEST|360P|480P|720P|1080P)$"
    ),
):
    """Search for a TV series and get episode download/stream links."""
    try:
        search = Search(session, query=query, subject_type=SubjectType.TV_SERIES)
        search_model = await search.get_content_model()

        if not search_model.items or index >= len(search_model.items):
            raise HTTPException(status_code=404, detail="Series not found")

        item = search_model.items[index]
        downloadable = DownloadableTVSeriesFilesDetail(session, item)
        files_metadata = await downloadable.get_content_model(season=season, episode=episode)

        target_file = resolve_media_file_to_be_downloaded(quality.upper(), files_metadata)

        quality_map = files_metadata.get_quality_downloads_map()
        available_qualities = {}
        for q, meta in quality_map.items():
            available_qualities[q] = {
                "resolution": meta.resolution,
                "size": meta.size_string,
                "url": meta.path,
            }

        subtitles = []
        for cap in files_metadata.caption_files:
            subtitles.append({
                "language": cap.language,
                "language_short": cap.language_short,
                "url": cap.path,
            })

        return {
            "success": True,
            "title": item.title,
            "season": season,
            "episode": episode,
            "selected_quality": {
                "resolution": target_file.resolution,
                "size": target_file.size_string,
                "download_url": target_file.path,
                "stream_url": target_file.path,
            },
            "available_qualities": available_qualities,
            "subtitles": subtitles,
        }
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=error_response(e))


# ──────────────────────────────────────────────
# MIRRORS
# ──────────────────────────────────────────────
@app.get("/api/v1/mirrors")
async def list_mirrors():
    """List all available MovieBox mirror hosts."""
    return {
        "success": True,
        "active_host": SELECTED_HOST,
        "mirrors": list(MIRROR_HOSTS),
    }


@app.post("/api/v1/mirror")
async def set_mirror(req: MirrorRequest):
    """Set active mirror host (sets environment variable for new sessions)."""
    if req.host not in MIRROR_HOSTS:
        raise HTTPException(
            status_code=400, 
            detail=f"Unknown host. Available: {list(MIRROR_HOSTS)}"
        )
    os.environ["MOVIEBOX_API_HOST"] = req.host
    # Note: This only affects new sessions, not the current one
    return {
        "success": True, 
        "message": f"Mirror set to {req.host}. New sessions will use this host."
    }


# ──────────────────────────────────────────────
# HEALTH
# ──────────────────────────────────────────────
@app.get("/api/v1/health")
async def health_check():
    """Check API server health."""
    return {
        "success": True,
        "status": "healthy",
        "version": "0.3.5",
        "active_host": SELECTED_HOST,
    }


# ──────────────────────────────────────────────
# ROOT
# ──────────────────────────────────────────────
@app.get("/")
async def root():
    return {
        "name": "MovieBox API",
        "version": "0.3.5",
        "docs": "/docs",
        "endpoints": [
            "GET /api/v1/search?query=...",
            "GET /api/v1/trending",
            "GET /api/v1/homepage",
            "GET /api/v1/popular",
            "GET /api/v1/details?query=...&index=0",
            "GET /api/v1/movie/links?query=...&quality=BEST",
            "GET /api/v1/series/links?query=...&season=1&episode=1&quality=BEST",
            "GET /api/v1/mirrors",
            "POST /api/v1/mirror",
            "GET /api/v1/health",
        ],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app", 
        host="0.0.0.0", 
        port=int(os.getenv("PORT", 8000)), 
        reload=os.getenv("ENV") == "development"
    )
