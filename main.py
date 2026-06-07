from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import httpx, asyncio, os, uuid, tempfile, traceback
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
    audio_url: str                    # URL do áudio ElevenLabs
    product_image_url: str            # URL da imagem do produto
    hook_text: str                    # Texto do hook
    cta_text: Optional[str] = "Clica no carrinho aqui embaixo!"
    product_name: Optional[str] = ""
    hashtags: Optional[str] = ""
    user_id: Optional[str] = ""
    titulo: Optional[str] = "Vídeo Creatorly"

@app.get("/")
def root():
    return {"status": "Creatorly Video Server online", "version": "1.0"}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/generate-free")
async def generate_free_video(req: VideoRequest):
    """
    Pipeline Free — custo zero:
    1. Baixa imagem do produto
    2. Baixa áudio ElevenLabs
    3. Cria vídeo com moviepy:
       - Produto animado (zoom cinematic)
       - Hook na tela (texto animado)
       - Áudio sincronizado
       - CTA final
    4. Faz upload para Supabase Storage
    5. Retorna URL pública do vídeo
    """
    job_id = str(uuid.uuid4())[:8]
    tmp_dir = Path(tempfile.mkdtemp())

    try:
        # ── 1. Baixar assets ─────────────────────────────────────
        async with httpx.AsyncClient(timeout=30) as client:
            # Imagem do produto
            img_resp = await client.get(req.product_image_url)
            if img_resp.status_code != 200:
                raise HTTPException(400, "Não foi possível baixar a imagem do produto")
            img_path = tmp_dir / f"product_{job_id}.jpg"
            img_path.write_bytes(img_resp.content)

            # Áudio ElevenLabs
            audio_resp = await client.get(req.audio_url)
            if audio_resp.status_code != 200:
                raise HTTPException(400, "Não foi possível baixar o áudio")
            audio_path = tmp_dir / f"audio_{job_id}.mp3"
            audio_path.write_bytes(audio_resp.content)

        # ── 2. Compor vídeo com moviepy ──────────────────────────
        from moviepy.editor import (
            ImageClip, AudioFileClip, TextClip,
            CompositeVideoClip, concatenate_videoclips,
            ColorClip
        )
        from PIL import Image
        import numpy as np

        # Carregar áudio e descobrir duração
        audio = AudioFileClip(str(audio_path))
        duration = audio.duration  # duração real do áudio

        # Redimensionar imagem para 9:16 (1080x1920)
        W, H = 1080, 1920
        img = Image.open(str(img_path)).convert("RGB")

        # Crop inteligente para 9:16
        ratio = W / H
        img_ratio = img.width / img.height
        if img_ratio > ratio:
            new_w = int(img.height * ratio)
            offset = (img.width - new_w) // 2
            img = img.crop((offset, 0, offset + new_w, img.height))
        else:
            new_h = int(img.width / ratio)
            offset = (img.height - new_h) // 2
            img = img.crop((0, offset, img.width, offset + new_h))
        img = img.resize((W, H), Image.LANCZOS)

        # Salvar imagem tratada
        img_treated = tmp_dir / f"treated_{job_id}.jpg"
        img.save(str(img_treated), quality=95)

        # ── Animação zoom-in cinematic (Ken Burns effect) ─────────
        def make_zoom_frame(t):
            """Zoom suave de 1.0x → 1.12x durante a duração"""
            zoom = 1.0 + (0.12 * t / duration)
            zoom = min(zoom, 1.15)
            frame = np.array(img)
            h, w = frame.shape[:2]
            new_h = int(h / zoom)
            new_w = int(w / zoom)
            y1 = (h - new_h) // 2
            x1 = (w - new_w) // 2
            cropped = frame[y1:y1+new_h, x1:x1+new_w]
            from PIL import Image as PILImage
            resized = PILImage.fromarray(cropped).resize((W, H), PILImage.LANCZOS)
            return np.array(resized)

        product_clip = VideoClip(make_zoom_frame, duration=duration)

        # ── Overlay escuro sutil para texto legível ───────────────
        dark_overlay = ColorClip(size=(W, H), color=[0, 0, 0], duration=duration)
        dark_overlay = dark_overlay.set_opacity(0.35)

        # ── Hook text (aparece nos primeiros 3s) ──────────────────
        hook_lines = _wrap_text(req.hook_text, max_chars=28)
        hook_clip = TextClip(
            hook_lines,
            fontsize=72,
            font="DejaVu-Sans-Bold",
            color="white",
            stroke_color="black",
            stroke_width=3,
            method="caption",
            size=(W - 120, None),
            align="center"
        ).set_position(("center", 180)).set_duration(min(4.5, duration))

        # ── Produto label (aparece no meio) ───────────────────────
        product_label = ""
        if req.product_name:
            product_label = req.product_name.upper()
        label_clip = TextClip(
            product_label,
            fontsize=44,
            font="DejaVu-Sans-Bold",
            color="#00FF88",
            stroke_color="black",
            stroke_width=2,
            method="label"
        ).set_position(("center", H - 380)).set_start(2.0).set_duration(duration - 2.0) if product_label else None

        # ── CTA final (últimos 4s) ────────────────────────────────
        cta_start = max(0, duration - 4.5)
        cta_clip = TextClip(
            req.cta_text,
            fontsize=52,
            font="DejaVu-Sans-Bold",
            color="white",
            bg_color="#FF0050",
            stroke_color="black",
            stroke_width=1,
            method="caption",
            size=(W - 80, None),
            align="center"
        ).set_position(("center", H - 220)).set_start(cta_start).set_duration(duration - cta_start)

        # ── Hashtags (últimos 2s) ─────────────────────────────────
        tags_start = max(0, duration - 2.5)
        hashtag_clip = TextClip(
            req.hashtags[:80] if req.hashtags else "",
            fontsize=32,
            font="DejaVu-Sans",
            color="#aaaaaa",
            method="label"
        ).set_position(("center", H - 120)).set_start(tags_start).set_duration(duration - tags_start) if req.hashtags else None

        # ── Composição final ──────────────────────────────────────
        layers = [product_clip, dark_overlay, hook_clip, cta_clip]
        if label_clip: layers.append(label_clip)
        if hashtag_clip: layers.append(hashtag_clip)

        final = CompositeVideoClip(layers, size=(W, H))
        final = final.set_audio(audio)

        # ── Render ────────────────────────────────────────────────
        output_path = tmp_dir / f"video_{job_id}.mp4"
        final.write_videofile(
            str(output_path),
            fps=30,
            codec="libx264",
            audio_codec="aac",
            preset="ultrafast",
            threads=2,
            logger=None
        )

        # ── 3. Upload para Supabase Storage ──────────────────────
        video_bytes = output_path.read_bytes()
        filename = f"free-videos/{req.user_id or 'anon'}/{job_id}.mp4"

        async with httpx.AsyncClient(timeout=60) as client:
            upload = await client.post(
                f"{SUPABASE_URL}/storage/v1/object/product-frames/{filename}",
                headers={
                    "Authorization": f"Bearer {SUPABASE_KEY}",
                    "Content-Type": "video/mp4",
                    "x-upsert": "true"
                },
                content=video_bytes
            )

        video_url = f"{SUPABASE_URL}/storage/v1/object/public/product-frames/{filename}"

        return {
            "success": True,
            "video_url": video_url,
            "duration": round(duration, 1),
            "job_id": job_id
        }

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"Erro na geração: {str(e)}")
    finally:
        # Limpar arquivos temporários
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _wrap_text(text: str, max_chars: int = 28) -> str:
    """Quebra texto em linhas para caber na tela"""
    words = text.split()
    lines, current = [], ""
    for word in words:
        if len(current) + len(word) + 1 <= max_chars:
            current += ("" if not current else " ") + word
        else:
            if current: lines.append(current)
            current = word
    if current: lines.append(current)
    return "\n".join(lines[:4])  # max 4 linhas


# Importar VideoClip aqui para o Ken Burns
from moviepy.video.VideoClip import VideoClip

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
