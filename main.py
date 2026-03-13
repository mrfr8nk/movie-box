

import os
import traceback
import re
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel
import httpx

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

# Allowed domains for proxy
ALLOWED_DOMAINS = [
    'h5.aoneroom.com',
    'moviebox.ph',
    'api.moviebox.ph',
    'cdn.moviebox.ph',
    'media.moviebox.ph',
    'static.moviebox.ph',
    'img.moviebox.ph',
    'sub.moviebox.ph',
    'videos.moviebox.ph',
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage the global session lifecycle."""
    global session
    session = Session()
    # Add additional headers to session
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://moviebox.ph/',
        'Origin': 'https://moviebox.ph',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-site',
    })
    yield
    await session.close()


app = FastAPI(
    title="MovieBox API with Proxy",
    description="Unofficial REST API for moviebox.ph — Search, stream, and download movies & TV series with proxy support to avoid 403 errors.",
    version="0.4.0",
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
# PROXY ENDPOINTS
# ──────────────────────────────────────────────

@app.get("/api/v1/proxy/download")
async def proxy_download(
    url: str = Query(..., description="The download URL to proxy"),
    filename: Optional[str] = Query(None, description="Optional filename for download")
):
    """
    Proxy download requests to avoid 403 Forbidden errors.
    This endpoint forwards the request with the proper session cookies and headers.
    """
    try:
        # Validate URL
        if not url.startswith(('http://', 'https://')):
            raise HTTPException(status_code=400, detail="Invalid URL")
        
        # Parse the URL to check if it's from allowed domains
        parsed_url = urlparse(url)
        
        # Get the session cookies from the global session
        cookies = session.cookies.get_dict() if session else {}
        
        # Prepare headers - copy important ones from the original session
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Sec-Fetch-Dest': 'video',
            'Sec-Fetch-Mode': 'no-cors',
            'Sec-Fetch-Site': 'cross-site',
            'Referer': 'https://moviebox.ph/',
            'Origin': 'https://moviebox.ph',
            'Range': 'bytes=0-',  # Request full video
        }
        
        # Add any session-specific headers if available
        if session and hasattr(session, 'headers'):
            # Don't override our headers with session headers that might cause issues
            for key, value in session.headers.items():
                if key not in headers and key.lower() not in ['host', 'content-length']:
                    headers[key] = value
        
        # Create a new client for streaming with timeout
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(60.0, connect=30.0, read=60.0)
        ) as client:
            # Make the request with cookies and headers
            response = await client.get(
                url, 
                headers=headers,
                cookies=cookies,
                timeout=60.0
            )
            
            # Check if request was successful
            response.raise_for_status()
            
            # Get content type and determine filename
            content_type = response.headers.get('content-type', 'video/mp4')
            
            # Generate filename if not provided
            if not filename:
                # Try to extract from Content-Disposition
                content_disposition = response.headers.get('content-disposition')
                if content_disposition:
                    filename_match = re.search(r'filename[^;=\n]*=((["\']).*?\2|[^;\n]*)', content_disposition)
                    if filename_match:
                        filename = filename_match.group(1).strip('"\'')
                
                if not filename:
                    # Extract from URL
                    path_parts = parsed_url.path.split('/')
                    filename = path_parts[-1] if path_parts[-1] else 'video.mp4'
                    
                    # Add .mp4 extension if missing
                    if not '.' in filename:
                        filename = f"{filename}.mp4"
            
            # Return streaming response
            return StreamingResponse(
                response.aiter_bytes(),
                media_type=content_type,
                headers={
                    'Content-Disposition': f'attachment; filename="{filename}"',
                    'Content-Length': response.headers.get('content-length', ''),
                    'Accept-Ranges': 'bytes',
                    'Cache-Control': 'no-cache',
                }
            )
            
    except httpx.HTTPStatusError as e:
        traceback.print_exc()
        raise HTTPException(
            status_code=e.response.status_code,
            detail=f"Proxy error: {str(e)}"
        )
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=error_response(e))


@app.get("/api/v1/proxy/stream")
async def proxy_stream(
    url: str = Query(..., description="The stream URL to proxy"),
    request: Request = None
):
    """
    Proxy streaming requests with support for range headers (for video seeking).
    This endpoint supports video streaming with proper byte-range requests.
    """
    try:
        if not url.startswith(('http://', 'https://')):
            raise HTTPException(status_code=400, detail="Invalid URL")
        
        # Get session cookies
        cookies = session.cookies.get_dict() if session else {}
        
        # Prepare headers - copy range header from original request if present
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Referer': 'https://moviebox.ph/',
            'Origin': 'https://moviebox.ph',
        }
        
        # Forward Range header if present (for video seeking)
        range_header = request.headers.get('range')
        if range_header:
            headers['Range'] = range_header
        
        # Create client with streaming support
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(60.0, connect=30.0, read=120.0)
        ) as client:
            # Make streaming request
            async with client.stream(
                'GET',
                url,
                headers=headers,
                cookies=cookies
            ) as response:
                # Check if request was successful
                if response.status_code >= 400:
                    raise HTTPException(
                        status_code=response.status_code,
                        detail=f"Stream error: {response.status_code}"
                    )
                
                # Prepare response headers
                response_headers = {}
                
                # Forward important headers
                for header in ['content-type', 'content-length', 'content-range', 'accept-ranges']:
                    if header in response.headers:
                        response_headers[header] = response.headers[header]
                
                # Handle partial content (206) for video seeking
                status_code = response.status_code
                
                # Return streaming response
                return StreamingResponse(
                    response.aiter_bytes(),
                    media_type=response.headers.get('content-type', 'video/mp4'),
                    status_code=status_code,
                    headers=response_headers
                )
            
    except httpx.HTTPStatusError as e:
        traceback.print_exc()
        raise HTTPException(
            status_code=e.response.status_code,
            detail=f"Proxy stream error: {str(e)}"
        )
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=error_response(e))


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
    use_proxy: bool = Query(True, description="Return proxy URLs instead of direct links")
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
            url = meta.path
            if use_proxy:
                # Convert to proxy URL
                url = f"/api/v1/proxy/stream?url={url}"
            
            available_qualities[q] = {
                "resolution": meta.resolution,
                "size": meta.size_string,
                "url": url,
            }

        # Subtitles
        subtitles = []
        for cap in files_metadata.caption_files:
            sub_url = cap.path
            if use_proxy:
                sub_url = f"/api/v1/proxy/download?url={sub_url}&filename={item.title}.{cap.language}.srt"
            
            subtitles.append({
                "language": cap.language,
                "language_short": cap.language_short,
                "url": sub_url,
            })

        # Selected quality URL
        selected_url = target_file.path
        if use_proxy:
            selected_url = f"/api/v1/proxy/stream?url={target_file.path}"

        return {
            "success": True,
            "title": item.title,
            "selected_quality": {
                "resolution": target_file.resolution,
                "size": target_file.size_string,
                "download_url": f"/api/v1/proxy/download?url={target_file.path}&filename={item.title}.{target_file.resolution}.mp4" if use_proxy else target_file.path,
                "stream_url": selected_url,
            },
            "available_qualities": available_qualities,
            "subtitles": subtitles,
            "proxy_enabled": use_proxy,
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
    use_proxy: bool = Query(True, description="Return proxy URLs instead of direct links")
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
            url = meta.path
            if use_proxy:
                url = f"/api/v1/proxy/stream?url={url}"
            
            available_qualities[q] = {
                "resolution": meta.resolution,
                "size": meta.size_string,
                "url": url,
            }

        subtitles = []
        for cap in files_metadata.caption_files:
            sub_url = cap.path
            if use_proxy:
                sub_url = f"/api/v1/proxy/download?url={sub_url}&filename={item.title}_S{season:02d}E{episode:02d}.{cap.language}.srt"
            
            subtitles.append({
                "language": cap.language,
                "language_short": cap.language_short,
                "url": sub_url,
            })

        # Selected quality URL
        selected_url = target_file.path
        if use_proxy:
            selected_url = f"/api/v1/proxy/stream?url={target_file.path}"

        filename = f"{item.title}_S{season:02d}E{episode:02d}_{target_file.resolution}.mp4"
        
        return {
            "success": True,
            "title": item.title,
            "season": season,
            "episode": episode,
            "selected_quality": {
                "resolution": target_file.resolution,
                "size": target_file.size_string,
                "download_url": f"/api/v1/proxy/download?url={target_file.path}&filename={filename}" if use_proxy else target_file.path,
                "stream_url": selected_url,
            },
            "available_qualities": available_qualities,
            "subtitles": subtitles,
            "proxy_enabled": use_proxy,
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
        "message": f"Mirror set to {req.host}. New sessions will use this host. Restart recommended."
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
        "version": "0.4.0",
        "active_host": SELECTED_HOST,
        "proxy_enabled": True,
    }


# ──────────────────────────────────────────────
# ROOT
# ──────────────────────────────────────────────
@app.get("/")
async def root():
    return {
        "name": "MovieBox API with Proxy",
        "version": "0.4.0",
        "docs": "/docs",
        "endpoints": [
            "GET /api/v1/search?query=...",
            "GET /api/v1/trending",
            "GET /api/v1/homepage",
            "GET /api/v1/popular",
            "GET /api/v1/details?query=...&index=0",
            "GET /api/v1/movie/links?query=...&quality=BEST&use_proxy=true",
            "GET /api/v1/series/links?query=...&season=1&episode=1&quality=BEST&use_proxy=true",
            "GET /api/v1/mirrors",
            "POST /api/v1/mirror",
            "GET /api/v1/health",
            "GET /api/v1/proxy/download?url=...",
            "GET /api/v1/proxy/stream?url=...",
        ],
        "proxy_feature": "Use proxy endpoints to avoid 403 Forbidden errors"
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app", 
        host="0.0.0.0", 
        port=int(os.getenv("PORT", 8000)), 
        reload=os.getenv("ENV") == "development"
    )
