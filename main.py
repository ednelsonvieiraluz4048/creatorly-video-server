from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import httpx, asyncio, os, uuid, tempfile, traceback, subprocess
from pathlib import Path

app = FastAPI(title="Creatorly Video Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://vogzafjwkzhxoahpxwsr.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

class VideoRequest(BaseModel):
    audio_url: str
    product_image_url: str
    hook_text: str
    cta_text: Optional[str] = "Clica no carrinho aqui embaixo!"
    product_name: Optional[str] = ""
    hashtags: Optional[str] = ""
    user_id: Optional[str] = ""
    titulo: Optional[str] = "Vídeo Creatorly"

@app.get("/")
def root():
    return {"status": "Creatorly Video Server online", "version": "2.0"}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/generate-free")
async def generate_free_video(req: VideoRequest):
    job_id = str(uuid.uuid4())[:8]
    tmp_dir = Path(tempfile.mkdtemp())

    try:
        # ── 1. Baixar assets ─────────────────────────────────────
        async with httpx.AsyncClient(timeout=30) as client:
            img_resp = await client.get(req.product_image_url)
            if img_resp.status_code != 200:
                raise HTTPException(400, "Não foi possível baixar a imagem do produto")
            img_path = tmp_dir / f"product_{job_id}.jpg"
            img_path.write_bytes(img_resp.content)

            audio_resp = await client.get(req.audio_url)
            if audio_resp.status_code != 200:
                raise HTTPException(400, "Não foi possível baixar o áudio")
            audio_path = tmp_dir / f"audio_{job_id}.mp3"
            audio_path.write_bytes(audio_resp.content)

        # ── 2. Preparar imagem 9:16 720x1280 com Pillow ──────────
        from PIL import Image, ImageDraw, ImageFont
        import numpy as np

        W, H = 720, 1280

        img = Image.open(str(img_path)).convert("RGB")

        # Crop inteligente para 9:16 — centralizado verticalmente com leve foco no topo
        ratio = W / H
        img_ratio = img.width / img.height
        if img_ratio > ratio:
            new_w = int(img.height * ratio)
            offset = (img.width - new_w) // 2
            img = img.crop((offset, 0, offset + new_w, img.height))
        else:
            new_h = int(img.width / ratio)
            # Foco no centro (não no topo) para produtos — evita cortar o produto
            offset = max(0, (img.height - new_h) // 3)
            img = img.crop((0, offset, img.width, offset + new_h))
        img = img.resize((W, H), Image.LANCZOS)

        # Overlay escuro para legibilidade do texto
        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 90))
        img = img.convert("RGBA")
        img = Image.alpha_composite(img, overlay).convert("RGB")

        # ── 3. Adicionar textos na imagem ────────────────────────
        draw = ImageDraw.Draw(img)

        def get_font(size):
            for font_path in [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
            ]:
                if os.path.exists(font_path):
                    return ImageFont.truetype(font_path, size)
            return ImageFont.load_default()

        def draw_text_wrapped(draw, text, y, font_size, color, max_width, stroke=2):
            font = get_font(font_size)
            words = text.split()
            lines, line = [], ""
            for word in words:
                test = (line + " " + word).strip()
                bbox = draw.textbbox((0, 0), test, font=font)
                if bbox[2] <= max_width:
                    line = test
                else:
                    if line: lines.append(line)
                    line = word
            if line: lines.append(line)
            lines = lines[:6]
            for i, l in enumerate(lines):
                bbox = draw.textbbox((0, 0), l, font=font)
                x = (W - (bbox[2] - bbox[0])) // 2
                draw.text((x, y + i * (font_size + 8)), l, font=font,
                          fill=color, stroke_width=stroke, stroke_fill=(0, 0, 0))

        # Hook — topo
        draw_text_wrapped(draw, req.hook_text, 80, 26, (255, 255, 255), W - 80)

        # Nome do produto — meio
        if req.product_name:
            draw_text_wrapped(draw, req.product_name.upper(), H // 2 - 20, 22, (0, 255, 136), W - 80, stroke=1)

        # CTA — rodapé
        draw_text_wrapped(draw, req.cta_text, H - 160, 25, (255, 255, 255), W - 80)

        # Hashtags
        if req.hashtags:
            draw_text_wrapped(draw, req.hashtags[:60], H - 80, 16, (170, 170, 170), W - 80, stroke=1)

        # Salvar frame tratado
        frame_path = tmp_dir / f"frame_{job_id}.jpg"
        img.save(str(frame_path), quality=90)

        # ── 4. Obter duração do áudio ────────────────────────────
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
            capture_output=True, text=True
        )
        try:
            duration = float(probe.stdout.strip())
        except:
            duration = 30.0

        # ── 5. Compor vídeo com ffmpeg direto ────────────────────
        output_path = tmp_dir / f"video_{job_id}.mp4"

        # ffmpeg: imagem estática + fade in/out + áudio
        # Muito mais rápido que moviepy frame-a-frame
        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-loop", "1",
            "-i", str(frame_path),
            "-i", str(audio_path),
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-tune", "stillimage",
            "-crf", "28",
            "-vf", f"fade=t=in:st=0:d=0.5,fade=t=out:st={max(0, duration-0.8)}:d=0.8,scale={W}:{H}:force_original_aspect_ratio=disable",
            "-c:a", "aac",
            "-b:a", "128k",
            "-shortest",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-t", str(duration + 0.5),
            str(output_path)
        ]

        proc = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            print("ffmpeg stderr:", proc.stderr[-500:])
            raise HTTPException(500, f"ffmpeg falhou: {proc.stderr[-200:]}")

        # ── 6. Upload para Supabase Storage ──────────────────────
        video_bytes = output_path.read_bytes()
        filename = f"free-videos/{req.user_id or 'anon'}/{job_id}.mp4"

        async with httpx.AsyncClient(timeout=60) as client:
            upload = await client.post(
                f"{SUPABASE_URL}/storage/v1/object/product-frames/{filename}",
                headers={
                    "apikey": SUPABASE_KEY,
                    "Authorization": f"Bearer {SUPABASE_KEY}",
                    "Content-Type": "video/mp4",
                    "x-upsert": "true"
                },
                content=video_bytes
            )

        if upload.status_code not in (200, 201):
            raise HTTPException(500, f"Falha no upload: {upload.status_code} — {upload.text[:200]}")

        video_url = f"{SUPABASE_URL}/storage/v1/object/public/product-frames/{filename}"

        return {
            "success": True,
            "video_url": video_url,
            "duration": round(duration, 1),
            "job_id": job_id
        }

    except HTTPException:
        raise
    except Exception as e:
        import sys
        tb = traceback.format_exc()
        print("=== ERRO DETALHADO ===", flush=True)
        print(tb, flush=True)
        print("=== FIM ERRO ===", flush=True)
        raise HTTPException(500, f"Erro na geração: {str(e)} | {tb[-300:]}")
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
