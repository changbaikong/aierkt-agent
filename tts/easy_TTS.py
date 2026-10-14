import os
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse,Response
from pydantic import BaseModel

from dotenv import load_dotenv


load_dotenv()


app = FastAPI(title="Standalone Japanese TTS")

GPT_SOVITS_URL = os.getenv("GPT_SOVITS_URL", "http://127.0.0.1:9880")
api_key = os.environ.get("OPENAI_API_KEY", "").strip()
base_url = os.environ.get("OPENAI_API_BASE", "").strip()
model = os.environ.get("OPENAI_API_MODEL", "").strip()

REF_AUDIO = os.getenv(
    "TTS_REF_AUDIO",
    str(Path(__file__).parent / "tts_arcueid_500" / "wav" / "A10_04_00_0012_0.wav"),
)
REF_TEXT = os.getenv("TTS_REF_TEXT", "ご主人はいつも我が輩を救ってくれるな")
MAX_TEXT_CHARS = 300

class TTSRequest(BaseModel):
    text:str
    
async def translate_zh_to_ja(text:str) -> str:
    if not api_key:
        raise HTTPException(
            status_code=503, 
            detail="Missing OPENAI_API_KEY environment variable",
        )
        
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 512,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Translate Chinese into natural spoken Japanese. "
                    "Return Japanese only. Do not explain, quote, or use Markdown."
                ),
            },
            {"role": "user", "content": text},
        ],
    }
    
    async with httpx.AsyncClient(
        base_url = base_url,
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        timeout = 60.0,
    ) as client:
        response = await client.post("/chat/completions", json=payload)
        
    if response.status_code != 200:
        raise HTTPException(
            status_code=response.status_code,
            detail=f"OpenAI API error: {response.text}",
        )
        
    try:
        japanese = str(response.json()["choices"][0]["message"]["content"]).strip()
    except (KeyError, IndexError,TypeError,ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"Failed to parse OpenAI API response: ") from exc
    
    if not japanese:
        raise HTTPException(status_code=502, detail="OpenAI API returned empty translation") 
    return japanese


@app.post("/health")
async def health():
    return {
        "ok": True, "gpt_sovits_url": GPT_SOVITS_URL,
        
    }
    
@app.post("/tts")
async def tts(req:TTSRequest):
    chinese_text = req.text.strip()
    if not chinese_text:
        raise HTTPException(status_code=422, detail="Text is required")
    if len(chinese_text) > MAX_TEXT_CHARS:
        raise HTTPException(
            status_code = 422,
            detail = f"Text exceeds maximum length of {MAX_TEXT_CHARS} characters",
        )
        
        
    japanese = await translate_zh_to_ja(chinese_text)
    
    payload = {
        "text": japanese,
        "text_lang": "ja",
        "ref_audio_path": REF_AUDIO,
        "prompt_text": REF_TEXT,
        "prompt_lang": "ja",
        "media_type": "wav",
        "streaming_mode": False,
        "text_split_method":"cut5",
        "batch_size": 1,
    }
    
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(f"{GPT_SOVITS_URL}/tts",json=payload)
    
    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"GPT-SoVITS failed:{response.status_code}",
            
        )
    
    return Response(
        content  = response.content,
        media_type="audio/wav",
        headers={"Content-Disposition": 'inline; filename="speech.wav"'},
    )
    
if __name__ == "__main__":
    uvicorn.run(app,host="127.0.0.1",port=8766)