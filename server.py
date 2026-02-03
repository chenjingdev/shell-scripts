import os
import time
import json
import asyncio
import secrets
import uvicorn
import mimetypes
from typing import Optional, List
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv

# 환경변수 로드
load_dotenv()

# 설정
PORT = int(os.getenv("PORT", 8787))
API_KEY = os.getenv("API_KEY")
SCRIPT_PATH = os.getenv("SCRIPT_PATH")
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "./results"))  # 통합된 결과 폴더

# 모델 정의
class SendRequest(BaseModel):
    type: str  # 'image' or 'text'
    prompt: str

# 유틸리티 함수
async def run_script(prompt_json: str, env: dict):
    """스크립트를 백그라운드에서 실행합니다."""
    process = await asyncio.create_subprocess_exec(
        SCRIPT_PATH,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **env}
    )
    # Fire and forget (백그라운드 실행)
    asyncio.create_task(process.communicate(input=prompt_json.encode()))

def find_result_file(job_id: str) -> Optional[Path]:
    """주어진 Job ID에 해당하는 결과 파일을 검색합니다."""
    if not OUTPUT_DIR.exists():
        return None
    try:
        for entry in os.scandir(OUTPUT_DIR):
            if entry.is_file() and entry.name.startswith(job_id):
                # 임시 파일 등 제외
                if entry.name.endswith(('.tmp', '.part')):
                    continue
                return Path(entry.path)
    except OSError:
        pass
    return None

# FastAPI 앱 설정
@asynccontextmanager
async def lifespan(app: FastAPI):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if not API_KEY or not SCRIPT_PATH:
        print("CRITICAL: API_KEY or SCRIPT_PATH not set in .env")
    yield

app = FastAPI(lifespan=lifespan)

# 정적 파일 서빙 (통합된 results 폴더)
app.mount("/results", StaticFiles(directory=OUTPUT_DIR), name="results")

def verify_key(x_api_key: str = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

@app.get("/health")
async def health():
    return {"ok": True}

@app.post("/send")
async def send_job(req: SendRequest, x_api_key: str = Header(None)):
    """작업 요청을 받고 Job ID를 반환합니다."""
    verify_key(x_api_key)
    
    if req.type not in ['image', 'text']:
        raise HTTPException(status_code=400, detail="Type must be 'image' or 'text'")
    
    # Job ID 생성
    job_id = f"{int(time.time()*1000)}_{secrets.token_hex(4)}"
    
    prompt_json = json.dumps({
        "type": req.type,
        "jobId": job_id,
        "prompt": req.prompt
    })
    
    # 스크립트 실행 (결과 기다리지 않음)
    await run_script(prompt_json, {
        "JOB_ID": job_id,
        "OUTPUT_TYPE": req.type,
        "OUTPUT_DIR": str(OUTPUT_DIR)
    })
    
    return JSONResponse(status_code=202, content={
        "ok": True, 
        "status": "accepted", 
        "jobId": job_id
    })

@app.get("/history/{job_id}")
async def get_history(job_id: str, x_api_key: str = Header(None)):
    """Job ID로 결과 파일이 생성되었는지 확인합니다."""
    verify_key(x_api_key)
    
    # 타임아웃 체크 (Job ID에서 타임스탬프 추출)
    TIMEOUT_MS = 180000  # 3분
    try:
        job_timestamp = int(job_id.split("_")[0])
        elapsed_ms = (time.time() * 1000) - job_timestamp
    except (ValueError, IndexError):
        elapsed_ms = 0  # 파싱 실패 시 타임아웃 체크 스킵
    
    found_path = find_result_file(job_id)
    
    if found_path:
        # 파일이 존재함 -> 완료 상태 반환
        filename = found_path.name
        mime_type, _ = mimetypes.guess_type(found_path)
        if not mime_type:
            mime_type = "application/octet-stream"
            
        return {
            "ok": True,
            "status": "done",
            "jobId": job_id,
            "filename": filename,
            "mime": mime_type
        }
    elif elapsed_ms > TIMEOUT_MS:
        # 타임아웃 -> 실패 상태 반환
        return JSONResponse(status_code=408, content={
            "ok": False, 
            "status": "timeout", 
            "jobId": job_id,
            "message": "작업이 3분 내에 완료되지 않았습니다."
        })
    else:
        # 파일이 없음 -> 진행 중
        return JSONResponse(status_code=202, content={
            "ok": False, 
            "status": "pending", 
            "jobId": job_id
        })

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
