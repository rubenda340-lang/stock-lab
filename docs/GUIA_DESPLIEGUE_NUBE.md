# Guía Rápida de Despliegue en Internet — StockLab (0 €/mes · Multiusuario)

Esta guía conecta las 4 piezas de producción en unos **15 minutos** sin coste fijo mensual.

---

## 1. Crear Base de Datos y Login en Supabase (0 €/mes)
1. Crea una cuenta en [supabase.com](https://supabase.com) y pulsa **New Project** (región **West EU / Frankfurt**).
2. Ve a **SQL Editor → New Query**, pega todo el contenido de `supabase/schema.sql` y pulsa **Run**.
   - Esto crea las tablas privadas (`profiles`, `watchlist`, `cartera`, `preferencias_alertas`, `alertas_enviadas_log`, `ia_consumo_diario`) con **Row-Level Security (RLS)** estricto por usuario.
3. Ve a **Authentication → Providers**:
   - Activa **Email**.
   - Para **Google**: crea un OAuth Client ID en [Google Cloud Console](https://console.cloud.google.com/apis/credentials), pega como *Authorized redirect URI* la URL callback que te muestra Supabase (`https://<TU-PROYECTO>.supabase.co/auth/v1/callback`) e introduce el Client ID y Secret en Supabase.
4. En **Authentication → URL Configuration**, añade tu URL pública final en **Site URL** y **Redirect URLs** (ej. `https://stocklab.streamlit.app`).
5. Copia tus claves de **Project Settings → API**:
   - `SUPABASE_URL`
   - `SUPABASE_ANON_KEY`
   - `SUPABASE_SERVICE_ROLE_KEY` *(solo para GitHub Actions, NUNCA la pongas en código ni en público)*.

---

## 2. Configurar Correos Automáticos en Resend (0 €/mes)
1. Crea una cuenta gratuita en [resend.com](https://resend.com) (3.000 correos/mes gratis).
2. Crea una **API Key** (`RESEND_API_KEY`).
3. Si tienes dominio propio, verifícalo para enviar desde `alertas@tudominio.com`; si aún no tienes dominio, Resend permite probar enviando a tu propio correo con `onboarding@resend.dev`.

---

## 3. Publicar la Web con Auto-Deploy en Streamlit Community Cloud (0 €/mes)
1. Sube tu repositorio a GitHub (`git push origin master`).
2. Entra en [share.streamlit.io](https://share.streamlit.io) → **Create app** → selecciona tu repositorio, rama `master` y archivo principal `app.py`.
3. En **Advanced settings → Secrets**, pega:
   ```toml
   STOCKLAB_GEMINI_API_KEY = "tu-clave-gemini"
   SUPABASE_URL = "https://tu-proyecto.supabase.co"
   SUPABASE_ANON_KEY = "tu-publishable-o-anon-key"
   APP_PUBLIC_URL = "https://tu-app.streamlit.app"
   ```
4. Pulsa **Deploy**. A partir de ese momento, **cada `git push` a `master` actualiza automáticamente la web en producción** con HTTPS.

> **Alternativa 16 GB RAM gratis:** el proyecto también incluye `Dockerfile` listo para desplegar en **Hugging Face Spaces (Docker SDK)** o **Railway/VPS** en cualquier momento.

---

## 4. Activar Alertas 24/7 en GitHub Actions (0 €/mes)
1. En tu repositorio de GitHub ve a **Settings → Secrets and variables → Actions → New repository secret** y añade:
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_ROLE_KEY`
   - `RESEND_API_KEY`
   - `ALERT_FROM_EMAIL`
   - `APP_PUBLIC_URL`
2. Los workflows en `.github/workflows/` ya quedan activos automáticamente:
   - `alertas_email.yml`: vigila Watchlist y Cartera cada 20 min durante horario BME (L–V).
   - `resumen_diario.yml`: envía resumen al cierre para usuarios que lo activen.
   - `ci_tests.yml`: valida compilación y contratos en cada `git push`.

---

## 5. (Opcional) Importar tu Cartera y Watchlist locales a tu cuenta
Tras registrarte en la web, puedes importar tus datos de `stocklab.db` una sola vez desde tu PC:
```bash
python scripts/migrar_datos_a_supabase.py --email tu@correo.com --dry-run
python scripts/migrar_datos_a_supabase.py --email tu@correo.com
```
