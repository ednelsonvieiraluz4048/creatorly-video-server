from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
import httpx, asyncio, os, uuid, tempfile, traceback, subprocess, base64 as b64lib
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
    product_frames: Optional[List[str]] = []  # NOVO: array de frames base64
    hook_text: str
    cta_text: Optional[str] = "Clica no carrinho aqui embaixo!"
    product_name: Optional[str] = ""
    hashtags: Optional[str] = ""
    user_id: Optional[str] = ""
    titulo: Optional[str] = "Vídeo Creatorly"

class MergeRequest(BaseModel):
    video_url: str
    audio_url: str
    user_id: Optional[str] = ""

@app.get("/")
def root():
    return {"status": "Creatorly Video Server online", "version": "2.4"}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/merge")
async def merge_video_audio(req: MergeRequest):
    job_id = str(uuid.uuid4())[:8]
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            video_resp = await client.get(req.video_url)
            if video_resp.status_code != 200:
                raise HTTPException(400, f"Não foi possível baixar o vídeo: {video_resp.status_code}")
            video_path = tmp_dir / f"video_{job_id}.mp4"
            video_path.write_bytes(video_resp.content)

            audio_resp = await client.get(req.audio_url)
            if audio_resp.status_code != 200:
                raise HTTPException(400, f"Não foi possível baixar o áudio: {audio_resp.status_code}")
            audio_path = tmp_dir / f"audio_{job_id}.mp3"
            audio_path.write_bytes(audio_resp.content)

        def get_duration(path):
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                capture_output=True, text=True
            )
            try:
                return float(probe.stdout.strip())
            except:
                return 0.0

        video_dur = get_duration(video_path)
        audio_dur = get_duration(audio_path)
        print(f"[merge] video: {video_dur:.1f}s | audio: {audio_dur:.1f}s")

        output_path = tmp_dir / f"merged_{job_id}.mp4"

        if audio_dur > video_dur and video_dur > 0:
            ffmpeg_cmd = [
                "ffmpeg", "-y",
                "-stream_loop", "-1", "-i", str(video_path),
                "-i", str(audio_path),
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
                "-c:a", "aac", "-b:a", "128k", "-shortest",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                "-t", str(audio_dur + 0.3), str(output_path)
            ]
        else:
            ffmpeg_cmd = [
                "ffmpeg", "-y",
                "-i", str(video_path), "-i", str(audio_path),
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
                "-c:a", "aac", "-b:a", "128k", "-shortest",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output_path)
            ]

        proc = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise HTTPException(500, f"ffmpeg merge falhou: {proc.stderr[-200:]}")

        video_bytes = output_path.read_bytes()
        filename = f"merged-videos/{req.user_id or 'anon'}/{job_id}.mp4"

        async with httpx.AsyncClient(timeout=60) as client:
            upload = await client.post(
                f"{SUPABASE_URL}/storage/v1/object/product-frames/{filename}",
                headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                         "Content-Type": "video/mp4", "x-upsert": "true"},
                content=video_bytes
            )

        if upload.status_code not in (200, 201):
            raise HTTPException(500, f"Falha no upload do merge: {upload.status_code}")

        merged_url = f"{SUPABASE_URL}/storage/v1/object/public/product-frames/{filename}"
        print(f"[merge] ✅ {merged_url}")
        return {"success": True, "merged_url": merged_url,
                "video_duration": round(video_dur, 1), "audio_duration": round(audio_dur, 1), "job_id": job_id}

    except HTTPException:
        raise
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(500, f"Erro no merge: {str(e)}")
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.post("/generate-free")
async def generate_free_video(req: VideoRequest):
    job_id = str(uuid.uuid4())[:8]
    tmp_dir = Path(tempfile.mkdtemp())

    try:
        from PIL import Image, ImageDraw, ImageFont
        W, H = 720, 1280

        # ── 1. Montar lista de imagens ───────────────────────────
        # Prioridade: product_frames (array) > product_image_url (único)
        frames_data = req.product_frames if req.product_frames else []
        if not frames_data and req.product_image_url:
            frames_data = [req.product_image_url]

        if not frames_data:
            raise HTTPException(400, "Nenhuma imagem de produto disponível")

        print(f"[generate-free] frames recebidos: {len(frames_data)}")

        def decode_image(src: str, path: Path):
            """Decodifica base64 ou baixa URL HTTP para arquivo."""
            if src.startswith('data:'):
                b64data = src.split(',')[1]
                path.write_bytes(b64lib.b64decode(b64data))
            else:
                import urllib.request
                urllib.request.urlretrieve(src, str(path))

        def prepare_frame(img_path: Path) -> Path:
            """Prepara frame 9:16 com overlay e textos."""
            img = Image.open(str(img_path)).convert("RGB")
            ratio = W / H
            img_ratio = img.width / img.height
            if img_ratio > ratio:
                new_w = int(img.height * ratio)
                offset = (img.width - new_w) // 2
                img = img.crop((offset, 0, offset + new_w, img.height))
            else:
                new_h = int(img.width / ratio)
                offset = max(0, (img.height - new_h) // 3)
                img = img.crop((0, offset, img.width, offset + new_h))
            img = img.resize((W, H), Image.LANCZOS)
            overlay = Image.new("RGBA", (W, H), (0, 0, 0, 80))
            img = img.convert("RGBA")
            img = Image.alpha_composite(img, overlay).convert("RGB")
            return img

        def get_font(size):
            for fp in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                       "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"]:
                if os.path.exists(fp):
                    return ImageFont.truetype(fp, size)
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
            for i, l in enumerate(lines[:6]):
                bbox = draw.textbbox((0, 0), l, font=font)
                x = (W - (bbox[2] - bbox[0])) // 2
                draw.text((x, y + i * (font_size + 8)), l, font=font,
                          fill=color, stroke_width=stroke, stroke_fill=(0, 0, 0))

        # ── 2. Processar cada frame ──────────────────────────────
        prepared_paths = []
        for idx, src in enumerate(frames_data[:20]):  # máximo 20 frames
            raw_path = tmp_dir / f"raw_{job_id}_{idx}.jpg"
            try:
                decode_image(src, raw_path)
                img = prepare_frame(raw_path)
                draw = ImageDraw.Draw(img)
                # Só adiciona textos no primeiro e último frame
                if idx == 0:
                    draw_text_wrapped(draw, req.hook_text, 80, 26, (255, 255, 255), W - 80)
                if idx == len(frames_data) - 1 or idx == min(7, len(frames_data)-1):
                    draw_text_wrapped(draw, req.cta_text, H - 160, 25, (255, 255, 255), W - 80)
                if req.product_name and idx == len(frames_data) // 2:
                    draw_text_wrapped(draw, req.product_name.upper(), H // 2 - 20, 22, (0, 255, 136), W - 80, stroke=1)
                out_path = tmp_dir / f"frame_{job_id}_{idx:03d}.jpg"
                img.save(str(out_path), quality=90)
                prepared_paths.append(out_path)
            except Exception as e:
                print(f"[generate-free] frame {idx} falhou: {e}")
                continue

        if not prepared_paths:
            raise HTTPException(400, "Nenhum frame válido para processar")

        # ── 3. Baixar áudio ──────────────────────────────────────
        async with httpx.AsyncClient(timeout=30) as client:
            audio_resp = await client.get(req.audio_url)
            if audio_resp.status_code != 200:
                raise HTTPException(400, "Não foi possível baixar o áudio")
            audio_path = tmp_dir / f"audio_{job_id}.mp3"
            audio_path.write_bytes(audio_resp.content)

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

        # ── 5. Montar vídeo com ffmpeg ───────────────────────────
        output_path = tmp_dir / f"video_{job_id}.mp4"
        n_frames = len(prepared_paths)

        if n_frames == 1:
            # Imagem única — loop estático
            ffmpeg_cmd = [
                "ffmpeg", "-y",
                "-loop", "1", "-i", str(prepared_paths[0]),
                "-i", str(audio_path),
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage",
                "-crf", "28",
                "-vf", f"fade=t=in:st=0:d=0.5,fade=t=out:st={max(0,duration-0.8)}:d=0.8,scale={W}:{H}:force_original_aspect_ratio=disable",
                "-c:a", "aac", "-b:a", "128k", "-shortest",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                "-t", str(duration + 0.5), str(output_path)
            ]
        else:
            # Múltiplos frames — sequência animada
            # Cada frame fica por (duration / n_frames) segundos
            frame_duration = min(1.0, max(0.3, duration / n_frames))  # 0.3s a 1.0s por frame
            # Cria arquivo de lista para ffmpeg concat
            concat_file = tmp_dir / "frames.txt"
            with open(str(concat_file), 'w') as cf:
                for fp in prepared_paths:
                    cf.write(f"file '{fp}'\n")
                    cf.write(f"duration {frame_duration:.2f}\n")
                # Adiciona último frame novamente (necessário para ffmpeg concat)
                cf.write(f"file '{prepared_paths[-1]}'\n")

            ffmpeg_cmd = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0", "-i", str(concat_file),
                "-i", str(audio_path),
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
                "-vf", f"scale={W}:{H}:force_original_aspect_ratio=disable,fade=t=in:st=0:d=0.5",
                "-c:a", "aac", "-b:a", "128k", "-shortest",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                str(output_path)
            ]

        print(f"[generate-free] montando vídeo com {n_frames} frame(s), duração {duration:.1f}s")
        proc = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            print("ffmpeg stderr:", proc.stderr[-500:])
            raise HTTPException(500, f"ffmpeg falhou: {proc.stderr[-200:]}")

        # ── 6. Upload para Supabase ──────────────────────────────
        video_bytes = output_path.read_bytes()
        filename = f"free-videos/{req.user_id or 'anon'}/{job_id}.mp4"

        async with httpx.AsyncClient(timeout=60) as client:
            upload = await client.post(
                f"{SUPABASE_URL}/storage/v1/object/product-frames/{filename}",
                headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                         "Content-Type": "video/mp4", "x-upsert": "true"},
                content=video_bytes
            )

        if upload.status_code not in (200, 201):
            raise HTTPException(500, f"Falha no upload: {upload.status_code} — {upload.text[:200]}")

        video_url = f"{SUPABASE_URL}/storage/v1/object/public/product-frames/{filename}"
        print(f"[generate-free] ✅ {video_url}")

        return {"success": True, "video_url": video_url, "duration": round(duration, 1),
                "frames_used": n_frames, "job_id": job_id}

    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        print("=== ERRO ===", flush=True)
        print(tb, flush=True)
        raise HTTPException(500, f"Erro na geração: {str(e)} | {tb[-300:]}")
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
