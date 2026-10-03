from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.templating import Jinja2Templates
from app.database.connection import files_col, users_col, logs_col, settings
import psutil
import time

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/templates")

# Simple admin auth check (placeholder)
def is_admin(request: Request):
    # In production, use proper session/cookie auth
    return True

@router.get("/")
async def admin_dashboard(request: Request):
    if not is_admin(request):
        raise HTTPException(status_code=403)
        
    total_users = await users_col.count_documents({})
    total_files = await files_col.count_documents({})
    
    # System Stats
    cpu_usage = psutil.cpu_percent()
    ram_usage = psutil.virtual_memory().percent
    
    return templates.TemplateResponse("admin/dashboard.html", {
        "request": request,
        "total_users": total_users,
        "total_files": total_files,
        "cpu": cpu_usage,
        "ram": ram_usage
    })

@router.get("/users")
async def admin_users(request: Request):
    users = await users_col.find().to_list(100)
    return templates.TemplateResponse("admin/users.html", {"request": request, "users": users})

@router.get("/files")
async def admin_files(request: Request, q: str = None):
    query = {}
    if q:
        query = {"filename": {"$regex": q, "$options": "i"}}
    
    files = await files_col.find(query).sort("created_at", -1).to_list(100)
    return templates.TemplateResponse("admin/files.html", {"request": request, "files": files, "query": q})

@router.post("/files/delete/{short_code}")
async def delete_file(request: Request, short_code: str):
    if not is_admin(request):
        raise HTTPException(status_code=403)
        
    await files_col.delete_one({"short_code": short_code})
    return {"status": "success"}
