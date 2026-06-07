# Creatorly Video Server

Servidor Python para geração de vídeos comerciais gratuitos.

## Deploy no Render.com

1. Crie um repositório no GitHub com esses arquivos
2. No Render Dashboard → New Web Service → conecte o repositório
3. Configure as variáveis de ambiente:
   - `SUPABASE_KEY` = sua service role key do Supabase
4. Clique em Deploy

## Endpoint

POST /generate-free
{
  "audio_url": "https://...",
  "product_image_url": "https://...",
  "hook_text": "Esse produto mudou minha vida!",
  "cta_text": "Clica no carrinho aqui embaixo!",
  "product_name": "Sérum vitamina C",
  "hashtags": "#skincare #beleza",
  "user_id": "uuid-do-usuario",
  "titulo": "Vídeo Free"
}
